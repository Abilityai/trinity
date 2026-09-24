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

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import db
from redis_breaker_util import get_breaker_redis
from services import rate_limiter
from services.operator_queue_choices import OPTIONS_DROPPED_MARKER
from services.agent_client import AgentClient
from utils.helpers import iso_cutoff, parse_iso_timestamp, to_utc_iso, utc_now_iso

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
OPERATOR_QUEUE_ID_MAX = int(os.getenv("OPERATOR_QUEUE_ID_MAX", "256"))
OPERATOR_QUEUE_EXECUTION_ID_MAX = int(os.getenv("OPERATOR_QUEUE_EXECUTION_ID_MAX", "128"))
# ent#364: RFC 5321 caps an address at 320 chars; anything longer is not an
# email and never matches a roster row, so it is refused before the DB read.
OPERATOR_QUEUE_EMAIL_MAX = int(os.getenv("OPERATOR_QUEUE_EMAIL_MAX", "320"))
# Flood alert: one per episode, un-guessable id, in-memory cooldown.
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
    "skill-not-found-",  # task_execution_service
    "val_",              # validation_service
    "system-seed-",      # system_seed_service first-run seed alerts (ent#124)
    "base-image-stale-", # system_agent_service staleness + start-failure (#1816)
    "alert-budget-",     # this service's #1677 budget episode alert (deterministic
                         # bucketed id — reservation is what keeps the DB
                         # on-conflict dedup from being agent-pre-suppressible)
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
    # git_service per-Push sweep alert (#2529). Reserved for the #1632 reason
    # above, and — since ent#499 keyed `is_platform_minted` on this very tuple —
    # this listing is ALSO what keeps the alert out of the agent's own
    # `~/.trinity/operator-queue.json`. Correct: the sweep alarm is a platform
    # alarm ABOUT the agent, not a loop the agent opened and is waiting on.
    "gitignore-untracked-",
    ROLE_DRIFT_ALERT_PREFIX,  # role-assignment drift (trinity-enterprise#500) —
                           # the role file lives in the AGENT'S OWN workspace, so
                           # an unreserved prefix would let it pre-create the id
                           # of the alert about its own configuration
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


def _valid_execution_id(value) -> Optional[str]:
    """#1632: an execution_id salvaged into a truncation marker must itself be
    bounded and id-shaped — else a 100 KB execution_id smuggled through the
    marker would defeat the context cap (C4)."""
    if (
        isinstance(value, str)
        and 0 < len(value) <= OPERATOR_QUEUE_EXECUTION_ID_MAX
        and _ID_RE.match(value)
    ):
        return value
    return None


def is_platform_minted(item) -> bool:
    """Was this queue item raised by the PLATFORM rather than by the agent?

    ent#499. The two agent-facing return paths — the responded write-back into
    ``~/.trinity/operator-queue.json`` and the ent#329 respond→resume dispatch —
    both exist to close a loop the AGENT opened: it parked a question, a human
    answered, the answer goes back. A platform alarm opened no such loop. The
    agent never asked, is not waiting, and in ent#499's case is the SUBJECT of
    the complaint rather than its author.

    Feeding those back is not merely useless, it is a disclosure: ent#499's body
    carries a client's email and their verbatim words, which ent#366 deliberately
    withholds from the rated agent (``comment_withheld``). Without this predicate
    an operator clicking "Got it" hands both to that agent within one 5s sync
    cycle, and — with ``operator_resume_enabled`` — spends one of its turns doing
    it.

    Keyed on the reserved id prefixes, which are already the platform's marker
    for "an agent may not mint this id" (#1632). One predicate, both sinks, so
    they cannot drift.
    """
    if isinstance(item, str):
        candidate = item
    elif isinstance(item, dict):
        candidate = item.get("request_id") or item.get("id") or ""
    else:
        candidate = getattr(item, "request_id", "") or getattr(item, "id", "") or ""
    return str(candidate).strip().lower().startswith(_RESERVED_ID_PREFIXES)


def _truncate_with_marker(text: str, max_len: int) -> str:
    """Truncate so the RESULT (content + marker) is ≤ max_len chars."""
    if len(text) <= max_len:
        return text
    keep = max(0, max_len - len(_TRUNC_MARKER))
    return text[:keep] + _TRUNC_MARKER


def _validated_addressee(agent_name: str, raw) -> Optional[str]:
    """The email an ask is addressed to, or None (ent#364).

    This is the one field on an agent-authored item that is an AUTHORIZATION
    decision: it determines who may answer the ask and whose Workspace sidebar it
    appears in. So it is validated here, at the same boundary that clamps every
    other agent-supplied field, against the agent's own roster — an agent may
    address only someone it was already shared with.

    `include_owned=False` is the rule, not a default (see `agent_on_roster`'s
    docstring): an external client's scope is exactly what was shared with them,
    and the owned-agents branch would hand a client agents nobody gave them.

    Fails CLOSED: a malformed value, an off-roster address, or a roster lookup that
    raises all yield None — an operator ask — because addressing an ask we could
    not validate is worse than not addressing it. Never raises, per this module's
    #1632 contract.
    """
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > OPERATOR_QUEUE_EMAIL_MAX or "@" not in email:
        return None
    try:
        from client_portal.service import agent_on_roster

        if agent_on_roster(agent_name, email, include_owned=False):
            return email
        logger.info(
            "[OperatorQueue] %s addressed an item to an off-roster email; "
            "treating it as an operator ask (ent#364)",
            agent_name,
        )
    except Exception:  # noqa: BLE001 — a roster read must never break ingestion
        logger.warning(
            "[OperatorQueue] could not validate the addressee for %s; "
            "treating it as an operator ask (ent#364)",
            agent_name,
            exc_info=True,
        )
    return None


# ent#429: the context key naming the chat an addressed ask belongs to. Written
# by the platform (below), read by `client_portal/asks/service.py::_project` as
# the client-facing `chat_id`, and stripped from anything the agent authored —
# named here so the writer and the stripper cannot drift apart.
_WORKSPACE_THREAD_KEY = "workspace_session_id"


def _workspace_thread_for(agent_name: str, email: str) -> Optional[str]:
    """The chat an addressed ask attaches to, or None (ent#429).

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
    try:
        from client_portal.service import ensure_thread_for_ask

        return ensure_thread_for_ask(agent_name, email) or None
    except Exception:  # noqa: BLE001
        logger.warning(
            "[OperatorQueue] could not attach a workspace chat for an ask from %s; "
            "it will render without a thread link (ent#429)",
            agent_name,
            exc_info=True,
        )
        return None


def _clamp_ingested_item(req: dict, agent_name: str = "") -> dict:
    """#1632: total field-hygiene clamp for an agent-authored queue item.

    Called INSIDE the create try/except so any failure is quarantined by #1525
    rather than hot-looping — but it is written to NEVER raise (every branch is
    isinstance-guarded, json.dumps is wrapped). Returns a NEW dict; never mutates
    the caller's request.

    NOT PURE, and the name undersells it. Two of the steps below reach the
    database, and one of them WRITES:

    * `addressed_to_email` (ent#364) is resolved against the agent's roster —
      an authorization decision, and the reason it is not simply copied through.
    * `context.workspace_session_id` (ent#429) is stripped and then re-written
      with a thread this call may CREATE (`_workspace_thread_for`).

    So this is not safe to call speculatively "just to see what a clamped item
    would look like": today's one production caller creates the row immediately
    after, and a second caller that did not would leave an empty client thread
    behind. Both reads/writes are fail-soft and neither can raise, so the #1525
    contract above still holds.

    - title / question: truncate-with-marker.
    - context: non-dict → {} (fixes the create_item .get crash class); serialized
      > cap OR non-serializable → a marker object that preserves only a *valid*
      (≤EXECUTION_ID_MAX, id-shaped) execution_id.
    - options: serialized > cap OR non-serializable → a small marker list.
    - created_at: normalized to ingest time (defeats future-date sort-pinning);
      expires_at is left untouched (honored).
    - priority: validate-only (unknown → medium; a legit `critical` is untouched —
      the depth cap already bounds critical *volume*).
    - addressed_to_email: resolved, never trusted (ent#364) — see above.
    - context.workspace_session_id: agent value stripped, platform value written
      for an addressed ask (ent#429) — see above.
    """
    out = dict(req)

    title = out.get("title")
    if isinstance(title, str):
        out["title"] = _truncate_with_marker(title, OPERATOR_QUEUE_TITLE_MAX)

    question = out.get("question")
    if isinstance(question, str):
        out["question"] = _truncate_with_marker(question, OPERATOR_QUEUE_QUESTION_MAX)

    # ent#364: the addressee is an authorization decision, so it is resolved here
    # rather than trusted. Absent/invalid/off-roster → None, i.e. an operator ask.
    #
    # Resolved BEFORE the context block below, not after, because ent#429 writes
    # the addressee's thread id INTO context — doing it afterwards would add
    # bytes the size cap had already signed off on.
    out["addressed_to_email"] = _validated_addressee(agent_name, out.get("addressed_to_email"))

    context = out.get("context")
    if not isinstance(context, dict):
        # Non-dict context (str/list/None) → {} — also fixes the pre-existing
        # create_item execution_id `.get` crash. An ask with no context at all is
        # the COMMON case for a scheduled run, and it is exactly the one that must
        # still get a thread (ent#429), so the attach happens on this branch too.
        out["context"] = {}
        if out["addressed_to_email"]:
            thread_id = _workspace_thread_for(agent_name, out["addressed_to_email"])
            if thread_id:
                out["context"][_WORKSPACE_THREAD_KEY] = thread_id
    else:
        # ent#429: `workspace_session_id` is PLATFORM-written and this is the one
        # place that writes it. Stripped UNCONDITIONALLY first — an agent that
        # could author it would be choosing which conversation its ask claims to
        # belong to, and `_project` hands that straight to the client as
        # `chat_id`. The projection's docstring already promised "platform-written
        # context only"; nothing enforced it until now.
        #
        # Rebuilt rather than popped: `out = dict(req)` is a SHALLOW copy, so the
        # context dict is still the caller's, and this function's contract is that
        # it never mutates the request it was handed.
        context = {k: v for k, v in context.items() if k != _WORKSPACE_THREAD_KEY}
        out["context"] = context
        if out["addressed_to_email"]:
            thread_id = _workspace_thread_for(agent_name, out["addressed_to_email"])
            if thread_id:
                context[_WORKSPACE_THREAD_KEY] = thread_id
        try:
            ctx_bytes = len(json.dumps(context).encode("utf-8"))
        except (TypeError, ValueError):
            ctx_bytes = None  # non-serializable
        if ctx_bytes is None or ctx_bytes > OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
            marker = {
                "_truncated": True,
                "_original_bytes": ctx_bytes,
                "execution_id": _valid_execution_id(context.get("execution_id")),
            }
            # The thread id survives truncation. It is platform-written and ~32
            # bytes, and dropping it would make an oversize agent context the one
            # way to produce a homeless ask (ent#429).
            if context.get(_WORKSPACE_THREAD_KEY):
                marker[_WORKSPACE_THREAD_KEY] = context[_WORKSPACE_THREAD_KEY]
            out["context"] = marker

    options = out.get("options")
    if options is not None:
        try:
            opt_bytes = len(json.dumps(options).encode("utf-8"))
        except (TypeError, ValueError):
            opt_bytes = None  # non-serializable
        if opt_bytes is None or opt_bytes > OPERATOR_QUEUE_OPTIONS_MAX_BYTES:
            out["options"] = [_OPTIONS_DROPPED_MARKER]

    if out.get("priority") not in _VALID_PRIORITIES:
        out["priority"] = "medium"

    # Ignore the agent-supplied created_at (a future date pins the item atop the
    # `created_at DESC` sort, C4). expires_at is honored as authored.
    out["created_at"] = utc_now_iso()

    return out


# =========================================================================
# #2915: sync honesty — the vocabulary the loop writes, the fingerprint it
# compares, and the aging predicate every reader shares.
#
# The file contract is unchanged (ingestion is create-only; an agent's rewrite
# is NEVER applied in place — an approval is frozen to the exact action the
# human read). What this adds is that every way the two sides can disagree is
# detected, recorded on the row, and audited, instead of presenting as fine.
# =========================================================================

SYNC_CONFIRMED = "confirmed"          # entry present, same content, same status
SYNC_CHANGED = "changed"              # entry present, content rewritten (detail = fields)
SYNC_CLOSED_BY_FILER = "closed_by_filer"  # entry carries a status the agent set
SYNC_MISSING = "missing"              # entry gone (entry_missing / file_missing)
SYNC_STALE_ID = "stale_id"            # a pending entry re-uses a terminal row's id
SYNC_UNCONFIRMED = "unconfirmed"      # the poller could not reconcile (detail = why)
SYNC_STATES = frozenset({
    SYNC_CONFIRMED, SYNC_CHANGED, SYNC_CLOSED_BY_FILER, SYNC_MISSING,
    SYNC_STALE_ID, SYNC_UNCONFIRMED,
})
# States that audit as `diverged` when entered, `reconciled` when left for confirmed.
DIVERGED_STATES = frozenset({SYNC_CHANGED, SYNC_CLOSED_BY_FILER, SYNC_MISSING, SYNC_STALE_ID})
# States on which a response is refused (409 item_diverged) without an explicit
# acknowledgement: the card the human read is not what the agent now holds.
REFUSE_RESPONSE_STATES = frozenset({SYNC_CHANGED, SYNC_CLOSED_BY_FILER})

DELIVERY_DELIVERED = "delivered"
DELIVERY_UNDELIVERED = "undelivered"
DELIVERY_NOT_APPLICABLE = "not_applicable"
DELIVERY_STATES = frozenset({DELIVERY_DELIVERED, DELIVERY_UNDELIVERED, DELIVERY_NOT_APPLICABLE})

# `sync_detail` / `delivery_detail` are durable, operator-visible, audited
# columns — a CLOSED vocabulary. Field names, folded status tokens and failure
# kinds only; never `str(e)`, never `response.text` (agent-controlled), never
# agent-authored text.
_DETAIL_RE = re.compile(r"^[a-z0-9_,]{1,96}$")
_AGENT_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")
READ_FAILURE_THRESHOLD = 3            # consecutive failed cycles before `unconfirmed` (sync_health precedent)
LAST_CONFIRMED_REFRESH_SECONDS = 60   # `last_confirmed_at` cadence — one batched UPDATE per agent per minute
OPERATOR_QUEUE_AGING_HOURS_KEY = "operator_queue_aging_hours"
OPERATOR_QUEUE_AGING_HOURS_DEFAULT = 24
_PLATFORM_BLOCK_KEY = "platform"      # the receipt lives under `platform` in the agent's entry
_AGING_SINCE_KEY = "aging_since"
_CONTENT_FIELDS = ("title", "question", "options", "expires_at", "type", "priority", "context", "addressee")
_CONTEXT_TRUNCATED_SENTINEL = "<truncated>"


def _detail(token) -> str:
    """Fold any candidate into the closed detail vocabulary; unknown ⇒ `other`."""
    s = str(token or "").strip().lower()
    return s if _DETAIL_RE.match(s) else "other"


def _fold_agent_status(raw) -> str:
    """An agent-written status becomes a short lowercase token or `other`."""
    s = str(raw or "").strip().lower()
    return s if _AGENT_STATUS_RE.match(s) else "other"


def _read_failure_detail(result: dict) -> str:
    """Why a read failed, as a token — a status code or a class, never text."""
    code = result.get("status_code") if isinstance(result, dict) else None
    if isinstance(code, int):
        return f"http_{code}"
    err = str(result.get("error") or "") if isinstance(result, dict) else ""
    return "timeout" if "timeout" in err.lower() else "unreachable"


def _normalise_expires(value) -> str:
    if not value:
        return ""
    try:
        return to_utc_iso(parse_iso_timestamp(str(value)))
    except Exception:  # noqa: BLE001 — an unparseable deadline compares as its text
        return str(value)


def _canonical_options(options) -> str:
    if options is None:
        return ""
    try:
        return json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps([_OPTIONS_DROPPED_MARKER])


def _entry_content(req: dict) -> dict:
    """The comparable content of an agent's file entry — the PURE half of
    `_clamp_ingested_item` (caps, markers) plus `create_item`'s defaults, so it
    matches what the row stored at ingest byte for byte. `_clamp_ingested_item`
    itself is deliberately not called: it reads the roster and may create a
    workspace thread, and must never run speculatively."""
    title = req.get("title")
    title = _truncate_with_marker(title, OPERATOR_QUEUE_TITLE_MAX) if isinstance(title, str) else None
    question = req.get("question")
    question = _truncate_with_marker(question, OPERATOR_QUEUE_QUESTION_MAX) if isinstance(question, str) else None
    options = req.get("options")
    if options is not None:
        try:
            if len(json.dumps(options).encode("utf-8")) > OPERATOR_QUEUE_OPTIONS_MAX_BYTES:
                options = [_OPTIONS_DROPPED_MARKER]
        except (TypeError, ValueError):
            options = [_OPTIONS_DROPPED_MARKER]
    return {
        "title": title or "Agent request",
        "question": question or title or "(no details provided)",
        "options": _canonical_options(options),
        "expires_at": _normalise_expires(req.get("expires_at")),
        "type": _comparable_type(req.get("type")),
        "priority": _comparable_priority(req.get("priority")),
        "context": _comparable_context(req.get("context")),
        "addressee": _comparable_addressee(req.get("addressed_to_email")),
    }


def _row_content(row: dict) -> dict:
    return {
        "title": row.get("title") or "Agent request",
        "question": row.get("question") or row.get("title") or "(no details provided)",
        "options": _canonical_options(row.get("options")),
        "expires_at": _normalise_expires(row.get("expires_at")),
        "type": _comparable_type(row.get("type")),
        "priority": _comparable_priority(row.get("priority")),
        "context": _comparable_context(row.get("context")),
        "addressee": _comparable_addressee(row.get("addressed_to_email")),
    }


def _comparable_type(value) -> str:
    return "question" if value in (None, "") else str(value)


def _comparable_priority(value) -> str:
    return value if value in _VALID_PRIORITIES else "medium"   # the clamp's default


def _comparable_addressee(value) -> Optional[str]:
    return (str(value).strip().lower() or None) if isinstance(value, str) else None


def _comparable_context(ctx) -> str:
    """The clamp's PURE half for `context` (#2989 review): non-dict → {}, the
    platform's workspace-thread key stripped (the clamp writes it; it is never
    agent content), and an oversize / unserialisable value → one sentinel — which
    is also what a row holds after ingest (`_truncated`). Compared as canonical
    JSON. Sized the way the clamp sizes it, so the cap is crossed on both sides
    at (very nearly) the same input."""
    if not isinstance(ctx, dict):
        return "{}"
    if ctx.get("_truncated") is True:
        return _CONTEXT_TRUNCATED_SENTINEL
    body = {k: v for k, v in ctx.items() if k != _WORKSPACE_THREAD_KEY}
    try:
        if len(json.dumps(body).encode("utf-8")) > OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
            return _CONTEXT_TRUNCATED_SENTINEL
        return json.dumps(body, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _CONTEXT_TRUNCATED_SENTINEL


def changed_fields(row: dict, req: dict) -> list:
    """Which content fields the agent rewrote since ingest. `addressee` counts
    only when the row resolved one: the ingest value is an authorization
    decision against the roster (ent#364), so an addressee the roster refused
    is not a rewrite — a re-address is ent#619's supersede vocabulary."""
    a, b = _row_content(row), _entry_content(req)
    out = []
    for f in _CONTENT_FIELDS:
        if f == "addressee" and not a["addressee"]:
            continue
        if a[f] != b[f]:
            out.append(f)
    return out


def _well_formed_queue(data) -> bool:
    """An object whose `requests` (if present) is a list. Anything else is the
    WRONG SHAPE and is read as `unconfirmed`, never as an empty queue (#2989
    review: `{"requests": {...}}` used to mark every row `missing` and then
    overwrite the agent's file; a top-level array raised inside `gather`)."""
    return isinstance(data, dict) and isinstance(data.get("requests", []), list)


def _deliver_into(req: dict, resp: dict) -> None:
    req["status"] = "responded"
    req["response"] = resp["response"]
    req["response_text"] = resp.get("response_text")
    req["responded_by"] = resp.get("responded_by_email")
    req["responded_at"] = resp.get("responded_at")


def _is_our_answer(req: dict, resp: dict) -> bool:
    """Did a previous write-back land THIS answer in the entry? Matched on the
    answer's own timestamp, so an entry the agent closed itself (`acknowledged`
    with no `responded_at`) is never mistaken for a delivered one."""
    return (
        req.get("status") in ("responded", "acknowledged")
        and bool(resp.get("responded_at"))
        and req.get("responded_at") == resp.get("responded_at")
    )


def aging_hours() -> int:
    """The operator's bound, `0` = disabled; unreadable ⇒ the default + WARN."""
    try:
        raw = db.get_setting_value(
            OPERATOR_QUEUE_AGING_HOURS_KEY, str(OPERATOR_QUEUE_AGING_HOURS_DEFAULT)
        )
        return max(0, int(str(raw).strip()))
    except Exception as e:  # noqa: BLE001 — a bad setting must not break a list
        logger.warning("[OperatorQueue] aging bound unreadable (%s); using %d h",
                       e, OPERATOR_QUEUE_AGING_HOURS_DEFAULT)
        return OPERATOR_QUEUE_AGING_HOURS_DEFAULT


def _aged_at(item: dict, hours: int) -> Optional[datetime]:
    if hours <= 0 or item.get("status") != "pending":
        return None
    created = item.get("created_at")
    if not created:
        return None
    try:
        return parse_iso_timestamp(str(created)) + timedelta(hours=hours)
    except Exception:  # noqa: BLE001
        return None


def is_aged(item: dict, hours: Optional[int] = None, now: Optional[datetime] = None) -> bool:
    """Has this pending item waited past the operator's bound? ONE predicate for
    the list API, the portal projection and the receipt the poller writes."""
    hours = aging_hours() if hours is None else hours
    at = _aged_at(item, hours)
    if at is None:
        return False
    return at <= (now or datetime.now(timezone.utc))


def annotate_aging(items: list, hours: Optional[int] = None) -> list:
    """Adds `aging` / `aged_since` to every item in place; the frontend renders,
    it never recomputes."""
    hours = aging_hours() if hours is None else hours
    now = datetime.now(timezone.utc)
    for item in items:
        at = _aged_at(item, hours)
        aged = at is not None and at <= now
        item["aging"] = aged
        item["aged_since"] = to_utc_iso(at) if aged else None
    return items


async def _audit_sync(action: str, agent_name: str, item_id: str, details: dict) -> None:
    """Accountability transitions only (ingested · diverged · reconciled ·
    written_back · undeliverable) — ids and enums, never agent text; best-effort."""
    try:
        from services.platform_audit_service import platform_audit_service, AuditEventType
        await platform_audit_service.log(
            event_type=AuditEventType.OPERATOR_QUEUE,
            event_action=action,
            source="system",
            target_type="operator_queue",
            target_id=str(item_id),
            details={"agent_name": agent_name, **details},
        )
    except Exception as e:  # noqa: BLE001 — audit never breaks the sync
        logger.debug("[OperatorQueue] audit %s for %s skipped: %s", action, item_id, e)


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


async def create_bounded_alert(agent_name: str, item: dict) -> bool:
    """#1677: the create seam for agent-INFLUENCEABLE platform alert emitters.

    Platform-only emitters (edge-triggered, idempotent-id, operator-cadence
    creates) keep calling ``db.create_operator_queue_item`` directly; an
    emitter whose volume an agent can drive routes through here instead. The
    budget type is derived from ``item["type"]`` — never a separate parameter
    (two sources of one fact silently void the bound) — and MUST be a static
    literal registered in ``_BUDGETED_ALERT_TYPES`` (a caller-interpolated
    type would mint a fresh budget per distinct string).

    Four-outcome contract (never raises):
      * type not registered      → ERROR log, ``False`` (fail-closed, no create,
        no episode alert — register the type, a one-line reviewed act);
      * pending-count read raises → ERROR log, ``False`` (fail-closed, NO
        episode alert: that path is "DB broken", not "at cap" — the #1632
        mirror, whose depth count also runs outside the per-item try; the
        FAILED execution rows remain the primary observability surface);
      * count ≥ cap               → ONE cooldown-gated, bucketed-id episode
        alert per window (``_maybe_emit_alert_budget_episode``), ``False``;
      * else                      → create; ``True`` on success, ERROR log +
        ``False`` on a create raise (NO episode alert).

    Callers gate every paired side-effect (e.g. the skill-not-found
    notification) on the returned bool, so a secondary surface can never
    outlive its queue item.
    """
    item_type = item.get("type") if isinstance(item, dict) else None
    if item_type not in _BUDGETED_ALERT_TYPES:
        logger.error(
            "[#1677 budget] refusing operator alert for '%s': type %r is not a "
            "registered budgeted type (%s) — add it to _BUDGETED_ALERT_TYPES "
            "before routing an emitter through create_bounded_alert "
            "(fail-closed; no item created)",
            agent_name, item_type, sorted(_BUDGETED_ALERT_TYPES),
        )
        return False

    try:
        pending = int(
            db.count_operator_queue_pending_for_agent(agent_name, item_type=item_type)
        )
    except Exception as e:
        logger.error(
            "[#1677 budget] pending-count read failed for %s/%s (%s) — alert "
            "suppressed (fail-closed, NO episode alert; the FAILED execution "
            "rows remain the primary surface)",
            agent_name, item_type, e,
        )
        return False

    if pending >= OPERATOR_ALERT_MAX_PENDING_PER_TYPE:
        context = item.get("context")
        last_triggered_by = (
            context.get("triggered_by") if isinstance(context, dict) else None
        )
        # Guarded so the four-outcome never-raises contract holds structurally:
        # the episode alert is a secondary signal over an already-refused item,
        # and a raise here (review fix: the cooldown-knob=0 ZeroDivisionError
        # in the bucket computation was one such path) must never leak into a
        # caller that trusts the docstring.
        try:
            await _maybe_emit_alert_budget_episode(
                agent_name, item_type, last_triggered_by
            )
        except Exception as e:
            logger.error(
                "[#1677 budget] episode-alert emit raised for %s/%s (%s) — "
                "swallowed (the refusal itself stands)",
                agent_name, item_type, e,
            )
        return False

    # NO `await` between the count read above and this create — the
    # check-then-act overshoot window stays cross-worker-only (bounded by the
    # number of concurrent terminal writers for one agent), never widened by a
    # yield on this worker.
    try:
        db.create_operator_queue_item(agent_name, item)
    except Exception as e:
        logger.error(
            "[#1677 budget] operator alert create failed for %s/%s (%s) — "
            "suppressed (fail-closed, NO episode alert)",
            agent_name, item_type, e,
        )
        return False
    return True


async def _maybe_emit_alert_budget_episode(
    agent_name: str, item_type: str, last_triggered_by=None
) -> None:
    """#1677: ONE aggregated budget-hit episode alert per cooldown window.

    Mirrors `_maybe_emit_flood_alert`'s pattern (cooldown stamped BEFORE the
    create, direct DB create, swallowed emit failure, best-effort WS
    broadcast) with two deliberate differences: module-level cooldown state
    (see `_alert_budget_cooldown`) and a **deterministic time-bucketed id** —
    `alert-budget-{agent}-{type}-b{bucket}` — so under `--workers 2` / worker
    churn every worker computes the SAME id within a window and the DB
    `(agent_name, request_id)` on_conflict_do_nothing target dedups the
    duplicate at the sink (#1816 bucketed-id precedent). A boundary-straddling
    episode may emit 2 alerts — the same acceptance as the #1632 cooldown.
    The `alert-budget-` prefix is in `_RESERVED_ID_PREFIXES`, so an agent
    cannot pre-create (and thereby on-conflict-suppress) the alert.
    """
    now = time.monotonic()
    key = (agent_name, item_type)
    # `None` = never alerted (the #1632 sentinel shape — a 0.0 default would
    # suppress the first-ever alert while monotonic < cooldown after boot).
    last = _alert_budget_cooldown.get(key)
    if last is not None and now - last < OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS:
        return  # already alerted this episode
    # Stamped BEFORE the create so a persistently-failing create backs off for
    # the window instead of retrying on every refusal (#1632 shape).
    _alert_budget_cooldown[key] = now

    # max(1, …): the cooldown knob at 0 (the natural "no cooldown" spelling)
    # must degrade to a 1s bucket — matching the flood alert's alert-every-
    # episode behavior at 0 — never a ZeroDivisionError on the at-cap path.
    bucket = int(time.time() // max(1, OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS))
    now_iso = utc_now_iso()
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

    alert = {
        "id": f"alert-budget-{agent_name}-{item_type}-b{bucket}",
        "type": "alert",
        "status": "pending",
        "priority": "high",
        "title": title,
        "question": question,
        "context": context,
        "created_at": now_iso,
    }

    # Direct DB create — a PLATFORM-only emitter by construction (this
    # function never re-enters create_bounded_alert, so the budget cannot
    # recurse into itself). Emit failure swallowed: the episode alert is a
    # secondary signal over an already-suppressed alert.
    try:
        db.create_operator_queue_item(agent_name, alert)
    except Exception as e:
        logger.error(
            "[#1677 budget] failed to emit budget episode alert for %s/%s: %s",
            agent_name, item_type, e,
        )
        return

    logger.warning(
        "[#1677 budget] alert budget hit for %s/%s (cap=%d) — episode alert emitted",
        agent_name, item_type, OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
    )

    if _websocket_manager:
        try:
            await _websocket_manager.broadcast(json.dumps({
                "type": "operator_queue_new",
                "data": {
                    "id": alert["id"],
                    "agent_name": agent_name,
                    "type": "alert",
                    "priority": "high",
                    "title": alert["title"],
                    "created_at": alert["created_at"],
                },
            }))
        except Exception as e:
            logger.error(
                "[#1677 budget] failed to broadcast budget episode alert for %s: %s",
                agent_name, e,
            )


class OperatorQueueSyncService:
    """Background service that syncs operator queue files with the database."""

    def __init__(self, poll_interval: int = DEFAULT_POLL_INTERVAL):
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # #1525: (agent_name, req_id) → consecutive create-failure count. Bounds
        # the retry loop for a request whose DB create keeps raising (malformed
        # input, a DB error, …) so it can't hot-loop forever. Entry is dropped on
        # success. #1631: keyed by the (agent, id) TUPLE — two agents can now
        # share a req_id, so a bare-id key would let agent A's failing id
        # quarantine agent B's distinct, healthy request.
        self._create_failures: dict[tuple[str, str], int] = {}
        # #1631: (agent, req_id) already warned about for a reserved-prefix
        # hijack attempt — logged once, not every ~5s cycle. Bounded like the
        # quarantine map so a crafted stream of unique reserved ids can't grow it
        # without bound.
        self._rejected_reserved: set[tuple[str, str]] = set()
        # #1632: unique per worker process so the cross-worker leader lock only
        # ever refreshes/releases ITS OWN lease (mirror monitoring #1464).
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs
        # #1632: agent_name → monotonic ts of the last flood alert, so a sustained
        # flood emits one alert per cooldown episode, not one per 5s cycle.
        self._flood_alert_cooldown: dict[str, float] = {}
        # #2915: consecutive failed reads per agent — hysteresis before a row is
        # called `unconfirmed` (a busy container times out intermittently).
        self._read_failures: dict[str, int] = {}
        # #2915: did any row's sync/delivery state change this cycle? ONE thin WS
        # trigger per cycle, never per agent or per item.
        self._changed_this_cycle = False

    def start(self):
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Operator queue sync service started (interval={self.poll_interval}s)")

    def stop(self):
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # #1632: hand leadership off immediately on graceful shutdown instead of
        # leaving a sibling worker idle until the TTL expires. Never raises.
        self._release_leadership()
        logger.info("Operator queue sync service stopped")

    def _leader_ttl(self) -> int:
        """Lease TTL. Refreshed once at the TOP of each `_poll_cycle`, so it must
        comfortably outlast ONE worst-case cycle plus the inter-cycle sleep — or
        the lease expires mid-cycle, a sibling grabs it, and leadership flaps
        (both workers run an overlapping cycle → the flood alert double-emits,
        since its id carries `utc_now_iso()` and so isn't deduped by
        `on_conflict`). A `_sync_agent` can take a fast read + `write_file`
        (10s timeout, `_write_responses_to_agent`), and the loop then sleeps
        `poll_interval`, so the bare `poll_interval * 3` (15s at the 5s default)
        has ~zero headroom over a single slow-writing agent. Floor at 30s so one
        slow write can't drop leadership. (Monitoring #1464 mirrors this pattern
        but at a 30s interval, where `* 3` already dwarfs its cycle.)"""
        return max(self.poll_interval * 3, 30)

    def _try_acquire_leadership(self) -> bool:
        """#1632 — cross-worker leader election for the sync loop (mirror
        monitoring #1464). Returns True iff this worker holds the lease for this
        cycle. Fail-open: if Redis is unreachable, act as leader (single-worker
        dev keeps syncing; in a Redis-down prod the DB depth cap still bounds a
        double feed, and duplicate creates are idempotent via on_conflict)."""
        r = get_breaker_redis()
        if r is None:
            return True  # fail-open: no Redis → behave as the sole worker
        ttl = self._leader_ttl()
        try:
            if r.set(_LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            # Already held — refresh the TTL only if the lease is OURS.
            if r.get(_LEADER_KEY) == self._worker_id:
                r.expire(_LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:
            logger.warning("operator-queue leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        """Delete the lease iff we hold it (best-effort, never raises)."""
        try:
            r = get_breaker_redis()
            if r is not None and r.get(_LEADER_KEY) == self._worker_id:
                r.delete(_LEADER_KEY)
        except Exception:
            pass

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Operator queue sync error: {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    async def _poll_cycle(self):
        """Single poll cycle: sync all running agents."""
        # #1632: only the lease-holding worker syncs (mirror monitoring #1464),
        # so `--workers 2` doesn't double-charge the rate limiter, double-scan
        # agent files, or double-broadcast the flood alert.
        leader = self._try_acquire_leadership()
        if leader and not self._is_leader:
            logger.info("Operator queue sync acquired leadership (worker %s)", self._worker_id)
        elif not leader and self._is_leader:
            logger.info("Operator queue sync yielded leadership (worker %s)", self._worker_id)
        self._is_leader = leader
        if not leader:
            return

        from services.docker_service import agent_container_states

        # Expire items past their deadline — BEFORE the running-agents gate
        # (#2915): the early return below used to sit above this, so an
        # all-stopped fleet never expired anything.
        try:
            expired_count = db.mark_operator_queue_expired()
            if expired_count > 0:
                logger.info(f"Expired {expired_count} operator queue items")
        except Exception as e:
            logger.error(f"Operator queue expiry failed: {e}")

        # #2915: TRI-state, deliberately (#2196 class). `list_all_agents_fast`
        # collapses "Docker unreadable" into "no agents"; keyed on that, one
        # daemon blip would flip every open row fleet-wide to
        # `unconfirmed:agent_not_running` and back. `None` here means "could
        # not look" — nothing is swept and nothing is synced this cycle.
        try:
            states = agent_container_states()
        except Exception as e:
            logger.debug(f"Could not read agent container states: {e}")
            states = None
        if states is None:
            return

        running_agents = sorted(name for name, state in states.items() if state == "running")
        now = utc_now_iso()
        self._changed_this_cycle = False

        # Sweep: every open row of an agent that is NOT running is
        # `unconfirmed:agent_not_running`. Edge-triggered — at steady state the
        # WHERE matches nothing. An empty running list sweeps every open row
        # (an explicit branch in the accessor; never `notin_([])`).
        try:
            swept = db.mark_operator_queue_unconfirmed(
                "agent_not_running", now, exclude_agents=running_agents,
                exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
            )
            if swept:
                self._changed_this_cycle = True
        except Exception as e:
            logger.error(f"Operator queue not-running sweep failed: {e}")

        # #2989 review (AC3): an answer or terminal flip waiting on an agent that
        # is NOT running cannot reach its file — the row says so,
        # `undelivered:agent_not_running`, not merely `unconfirmed`. Edge-triggered
        # and audited once per transition; the write-back flips it to `delivered`
        # when the agent is back. Platform alarms (`not_applicable`) are skipped.
        try:
            stalled = db.mark_operator_queue_undelivered_for_stopped_agents(
                now, running_agents=running_agents,
                exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
            )
        except Exception as e:
            logger.error(f"Operator queue not-running delivery sweep failed: {e}")
            stalled = []
        for row in stalled:
            self._changed_this_cycle = True
            await _audit_sync("undeliverable", row.get("agent_name") or "", row["id"],
                              {"status": row.get("status"), "detail": "agent_not_running"})

        if running_agents:
            # Sync each agent concurrently (with a reasonable limit)
            tasks = [self._sync_agent(name) for name in running_agents]
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._changed_this_cycle:
            await self._broadcast_sync()

    async def _broadcast_sync(self):
        """ONE thin trigger per cycle (#918: identifiers only — here none; the
        store refetches the access-controlled list). Never per agent or per
        item: a fleet restart would otherwise fire hundreds of refetches."""
        if not _websocket_manager:
            return
        try:
            await _websocket_manager.broadcast(json.dumps({
                "type": "operator_queue_sync",
                "data": {},
            }))
        except Exception as e:
            logger.error(f"Failed to broadcast operator-queue sync: {e}")

    async def _apply_sync_state(self, agent_name: str, row: dict, state: str,
                                detail: Optional[str], now: str) -> bool:
        """Record what the poller established about one row; True iff it changed.

        The row is the edge: `set_sync_state`'s WHERE excludes rows already
        carrying the value and its rowcount is the transition, so two
        overlapping leaders cannot double-record. Audit ONLY the accountability
        transitions — entering a diverged state, or leaving one for confirmed.
        The `confirmed ↔ unconfirmed` flap is never audited: a fleet restart
        must write zero audit rows.
        """
        if state not in SYNC_STATES:
            logger.error(f"Refusing unknown sync_state {state!r} for {row.get('id')} (programming error)")
            return False
        detail = _detail(detail) if detail else None
        prior = row.get("sync_state")
        if prior == state and (row.get("sync_detail") or "") == (detail or ""):
            return False
        try:
            changed = db.set_operator_queue_sync_state(row["id"], state, detail, now)
        except Exception as e:
            logger.error(f"Failed to record sync state for {row.get('id')}: {e}")
            return False
        if not changed:
            return False
        row["sync_state"] = state
        row["sync_detail"] = detail
        self._changed_this_cycle = True
        if state in DIVERGED_STATES:
            await _audit_sync("diverged", agent_name, row["id"],
                              {"from": prior, "to": state, "detail": detail})
        elif state == SYNC_CONFIRMED and prior in DIVERGED_STATES:
            await _audit_sync("reconciled", agent_name, row["id"], {"from": prior})
        return True

    async def _apply_delivery_state(self, agent_name: str, row: dict, state: str,
                                    detail: Optional[str], now: str) -> bool:
        """Record whether the answer / terminal flip reached the agent's file."""
        if state not in DELIVERY_STATES:
            logger.error(f"Refusing unknown delivery_state {state!r} for {row.get('id')} (programming error)")
            return False
        detail = _detail(detail) if detail else None
        if row.get("delivery_state") == state and (row.get("delivery_detail") or "") == (detail or ""):
            return False
        try:
            changed = db.set_operator_queue_delivery_state(row["id"], state, detail, now)
        except Exception as e:
            logger.error(f"Failed to record delivery state for {row.get('id')}: {e}")
            return False
        if not changed:
            return False
        row["delivery_state"] = state
        row["delivery_detail"] = detail
        self._changed_this_cycle = True
        if state == DELIVERY_DELIVERED:
            await _audit_sync("written_back", agent_name, row["id"], {"status": row.get("status")})
        elif state == DELIVERY_UNDELIVERED:
            await _audit_sync("undeliverable", agent_name, row["id"],
                              {"status": row.get("status"), "detail": detail})
        return True

    async def _sync_agent(self, agent_name: str):
        """Sync a single agent's operator queue file."""
        client = AgentClient(agent_name)
        now = utc_now_iso()

        # 1. Read the queue file from the agent
        try:
            result = await client.read_file(QUEUE_FILE_PATH, timeout=5.0)
        except Exception as e:
            result = {"success": False, "error": type(e).__name__}

        if not result.get("success"):
            # #2915: "could not read" is a fact, not silence — but with
            # hysteresis. The 5 s read times out intermittently on a busy
            # container, so a row is called `unconfirmed` only after
            # READ_FAILURE_THRESHOLD consecutive failed cycles, and returns to
            # `confirmed` on the first good read. No ingest and no write-back
            # this cycle either way.
            failures = self._read_failures.get(agent_name, 0) + 1
            self._read_failures[agent_name] = failures
            if failures >= READ_FAILURE_THRESHOLD:
                try:
                    if db.mark_operator_queue_unconfirmed(
                        _read_failure_detail(result), now, agent_name=agent_name,
                        exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
            return
        self._read_failures.pop(agent_name, None)

        file_exists = result.get("success") and not result.get("not_found")
        content = result.get("content") if file_exists else None
        if file_exists and not content:
            file_exists = False

        # #1632: skip a pathologically large queue file wholesale (C1 per-cycle
        # DoS guard) — don't even parse it. One flood alert, then skip this agent
        # this cycle; response write-back resumes once the file is sane again.
        if file_exists and len(content) > OPERATOR_QUEUE_MAX_FILE_BYTES:
            logger.warning(
                f"operator-queue.json for {agent_name} is {len(content)} bytes "
                f"(> {OPERATOR_QUEUE_MAX_FILE_BYTES}); skipping ingestion this cycle"
            )
            await self._maybe_emit_flood_alert(agent_name, reason="oversize_file")
            try:
                if db.mark_operator_queue_unconfirmed(
                    "oversize_file", now, agent_name=agent_name,
                    exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
                ):
                    self._changed_this_cycle = True
            except Exception as e:
                logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
            return

        if file_exists:
            try:
                queue_data = json.loads(content)
            except json.JSONDecodeError:
                # #2915: an unparseable file is `unconfirmed`, never "empty", and
                # NOTHING is written back this cycle — the previous path treated
                # it as an empty request list and then overwrote the agent's
                # file with the reconstructed responses alone.
                logger.warning(f"Invalid JSON in operator-queue.json for {agent_name}")
                try:
                    if db.mark_operator_queue_unconfirmed(
                        "invalid_json", now, agent_name=agent_name,
                        exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
                return
            if not _well_formed_queue(queue_data):
                logger.warning(
                    f"operator-queue.json for {agent_name} is not the expected shape "
                    f"(an object with a `requests` list); not reconciling, not writing"
                )
                try:
                    if db.mark_operator_queue_unconfirmed(
                        "wrong_shape", now, agent_name=agent_name,
                        exclude_request_id_prefixes=_RESERVED_ID_PREFIXES,
                    ):
                        self._changed_this_cycle = True
                except Exception as e:
                    logger.error(f"Failed to mark {agent_name} rows unconfirmed: {e}")
                return
        else:
            queue_data = {"$schema": "operator-queue-v1", "requests": []}

        requests = queue_data.get("requests") or []
        content_sha = (
            hashlib.sha256(content.encode("utf-8")).hexdigest() if file_exists else None
        )

        # #2915: everything the platform holds for this agent, in two reads —
        # full rows for the open (pending/responded) items the file is expected
        # to carry, and id/status for every terminal row, so a pending entry that
        # re-uses a terminal row's id is recognised as `stale_id` instead of
        # being re-admitted (the on-conflict create returns the surviving uuid
        # silently: a phantom admit against the depth cap and a "new" broadcast
        # every cycle). Replaces the per-entry `exists()` probe.
        try:
            index = db.get_operator_queue_sync_index_for_agent(agent_name)
        except Exception as e:
            logger.error(f"Failed to read the sync index for {agent_name}: {e}")
            return
        open_rows = list(index["open"] or [])
        terminal_index = index["terminal"] or {}
        open_by_rid = {r["request_id"]: r for r in open_rows if r.get("request_id")}
        seen_rids: set = set()
        hours = aging_hours()
        receipt_rids: set = set()

        # 2. Process each request. Two independent bounds guard this agent-authored
        #    seam (#1632): a DB-measured pending-DEPTH cap (primary, Redis-independent)
        #    and a per-agent + fleet RATE cap (burst smoothing, fail-open), plus
        #    per-field hygiene inside the create try/except.
        new_items = []
        acknowledged_items = []

        # DEPTH cap baseline — one DB count per cycle; `admitted` tracks this
        # cycle's creates so the cap holds without re-counting per item.
        pending_count = db.count_operator_queue_pending_for_agent(agent_name)
        admitted = 0
        held = 0

        for req in requests[:OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE]:
            if not isinstance(req, dict):
                continue
            req_id = req.get("id")
            if not req_id:
                continue

            # #1631: reject an agent-authored id that impersonates a platform id
            # prefix (hijack/suppress guard). Log once per (agent, id) so it
            # can't hot-loop the ~5s sync at WARNING. Normalize before the check
            # (case/whitespace-fold) so a lookalike like ` Poison-x` can't slip a
            # platform-styled alert past the filter — the platform mints these
            # prefixes lowercase and unpadded, so a normalized id that matches can
            # only be an impersonation attempt.
            if isinstance(req_id, str) and req_id.strip().lower().startswith(
                _RESERVED_ID_PREFIXES
            ):
                key = (agent_name, req_id)
                if key not in self._rejected_reserved:
                    if len(self._rejected_reserved) >= _MAX_QUARANTINE_ENTRIES:
                        self._rejected_reserved.clear()  # safety valve
                    self._rejected_reserved.add(key)
                    logger.warning(
                        f"Rejecting operator-queue request '{req_id}' from "
                        f"'{agent_name}': ids with a reserved platform prefix are "
                        f"minted only by the platform (#1631)"
                    )
                continue

            fail_key = (agent_name, req_id)
            req_status = req.get("status", "pending")
            if isinstance(req_id, str):
                if req_id in seen_rids:
                    # #2989 review: a duplicated id is reconciled ONCE, on its first
                    # entry. Two copies with different content used to flip the row
                    # confirmed ↔ changed every cycle: two audit rows, two writes and
                    # a broadcast per 5 s.
                    continue
                seen_rids.add(req_id)

            if req_status == "acknowledged":
                # Agent acknowledged our response. #1631: broadcast the row's
                # platform uuid (returned here), not the agent's `req_id` — the
                # frontend store keys items by uuid `id`, so an ack keyed on
                # request_id would never match a live item.
                ack_uuid = db.mark_operator_queue_acknowledged(agent_name, req_id)
                if ack_uuid:
                    acknowledged_items.append(ack_uuid)
                    open_by_rid.pop(req_id, None)  # it just went terminal
                elif req_id in open_by_rid:
                    row = open_by_rid[req_id]
                    if row.get("status") == "responded" and _is_our_answer(req, row):
                        # Our answer landed and the agent acknowledged it, but the
                        # flip waits for the delivery record (`mark_acknowledged`
                        # requires `delivered`, #2989 review) — confirmed, not closed.
                        await self._apply_sync_state(agent_name, row, SYNC_CONFIRMED, None, now)
                    else:
                        # #2915: acknowledged on a row that was never responded — the
                        # agent closed its own ask. The platform used to drop this
                        # (the UPDATE matches only `responded` rows) and keep showing
                        # the human a pending card for an item the agent had closed.
                        await self._apply_sync_state(
                            agent_name, row, SYNC_CLOSED_BY_FILER, "acknowledged", now
                        )
                continue

            if req_id in open_by_rid:
                # #2915: the platform already holds this item — reconcile the
                # entry against the row instead of skipping it. The row is the
                # frozen ingest snapshot; nothing here rewrites it.
                row = open_by_rid[req_id]
                if req_status == "pending":
                    fields = changed_fields(row, req)
                    if fields:
                        await self._apply_sync_state(agent_name, row, SYNC_CHANGED, ",".join(fields), now)
                    else:
                        await self._apply_sync_state(agent_name, row, SYNC_CONFIRMED, None, now)
                        if (
                            row.get("status") == "pending"
                            and is_aged(row, hours)
                            and not (isinstance(req.get(_PLATFORM_BLOCK_KEY), dict)
                                     and req[_PLATFORM_BLOCK_KEY].get(_AGING_SINCE_KEY))
                        ):
                            receipt_rids.add(req_id)
                elif req_status == "responded":
                    # Our own write-back landed; the agent has not acknowledged yet.
                    await self._apply_sync_state(agent_name, row, SYNC_CONFIRMED, None, now)
                else:
                    await self._apply_sync_state(
                        agent_name, row, SYNC_CLOSED_BY_FILER, _fold_agent_status(req_status), now
                    )
                continue

            if isinstance(req_id, str) and req_id in terminal_index:
                if req_status == "pending":
                    term = terminal_index[req_id]
                    await self._apply_sync_state(
                        agent_name,
                        {"id": term["id"], "sync_state": term.get("sync_state"), "sync_detail": None},
                        SYNC_STALE_ID, _fold_agent_status(term.get("status")), now,
                    )
                continue

            if req_status != "pending":
                continue

            # #1525: quarantine a request whose create keeps failing so a single
            # malformed/unpersistable entry can't hot-loop the ~5s sync (the row
            # never persists → exists() stays False → retry forever). #1631: keyed
            # by the (agent, id) tuple so agent A's failing id can't quarantine
            # agent B's distinct, healthy request.
            if self._create_failures.get(fail_key, 0) >= MAX_CREATE_ATTEMPTS:
                continue

            # NB: an agent-authored id impersonating a platform-reserved prefix is
            # already rejected above (normalized, logged once per (agent, id)), so
            # no second reserved-prefix check is needed here.

            # #1632 C4: a create PK can't be safely rewritten, so a malformed /
            # oversize id is rejected (held → may trigger the summary alert).
            if (
                not isinstance(req_id, str)
                or len(req_id) > OPERATOR_QUEUE_ID_MAX
                or not _ID_RE.match(req_id)
            ):
                logger.warning(
                    f"Rejecting malformed operator-queue id from {agent_name}: {req_id!r}"
                )
                held += 1
                continue

            # #1632 C3: DEPTH cap (hard, primary). At the cap STOP ingesting —
            # `admitted` only grows, so every later item is over too → break.
            if pending_count + admitted >= OPERATOR_QUEUE_MAX_PENDING_PER_AGENT:
                held += 1
                break

            # #1632: RATE cap (per-agent + fleet, fail-open). Charged only at the
            # real create point. Denied → hold + stop scanning (the window is
            # monotonic within a cycle, so a re-check would also deny). NB: on a
            # per-agent-allow / fleet-deny, this cycle's ≤1 per-agent token is
            # "spent" without a create — a fail-safe (slightly stricter) drift
            # bounded to one item because we break.
            if not rate_limiter.check(
                f"operator_queue_create:{agent_name}",
                OPERATOR_QUEUE_CREATE_RATE_LIMIT,
                OPERATOR_QUEUE_CREATE_RATE_WINDOW,
            ).allowed or not rate_limiter.check(
                "operator_queue_create:_fleet",
                OPERATOR_QUEUE_FLEET_CREATE_RATE_LIMIT,
                OPERATOR_QUEUE_CREATE_RATE_WINDOW,
            ).allowed:
                held += 1
                break

            # New item — clamp then create. The clamp runs INSIDE the try so any
            # clamp/create failure is quarantined by #1525 rather than hot-looping.
            try:
                clamped = _clamp_ingested_item(req, agent_name)
                new_id = db.create_operator_queue_item(agent_name, clamped)
                admitted += 1
                new_items.append(clamped)
                self._create_failures.pop(fail_key, None)  # recovered — clear count
            except Exception as e:
                attempts = self._create_failures.get(fail_key, 0) + 1
                if len(self._create_failures) >= _MAX_QUARANTINE_ENTRIES:
                    self._create_failures.clear()  # safety valve — never unbounded
                self._create_failures[fail_key] = attempts
                if attempts >= MAX_CREATE_ATTEMPTS:
                    logger.error(
                        f"Quarantining operator-queue request {req_id} for "
                        f"'{agent_name}' after {attempts} failed create attempts "
                        f"(last error: {e}); it won't be retried until it changes "
                        f"or the service restarts"
                    )
                else:
                    logger.error(
                        f"Failed to create queue item {req_id} for '{agent_name}' "
                        f"(attempt {attempts}/{MAX_CREATE_ATTEMPTS}): {e}"
                    )
                continue

            # #2915: a freshly ingested row is confirmed by construction (the
            # entry was just read) — and `ingested` is the first accountability
            # row of its audit story.
            await self._apply_sync_state(
                agent_name, {"id": new_id, "sync_state": None, "sync_detail": None},
                SYNC_CONFIRMED, None, now,
            )
            # `type` is agent-authored free text the clamp does not bound; the
            # audit row is durable operator-visible state (G-04), so it carries
            # the folded token or `other` — never the string itself. `req_id`
            # passed the `_ID_RE` shape check above.
            await _audit_sync("ingested", agent_name, new_id,
                              {"request_id": req_id, "type": _fold_agent_status(clamped.get("type"))})

        # #2915: open rows the file no longer carries. The entry was pruned or the
        # file is gone — the platform used to keep showing a live card for it.
        for rid, row in open_by_rid.items():
            if rid in seen_rids:
                continue
            # A platform alarm (ent#499) was never in this file — there is no entry
            # to miss (#2989 review: every alarm went `missing` on the first cycle
            # after upgrade). A `responded` row the file lost is the write-back's
            # business: it re-appends the entry this same cycle, so marking it
            # `missing` first only minted a `diverged` + `reconciled` pair.
            if is_platform_minted(row) or row.get("status") == "responded":
                continue
            await self._apply_sync_state(
                agent_name, row, SYNC_MISSING,
                "entry_missing" if file_exists else "file_missing", now,
            )

        # #2915: "last confirmed at HH:MM" without a write per row per cycle —
        # one batched UPDATE per agent, for confirmed rows older than a minute.
        try:
            db.refresh_operator_queue_last_confirmed(
                agent_name, now, iso_cutoff(minutes=LAST_CONFIRMED_REFRESH_SECONDS // 60)
            )
        except Exception as e:
            logger.error(f"Failed to refresh last_confirmed_at for {agent_name}: {e}")

        # #1632: one aggregated summary alert per episode when items were held
        # (depth cap, rate cap, or malformed ids) — never one per skipped item.
        if held > 0:
            await self._maybe_emit_flood_alert(agent_name, held=held)

        # 3. Broadcast new items via WebSocket
        if new_items and _websocket_manager:
            for item in new_items:
                try:
                    await _websocket_manager.broadcast(json.dumps({
                        "type": "operator_queue_new",
                        "data": {
                            "id": item.get("id", ""),
                            "agent_name": agent_name,
                            "type": item.get("type", "question"),
                            "priority": item.get("priority", "medium"),
                            "title": item.get("title", ""),
                            "created_at": item.get("created_at", ""),
                        }
                    }))
                except Exception as e:
                    logger.error(f"Failed to broadcast queue event: {e}")

        if acknowledged_items and _websocket_manager:
            for ack_id in acknowledged_items:
                try:
                    await _websocket_manager.broadcast(json.dumps({
                        "type": "operator_queue_acknowledged",
                        "data": {
                            "id": ack_id,
                            "agent_name": agent_name,
                        }
                    }))
                except Exception:
                    pass

        # 4. Write responses back to the agent's file. Cancelled/expired
        # items are propagated too (#1017) so the agent stops waiting on
        # them — as in-place status flips on entries still in the file; an
        # entry that is gone is recorded `undelivered:entry_missing` (#2915),
        # never silently skipped. The aging receipt rides the same write.
        responded_items = db.get_operator_queue_responded_for_agent(agent_name)
        terminal_items = db.get_operator_queue_terminal_for_agent(agent_name)
        if responded_items or terminal_items or receipt_rids:
            await self._write_responses_to_agent(
                agent_name, client, queue_data, responded_items,
                terminal_items, file_exists,
                content_sha=content_sha, receipt_request_ids=receipt_rids,
            )

    async def _write_responses_to_agent(
        self,
        agent_name: str,
        client: AgentClient,
        queue_data: dict,
        responded_items: list,
        terminal_items: Optional[list] = None,
        file_exists: bool = True,
        *,
        content_sha: Optional[str] = None,
        receipt_request_ids=None,
    ):
        """Write operator responses back to the agent's queue file.

        #2915 — the write is honest and narrow. Before writing, the file is
        READ AGAIN and merged by id, and the write carries `if_match` (the sha of
        what was read) so the agent server refuses (412) rather than clobbers an
        entry the agent appended in between — the window is narrowed to one
        round trip, and a refused write is recorded and retried, not lost. A
        response is delivered ONLY into an entry that is still `pending` and
        whose content still matches the row (an answer to a rewritten question
        is `undelivered:entry_changed`). Every outcome lands on the row as a
        delivery state; nothing is dropped silently.
        """
        now = utc_now_iso()
        terminal_items = terminal_items or []
        receipt_request_ids = set(receipt_request_ids or ())
        if not responded_items and not terminal_items and not receipt_request_ids:
            return

        # Re-read immediately before writing (the cycle-start read may be a whole
        # cycle old). A transient failure here means "try next cycle" — the
        # agent WAS readable seconds ago, so nothing is recorded for it.
        if file_exists:
            try:
                fresh = await client.read_file(QUEUE_FILE_PATH, timeout=5.0)
            except Exception as e:
                logger.warning(f"Re-read before write-back failed for {agent_name}: {type(e).__name__}")
                return
            if not fresh.get("success"):
                return
            if fresh.get("not_found") or not fresh.get("content"):
                # The cycle-start read HAD a file and this re-read does not: an
                # agent mid-rewrite, not a lost file. Write nothing, record
                # nothing — the next cycle decides. (A file missing at BOTH reads
                # is the container-restart case and is reconstructed below.)
                logger.info(f"operator-queue.json for {agent_name} vanished between the two reads; not writing")
                return
            try:
                queue_data = json.loads(fresh["content"])
            except json.JSONDecodeError:
                logger.warning(f"Re-read of operator-queue.json for {agent_name} is not JSON; not writing")
                return
            if not _well_formed_queue(queue_data):
                logger.warning(f"Re-read of operator-queue.json for {agent_name} is not the expected shape; not writing")
                return
            content_sha = hashlib.sha256(fresh["content"].encode("utf-8")).hexdigest()

        requests = queue_data.get("requests") or []
        updated = False
        terminal_flips = 0

        # #1631: the DB `id` is now a platform uuid; the agent's file entries are
        # keyed by the string the agent authored, which is persisted as the row's
        # `request_id`. So every match against a file entry (`req.get("id")`)
        # MUST key on `request_id`, not the DB `id` — otherwise write-back
        # silently stops matching and the agent never sees its answer.
        # #2915: a platform-minted row (ent#499) has no loop to close — it is
        # `not_applicable` and takes NO part in delivery. Files written before
        # ent#499 still carry those alarms as `responded` entries; matching them
        # here flip-flopped the row between delivered and not_applicable every
        # cycle, minting an audit row each time (seen live on the first run).
        response_map = {
            item["request_id"]: item for item in responded_items
            if not is_platform_minted(item)
        }
        # Cancelled/expired items (#1017): flip still-'pending' file entries
        # to their terminal status so the agent stops waiting (and so a
        # stale 'pending' file entry can't resurrect a purged row). Never
        # appended if missing from the file.
        terminal_map = {
            item["request_id"]: item for item in (terminal_items or [])
            if not is_platform_minted(item)
        }
        delivered: dict = {}      # row id -> row, IN this write (or already there)
        undelivered: dict = {}    # row id -> (row, detail), decided before the write

        # Update items already in the agent's requests array
        seen_ids = set()
        for req in requests:
            if not isinstance(req, dict):
                continue
            req_id = req.get("id")
            if req_id in response_map:
                resp = response_map[req_id]
                # #2989 review: "send again to answer anyway" must reach the file.
                # The operator saw the divergence and acknowledged it at respond
                # time (`divergence_acknowledged_at`), so the answer is written
                # into the entry as it is now — rewritten or closed by the agent.
                forced = bool(resp.get("divergence_acknowledged_at"))
                if req.get("status") == "pending":
                    if changed_fields(resp, req) and not forced:
                        # The agent rewrote the question after the human answered
                        # it. Never hand an answer to a different question.
                        undelivered[resp["id"]] = (resp, "entry_changed")
                    else:
                        _deliver_into(req, resp)
                        updated = True
                        delivered[resp["id"]] = resp
                elif _is_our_answer(req, resp):
                    delivered[resp["id"]] = resp  # a previous write landed
                elif forced:
                    # The agent closed the entry on its side; the acknowledged
                    # answer is WRITTEN into it (status → responded) so the agent
                    # finds it. Recording `delivered` without a write let the next
                    # cycle read the agent's own `acknowledged` as an ack of an
                    # answer it never saw.
                    _deliver_into(req, resp)
                    updated = True
                    delivered[resp["id"]] = resp
                else:
                    undelivered[resp["id"]] = (resp, "closed_by_filer")
            elif req_id in terminal_map:
                term = terminal_map[req_id]
                if req.get("status") == "pending":
                    req["status"] = term["status"]
                    updated = True
                    terminal_flips += 1
                delivered[term["id"]] = term
            if req_id in receipt_request_ids and req.get("status") == "pending":
                block = req.get(_PLATFORM_BLOCK_KEY)
                if not isinstance(block, dict):
                    block = {}
                    req[_PLATFORM_BLOCK_KEY] = block
                if not block.get(_AGING_SINCE_KEY):
                    block[_AGING_SINCE_KEY] = now
                    updated = True
            if req_id:
                seen_ids.add(req_id)

        # Reconstruct items missing from the file (e.g. after container restart).
        # #1631: write the agent's own `request_id` back as the file entry's
        # `id` (never the DB uuid) so the agent recognises the item and the next
        # sync cycle's index — keyed on request_id — matches instead of
        # creating a duplicate.
        for resp in responded_items:
            # ent#499: a platform alarm was never in this agent's file and must
            # not be written into it — see `is_platform_minted`.
            if is_platform_minted(resp):
                continue
            if resp["request_id"] not in seen_ids:
                requests.append({
                    "id": resp["request_id"],
                    "type": resp.get("type", "question"),
                    "status": "responded",
                    "priority": resp.get("priority", "medium"),
                    "title": resp.get("title", ""),
                    "question": resp.get("question", ""),
                    "options": resp.get("options"),
                    "context": resp.get("context"),
                    "created_at": resp.get("created_at", ""),
                    "response": resp["response"],
                    "response_text": resp.get("response_text"),
                    "responded_by": resp.get("responded_by_email"),
                    "responded_at": resp.get("responded_at"),
                })
                updated = True
                delivered[resp["id"]] = resp

        # #2915: a platform alarm has no loop to close — `not_applicable`, once,
        # whether it is still `responded` or already terminal.
        for row in list(responded_items) + list(terminal_items or []):
            if is_platform_minted(row):
                await self._apply_delivery_state(
                    agent_name, row, DELIVERY_NOT_APPLICABLE, "platform_minted", now
                )
        # #2915: a terminal flip whose entry is gone is recorded, not dropped.
        for term in terminal_map.values():
            if term["request_id"] not in seen_ids:
                undelivered[term["id"]] = (term, "entry_missing" if file_exists else "file_missing")
        for row, detail in undelivered.values():
            await self._apply_delivery_state(agent_name, row, DELIVERY_UNDELIVERED, detail, now)

        if not updated:
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, DELIVERY_DELIVERED, None, now)
            return

        queue_data["requests"] = requests

        # Write the updated file back to the agent
        try:
            new_content = json.dumps(queue_data, indent=2)
            result = await client.write_file(
                QUEUE_FILE_PATH,
                new_content,
                timeout=10.0,
                platform=True,  # Allow writes to .trinity directory
                if_match=content_sha,
            )
        except Exception as e:
            result = {"success": False, "error": type(e).__name__}

        if result.get("success"):
            logger.info(
                f"Wrote {len(response_map)} responses and {terminal_flips} "
                f"terminal-status flips back to {agent_name}"
            )
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, DELIVERY_DELIVERED, None, now)
        else:
            code = result.get("status_code")
            if code == 412:
                detail = "conflict"      # the agent wrote in between — retried next cycle
            elif isinstance(code, int):
                detail = f"http_{code}"
            else:
                detail = _read_failure_detail(result)
            logger.warning(
                f"Failed to write responses to {agent_name}: {detail}"
            )
            for row in delivered.values():
                await self._apply_delivery_state(agent_name, row, DELIVERY_UNDELIVERED, detail, now)

    async def _maybe_emit_flood_alert(
        self, agent_name: str, held: int = 0, reason: str = "ingestion_cap"
    ):
        """#1632: emit ONE aggregated flood alert per episode (cooldown-gated).

        Fired when this agent's ingestion was depth-held / rate-skipped, or its
        queue file was oversized. A platform **direct-DB create** — exempt from
        the caps; an **un-guessable** `queue-flood-{agent}-{utc_now_iso()}` id so
        the agent can't pre-suppress it (C2); softened wording to avoid cry-wolf
        (C9). Wrapped so an emit failure never kills the sync
        (sync_health_service precedent). The single leader means no WS
        double-broadcast.

        The cooldown is stamped BEFORE the create so a persistently-failing alert
        create backs off for the cooldown window rather than retrying every 5s
        (its id changes each cycle, so it would otherwise dodge the #1525
        quarantine map). At most one duplicate alert can follow a leader failover
        (in-memory cooldown resets) — harmless.
        """
        now = time.monotonic()
        # `None` = never alerted for this agent. A `0.0` default would be wrong:
        # `time.monotonic()` is seconds from an arbitrary reference and can be
        # < COOLDOWN on a freshly-booted process, so `now - 0.0 < COOLDOWN` would
        # suppress the FIRST-ever alert for the first COOLDOWN seconds of uptime.
        last = self._flood_alert_cooldown.get(agent_name)
        if last is not None and now - last < OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS:
            return  # already alerted this episode
        self._flood_alert_cooldown[agent_name] = now

        if reason == "oversize_file":
            title = f"Agent '{agent_name}' produced an oversized operator-queue file"
            question = (
                f"Agent '{agent_name}' wrote an operator-queue.json larger than the "
                f"allowed size; its requests were not ingested this cycle. This can "
                f"indicate a runaway or compromised agent — review its recent "
                f"activity before acting on its requests."
            )
        else:
            title = f"Agent '{agent_name}' exceeded its operator-queue ingestion limit"
            question = (
                f"Agent '{agent_name}' produced more operator-queue requests than the "
                f"per-agent ingestion limit allows; {held} request(s) were held this "
                f"cycle and are not shown. This can indicate a runaway or compromised "
                f"agent — review its recent activity before acting on its requests."
            )

        alert = {
            "id": f"queue-flood-{agent_name}-{utc_now_iso()}",
            "type": "alert",
            "status": "pending",
            "priority": "high",
            "title": title,
            "question": question,
            "context": {"held": held, "reason": reason},
            "created_at": utc_now_iso(),
        }

        try:
            db.create_operator_queue_item(agent_name, alert)
        except Exception as e:
            logger.error(f"Failed to emit operator-queue flood alert for {agent_name}: {e}")
            return

        logger.warning(
            f"Operator-queue flood alert emitted for {agent_name} "
            f"(reason={reason}, held={held})"
        )

        if _websocket_manager:
            try:
                await _websocket_manager.broadcast(json.dumps({
                    "type": "operator_queue_new",
                    "data": {
                        "id": alert["id"],
                        "agent_name": agent_name,
                        "type": "alert",
                        "priority": "high",
                        "title": alert["title"],
                        "created_at": alert["created_at"],
                    }
                }))
            except Exception as e:
                logger.error(f"Failed to broadcast flood alert for {agent_name}: {e}")


# Global service instance
operator_queue_service = OperatorQueueSyncService()
