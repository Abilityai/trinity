"""
Operator Queue Sync Service (OPS-001).

Background service that polls agent containers for operator-queue.json files,
syncs new requests to the database, and writes operator responses back to
agent files.

Polling cycle:
  1. Get list of running agents
  2. For each agent, read ~/.trinity/operator-queue.json
  3. Detect new 'pending' entries -> create DB records, broadcast WebSocket
  4. Detect 'acknowledged' entries -> update DB records
  5. Write operator responses back to agent JSON files
  6. Handle expired entries
"""

# ruff: noqa: F401
# #3785: this external block stays whole, first, even where `__init__` itself
# no longer reads a name. A submodule may import at module level only what is
# already imported here (test_3785's G13), so an ImportError fires before any
# `_oq`-holding submodule has loaded; and several names here are read through
# the package from outside (`oqs.time`, `oqs.to_utc_iso`, `oqs.rate_limiter`).
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, Tuple

from services import rate_limiter
from services.operator_queue_choices import (
    OPTIONS_DROPPED_MARKER,
    options_cap_violation,
    title_cap_violation,
)
from utils.helpers import iso_cutoff, parse_iso_timestamp, to_utc_iso, utc_now_iso

# #3785 (O2): the collaborators (`db`, `AgentClient`, …) are bound once, in
# `deps`, and read as `deps.X`, never bound here — a stale
# `monkeypatch.setattr(oqs, "db", …)` raises; patch `oqs.deps.X`. Imported
# after every external import, so a failed first import leaves nothing stale.
from . import deps

logger = logging.getLogger(__name__)

# WebSocket manager injected from main.py
_websocket_manager = None

QUEUE_FILE_PATH = ".trinity/operator-queue.json"
DEFAULT_POLL_INTERVAL = 5  # seconds
# #1525: after this many consecutive failed create attempts for the same request
# id, quarantine it — stop re-attempting so one persistently-failing create can't
# produce an unbounded ~5s ERROR loop. It's retried again after a process restart
# (or if it leaves `pending` / gets created some other way).
MAX_CREATE_ATTEMPTS = 3
# Safety valve so the in-memory quarantine map can never grow without bound if an
# agent streams unique failing ids; cleared wholesale past this size.
_MAX_QUARANTINE_ENTRIES = 5000

# =========================================================================
# #1632: ingestion caps at the agent-authored create seam.
#
# #1402 makes the operator queue the approval channel for irreversible actions,
# so a compromised / prompt-injected agent that floods plausible "approve this"
# items causes operator fatigue → reflexive approval. XSS is already handled
# (DOMPurify); the exposure is VOLUME + social engineering. These bound a
# HOSTILE agent, not just a runaway. All env-tunable and generous by design —
# the job is to cap abuse, not throttle real usage.
# =========================================================================

# DEPTH cap (primary, DB-measured ⇒ Redis-independent). Once an agent has this
# many pending rows the sync STOPS ingesting new items — the fatigue metric is
# depth, not rate, so a rate-aware drip can't accumulate a backlog.
OPERATOR_QUEUE_MAX_PENDING_PER_AGENT = int(
    os.getenv("OPERATOR_QUEUE_MAX_PENDING_PER_AGENT", "25")
)
# RATE cap (burst smoothing, Redis, fail-open) — per-agent + fleet-wide.
OPERATOR_QUEUE_CREATE_RATE_LIMIT = int(os.getenv("OPERATOR_QUEUE_CREATE_RATE_LIMIT", "60"))
OPERATOR_QUEUE_CREATE_RATE_WINDOW = int(os.getenv("OPERATOR_QUEUE_CREATE_RATE_WINDOW", "60"))
OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT = int(
    os.getenv("OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT", "300")
)
# Per-cycle scan bounds (C1: a deferred-not-dropped item + full-file rescan is a
# per-cycle DoS on a 10k-item file). Skip a pathological file wholesale, and
# never scan more than N requests per cycle.
OPERATOR_QUEUE_MAX_FILE_BYTES = int(
    os.getenv("OPERATOR_QUEUE_MAX_FILE_BYTES", str(2 * 1024 * 1024))
)
OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE = int(os.getenv("OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE", "500"))
# Field size caps — truncate-with-marker (losing a real approval is worse than a
# clamped one). title/question measured in chars; context/options serialized bytes.
OPERATOR_QUEUE_TITLE_MAX = int(os.getenv("OPERATOR_QUEUE_TITLE_MAX", "300"))
OPERATOR_QUEUE_QUESTION_MAX = int(os.getenv("OPERATOR_QUEUE_QUESTION_MAX", "4000"))
OPERATOR_QUEUE_CONTEXT_MAX_BYTES = int(os.getenv("OPERATOR_QUEUE_CONTEXT_MAX_BYTES", "8192"))
OPERATOR_QUEUE_OPTIONS_MAX_BYTES = int(os.getenv("OPERATOR_QUEUE_OPTIONS_MAX_BYTES", "4096"))
# #3385: `type` is agent-authored free text. A plain constant, not an env knob:
# it must stay above every platform type literal (the longest is 24 chars) or the
# clamp would re-type a platform row.
OPERATOR_QUEUE_TYPE_MAX = 64
# #3243: the authoring caps on an agent's ask, refused (never truncated) on the
# native path and held on the queue-file path. Floored at load so a mis-set env
# cannot refuse every skill-gate approval (two short options) or every title.
def _floored_env_cap(name: str, default: int, floor: int) -> int:
    return max(floor, int(os.getenv(name, str(default))))


OPERATOR_QUEUE_MAX_OPTIONS = _floored_env_cap("OPERATOR_QUEUE_MAX_OPTIONS", 5, 2)
OPERATOR_QUEUE_OPTION_MAX_CHARS = _floored_env_cap("OPERATOR_QUEUE_OPTION_MAX_CHARS", 60, 16)
OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS = _floored_env_cap("OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS", 120, 40)
# trinity-enterprise#611: the frozen action an agent-raised ask carries (the
# values a decision would submit). Refused above this on the native path.
OPERATOR_QUEUE_PROPOSAL_MAX_BYTES = int(os.getenv("OPERATOR_QUEUE_PROPOSAL_MAX_BYTES", "8192"))
OPERATOR_QUEUE_ID_MAX = int(os.getenv("OPERATOR_QUEUE_ID_MAX", "256"))
OPERATOR_QUEUE_EXECUTION_ID_MAX = int(os.getenv("OPERATOR_QUEUE_EXECUTION_ID_MAX", "128"))
# ent#364: RFC 5321 caps an address at 320 chars; anything longer is not an
# email and never matches a roster row, so it is refused before the DB read.
OPERATOR_QUEUE_EMAIL_MAX = int(os.getenv("OPERATOR_QUEUE_EMAIL_MAX", "320"))
# Flood alert: one per over-cap episode (edge-triggered, #3130), un-guessable
# id, budgeted (#1677); the cooldown is the minimum spacing between episodes.
OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS = int(
    os.getenv("OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS", "300")
)

# =========================================================================
# #1677: budget for agent-INFLUENCEABLE platform alert emitters.
#
# The #1632 caps above bound the agent-authored FILE seam; platform direct-DB
# creates bypass `_sync_agent` by construction. Most of those emitters are
# platform-cadence-bound (edge-triggered, idempotent ids, leader-locked) —
# but an emitter whose VOLUME an agent can drive (e.g. skill-not-found: one
# high-priority item per distinct unknown slash-command, #1410) re-opens the
# operator-fatigue channel #1632 closed. Such emitters route through
# `create_bounded_alert` below: a per-(agent, registered-type) pending-DEPTH
# cap — DB-measured ⇒ Redis-independent, the #1632 primary-bound mirror; no
# rate cap, because depth is rate-independent (a fast spray only reaches the
# cap faster). The caller-parity test
# (tests/unit/test_1677_operator_alert_emitters.py) forces every
# create_operator_queue_item call site to be classified platform-only or
# routed through this helper — misclassification fails at CI, never quietly
# at a load-bearing platform create (the poison-park fail-direction argument
# for rejecting a db-sink default bound).
# =========================================================================

# Per-(agent, type) pending budget for budgeted platform alerts. Generous vs
# the legit case (a broken agent produces 1-3 distinct unknown commands),
# tight vs the exploit (100 distinct commands → cap + one episode alert).
# Self-healing: operator resolution drops depth below the cap.
OPERATOR_ALERT_MAX_PENDING_PER_TYPE = int(
    os.getenv("OPERATOR_ALERT_MAX_PENDING_PER_TYPE", "5")
)

# The registered budgeted types — a closed, reviewed set (#1890 shape). An
# unregistered `item["type"]` is refused fail-closed: a caller-trusted string
# would mint a fresh budget per distinct value (unbounded cap keyspace), so
# registration is a one-line reviewed act here, never a call-site decision.
_BUDGETED_ALERT_TYPES = frozenset({
    "skill_not_found",
    # ent#499: a Workspace client's thumbs-down. No agent authors it, but the
    # volume is driven by a person clicking, which is the same
    # not-bound-by-platform-cadence side of the #1677 classification.
    "workspace_problem_report",
    # #2529: the per-Push `.gitignore` sweep's alert. Budgeted rather than
    # exempted because `sync_to_github` is reachable from the `git_sync` MCP
    # tool, which an agent-scoped key may call on itself — so an agent CAN drive
    # the volume, which is the whole test the #1677 classification applies.
    "gitignore_untracked",
    # #2392: an effect refused on a pull-mode agent for lack of an execution id.
    # Budgeted because the agent drives the volume — every send it attempts
    # without an id is one refusal.
    "effect_unguarded",
    # #3130: the file seam's own flood alarm. An agent's filing volume is what
    # trips it, so it is agent-influenceable by construction; as a raw create it
    # was the dominant producer of the depth it reported on (386 of one agent's
    # 435 rows). Edge-triggered at the emitter, budgeted here as the backstop.
    "queue_flood",
})

# Shape guard for the episode alert's `last_triggered_by` triage field: a
# platform trigger enum only — NEVER agent-controlled free text (G-04: the
# episode alert is durable operator-visible state). Digits included because
# the real enum carries them (`a2a`).
_TRIGGERED_BY_RE = re.compile(r"[a-z0-9_]{1,32}")

# Valid priority values — an agent-supplied unknown collapses to "medium".
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# Reserved id prefix for role-assignment drift alerts (trinity-enterprise#500).
# A NAMED public constant rather than a bare literal in the emitter, because the
# emitter is CROSS-REPO: the registered module that raises these items imports
# this name, so the reservation below and the id it produces cannot drift apart
# across two repositories. (The house convention puts the constant in the
# emitter's own module — `BASE_IMAGE_STALE_ALERT_PREFIX` in
# `system_agent_service.py`. This is the deliberate deviation, and the reason is
# exactly that the emitter is not in this repo.)
ROLE_DRIFT_ALERT_PREFIX = "role-drift-"

# trinity-enterprise#611 / #164: the id prefix of an ask the PLATFORM raises
# through `ask_service.raise_ask` (the approval gate, `raised_by="gate"`, which
# must use it). Reserved below like the alert prefixes. An agent that could
# author one would pre-create the gate's id and have the gate's raise answered
# as a replay of the AGENT's proposal; and since `is_platform_minted` keys on the
# tuple, the reservation is also what keeps a gate row from waking the agent to
# redo an action the gate resumes itself.
GATE_ASK_ID_PREFIX = "gate-"

# Platform-reserved id prefixes an agent must NOT author. If it could, it would
# pre-create — and via create_item's on_conflict_do_nothing, silently suppress —
# its own flood alarm or the #1402 poison alert (C2). Verified against source
# (lease_reaper_service / agent_client / sync_health_service / task_execution_service
# / validation_service, plus this service's own alert id).
_RESERVED_ID_PREFIXES = (
    "queue-flood-",      # this service's own flood alert
    "poison-",           # lease_reaper_service poison-park (#1402)
    "cb-dormant-",       # agent_client circuit-breaker-dormant alert
    "sync-failing-",     # sync_health_service
    "git-bloat-",        # sync_health_service
    "sync-diverged-",    # sync_health_service divergence-freeze episode item
                         # (trinity-enterprise#706) — deterministic id, so the
                         # reservation is what keeps it unsuppressible
    "skill-not-found-",  # task_execution_service
    "val_",              # validation_service
    "system-seed-",      # system_seed_service first-run seed alerts (ent#124)
    "base-image-stale-", # system_agent_service staleness + start-failure (#1816)
    "alert-budget-",     # this service's #1677 budget episode alert (deterministic
                         # bucketed id — reservation is what keeps the DB
                         # on-conflict dedup from being agent-pre-suppressible)
    "effect-unguarded-", # idempotency_service refused-effect alarm (#2392)
    "db-backup-",        # db_backup_service failure/staleness alarms (#2216)
    "log-archive-",      # archive_storage unwritable-directory alarm (#2205)
    "sub-headroom-",     # subscription_headroom_alerts weekly-window alarm (ent#434)
    # skills legacy-adoption refusal (#2744). The family prefix, so it covers
    # BOTH the steady-state `…-refused-<sha256(url)[:12]>` id and the two
    # actionable branches' timestamped ids (and the historical rows). The
    # steady-state id is derived from an admin-visible URL and is therefore
    # GUESSABLE, and `_skills-sync` is uncreatable at agent CREATE but NOT on
    # the rename path (`routers/agent_rename.py` keeps a leading `_`), so an
    # owner-hijacked host could pre-create the id and suppress the alarm.
    # `is_platform_minted` is keyed on this tuple too — it gates the ent#499
    # responded write-back and the ent#329 respond→resume dispatch, and this
    # item is one an operator is now EXPECTED to click through on.
    "skills-legacy-adoption-",
    "workspace-problem-",  # client_portal report-a-problem (ent#499) — reserved
                           # so an agent cannot pre-create the id of a complaint
                           # ABOUT ITSELF and silence it through ON CONFLICT
    "portal-inbox-collision-",  # client_portal shared legacy inbox (ent#308).
                           # Its text lists client addresses: reserved (#715) so
                           # `is_platform_minted` keeps it out of the agent's file
                           # and its resume turn, and so an agent cannot pre-create
                           # the id and swallow the alert through ON CONFLICT
    # git_service per-Push sweep alert (#2529). Reserved for the #1632 reason
    # above, and — since ent#499 keyed `is_platform_minted` on this very tuple —
    # this listing is ALSO what keeps the alert out of the agent's own
    # `~/.trinity/operator-queue.json`. Correct: the sweep alarm is a platform
    # alarm ABOUT the agent, not a loop the agent opened and is waiting on.
    "gitignore-untracked-",
    # #3246: four platform families that were never reserved. Reserving them
    # also makes `is_platform_minted` true for their rows, which keeps them out
    # of the agent's own file and its answer wake — intended: each is an alarm
    # ABOUT the agent (or the install), not a loop the agent opened.
    "skills-reconcile-",       # skill_service reconcile refusal (ent#236)
    "skills-fleet-reinject-",  # skills_sync_service fleet re-inject failures (ent#236)
    "retention-guard-",        # retention_guard prune refusal (#1644)
    "ent615-git-token-scrub-", # git_service/token_scrub refusal + unreadable (ent#615)
    ROLE_DRIFT_ALERT_PREFIX,  # role-assignment drift (trinity-enterprise#500) —
                           # the role file lives in the AGENT'S OWN workspace, so
                           # an unreserved prefix would let it pre-create the id
                           # of the alert about its own configuration
    GATE_ASK_ID_PREFIX,    # platform-raised asks (trinity-enterprise#611 / #164)
    # trinity-enterprise#631: a PERSON's tag of another person. Reserved so an
    # agent can neither forge one through its queue file (nothing lets an
    # agent tag a person) nor pre-create a tag's deterministic id and swallow
    # it through ON CONFLICT; `is_platform_minted` then also keeps the row out
    # of the agent's file and its answer wake. Literal, not imported: this
    # module is a leaf the tag service imports from.
    "mention-",
)

# Agent ids must be id-shaped: a create PK can't be safely rewritten, so a
# malformed / oversize id is rejected rather than clamped (C4).
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

# Inline truncation marker (kept short so the clamped field length stays ≤ cap).
_TRUNC_MARKER = "…[truncated]"
# #2376: one definition, imported. The respond-side validator has to exempt
# items wearing this marker, and a second literal would drift the day either
# side is reworded — leaving the sink accepting a placeholder as a decision.
_OPTIONS_DROPPED_MARKER = OPTIONS_DROPPED_MARKER

# #1632: single Redis key for the operator-queue sync leader (mirror monitoring
# #1464). Only the lease-holder runs a poll cycle, so `--workers 2` doesn't
# double-charge the rate limiter, double-broadcast the flood alert, or
# double-scan agent files.
_LEADER_KEY = "opqueue:leader"


# #715: the platform alarms whose TEXT is about a person — a Workspace client's
# complaint (ent#499: their email and verbatim words, which ent#366 withholds from
# the rated agent) and the client addresses behind a shared legacy inbox (ent#308,
# whose id also carries an email slug), and the skill gate's rows
# (trinity-enterprise#751: the approval card names the person who asked, the
# notice the person who decided; `gate-` covers `gate-note-`). A subset of
# `_RESERVED_ID_PREFIXES`, so no agent can mint a row into it.
_ABOUT_A_PERSON_ID_PREFIXES = ("workspace-problem-", "portal-inbox-collision-", "gate-",
                               # trinity-enterprise#631: who tagged whom, in what
                               "mention-")


# trinity-enterprise#815: the same tuple, public, for the SQL twin of
# `is_about_a_person` — the list routes pass it to `_list_conditions` so the
# exclusion runs BEFORE the limit. One tuple, read by SQL and Python alike.
ABOUT_A_PERSON_ID_PREFIXES = _ABOUT_A_PERSON_ID_PREFIXES

# trinity-enterprise#866: the rows about a person that are the agent OWNER's
# alone. A Workspace client's complaint names the rater and quotes their words
# (ent#499, trinity#3521); a person the agent is merely SHARED with is often a
# client too, so reading it would tell one client which others rated the agent
# and what they wrote — the facts `evaluations.py` already withholds from a
# shared non-owner. Deliberately not the whole `_ABOUT_A_PERSON_ID_PREFIXES`:
# a shared operator must still see and answer a skill gate's approval (`gate-`).
OWNER_ONLY_ID_PREFIXES = ("workspace-problem-",)


def _truncate_with_marker(text: str, max_len: int) -> str:
    """Truncate so the RESULT (content + marker) is ≤ max_len chars."""
    if len(text) <= max_len:
        return text
    keep = max(0, max_len - len(_TRUNC_MARKER))
    return text[:keep] + _TRUNC_MARKER


# ent#429: the context key naming the chat an addressed ask belongs to. Written
# by the platform (below), read by `client_portal/asks/service.py::_project` as
# the client-facing `chat_id`, and stripped from anything the agent authored —
# named here so the writer and the stripper cannot drift apart.
_WORKSPACE_THREAD_KEY = "workspace_session_id"
# ent#734: true only when the ask was raised BY the turn serving that chat. Main
# is both a chat-turn ask's home (drawn as a tile there) and a background ask's
# reply target (drawn in no chat), so `chat_id` alone cannot tell them apart.
_WORKSPACE_TURN_KEY = "workspace_raised_in_turn"
# trinity-enterprise#747: the chat the addressee opened to DISCUSS this ask.
# Separate from the two keys above on purpose: discussing an ask moves neither
# where it was raised nor where a background ask's reply goes.
_WORKSPACE_DISCUSSION_KEY = "workspace_discussion_id"
# Every context key the platform writes and no agent may author: stripped at both
# ingestion boundaries and ignored when comparing an agent's content.
_PLATFORM_CONTEXT_KEYS = frozenset({
    _WORKSPACE_THREAD_KEY, _WORKSPACE_TURN_KEY, _WORKSPACE_DISCUSSION_KEY,
})


def _workspace_thread_for(agent_name: str, email: str) -> Optional[str]:
    """The chat an addressed ask with no platform-known turn attaches to — the
    pair's Main — or None (ent#429). The file path's view of
    `_workspace_attachment`: a file carries only what the agent wrote, so it has
    no raising turn to offer."""
    return _workspace_attachment(agent_name, email)[0]


def _workspace_attachment(agent_name: str, email: str,
                          execution_id: Optional[str] = None) -> Tuple[Optional[str], bool]:
    """`(chat, raised_in_turn)` for an addressed ask (ent#429, ent#734): the
    chat it attaches to, or None; and whether that chat is the one the raising
    turn serves, which is what `_WORKSPACE_TURN_KEY` records.

    ent#734: `execution_id` is the RAISING execution as the PLATFORM knows it
    (the native path's `X-Trinity-Execution-Id`, #2392, validated as this
    agent's own) — never an agent-written `context.execution_id`, which a model
    could fill with any turn it has seen. When it is a RUNNING Workspace chat
    turn of this same (agent, addressee), the ask attaches to
    that chat; anything else — a schedule, loop or gate run, a finished, unknown
    or foreign execution, another person's chat, a failed
    lookup — gets the pair's Main, exactly as before. The header is set by the
    platform but not unforgeable by the agent's own process, so the bound is
    "one of this agent's live turns for this addressee", never another person's
    conversation (`client_portal.service.chat_for_execution`).

    Resolved at RAISE time, never at render time: an ask raised by a scheduled
    run has no conversation of its own, and "we will work out where it belongs
    when someone looks at it" is not an attachment — it is a guess repeated
    per view, with nothing durable to audit.

    Fail-SOFT, and deliberately the opposite direction to `_validated_addressee`
    beside it. That one fails CLOSED because an addressee it cannot verify is an
    authorization decision it must not make. This one only decides where a link
    POINTS: a thread we could not resolve costs the reader one extra click, and
    refusing the whole ask over it would lose the question entirely. Never
    raises — the #1632 clamp contract.
    """
    if execution_id:
        try:
            from client_portal.service import chat_for_execution

            chat = chat_for_execution(agent_name, email, execution_id)
            if chat:
                return chat, True
        except Exception:  # noqa: BLE001
            logger.warning(
                "[OperatorQueue] could not resolve the chat of the turn that raised "
                "an ask from %s; attaching it to Main (ent#734)",
                agent_name,
                exc_info=True,
            )
    try:
        from client_portal.service import ensure_thread_for_ask

        return ensure_thread_for_ask(agent_name, email) or None, False
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OperatorQueue] could not attach a workspace chat for an ask from %s; "
            "it will render without a thread link (ent#429)",
            agent_name,
            exc_info=True,
        )
        return None, False


def _json_bytes(value) -> Optional[int]:
    """The UTF-8 size of `value` serialized as JSON, or None when it does not
    serialize. The ONE measure every operator-queue size cap reads, so the file
    path's clamp and the native path's refusal (trinity-enterprise#611) cannot
    disagree about how big a field is."""
    try:
        return len(json.dumps(value).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting events."""
    global _websocket_manager
    _websocket_manager = manager


# #1677: (agent_name, item_type) → monotonic ts of the last budget episode
# alert. MODULE-level (the budget seam runs on any worker from terminal paths —
# there is no leader lock there) and named DISTINCTLY from the instance-level
# `OperatorQueueSyncService._flood_alert_cooldown`: a file-seam flood alert
# must not suppress a budget episode for the same agent (different episodes,
# different meanings), and the #1632 tests pin the flood cooldown's
# instance-scoped semantics on fresh service objects. Growth is bounded by
# fleet size × len(_BUDGETED_ALERT_TYPES).
_alert_budget_cooldown: dict = {}


def reset_alert_budget_state() -> None:
    """Test hook: clear the module-level #1677 budget-episode cooldown map."""
    _alert_budget_cooldown.clear()


# `create_bounded_alert_outcome` results. Only `ALERT_CREATED` means a row was
# written; the rest separate a deliberate REFUSAL (unregistered type, budget
# full) from a FAILURE (count read or create raised) for a caller that must
# treat them differently (#3130 review I1: the flood emitter retries a failure
# but not a refusal).
ALERT_CREATED = "created"
ALERT_REFUSED_UNREGISTERED = "refused_unregistered"
ALERT_REFUSED_AT_BUDGET = "refused_at_budget"
ALERT_FAILED_COUNT = "failed_count"
ALERT_FAILED_CREATE = "failed_create"


async def create_bounded_alert(agent_name: str, item: dict) -> bool:
    """#1677: the bool view of `create_bounded_alert_outcome` (see there)."""
    return await create_bounded_alert_outcome(agent_name, item) == ALERT_CREATED


async def create_bounded_alert_outcome(agent_name: str, item: dict) -> str:
    """#1677: the create seam for agent-INFLUENCEABLE platform alert emitters,
    since #3254 a thin delegate of ``platform_alerts.observe``.

    Every in-repo budgeted emitter now reports through ``observe`` itself, which
    composes the same per-(agent, registered-type) pending budget inside its
    locked create (``OPERATOR_ALERT_MAX_PENDING_PER_TYPE``) and spawns the budget
    episode on a refusal. This helper keeps its signature and its outcome
    contract for any caller still on it; the #3246 ratchet counts each such
    caller as off the seam, which steers new code to ``observe``.

    The budget type is derived from ``item["type"]`` (never a separate
    parameter: two sources of one fact silently void the bound) and MUST be a
    static literal registered in ``_BUDGETED_ALERT_TYPES``. That type names its
    platform-alert kind (the kind whose ``budgeted_type`` it is), and the key is
    read back from ``item["id"]`` + ``item["context"]`` by
    ``platform_alerts.derive_legacy_subject``, which must resolve to that same
    kind. The two kinds whose ids carry no derivable subject (the gitignore
    sweep's alert and the Workspace problem report) are keyed by the request id
    minus the kind's prefix: for the problem report that is the per-person /
    target / day digest its own emitter keys by; for gitignore it is the old
    per-call id, so a call still files one row.

    Outcomes (never raises; every refused or failed arm logs ``[#1677 budget]``):
      * type not registered          → ``ALERT_REFUSED_UNREGISTERED`` (fail-closed,
        nothing written: register the type, a one-line reviewed act);
      * the id does not read back as the type's kind, or carries no key
                                     → ``ALERT_FAILED_CREATE`` (fail-closed);
      * else ``observe``'s outcome: ``ALERT_CREATED``; ``"updated"`` (the
        subject's pending row was rewritten in place); ``"snoozed"`` (a person
        ended the subject inside the snooze window, nothing written);
        ``ALERT_REFUSED_AT_BUDGET`` (the budget was full; the episode alert is
        spawned by ``observe``); ``ALERT_FAILED_CREATE`` for its ``"failed"``.

    ``ALERT_FAILED_COUNT`` stays importable and is no longer returned: the
    per-type count now runs inside the seam's locked create, where a failed
    count and a failed create are one exception path. ``create_bounded_alert``
    is the bool view and is true for ``ALERT_CREATED`` only, so a paired side
    effect (a notification) never fires twice for one pending row.
    """
    from services import platform_alerts

    item_type = item.get("type") if isinstance(item, dict) else None
    if item_type not in _BUDGETED_ALERT_TYPES:
        logger.error(
            "[#1677 budget] refusing operator alert for '%s': type %r is not a "
            "registered budgeted type (%s) — add it to _BUDGETED_ALERT_TYPES "
            "before routing an emitter through create_bounded_alert "
            "(fail-closed; no item created)",
            agent_name, item_type, sorted(_BUDGETED_ALERT_TYPES),
        )
        return ALERT_REFUSED_UNREGISTERED

    kind = next(
        (k for k in platform_alerts.KINDS.values() if k.budgeted_type == item_type), None
    )
    match = platform_alerts.derive_legacy_subject(item.get("id"), item.get("context"))
    key = None
    if kind is not None and match is not None and match.kind == kind.name:
        if match.subject is not None:
            key = match.subject[len(kind.name) + 1:]
        elif kind.name in ("gitignore_untracked", "workspace_problem_report"):
            key = item["id"][len(kind.prefix):] or None
    if key is None:
        logger.error(
            "[#1677 budget] refusing operator alert for '%s': id %r does not read "
            "back as a keyed %r alert, the kind its type %r registers "
            "(fail-closed; no item created)",
            agent_name, item.get("id"), kind.name if kind else None, item_type,
        )
        return ALERT_FAILED_CREATE

    outcome = platform_alerts.observe(
        agent_name, kind.name, key,
        title=item.get("title") or "",
        question=item.get("question") or "",
        priority=item.get("priority") or "high",
        context=item.get("context"),
    )
    if outcome == platform_alerts.OBSERVED_FAILED:
        logger.error(
            "[#1677 budget] operator alert create failed for %s/%s — suppressed "
            "(fail-closed; the seam logged the cause)",
            agent_name, item_type,
        )
        return ALERT_FAILED_CREATE
    if outcome == ALERT_REFUSED_AT_BUDGET:
        logger.warning(
            "[#1677 budget] alert budget full for %s/%s (cap=%d) — refused; the "
            "budget episode alert reports it",
            agent_name, item_type, OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
        )
    return outcome


async def _maybe_emit_alert_budget_episode(
    agent_name: str, item_type: str, last_triggered_by=None
) -> None:
    """#1677: ONE budget-hit episode reading per cooldown window.

    Reported through ``platform_alerts.observe`` as kind ``alert_budget`` keyed by
    the budgeted type (#3254): one pending row per (agent, type), subject
    ``alert_budget:<type>``, which a later refusal outside the window rewrites
    in place rather than filing again. No emitter observes the budget freeing
    up (it is measured only inside the seam's own create), so a row nobody
    re-reads ends with its 14-day lifetime (``NO_CLEAR_SITE`` in the #3246
    registry test). Two workers' readings land on the one subject row, which is
    what the old time-bucketed id approximated.

    The cooldown is module-level (see ``_alert_budget_cooldown``) and stamped
    BEFORE the reading, so a persistently failing write backs off for the
    window instead of retrying on every refusal (#1632 shape). The kind is
    unbudgeted, so the episode can never recurse into the budget. ``observe``
    never raises and sends the thin ``operator_queue_sync`` trigger (#918) when
    a rendered field changed.
    """
    from services import platform_alerts

    now = time.monotonic()
    key = (agent_name, item_type)
    # `None` = never alerted (the #1632 sentinel shape — a 0.0 default would
    # suppress the first-ever alert while monotonic < cooldown after boot).
    last = _alert_budget_cooldown.get(key)
    if last is not None and now - last < OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS:
        return  # already alerted this episode
    _alert_budget_cooldown[key] = now

    title = f"Agent '{agent_name}' exceeded its '{item_type}' alert budget"
    # No `held` count (untruthful at a first-trip emit — true volume lives in
    # the FAILED executions list) and no agent-controlled text (no command
    # echo — G-04: this is durable operator-visible state).
    question = (
        f"Agent '{agent_name}' produced more '{item_type}' alerts than the "
        f"per-agent budget allows ({OPERATOR_ALERT_MAX_PENDING_PER_TYPE} "
        f"pending); further occurrences are suppressed until the pending items "
        f"are resolved. Suppressed occurrences still record as FAILED "
        f"executions in the executions list. This can indicate a runaway or "
        f"compromised agent — review its recent activity before acting on its "
        f"requests."
    )
    context = {
        "reason": "platform_alert_budget",
        "alert_type": item_type,
        "cap": OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
    }
    # Triage aid for the cross-agent budget-fill scenario (a permitted peer
    # spraying at the victim): the platform trigger enum only, shape-guarded.
    if isinstance(last_triggered_by, str) and _TRIGGERED_BY_RE.fullmatch(
        last_triggered_by
    ):
        context["last_triggered_by"] = last_triggered_by

    outcome = platform_alerts.observe(
        agent_name, "alert_budget", item_type,
        title=title, question=question, priority="high", context=context,
    )
    if outcome == platform_alerts.OBSERVED_FAILED:
        logger.error(
            "[#1677 budget] failed to emit budget episode alert for %s/%s "
            "(the seam logged the cause)",
            agent_name, item_type,
        )
        return
    logger.warning(
        "[#1677 budget] alert budget hit for %s/%s (cap=%d) — episode alert %s",
        agent_name, item_type, OPERATOR_ALERT_MAX_PENDING_PER_TYPE, outcome,
    )


# #3785: the package's own submodules, imported after the names they read
# from `__init__` (test_3785's G6 and G13).
from . import listing  # noqa: E402

# #3785: the poller core (instance state, start/stop, the leader lease, the
# loop and the cycle) lives in `poller.PollerCoreMixin`. Imported after every
# O1 definition: its `__init__` default reads `_oq.DEFAULT_POLL_INTERVAL` at
# import time.
from . import poller  # noqa: E402

# #3785 (S5): the agent-file channel lives in its own modules: the helpers in
# `filechannel`, the row vocabulary both sides read in `rows`, and the ingest
# and write-back halves of the service in `ingest` / `write_back`, composed
# below. Imported after every package-owned definition, so nothing they read
# from `_oq` at import time can be missing (none does today).
from . import filechannel  # noqa: E402
from . import rows  # noqa: E402
from . import ingest  # noqa: E402
from . import write_back  # noqa: E402


class OperatorQueueSyncService(poller.PollerCoreMixin, ingest.IngestMixin, write_back.WriteBackMixin):
    """Background service that syncs operator queue files with the database."""

    async def _maybe_emit_flood_alert(
        self, agent_name: str, held: int = 0, reason: str = "ingestion_cap"
    ):
        """#1632 / #3130: ONE flood alert per over-cap EPISODE.

        Fired when this agent's ingestion was depth-held / rate-skipped, or its
        queue file was oversized.

        Edge-triggered (#3130): an episode alerts once, however many cycles or
        cooldown windows it lasts, and the next alert needs the condition to
        have cleared first (`_sync_agent` ends the episode through
        `_end_flood_episode`, which ends its alert too). It used to be one
        alert per cooldown window for as long as the condition held — and since
        the alert counted toward the very depth it reported, the condition never
        cleared: 241 alerts in one day on one agent. The cooldown stays as the
        minimum spacing between episodes, so a cap that flaps cannot re-alert
        every cycle.

        Reported through `platform_alerts.observe` (#3254) as kind
        `queue_flood`, keyed by the reason: one pending row per (agent, reason),
        rewritten in place. Budgeted (#1677, #3130): the kind's budgeted type is
        `queue_flood`, so `observe` applies the per-(agent, type) budget — an
        agent's own filing volume drives this emitter, which is the test that
        classification applies. The budget is the backstop for what the
        in-memory episode state cannot see (a leader failover re-reads once,
        onto the same row). Excluded from the agent's own depth read by its
        reserved prefix. Softened wording to avoid cry-wolf (C9).

        Re-read while it lasts (#3254 R9): an episode that persists is observed
        again at most every 6 hours, so the kind's 14-day lifetime ends only a
        row nobody re-read (a lost clear), never one whose condition still
        holds. The refresh time per (agent, reason) lives in a dict this method
        creates on the instance for itself (`_flood_refreshed_at`).

        The cooldown and the episode are stamped BEFORE every reading (a re-read
        included). Outcomes:
          * created / updated / snoozed → the episode is consumed (one alert per
            episode; a snoozed reading is a person's ending, which stands);
          * failed on the episode's first reading → the episode is RELEASED so
            the alarm is retried, and the cooldown stamp spaces those retries to
            at most one per window (review I1: holding the episode on a failure
            silenced the alarm until the condition cleared — for a runaway agent,
            never);
          * failed on a re-read → the episode is KEPT: an earlier reading filed
            its alert, and only a live episode lets `_end_flood_episode` end it
            when the condition clears (AC 2). The re-read is retried after the
            cooldown, not after the full 6 hours;
          * refused at budget → the episode is consumed. The agent already has
            a full budget of pending flood alerts and the budget emits its own
            `alert-budget-` episode alert, so re-knocking every window would
            add nothing but budget-episode churn. An unregistered kind is the
            same (a code defect retrying cannot fix).
        Never raises (`observe` never does): an alert must not kill the sync.
        """
        from services import platform_alerts

        key = (agent_name, reason)
        now = time.monotonic()
        refreshed = vars(self).setdefault("_flood_refreshed_at", {})
        rereading = key in self._flood_episodes
        if rereading:
            # R9: the 6-hour re-read of a persisting episode, the #3255
            # base-image alarm's spacing (BASE_IMAGE_ALERT_COOLDOWN_SECONDS).
            last_read = refreshed.get(key)
            if last_read is not None and now - last_read < 6 * 60 * 60:
                return  # this episode already alerted and was read recently
        else:
            # `None` = never alerted for this agent. A `0.0` default would be
            # wrong: `time.monotonic()` is seconds from an arbitrary reference
            # and can be < COOLDOWN on a freshly-booted process, so
            # `now - 0.0 < COOLDOWN` would suppress the FIRST-ever alert for
            # the first COOLDOWN seconds of uptime.
            last = self._flood_alert_cooldown.get(agent_name)
            if last is not None and now - last < OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS:
                return  # too soon after the last episode — the next cycle retries
            self._flood_episodes.add(key)
        # Stamped before every reading, a re-read included, so a failed one —
        # released below — is retried no sooner than the cooldown (I1).
        self._flood_alert_cooldown[agent_name] = now
        refreshed[key] = now

        if reason == "oversize_file":
            title = f"Agent '{agent_name}' produced an oversized operator-queue file"
            question = (
                f"Agent '{agent_name}' wrote an operator-queue.json larger than the "
                f"allowed size; its requests are not being ingested. This can "
                f"indicate a runaway or compromised agent — review its recent "
                f"activity before acting on its requests."
            )
        else:
            title = f"Agent '{agent_name}' exceeded its operator-queue ingestion limit"
            question = (
                f"Agent '{agent_name}' produced more operator-queue requests than the "
                f"per-agent ingestion limit allows; {held} request(s) were held and "
                f"are not shown. Held requests stay in the agent's file and are read "
                f"again once it is under the limit; the agent is told they are held. "
                f"This can indicate a runaway or compromised agent — review its "
                f"recent activity before acting on its requests."
            )

        outcome = platform_alerts.observe(
            agent_name, "queue_flood", reason,
            title=title, question=question, priority="high",
            context={"held": held, "reason": reason},
        )
        if outcome == platform_alerts.OBSERVED_FAILED:
            if rereading:
                # keep the episode so its end still clears the alert; retry the
                # re-read once the cooldown has passed
                refreshed[key] = now - 6 * 60 * 60 + OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS
            else:
                self._flood_episodes.discard(key)  # retry next window (I1)
                refreshed.pop(key, None)
            return
        if outcome in (platform_alerts.OBSERVED_CREATED, platform_alerts.OBSERVED_UPDATED):
            logger.warning(
                f"Operator-queue flood alert {outcome} for {agent_name} "
                f"(reason={reason}, held={held})"
            )

    def _end_flood_episode(self, agent_name: str, reason: str) -> None:
        """#3130 / #3254: the over-cap condition behind `reason` cleared.

        Called from `_sync_agent` where it knows (a sane-size read ends an
        oversize episode; a cycle that held nothing ends an ingestion-cap one).
        Ends the episode, so the next over-cap reading alerts again, and, only
        when one was running, ends its alert as the platform
        (`platform_alerts.clear`, a compare-and-set on pending: a person's
        ending is never overwritten). Every cycle without an episode costs no
        read. Never raises (`clear` never does).
        """
        key = (agent_name, reason)
        if key not in self._flood_episodes:
            return
        self._flood_episodes.discard(key)
        from services import platform_alerts
        platform_alerts.clear(agent_name, "queue_flood", reason)


# Global service instance
operator_queue_service = OperatorQueueSyncService()


# ---------------------------------------------------------------------------
# Re-exports (#3785, O3): only names read from outside the package, by
# production code or by a test that calls or compares one (never patches it).
# Inside the package every reader names the owning module.
# ---------------------------------------------------------------------------

# listing: the router's engine and refusal; the cursor codec tests drive.
from .listing import (  # noqa: E402,F401
    CURSOR_MAX_BYTES,
    QueueListError,
    decode_cursor,
    encode_cursor,
    list_for_principal,
    narrow_to_agent_key,
)
# filechannel: the ingest clamp, the #2915 comparison and the #3130 marker,
# which tests drive directly.
from .filechannel import (  # noqa: E402,F401
    READ_FAILURE_THRESHOLD,
    _AGENT_STATUS_RE,
    _CONTEXT_TRUNCATED_SENTINEL,
    _DETAIL_RE,
    _FLIPPED_BY_WRITE_BACK,
    _HOLD_RANK,
    _apply_ingestion_marker,
    _canonical_options,
    _clamp_ingested_item,
    _comparable_context,
    _current_marker,
    _detail,
    _entry_content,
    _fold_agent_status,
    _ingestion_marker,
    _marker_differs,
    _normalise_expires,
    _read_failure_detail,
    _row_content,
    _stronger_hold,
    _valid_execution_id,
    awaits_terminal_flip,
    changed_fields,
)
# rows: the predicates, the receipt vocabulary and aging, which the router, the
# ask sink, the Workspace and tests read.
from .rows import (  # noqa: E402,F401
    DELIVERY_DELIVERED,
    DELIVERY_NOT_APPLICABLE,
    DELIVERY_UNDELIVERED,
    OPERATOR_QUEUE_AGING_HOURS_DEFAULT,
    REFUSE_RESPONSE_STATES,
    SYNC_CHANGED,
    SYNC_CLOSED_BY_FILER,
    SYNC_CONFIRMED,
    SYNC_MISSING,
    SYNC_STALE_ID,
    aging_hours,
    annotate_aging,
    is_about_a_person,
    is_aged,
    is_owner_only,
    is_person_tag,
    is_platform_minted,
)
