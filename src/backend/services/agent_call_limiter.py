"""
Backend agent-call budget limiter (#904 RC-1).

Bounds the fan-out of outbound agent HTTP calls from
`task_execution_service.agent_post_with_retry`. Two layered caps:

  * per-agent: how many concurrent backend coroutines may be mid-call
    to a single agent. Default = the agent's `max_parallel_tasks`,
    fallback 3.
  * global: how many concurrent agent calls the backend will hold
    across all agents. Default = `BACKEND_AGENT_CALL_LIMIT` env (8).

Why this exists: every `await` on `httpx.post(agent_url, ...)` is
intermixed with **synchronous** `sqlite3` calls in the surrounding
`task_execution_service.execute_task` (see `db/connection.py:18`).
Sync DB inside an async coroutine stalls the event loop for the
duration of the call — fine for a single short write, but with N
parallel long-running agent calls each emitting periodic
`mark_execution_dispatched` / `log_activity` / `update_execution_status`
writes, the SQLite writer lock + GIL serialise the writes and
starve unrelated handlers (dashboard, healthcheck). The end state
seen in #904: a single misbehaving agent's 11.5-min HTTP call
drove all backend coroutines into sync-DB contention long enough
that the Docker healthcheck (10s) flipped the container to
`unhealthy` and the dashboard's parallel API fan-out timed out.

This module does NOT fix the underlying sync-DB problem — it bounds
how much concurrent agent work the backend will accept so the
event-loop stalls stay short enough that healthcheck + dashboard
keep responding. The proper sync→async-DB migration is a separate
follow-up.

In-flight dispatch registry (#2433)
-----------------------------------
The semaphore wait above is a *queue* the rest of the platform could not
see: the execution row was already ``running`` and its capacity slot held,
but the agent had never heard of it, so the cleanup watchdog's proof-of-life
(``GET agent/api/executions/running``) classified it as an orphan after 60s —
a false ``failed``, a released slot, and a turn that later ran anyway (billed,
overbooked, its result either silently overwritten in or discarded). Every
outbound call is therefore registered here for its whole lifetime
(``track_inflight_dispatch``), in two forms:

* **in-process** ``_INFLIGHT`` — exact and free for the worker that owns the
  coroutine;
* **cross-worker** Redis marker ``execution:inflight:{execution_id}`` — the
  cleanup watchdog runs in EVERY uvicorn worker with no leader lease, so the
  other worker's sweep needs a signal too. ONE refresher task per process
  pipelines every marker every ``INFLIGHT_TICK_SECONDS`` with a
  ``INFLIGHT_MARKER_TTL_SECONDS`` expiry: the marker is **liveness, not
  state** — a dead worker stops refreshing and the marker lapses, so the next
  sweep orphans the row exactly as before (the #408 dead-coroutine class is
  deliberately unchanged). A static marker with a bounded skip window would
  instead have left a dead dispatcher's rows (and their slots) for up to the
  queue timeout.

Proof-of-life for the watchdog is now *the agent knows it* **or** *a live
dispatcher owns it* (``inflight_verdicts``). Two more consequences ride the
same registry: a park is re-anchored at grant (``on_granted`` → the caller
re-stamps ``started_at`` and renews the slot lease, because every age check
and the slot TTL were measured from admission and a long park would spend the
run's own budget), and a parked row is cancellable (``cancel_inflight`` /
``request_cross_worker_cancel`` — the grant checks the flag and refuses to
POST).

Every Redis touch here is fail-open: no client, a raise, or a slow server
degrades to "no marker" (plus a 30s negative cache so a flapping Redis is
never re-pinged on every tick). The watchdog reads the split state as
*unknown* rather than *absent* — see ``inflight_verdicts``.

Public API
----------
* ``acquire_agent_call_slot(agent_name, *, execution_id=None, on_granted=None)``
  — async context manager
* ``track_inflight_dispatch(execution_id, agent_name, http_timeout)`` —
  async context manager wrapping a whole outbound call
* ``inflight_verdicts(ids)`` — ``{id: "alive" | "absent" | "unknown"}``
* ``cancel_inflight(id)`` / ``request_cross_worker_cancel(id)``
* ``BackendAgentCallBudgetExhausted`` — raised when acquire times out
* ``BackendAgentCallCancelled`` — its subclass, raised at grant for a
  cancelled park (so the caller's existing except branch handles it)

Tunables (env)
--------------
* ``BACKEND_AGENT_CALL_LIMIT`` — int, default 8
* ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`` — float, default 3600 (0 = wait forever)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# Read once at import. Production never touches them after boot;
# tests use `_reset_for_testing()` to re-create the primitives with
# different bounds.
BACKEND_AGENT_CALL_LIMIT: int = int(os.getenv("BACKEND_AGENT_CALL_LIMIT", "8"))

# Queue-acquire timeout. Default 3600s (1 hour) — matches the
# platform-wide max `execution_timeout_seconds` (TIMEOUT-001 ceiling
# is 7200s, default 3600s, #665) so any task that would have
# eventually succeeded pre-#904 still succeeds: pre-fix the worst
# wall-clock was the agent timeout (max ~610s by default), and the
# queue wait is on top of that, so 3600s leaves a generous margin
# even under sustained backlog.
#
# Why we keep a finite cap instead of "wait forever":
# agent-to-agent chat chains (chat_with_agent MCP tool, X→Y→Z
# collaborations) can deadlock when concurrent chains exceed the
# global semaphore: each chain holds slots for its outer caller
# while waiting on the next-hop call which itself wants a slot.
# A finite timeout surfaces such a deadlock as a 503 within an
# hour, lets the queue drain, and keeps the system unstuck.
# Setting this to 0 disables the cap — explicitly opt-in only.
BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S: float = float(
    os.getenv("BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S", "3600")
)


class BackendAgentCallBudgetExhausted(Exception):
    """Raised when an outbound agent HTTP call can't be admitted within
    ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S``. The caller should translate
    to HTTP 503 — the work was rejected at the backend before any
    Claude subprocess started, so retrying after backoff is safe."""

    def __init__(
        self, agent_name: str, agent_cap: int, global_cap: int, wait_ms: int,
    ):
        self.agent_name = agent_name
        self.agent_cap = agent_cap
        self.global_cap = global_cap
        self.wait_ms = wait_ms
        super().__init__(
            f"Backend call budget exhausted for {agent_name} after {wait_ms}ms "
            f"(agent_cap={agent_cap}, global_cap={global_cap})"
        )


class BackendAgentCallCancelled(BackendAgentCallBudgetExhausted):
    """#2433: raised at semaphore grant when the execution was cancelled while
    it was parked in the queue. A subclass so every caller's existing
    ``except BackendAgentCallBudgetExhausted`` branch handles it — its FAILED
    write then loses the CAS to the CANCELLED row the cancel path already wrote,
    the slot release is idempotent, and no Claude work ever started."""

    def __init__(self, agent_name: str, agent_cap: int, global_cap: int, wait_ms: int):
        super().__init__(agent_name, agent_cap, global_cap, wait_ms)
        self.args = (
            f"Backend call cancelled while queued for {agent_name} after {wait_ms}ms "
            f"(agent_cap={agent_cap}, global_cap={global_cap})",
        )


# ---------------------------------------------------------------------------
# #2433: in-flight dispatch registry
# ---------------------------------------------------------------------------

INFLIGHT_MARKER_PREFIX = "execution:inflight:"
INFLIGHT_CANCEL_PREFIX = "execution:cancel:"
# TTL ≥ 4× the tick so one BGSAVE-class stall (the breaker client has 1s
# socket timeouts) cannot expire a LIVE worker's marker.
INFLIGHT_MARKER_TTL_SECONDS = 60
INFLIGHT_TICK_SECONDS = 15.0
# Entries younger than this never touch Redis: a fast acquire is the hot path
# and must stay free (the `_log_long_queue_wait` shape).
INFLIGHT_MARKER_GRACE_SECONDS = 5.0
# A park at or past this re-anchors the execution's clock at grant (the
# caller's `on_granted`): `started_at` + the slot lease are measured from
# admission, so without it a long park spends the run's own budget and the
# registry-blind Phase-1 stale sweep fails the row mid-run.
DISPATCH_RESTAMP_THRESHOLD_SECONDS = 5.0
# Negative cache after a Redis failure — `get_breaker_redis` re-runs
# from_url + ping with 1s timeouts on every call while Redis is down.
INFLIGHT_REDIS_RETRY_SECONDS = 30.0
# Slack on an entry's hard deadline (registered_at + queue wait bound + the
# call's own HTTP timeout + this). Past it the refresher stops refreshing and
# the watchdog ignores the in-process entry, so a leaked entry can never keep
# a `running` row alive forever.
INFLIGHT_DEADLINE_SLACK_SECONDS = 60.0
# The widest HTTP timeout a caller can carry (TIMEOUT-001 ceiling + margin);
# used only when a caller does not supply its own.
_MAX_HTTP_TIMEOUT_SECONDS = 7200.0 + 60.0


@dataclass
class InflightEntry:
    execution_id: str
    agent_name: str
    registered_at: float        # time.monotonic()
    registered_wall: float      # time.time(), for the marker payload
    deadline: float             # time.monotonic()
    http_timeout: float
    phase: str = "parked"       # "parked" (waiting on a semaphore) | "calling"
    parked_since: Optional[float] = None  # time.monotonic()
    cancel_requested: bool = False


_INFLIGHT: Dict[str, InflightEntry] = {}
# Markers whose entry is gone but whose key may still be in Redis. The
# refresher is the SOLE writer/deleter of markers (no SET/DEL race between
# two writers); an unregister only queues the delete.
_PENDING_DELETES: set = set()
_REFRESHER_TASK: Optional[asyncio.Task] = None
_REDIS_UNAVAILABLE_UNTIL: float = 0.0
_REDIS_EPISODE_LOGGED: bool = False
# Test seams. None → the production resolvers (lazy imports, so this module
# stays importable with the stdlib alone).
_client_factory: Optional[Callable[[], Any]] = None
_slot_renewer: Optional[Callable[[str, str], bool]] = None


def _queue_wait_bound_seconds() -> float:
    """How long a call may sit in the semaphore queue at most."""
    timeout = BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S
    return float(timeout) if timeout > 0 else _MAX_HTTP_TIMEOUT_SECONDS


def inflight_max_age_seconds() -> float:
    """Upper bound on how long a live dispatcher can own an execution:
    the queue wait bound + the widest HTTP timeout + slack. The watchdog
    uses it to scope an ``unknown`` verdict (Redis unreadable) — rows older
    than this cannot be in flight, so they are orphaned regardless."""
    return _queue_wait_bound_seconds() + _MAX_HTTP_TIMEOUT_SECONDS + INFLIGHT_DEADLINE_SLACK_SECONDS


def _marker_key(execution_id: str) -> str:
    return f"{INFLIGHT_MARKER_PREFIX}{execution_id}"


def _cancel_key(execution_id: str) -> str:
    return f"{INFLIGHT_CANCEL_PREFIX}{execution_id}"


def _get_client(*, use_negative_cache: bool = True):
    """The fail-open breaker Redis client, negative-cached for
    ``INFLIGHT_REDIS_RETRY_SECONDS`` after a failure. None ⇒ no Redis.

    ``use_negative_cache=False`` is for the WATCHDOG read: the cache exists
    so the 15s refresher stops re-pinging a dead Redis, but a 5-minute sweep
    landing inside that 30s window must not read "no client" as "no marker"
    — it needs the real answer (a raise ⇒ ``unknown``), or the flapping case
    the tri-state exists for would fail open for exactly one sweep."""
    global _REDIS_UNAVAILABLE_UNTIL, _REDIS_EPISODE_LOGGED
    now = time.monotonic()
    if use_negative_cache and now < _REDIS_UNAVAILABLE_UNTIL:
        return None
    try:
        if _client_factory is not None:
            client = _client_factory()
        else:
            from redis_breaker_util import get_breaker_redis  # noqa: WPS433 — lazy on purpose

            client = get_breaker_redis()
    except Exception as e:  # noqa: BLE001
        client = None
        if not _REDIS_EPISODE_LOGGED:
            logger.warning(f"[InflightDispatch] Redis client unavailable ({e}) — markers lapse until it returns")
    if client is None:
        _REDIS_UNAVAILABLE_UNTIL = now + INFLIGHT_REDIS_RETRY_SECONDS
        if not _REDIS_EPISODE_LOGGED:
            logger.warning(
                "[InflightDispatch] No Redis — cross-worker in-flight markers are off; "
                "same-worker proof-of-life is unaffected"
            )
            _REDIS_EPISODE_LOGGED = True
        return None
    if _REDIS_EPISODE_LOGGED:
        logger.info("[InflightDispatch] Redis back — in-flight markers resume")
        _REDIS_EPISODE_LOGGED = False
    return client


def _note_redis_failure(exc: BaseException) -> None:
    global _REDIS_UNAVAILABLE_UNTIL, _REDIS_EPISODE_LOGGED
    _REDIS_UNAVAILABLE_UNTIL = time.monotonic() + INFLIGHT_REDIS_RETRY_SECONDS
    if not _REDIS_EPISODE_LOGGED:
        logger.warning(f"[InflightDispatch] Redis write failed ({exc}) — markers lapse until it returns")
        _REDIS_EPISODE_LOGGED = True


def _renew_slot(agent_name: str, execution_id: str) -> bool:
    """Re-anchor the capacity slot's lease at 'now' (sync — called from the
    refresher's worker thread and via ``to_thread`` at grant)."""
    try:
        if _slot_renewer is not None:
            return bool(_slot_renewer(agent_name, execution_id))
        from services.slot_service import get_slot_service  # noqa: WPS433 — lazy on purpose

        return bool(get_slot_service().renew_slot(agent_name, execution_id))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[InflightDispatch] slot renew failed for {execution_id}: {e}")
        return False


def _marker_payload(entry: InflightEntry) -> str:
    return json.dumps(
        {
            "agent": entry.agent_name,
            "phase": entry.phase,
            "since": entry.registered_wall,
            "pid": os.getpid(),
        }
    )


def _tick_sync(snapshot: List[InflightEntry], deletes: List[str]) -> None:
    """One refresher tick, off the event loop: flush queued deletes, refresh
    every live marker in ONE pipeline, renew the slot lease of every PARKED
    entry (a park must not outlive the slot TTL that was set at admission)."""
    client = _get_client()
    if client is None:
        return
    try:
        pipe = client.pipeline(transaction=False)
        for eid in deletes:
            pipe.delete(_marker_key(eid))
        for entry in snapshot:
            pipe.set(_marker_key(entry.execution_id), _marker_payload(entry), ex=INFLIGHT_MARKER_TTL_SECONDS)
        pipe.execute()
    except Exception as e:  # noqa: BLE001
        _note_redis_failure(e)
        return
    for entry in snapshot:
        if entry.phase == "parked":
            _renew_slot(entry.agent_name, entry.execution_id)


async def _tick() -> None:
    now = time.monotonic()
    snapshot = [
        e for e in list(_INFLIGHT.values())
        if (now - e.registered_at) >= INFLIGHT_MARKER_GRACE_SECONDS and now < e.deadline
    ]
    deletes = list(_PENDING_DELETES)
    _PENDING_DELETES.clear()
    if not snapshot and not deletes:
        return
    try:
        await asyncio.to_thread(_tick_sync, snapshot, deletes)
    except Exception as e:  # noqa: BLE001 — never let the refresher die
        logger.debug(f"[InflightDispatch] tick failed: {e}")


async def _refresher_loop() -> None:
    try:
        while _INFLIGHT or _PENDING_DELETES:
            await asyncio.sleep(INFLIGHT_TICK_SECONDS)
            await _tick()
    except asyncio.CancelledError:
        pass


def _ensure_refresher() -> None:
    global _REFRESHER_TASK
    if _REFRESHER_TASK is not None and not _REFRESHER_TASK.done():
        return
    try:
        _REFRESHER_TASK = asyncio.get_running_loop().create_task(_refresher_loop())
    except RuntimeError:
        # No running loop (sync caller) — the next async register starts it.
        _REFRESHER_TASK = None


def register_inflight(execution_id: str, agent_name: str, http_timeout: Optional[float]) -> InflightEntry:
    now = time.monotonic()
    bound = float(http_timeout) if isinstance(http_timeout, (int, float)) and http_timeout > 0 else _MAX_HTTP_TIMEOUT_SECONDS
    entry = InflightEntry(
        execution_id=execution_id,
        agent_name=agent_name,
        registered_at=now,
        registered_wall=time.time(),
        deadline=now + _queue_wait_bound_seconds() + bound + INFLIGHT_DEADLINE_SLACK_SECONDS,
        http_timeout=bound,
    )
    _INFLIGHT[execution_id] = entry
    _PENDING_DELETES.discard(execution_id)
    _ensure_refresher()
    return entry


def unregister_inflight(execution_id: str) -> None:
    if _INFLIGHT.pop(execution_id, None) is not None:
        _PENDING_DELETES.add(execution_id)
        _ensure_refresher()  # flushes the delete even if the registry is now empty


@contextlib.asynccontextmanager
async def track_inflight_dispatch(execution_id: Optional[str], agent_name: str, http_timeout: Optional[float] = None):
    """Register an outbound agent call for its WHOLE lifetime (queue wait,
    connect retries, the HTTP call itself). No-op when there is no execution
    row to protect (``execution_id`` None)."""
    if not execution_id:
        yield None
        return
    entry = register_inflight(execution_id, agent_name, http_timeout)
    try:
        yield entry
    finally:
        unregister_inflight(execution_id)


def inflight_entry(execution_id: str) -> Optional[InflightEntry]:
    """The live in-process entry, or None (also None past its deadline)."""
    entry = _INFLIGHT.get(execution_id)
    if entry is None or time.monotonic() >= entry.deadline:
        return None
    return entry


def cancel_inflight(execution_id: str, *, agent_name: Optional[str] = None) -> Optional[str]:
    """Flag an in-process entry as cancelled. Returns its phase (``parked`` —
    the grant will refuse to POST — or ``calling``, in which case the caller
    must go through the agent as before), or None when this worker does not
    own it.

    ``agent_name`` scopes the cancel: an entry registered for a different
    agent is left untouched and reads as None. The caller is authorised on an
    AGENT, never on a bare execution id, so a cancel must never reach across
    agents (the agent-proxy path is scoped the same way — a foreign id 404s)."""
    entry = inflight_entry(execution_id)
    if entry is None:
        return None
    if agent_name is not None and entry.agent_name != agent_name:
        return None
    entry.cancel_requested = True
    return entry.phase


def _read_marker_sync(execution_id: str) -> Optional[dict]:
    client = _get_client()
    if client is None:
        return None
    raw = client.get(_marker_key(execution_id))
    if not isinstance(raw, (str, bytes)):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"phase": "unknown"}
    return parsed if isinstance(parsed, dict) else {"phase": "unknown"}


def _set_cancel_sync(execution_id: str) -> None:
    client = _get_client()
    if client is None:
        return
    client.set(_cancel_key(execution_id), "1", ex=int(_queue_wait_bound_seconds() + INFLIGHT_DEADLINE_SLACK_SECONDS))


def _set_cancel_then_reread_phase_sync(execution_id: str) -> str:
    """Set the cancel key and re-read the marker, IN THAT ORDER, in one
    pipeline (single connection ⇒ the server applies them in order).

    The order is the whole point. A remote terminate must never finalize
    CANCELLED for an execution the owning worker is already POSTing — the
    agent would run the turn, billed, against a released slot, and its late
    SUCCESS would lose the CAS to the standing CANCELLED (the #378 symptom
    this issue exists to remove). Pair this with the owner's
    ``_publish_calling_and_check_cancel_sync`` (marker write BEFORE the cancel
    read) and the interleaving is closed, not merely narrowed:

        owner : W(marker=calling) -> R(cancel)
        remote: W(cancel)         -> R(marker)

    If the remote's read observes ``parked`` then R_remote(marker) precedes
    W_owner(marker), so the full chain is
    ``W_remote(cancel) < R_remote(marker) < W_owner(marker) < R_owner(cancel)``
    — the owner's cancel read is guaranteed to see the key and raise before
    any POST. The pre-#2433-review order (read-then-set) admitted the reverse
    interleaving, and the marker's phase could additionally be up to
    ``INFLIGHT_TICK_SECONDS`` stale because only the refresher published it.

    Returns the phase the SECOND read observed. A marker that vanished
    between the two reads answers ``"unknown"``, which the caller treats as
    not-parked and routes through the agent — the safe direction.
    """
    client = _get_client()
    if client is None:
        return "unknown"
    pipe = client.pipeline(transaction=False)
    pipe.set(_cancel_key(execution_id), "1", ex=int(_queue_wait_bound_seconds() + INFLIGHT_DEADLINE_SLACK_SECONDS))
    pipe.get(_marker_key(execution_id))
    _, raw = pipe.execute()
    if not isinstance(raw, (str, bytes)):
        return "unknown"
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    return str(parsed.get("phase") or "unknown")


async def request_cross_worker_cancel(
    execution_id: str, *, agent_name: Optional[str] = None
) -> Optional[str]:
    """For an execution some OTHER worker owns: read its marker to scope the
    request, then set the cancel key its grant will check and RE-READ the
    phase from after that write (``_set_cancel_then_reread_phase_sync``).
    None when no marker (or no Redis). ``agent_name`` scopes it exactly like
    ``cancel_inflight`` — a marker written for a different agent (or one whose
    payload cannot name its agent) is never cancelled through this path, and
    the scope check runs on the FIRST read so no key is ever written for a
    foreign agent.

    The returned phase is only safe to act on because it post-dates the cancel
    key: ``parked`` here means the owner's grant is guaranteed to observe the
    key and refuse to POST, so the caller may finalize CANCELLED itself.
    Anything else routes through the agent as before."""
    try:
        marker = await asyncio.to_thread(_read_marker_sync, execution_id)
        if marker is None:
            return None
        if agent_name is not None and marker.get("agent") != agent_name:
            return None
        # Scope decided above on the first read; the PHASE must come from a
        # read taken AFTER the cancel key is set, or a marker that is stale in
        # the unsafe direction lets this finalize CANCELLED under a live POST.
        return await asyncio.to_thread(_set_cancel_then_reread_phase_sync, execution_id)
    except Exception as e:  # noqa: BLE001
        _note_redis_failure(e)
        return None


def _cancel_requested_cross_worker_sync(execution_id: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.get(_cancel_key(execution_id)))
    except Exception as e:  # noqa: BLE001
        _note_redis_failure(e)
        return False


def _publish_calling_and_check_cancel_sync(entry: InflightEntry) -> bool:
    """Publish this entry's parked->calling transition and read the cancel key,
    IN THAT ORDER, in one pipeline — replacing the bare cancel read, so the
    fix costs no extra round-trip.

    Before this, ``entry.phase`` flipped to ``calling`` in memory only and the
    marker was rewritten by the 15s refresher, so ``execution:inflight:{id}``
    could advertise ``parked`` for up to ``INFLIGHT_TICK_SECONDS`` after the
    POST had begun. With ``--workers 2`` roughly half of all cancels are
    served by the worker that does NOT own the coroutine and therefore trust
    that value: the row was finalized CANCELLED and its slot released while
    the agent ran the turn to a billed completion whose SUCCESS then lost the
    CAS. See ``_set_cancel_then_reread_phase_sync`` for the ordering proof.

    Returns True when a cancel is already pending (the caller raises before
    dispatching). Fails soft — a Redis error reads as "no cancel", exactly
    as the bare read it replaces did.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        pipe = client.pipeline(transaction=False)
        pipe.set(
            _marker_key(entry.execution_id),
            _marker_payload(entry),
            ex=INFLIGHT_MARKER_TTL_SECONDS,
        )
        pipe.get(_cancel_key(entry.execution_id))
        _, cancel_raw = pipe.execute()
        return bool(cancel_raw)
    except Exception as e:  # noqa: BLE001
        _note_redis_failure(e)
        return False


def _mget_markers_sync(ids: List[str]) -> Optional[List[Any]]:
    """None ⇒ Redis is not available in this process (fail-open: absent);
    raises ⇒ an established client failed (the watchdog maps that to unknown).
    Bypasses the negative cache on purpose — see ``_get_client``."""
    client = _get_client(use_negative_cache=False)
    if client is None:
        return None
    return client.mget([_marker_key(eid) for eid in ids])


async def inflight_verdicts(execution_ids: Iterable[str]) -> Dict[str, str]:
    """Proof-of-life verdict per execution id:

    * ``alive`` — this worker's registry holds it, or a cross-worker marker exists;
    * ``absent`` — neither, and Redis answered (or no Redis is configured in
      this process at all, where the in-process registry is the whole truth);
    * ``unknown`` — an established Redis client failed or timed out (slow /
      flapping Redis): the marker could not be asked. The watchdog skips
      orphan recovery for park-age rows on ``unknown`` rather than failing
      open (#2196's rule: a read that could not be asked ≠ a read that said no).
    """
    ids = [eid for eid in execution_ids if eid]
    verdicts: Dict[str, str] = {}
    remaining: List[str] = []
    for eid in ids:
        if inflight_entry(eid) is not None:
            verdicts[eid] = "alive"
        else:
            remaining.append(eid)
    if not remaining:
        return verdicts
    try:
        values = await asyncio.to_thread(_mget_markers_sync, remaining)
    except Exception as e:  # noqa: BLE001
        _note_redis_failure(e)
        for eid in remaining:
            verdicts[eid] = "unknown"
        return verdicts
    if values is None:
        for eid in remaining:
            verdicts[eid] = "absent"
        return verdicts
    for eid, raw in zip(remaining, list(values) + [None] * (len(remaining) - len(values))):
        verdicts[eid] = "alive" if isinstance(raw, (str, bytes)) else "absent"
    return verdicts


_GLOBAL_AGENT_CALL_SEM: Optional[asyncio.Semaphore] = None
_AGENT_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_AGENT_SEMAPHORES_LOCK: Optional[asyncio.Lock] = None


def _ensure_globals() -> None:
    """Lazily create the global primitives on the running event loop.

    Module-import-time instantiation would bind the semaphores to
    whatever loop happened to be current — fine in production
    (uvicorn's single loop) but fragile under pytest's per-test loops.
    """
    global _GLOBAL_AGENT_CALL_SEM, _AGENT_SEMAPHORES_LOCK
    if _GLOBAL_AGENT_CALL_SEM is None:
        _GLOBAL_AGENT_CALL_SEM = asyncio.Semaphore(BACKEND_AGENT_CALL_LIMIT)
    if _AGENT_SEMAPHORES_LOCK is None:
        _AGENT_SEMAPHORES_LOCK = asyncio.Lock()


async def _get_agent_sem(agent_name: str) -> tuple[asyncio.Semaphore, int]:
    """Return ``(per_agent_semaphore, cap)`` — lazily created.

    The cap is read from ``db.get_max_parallel_tasks(agent_name)`` on
    first access. Unknown agents (deleted, or under unit-test stubs)
    fall back to 3.
    """
    _ensure_globals()
    sem = _AGENT_SEMAPHORES.get(agent_name)
    if sem is not None:
        return sem, _AGENT_SEMAPHORE_CAPS.get(agent_name, 3)

    cap = 3
    try:
        # Local import so the limiter is unit-testable without the heavy
        # `database` module init.
        # #506: facade bypass — read the effective cap (stored clamped to the
        # fleet ceiling). NOTE (documented limitation): the cap is frozen in
        # `_AGENT_SEMAPHORE_CAPS` at first access and never re-read, so a live
        # agent's semaphore does not shrink when the ceiling (or its own
        # max_parallel_tasks) drops until process restart. New agents get the
        # clamped cap immediately. Semaphore-resize machinery is out of scope.
        from services.settings_service import (  # noqa: WPS433 — local on purpose
            get_effective_max_parallel_tasks,
        )
        actual = get_effective_max_parallel_tasks(agent_name)
        if isinstance(actual, int) and actual > 0:
            cap = actual
    except Exception:  # pragma: no cover — defensive, unit tests stub `database`
        pass

    assert _AGENT_SEMAPHORES_LOCK is not None
    async with _AGENT_SEMAPHORES_LOCK:
        sem = _AGENT_SEMAPHORES.get(agent_name)
        if sem is None:
            sem = asyncio.Semaphore(cap)
            _AGENT_SEMAPHORES[agent_name] = sem
            _AGENT_SEMAPHORE_CAPS[agent_name] = cap
    return sem, cap


# Companion dict so the cap survives lookups for the log line.
_AGENT_SEMAPHORE_CAPS: dict[str, int] = {}


async def _acquire_with_optional_timeout(
    sem: asyncio.Semaphore,
    agent_name: str,
    where: str,
    agent_cap: int,
    global_cap: int,
    t0: float,
) -> None:
    """Acquire ``sem``. If ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S`` is
    > 0 (the default, 3600), enforce it and raise
    ``BackendAgentCallBudgetExhausted`` on timeout. If 0 (opt-in), wait
    indefinitely — preserves the pre-#904 semantics that calls which
    would have eventually succeeded still do, just at higher latency
    under congestion. Either way a one-shot "queued > 5s" warning
    surfaces a sustained queue so operators can see when the cap is
    actually biting (#2433: it used to fire only on the opt-in branch,
    so the default configuration parked calls for minutes in silence).

    ``where`` is "per-agent" or "global" for the log line. ``t0`` is
    the monotonic timestamp captured before the first acquire so
    `wait_ms` reflects total queue wait, not just this call.
    """
    timeout = BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S

    # Fast path: a slot is immediately available, skip the monitoring
    # task. `Semaphore.locked()` returns False when the internal counter
    # is > 0, i.e. acquire() would not block. (Don't trust this on its
    # own — race with another awaiter — so we still `acquire()` below;
    # the check just gates the warning task spawn for the hot path.)
    warning_task = None
    if sem.locked():
        # Slow path: at least one task is already queued ahead of us.
        # Spawn a one-shot warning timer so a sustained queue surfaces in
        # Vector logs without spamming every wait. Cancelled on acquire.
        warning_task = asyncio.create_task(
            _log_long_queue_wait(agent_name, where, agent_cap, global_cap, t0)
        )
    try:
        if timeout > 0:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                wait_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    f"[TaskExecService] Backend call budget exhausted ({where}) for "
                    f"{agent_name} after {wait_ms}ms "
                    f"(agent_cap={agent_cap}, global_cap={global_cap})"
                )
                raise BackendAgentCallBudgetExhausted(
                    agent_name, agent_cap, global_cap, wait_ms,
                )
        else:
            # Timeout disabled (opt-in). Wait forever — never fail a caller
            # that would have eventually succeeded pre-fix.
            await sem.acquire()
    finally:
        if warning_task is not None:
            warning_task.cancel()


async def _log_long_queue_wait(
    agent_name: str,
    where: str,
    agent_cap: int,
    global_cap: int,
    t0: float,
) -> None:
    """Warn once if a queue wait exceeds 5 seconds. Cancelled by the
    parent acquire when the slot is granted."""
    try:
        await asyncio.sleep(5.0)
        waited_ms = int((time.monotonic() - t0) * 1000)
        logger.warning(
            f"[TaskExecService] Agent-call queue wait > 5s ({where}) for "
            f"{agent_name} (waited={waited_ms}ms, "
            f"agent_cap={agent_cap}, global_cap={global_cap}) — backend "
            f"under sustained pressure"
        )
    except asyncio.CancelledError:
        pass  # acquired in time — no warning needed


@contextlib.asynccontextmanager
async def acquire_agent_call_slot(
    agent_name: str,
    *,
    execution_id: Optional[str] = None,
    on_granted: Optional[Callable[[float], Awaitable[None]]] = None,
):
    """Acquire per-agent + global slots for an outbound agent HTTP call.

    Acquire semantics depend on ``BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S``:

    * **> 0** (default, 3600): enforce the timeout. Acquire failures raise
      ``BackendAgentCallBudgetExhausted`` (caller translates to
      HTTP 503).
    * **= 0** (opt-in): wait indefinitely. The semaphore queues
      callers; the caller's HTTP connection stays open the whole
      time. Behavior matches pre-#904 except that fan-out is
      bounded — any call that would have eventually succeeded
      still does.

    A one-shot warning fires at 5s queue wait on both branches.

    #2433 — when ``execution_id`` names a live ``track_inflight_dispatch``
    entry, the wait is recorded as its ``parked`` phase (the refresher
    keeps the marker AND the capacity-slot lease alive for the whole park);
    at grant the entry flips to ``calling`` — published to the marker in the
    same round-trip that reads the cancel key, so a remote terminate can never
    see a stale ``parked`` — a cancel requested meanwhile (in-process flag, or
    the cross-worker cancel key once the entry is past the marker grace)
    raises ``BackendAgentCallCancelled`` before any POST, and
    ``on_granted(parked_seconds)`` is awaited when the park reached
    ``DISPATCH_RESTAMP_THRESHOLD_SECONDS`` so the caller can re-anchor the
    execution's clock and lease at dispatch.

    On successful entry, releases both semaphores on context exit
    (including the exception path).
    """
    _ensure_globals()
    agent_sem, agent_cap = await _get_agent_sem(agent_name)
    assert _GLOBAL_AGENT_CALL_SEM is not None
    global_sem = _GLOBAL_AGENT_CALL_SEM
    global_cap = BACKEND_AGENT_CALL_LIMIT

    entry = inflight_entry(execution_id) if execution_id else None
    t0 = time.monotonic()
    if entry is not None:
        entry.phase = "parked"
        entry.parked_since = t0

    # Per-agent first — bounds blast radius per agent before charging
    # against the global pool. Order matters for fairness: a single
    # bursty agent can't acquire the global slot before its per-agent
    # cap rejects it.
    await _acquire_with_optional_timeout(
        agent_sem, agent_name, "per-agent", agent_cap, global_cap, t0,
    )

    try:
        await _acquire_with_optional_timeout(
            global_sem, agent_name, "global", agent_cap, global_cap, t0,
        )

        try:
            waited_s = time.monotonic() - t0
            wait_ms = int(waited_s * 1000)
            logger.debug(
                f"[TaskExecService] Acquired agent-call slot for {agent_name} "
                f"(wait={wait_ms}ms, agent_cap={agent_cap}, global_cap={global_cap})"
            )
            if entry is not None:
                entry.phase = "calling"
                entry.parked_since = None
                cancelled = entry.cancel_requested
                # Gate on the ENTRY's age, never on this attempt's park: the
                # marker exists once the entry is older than the grace (that is
                # `_tick`'s own filter), and `track_inflight_dispatch` wraps the
                # whole retry loop — so a retry that grants instantly can still
                # have a marker a tick left saying `parked`. Publishing the
                # parked->calling transition BEFORE reading the cancel key is
                # what closes the cross-worker race; see
                # `_publish_calling_and_check_cancel_sync`.
                entry_age = time.monotonic() - entry.registered_at
                if not cancelled and entry_age >= INFLIGHT_MARKER_GRACE_SECONDS:
                    cancelled = await asyncio.to_thread(
                        _publish_calling_and_check_cancel_sync, entry
                    )
                if cancelled:
                    logger.info(
                        f"[TaskExecService] Execution {execution_id} on {agent_name} was "
                        f"cancelled while queued ({wait_ms}ms) — not dispatching"
                    )
                    raise BackendAgentCallCancelled(agent_name, agent_cap, global_cap, wait_ms)
                if on_granted is not None and waited_s >= DISPATCH_RESTAMP_THRESHOLD_SECONDS:
                    logger.info(
                        f"[TaskExecService] Execution {execution_id} on {agent_name} parked "
                        f"{wait_ms}ms in the backend call queue — re-anchoring at dispatch"
                    )
                    try:
                        await on_granted(waited_s)
                    except Exception as e:  # noqa: BLE001 — never block the dispatch on bookkeeping
                        logger.warning(
                            f"[TaskExecService] on_granted failed for {execution_id}: {e}"
                        )
            yield
        finally:
            global_sem.release()
    finally:
        agent_sem.release()


def _reset_for_testing(
    global_limit: Optional[int] = None,
    queue_timeout_s: Optional[float] = None,
    *,
    client_factory: Optional[Callable[[], Any]] = None,
    slot_renewer: Optional[Callable[[str, str], bool]] = None,
) -> None:
    """Reset module-level state. Test-only — not part of the public API.

    Call from a fixture's setup phase to get a fresh global semaphore
    and per-agent dict bound to the current test's event loop. Also clears
    the #2433 in-flight registry, cancels a refresher left over from a
    previous loop, resets the Redis negative cache, and installs the
    optional test seams (``client_factory`` → the Redis client the
    refresher/readers use; ``slot_renewer`` → the slot-lease renewal).
    """
    global BACKEND_AGENT_CALL_LIMIT, BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S
    global _GLOBAL_AGENT_CALL_SEM, _AGENT_SEMAPHORES, _AGENT_SEMAPHORES_LOCK
    global _AGENT_SEMAPHORE_CAPS
    global _INFLIGHT, _PENDING_DELETES, _REFRESHER_TASK
    global _REDIS_UNAVAILABLE_UNTIL, _REDIS_EPISODE_LOGGED
    global _client_factory, _slot_renewer

    if global_limit is not None:
        BACKEND_AGENT_CALL_LIMIT = global_limit
    if queue_timeout_s is not None:
        BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S = queue_timeout_s

    _GLOBAL_AGENT_CALL_SEM = None
    _AGENT_SEMAPHORES_LOCK = None
    _AGENT_SEMAPHORES = {}
    _AGENT_SEMAPHORE_CAPS = {}

    task = _REFRESHER_TASK
    _REFRESHER_TASK = None
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:  # noqa: BLE001 — a task from a closed loop
            pass
    _INFLIGHT = {}
    _PENDING_DELETES = set()
    _REDIS_UNAVAILABLE_UNTIL = 0.0
    _REDIS_EPISODE_LOGGED = False
    _client_factory = client_factory
    _slot_renewer = slot_renewer
