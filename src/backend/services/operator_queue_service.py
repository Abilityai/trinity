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
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

from database import db
from redis_breaker_util import get_breaker_redis
from services import rate_limiter
from services.agent_client import AgentClient
from utils.helpers import utc_now_iso

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
_BUDGETED_ALERT_TYPES = frozenset({"skill_not_found"})

# Shape guard for the episode alert's `last_triggered_by` triage field: a
# platform trigger enum only — NEVER agent-controlled free text (G-04: the
# episode alert is durable operator-visible state). Digits included because
# the real enum carries them (`a2a`).
_TRIGGERED_BY_RE = re.compile(r"[a-z0-9_]{1,32}")

# Valid priority values — an agent-supplied unknown collapses to "medium".
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

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
)

# Agent ids must be id-shaped: a create PK can't be safely rewritten, so a
# malformed / oversize id is rejected rather than clamped (C4).
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

# Inline truncation marker (kept short so the clamped field length stays ≤ cap).
_TRUNC_MARKER = "…[truncated]"
_OPTIONS_DROPPED_MARKER = "(options omitted: exceeded size cap)"

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


def _clamp_ingested_item(req: dict, agent_name: str = "") -> dict:
    """#1632: total field-hygiene clamp for an agent-authored queue item.

    Called INSIDE the create try/except so any failure is quarantined by #1525
    rather than hot-looping — but it is written to NEVER raise (every branch is
    isinstance-guarded, json.dumps is wrapped). Returns a NEW dict; never mutates
    the caller's request.

    - title / question: truncate-with-marker.
    - context: non-dict → {} (fixes the create_item .get crash class); serialized
      > cap OR non-serializable → a marker object that preserves only a *valid*
      (≤EXECUTION_ID_MAX, id-shaped) execution_id.
    - options: serialized > cap OR non-serializable → a small marker list.
    - created_at: normalized to ingest time (defeats future-date sort-pinning);
      expires_at is left untouched (honored).
    - priority: validate-only (unknown → medium; a legit `critical` is untouched —
      the depth cap already bounds critical *volume*).
    """
    out = dict(req)

    title = out.get("title")
    if isinstance(title, str):
        out["title"] = _truncate_with_marker(title, OPERATOR_QUEUE_TITLE_MAX)

    question = out.get("question")
    if isinstance(question, str):
        out["question"] = _truncate_with_marker(question, OPERATOR_QUEUE_QUESTION_MAX)

    context = out.get("context")
    if not isinstance(context, dict):
        # Non-dict context (str/list/None) → {} — also fixes the pre-existing
        # create_item execution_id `.get` crash.
        out["context"] = {}
    else:
        try:
            ctx_bytes = len(json.dumps(context).encode("utf-8"))
        except (TypeError, ValueError):
            ctx_bytes = None  # non-serializable
        if ctx_bytes is None or ctx_bytes > OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
            out["context"] = {
                "_truncated": True,
                "_original_bytes": ctx_bytes,
                "execution_id": _valid_execution_id(context.get("execution_id")),
            }

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

    # ent#364: the addressee is an authorization decision, so it is resolved here
    # rather than trusted. Absent/invalid/off-roster → None, i.e. an operator ask.
    out["addressed_to_email"] = _validated_addressee(agent_name, out.get("addressed_to_email"))

    # Ignore the agent-supplied created_at (a future date pins the item atop the
    # `created_at DESC` sort, C4). expires_at is honored as authored.
    out["created_at"] = utc_now_iso()

    return out


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

        from services.docker_service import list_all_agents_fast

        try:
            agents = list_all_agents_fast()
        except Exception as e:
            logger.debug(f"Could not list agents: {e}")
            return

        running_agents = [a.name for a in agents if a.status == "running"]
        if not running_agents:
            return

        # Expire items past their deadline
        expired_count = db.mark_operator_queue_expired()
        if expired_count > 0:
            logger.info(f"Expired {expired_count} operator queue items")

        # Sync each agent concurrently (with a reasonable limit)
        tasks = [self._sync_agent(name) for name in running_agents]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_agent(self, agent_name: str):
        """Sync a single agent's operator queue file."""
        client = AgentClient(agent_name)

        # 1. Read the queue file from the agent
        try:
            result = await client.read_file(QUEUE_FILE_PATH, timeout=5.0)
        except Exception:
            # Agent not reachable or file API not ready — skip silently
            return

        file_exists = result.get("success") and not result.get("not_found")

        if file_exists:
            content = result.get("content")
            if not content:
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
            return

        if file_exists:
            try:
                queue_data = json.loads(content)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in operator-queue.json for {agent_name}")
                queue_data = {"$schema": "operator-queue-v1", "requests": []}
        else:
            queue_data = {"$schema": "operator-queue-v1", "requests": []}

        requests = queue_data.get("requests", [])
        if not isinstance(requests, list):
            requests = []

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

            if req_status == "acknowledged":
                # Agent acknowledged our response. #1631: broadcast the row's
                # platform uuid (returned here), not the agent's `req_id` — the
                # frontend store keys items by uuid `id`, so an ack keyed on
                # request_id would never match a live item.
                ack_uuid = db.mark_operator_queue_acknowledged(agent_name, req_id)
                if ack_uuid:
                    acknowledged_items.append(ack_uuid)
                continue

            # #1631: exists() is scoped to (agent, req_id) — two agents may share a
            # req_id, so an id-only check would treat B's distinct request as
            # already-created.
            if req_status != "pending" or db.operator_queue_item_exists(agent_name, req_id):
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
                db.create_operator_queue_item(agent_name, clamped)
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
        # them — but only as in-place status flips on entries still in the
        # file.
        responded_items = db.get_operator_queue_responded_for_agent(agent_name)
        terminal_items = (
            db.get_operator_queue_terminal_for_agent(agent_name)
            if file_exists else []
        )
        if responded_items or terminal_items:
            await self._write_responses_to_agent(
                agent_name, client, queue_data, responded_items,
                terminal_items, file_exists
            )

    async def _write_responses_to_agent(
        self,
        agent_name: str,
        client: AgentClient,
        queue_data: dict,
        responded_items: list,
        terminal_items: Optional[list] = None,
        file_exists: bool = True,
    ):
        """Write operator responses back to the agent's queue file."""
        requests = queue_data.get("requests", [])
        updated = False
        terminal_flips = 0

        # #1631: the DB `id` is now a platform uuid; the agent's file entries are
        # keyed by the string the agent authored, which is persisted as the row's
        # `request_id`. So every match against a file entry (`req.get("id")`)
        # MUST key on `request_id`, not the DB `id` — otherwise write-back
        # silently stops matching and the agent never sees its answer.
        response_map = {item["request_id"]: item for item in responded_items}
        # Cancelled/expired items (#1017): flip still-'pending' file entries
        # to their terminal status so the agent stops waiting (and so a
        # stale 'pending' file entry can't resurrect a purged row). Never
        # appended if missing from the file.
        terminal_map = {item["request_id"]: item for item in (terminal_items or [])}

        # Update items already in the agent's requests array
        seen_ids = set()
        for req in requests:
            req_id = req.get("id")
            if req_id in response_map and req.get("status") == "pending":
                resp = response_map[req_id]
                req["status"] = "responded"
                req["response"] = resp["response"]
                req["response_text"] = resp.get("response_text")
                req["responded_by"] = resp.get("responded_by_email")
                req["responded_at"] = resp.get("responded_at")
                updated = True
            elif req_id in terminal_map and req.get("status") == "pending":
                req["status"] = terminal_map[req_id]["status"]
                updated = True
                terminal_flips += 1
            if req_id:
                seen_ids.add(req_id)

        # Reconstruct items missing from the file (e.g. after container restart).
        # #1631: write the agent's own `request_id` back as the file entry's
        # `id` (never the DB uuid) so the agent recognises the item and the next
        # sync cycle's exists() check — keyed on request_id — matches instead of
        # creating a duplicate.
        for resp in responded_items:
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

        if not updated:
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
            )
            if result.get("success"):
                logger.info(
                    f"Wrote {len(response_map)} responses and {terminal_flips} "
                    f"terminal-status flips back to {agent_name}"
                )
            else:
                logger.warning(
                    f"Failed to write responses to {agent_name}: {result.get('error')}"
                )
        except Exception as e:
            logger.error(f"Error writing responses to {agent_name}: {e}")

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
