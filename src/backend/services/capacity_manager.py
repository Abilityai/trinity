"""
CapacityManager — unified per-agent execution capacity (#428).

Replaces the three-class pyramid (`ExecutionQueue` + `SlotService` +
`BacklogService`) with a single facade. The two surviving primitives are kept
as internal collaborators because each has a distinct, well-tested job:

    SlotService    — atomic N-ary count gate (Redis ZSET).
    BacklogService — persistent overflow store (SQL row + drain spawn).

The third class (`ExecutionQueue`) is deleted; its responsibilities collapse
into:
    - the N=1 case of the count gate (handled by SlotService), and
    - an in-memory FIFO overflow store (Redis LIST) implemented inline below.

Public API (this is the *only* surface callers should reach for):

    acquire(agent_name, execution_id, max_concurrent, *,
            overflow_policy, overflow_payload=None, ...) -> AcquireResult
    release(agent_name, execution_id) -> None
    release_if_matches(agent_name, execution_id) -> bool
    get_status(agent_name, max_concurrent) -> QueueStatus
    get_all_states(agent_capacities) -> Dict[str, Dict[str, int]]
    force_release(agent_name) -> ForceReleaseResult
    reclaim_stale(agent_timeouts) -> Dict[str, List[str]]
    cancel_all_overflow(agent_name, reason) -> int

Overflow policies:
    "reject"           — no queue; raise CapacityFull when at capacity.
    "queue_in_memory"  — Redis LIST FIFO bounded by IN_MEMORY_DEPTH.
                         Used by /chat: position is observability only;
                         the agent serializes Claude subprocess execution.
    "queue_persistent" — SQL backlog (BacklogService). Used by /task: caller
                         returns 202 Accepted and the drain reconstructs the
                         request later.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import redis

from models import (
    Execution,
    ExecutionSource,
    QueueItemStatus,
    QueueStatus,
)
from services.backlog_service import BacklogService
from services.dispatch_breaker import DispatchBreaker
from services.slot_service import SlotService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

OverflowPolicy = Literal["reject", "queue_in_memory", "queue_persistent"]

# In-memory overflow depth — preserved from the original ExecutionQueue
# (MAX_QUEUE_SIZE=3). This is a rate-limit / observability bound, not a
# serialization mechanism — the agent's Claude subprocess is the real serial
# bottleneck.
IN_MEMORY_DEPTH = 3


@dataclass
class AcquireResult:
    """Outcome of `CapacityManager.acquire`.

    state:
        "admitted"          — slot was acquired; caller proceeds.
        "queued_in_memory"  — overflow recorded in the in-memory FIFO; caller
                              still proceeds (the agent serializes execution).
                              `queue_position` is set for observability.
        "queued_persistent" — overflow persisted to the SQL backlog; caller
                              should return 202 Accepted. The drain handles
                              actual execution later.
    """
    state: Literal["admitted", "queued_in_memory", "queued_persistent"]
    execution_id: str
    queue_position: Optional[int] = None


@dataclass
class ForceReleaseResult:
    """Result of an emergency `force_release` — used by force-release endpoint."""
    was_running: bool
    slots_cleared: int


@dataclass
class PersistentTaskPayload:
    """All fields needed to reconstruct a /task request when drained from the
    persistent backlog. Mirrors the existing BacklogService.enqueue signature
    so the wire format on the SQL row is unchanged."""
    request: Any  # ParallelTaskRequest — kept Any to avoid a router import here
    effective_timeout: int
    user_id: Optional[int]
    user_email: Optional[str]
    subscription_id: Optional[str]
    x_source_agent: Optional[str]
    x_mcp_key_id: Optional[str]
    x_mcp_key_name: Optional[str]
    triggered_by: str
    collaboration_activity_id: Optional[str]
    is_self_task: bool = False
    self_task_activity_id: Optional[str] = None


class CapacityFull(Exception):
    """Raised when admit fails: capacity exhausted AND no overflow available.

    `reason` distinguishes the cases for the caller's HTTP error message:
        "rejected"        — overflow_policy="reject" and at capacity.
        "in_memory_full"  — in-memory queue at IN_MEMORY_DEPTH.
        "persistent_full" — persistent backlog at its configured depth.
    """
    def __init__(
        self,
        agent_name: str,
        max_concurrent: int,
        reason: Literal["rejected", "in_memory_full", "persistent_full"],
        depth: Optional[int] = None,
    ):
        self.agent_name = agent_name
        self.max_concurrent = max_concurrent
        self.reason = reason
        self.depth = depth
        super().__init__(
            f"Agent '{agent_name}' at capacity ({max_concurrent}); reason={reason}"
        )


class CircuitOpen(Exception):
    """Raised by ``acquire`` when the per-agent dispatch breaker is open
    (RELIABILITY-007, #526).

    Sibling to ``CapacityFull``. Raised at the TOP of ``acquire`` — before the
    slot acquisition and overflow branch — so the doomed execution is never
    enqueued (the no-enqueue invariant). The caller maps it to HTTP 503 +
    ``Retry-After`` / ``X-Circuit-Open`` and closes the pre-created execution
    row FAILED(circuit_open).
    """

    def __init__(self, agent_name: str, retry_after_seconds: int = 0):
        self.agent_name = agent_name
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Agent '{agent_name}' dispatch circuit open; "
            f"retry after {retry_after_seconds}s"
        )


# ---------------------------------------------------------------------------
# CapacityManager
# ---------------------------------------------------------------------------


class CapacityManager:
    """Unified capacity facade — composes SlotService + BacklogService and owns
    the in-memory overflow store. See module docstring for usage."""

    # In-memory overflow stores under a distinct key from the slot ZSET so the
    # two never collide. Preserved from the original ExecutionQueue prefix
    # naming so any existing dashboards / debug tools keep working.
    _MEM_QUEUE_PREFIX = "agent:queue:"

    def __init__(
        self,
        redis_url: Optional[str] = None,
        slot_service: Optional[SlotService] = None,
        backlog_service: Optional[BacklogService] = None,
    ):
        from config import REDIS_URL as _DEFAULT_REDIS_URL
        url = redis_url or _DEFAULT_REDIS_URL
        self._redis = redis.from_url(url, decode_responses=True)
        self._slots = slot_service or SlotService(url)
        self._backlog = backlog_service or BacklogService()
        # The drain callback used to live in main.py wiring SlotService → Backlog;
        # CapacityManager owns it now so callers don't have to know.
        self._slots.register_on_release(self._on_slot_released)
        # Seed the canary B-02 drain-tick heartbeat at construction so the
        # on-demand `POST /api/canary/run-cycle` can never fire B-02 with
        # `drain_tick_age_seconds: null` during the ~15s window between
        # backend boot and the first `_capacity_maintenance_loop` tick.
        # On every successful `run_maintenance()` this gets overwritten
        # with a fresh timestamp; on init we only need a non-stale floor.
        try:
            self._redis.set("canary:drain_tick_at", str(time.time()))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                f"[Capacity] failed to seed canary drain-tick heartbeat: {e}"
            )

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------

    async def acquire(
        self,
        *,
        agent_name: str,
        execution_id: str,
        max_concurrent: int,
        message_preview: str = "",
        timeout_seconds: int = 900,
        overflow_policy: OverflowPolicy = "reject",
        overflow_payload: Optional[PersistentTaskPayload] = None,
        # In-memory overflow needs source metadata for the status endpoint:
        source: Optional[ExecutionSource] = None,
        source_agent: Optional[str] = None,
        source_user_id: Optional[str] = None,
        source_user_email: Optional[str] = None,
        message: Optional[str] = None,
        breaker_enabled: bool = False,
    ) -> AcquireResult:
        """Try to acquire a slot. On overflow, dispatch to the chosen policy.

        Args:
            breaker_enabled: per-agent dispatch-breaker opt-in
                (``agent_ownership.circuit_breaker_enabled``). When True AND the
                global ``DISPATCH_BREAKER_ENABLED`` master switch is on, the
                per-agent dispatch breaker gates this call. Default False →
                zero added cost on the dispatch hot path (#526 D7).

        Raises:
            CircuitOpen: if the dispatch breaker is open (raised BEFORE slot
                         acquisition / overflow so the doomed task is never
                         enqueued — the no-enqueue invariant, #526 D2).
            CapacityFull: if at capacity AND overflow store is also full
                          (or overflow_policy="reject").
        """
        # ---- #506: clamp the per-agent cap to the fleet-wide ceiling -----
        # Single runtime clamp point for every facade caller (chat ×3,
        # task_execution_service, …). "Clamp on use, never rewrite stored":
        # the agent keeps its chosen max_parallel_tasks; only the effective
        # admit limit is capped here.
        from services.settings_service import clamp_to_ceiling
        max_concurrent = clamp_to_ceiling(max_concurrent)

        # ---- 0. Dispatch breaker gate (#526) ----------------------------
        # Checked FIRST so a raised CircuitOpen never reaches the overflow
        # branch → no backlog poisoning. Per-agent opt-in short-circuits the
        # global-flag read so disabled agents pay nothing.
        if breaker_enabled:
            from config import DISPATCH_BREAKER_ENABLED
            if DISPATCH_BREAKER_ENABLED:
                breaker = DispatchBreaker(agent_name)
                if not breaker.allow_dispatch():
                    # Open within cooldown, or a sibling already holds the probe.
                    raise CircuitOpen(agent_name, breaker.retry_after_seconds())
                # allow_dispatch() returned True either because the breaker is
                # CLOSED (no probe consumed) or because we just claimed the
                # half-open PROBE. In the probe case the dispatch must be REAL: a
                # probe spent on a backlog enqueue records no verdict, so
                # next_probe_at never advances and the breaker re-probes every
                # lock-TTL (10s) instead of backing off (#526 F1). So when the
                # breaker is open, admit the probe ONLY into a free slot and
                # NEVER enqueue — extending the no-enqueue invariant (D2) across
                # the half-open window.
                if breaker.to_dict().get("state") == "open":
                    probe_admitted = await self._slots.acquire_slot(
                        agent_name=agent_name,
                        execution_id=execution_id,
                        max_parallel_tasks=max_concurrent,
                        message_preview=message_preview,
                        timeout_seconds=timeout_seconds,
                    )
                    if not probe_admitted:
                        raise CircuitOpen(agent_name, breaker.retry_after_seconds())
                    return AcquireResult(state="admitted", execution_id=execution_id)

        # First try to acquire a slot directly. SlotService already does the
        # atomic ZADD + count check.
        admitted = await self._slots.acquire_slot(
            agent_name=agent_name,
            execution_id=execution_id,
            max_parallel_tasks=max_concurrent,
            message_preview=message_preview,
            timeout_seconds=timeout_seconds,
        )
        if admitted:
            return AcquireResult(state="admitted", execution_id=execution_id)

        # At capacity — apply overflow policy.
        if overflow_policy == "reject":
            raise CapacityFull(agent_name, max_concurrent, "rejected")

        if overflow_policy == "queue_in_memory":
            position = self._mem_enqueue(
                agent_name=agent_name,
                execution_id=execution_id,
                source=source or ExecutionSource.USER,
                source_agent=source_agent,
                source_user_id=source_user_id,
                source_user_email=source_user_email,
                message=message or "",
            )
            return AcquireResult(
                state="queued_in_memory",
                execution_id=execution_id,
                queue_position=position,
            )

        if overflow_policy == "queue_persistent":
            if overflow_payload is None:
                raise ValueError(
                    "queue_persistent overflow requires overflow_payload"
                )
            enqueued = await self._backlog.enqueue(
                agent_name=agent_name,
                execution_id=execution_id,
                request=overflow_payload.request,
                effective_timeout=overflow_payload.effective_timeout,
                user_id=overflow_payload.user_id,
                user_email=overflow_payload.user_email,
                subscription_id=overflow_payload.subscription_id,
                x_source_agent=overflow_payload.x_source_agent,
                x_mcp_key_id=overflow_payload.x_mcp_key_id,
                x_mcp_key_name=overflow_payload.x_mcp_key_name,
                triggered_by=overflow_payload.triggered_by,
                collaboration_activity_id=overflow_payload.collaboration_activity_id,
                is_self_task=overflow_payload.is_self_task,
                self_task_activity_id=overflow_payload.self_task_activity_id,
            )
            if not enqueued:
                raise CapacityFull(agent_name, max_concurrent, "persistent_full")
            return AcquireResult(
                state="queued_persistent", execution_id=execution_id
            )

        raise ValueError(f"Unknown overflow_policy: {overflow_policy!r}")

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release(self, agent_name: str, execution_id: str) -> None:
        """Release the slot for `execution_id`. Drains overflow internally:
        - In-memory queue: pops the next entry (bookkeeping only).
        - Persistent backlog: SlotService.register_on_release fires
          `_on_slot_released` which drains one row.
        """
        await self._slots.release_slot(agent_name, execution_id)
        # In-memory overflow: pop next for status-endpoint bookkeeping. The
        # persistent backlog is drained via the slot-release callback wired
        # in __init__.
        self._mem_pop(agent_name)

    async def release_if_matches(
        self, agent_name: str, execution_id: str
    ) -> bool:
        """Release the slot only if `execution_id` currently holds it.

        Used by the watchdog (TOCTOU-safe): we only want to release when the
        execution we're recovering is still the running one. SlotService's
        ZSET model is naturally per-execution_id — release_slot is a no-op
        for an execution_id that isn't there — so this is effectively the
        same as `release`. Kept as a separate method to preserve the existing
        watchdog call site's intent.
        """
        # Check membership first so we can report whether this was a match.
        slots_key = f"{self._slots.slots_prefix}{agent_name}"
        if self._redis.zscore(slots_key, execution_id) is None:
            return False
        await self._slots.release_slot(agent_name, execution_id)
        self._mem_pop(agent_name)
        return True

    async def _on_slot_released(self, agent_name: str) -> None:
        """SlotService callback. Drains one persistent backlog row if any."""
        try:
            await self._backlog.drain_next(agent_name)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"[Capacity] backlog drain on release failed for "
                f"'{agent_name}': {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    async def get_status(
        self, agent_name: str, max_concurrent: int = 1
    ) -> QueueStatus:
        """Status for the queue endpoint — current execution + in-memory queue.

        For backwards compatibility with the original /api/agents/{name}/queue
        endpoint, this reports only the in-memory queue (the historical /chat
        observability surface). Persistent backlog state is exposed via the
        executions endpoints (status='queued' rows).
        """
        slot_state = await self._slots.get_slot_state(agent_name, max_concurrent)
        is_busy = slot_state.active_slots > 0

        # The original ExecutionQueue.get_status surfaced one "current" item;
        # SlotService tracks N. For status-endpoint compatibility, surface
        # the oldest active slot as `current_execution` when there is one.
        current_execution: Optional[Execution] = None
        if is_busy and slot_state.slots:
            oldest = slot_state.slots[0]
            current_execution = Execution(
                id=oldest.execution_id,
                agent_name=agent_name,
                source=ExecutionSource.USER,  # not tracked per-slot today
                message=oldest.message_preview,
                queued_at=datetime.utcnow(),
                status=QueueItemStatus.RUNNING,
            )

        queued_executions = self._mem_list(agent_name)
        return QueueStatus(
            agent_name=agent_name,
            is_busy=is_busy,
            current_execution=current_execution,
            queue_length=len(queued_executions),
            queued_executions=queued_executions,
        )

    async def get_all_states(
        self, agent_capacities: Dict[str, int]
    ) -> Dict[str, Dict[str, int]]:
        """Bulk capacity meter for the dashboard.

        #506: each stored cap is clamped to the fleet ceiling so the dashboard
        reports the effective limit (available + active == effective).
        """
        from services.settings_service import clamp_to_ceiling
        clamped = {
            name: clamp_to_ceiling(cap) for name, cap in agent_capacities.items()
        }
        return await self._slots.get_all_slot_states(clamped)

    async def get_slot_state(self, agent_name: str, max_concurrent: int):
        """Detailed slot view for the per-agent capacity endpoint.

        Returns the underlying SlotState (slot_number, started_at,
        message_preview, duration_seconds per slot). Kept as a thin
        pass-through so the agent_config router doesn't need to know about
        the underlying primitive.

        #506: clamps the cap to the fleet ceiling so available_slots reflects
        the effective admit limit.
        """
        from services.settings_service import clamp_to_ceiling
        return await self._slots.get_slot_state(
            agent_name, clamp_to_ceiling(max_concurrent)
        )

    # ------------------------------------------------------------------
    # Cleanup / emergency
    # ------------------------------------------------------------------

    async def reclaim_stale(
        self, agent_timeouts: Optional[Dict[str, int]] = None
    ) -> Dict[str, List[str]]:
        """Reclaim slots whose TTL has expired. Used by the cleanup watchdog.

        `agent_timeouts` lets the watchdog supply per-agent execution timeouts
        so each agent's slots are cleaned with the right TTL+buffer (#226).
        """
        return await self._slots.cleanup_stale_slots(agent_timeouts=agent_timeouts)

    async def force_release(self, agent_name: str) -> ForceReleaseResult:
        """Emergency: clear all running slots and the in-memory queue."""
        slots_cleared = await self._slots.force_clear_slots(agent_name)
        # Clear in-memory queue too.
        mem_key = self._mem_queue_key(agent_name)
        was_running_or_queued = (
            slots_cleared > 0 or self._redis.exists(mem_key) > 0
        )
        if self._redis.exists(mem_key):
            self._redis.delete(mem_key)
        return ForceReleaseResult(
            was_running=was_running_or_queued,
            slots_cleared=slots_cleared,
        )

    async def clear_in_memory_queue(self, agent_name: str) -> int:
        """Clear only the in-memory overflow queue (leaves running executions)."""
        mem_key = self._mem_queue_key(agent_name)
        count = self._redis.llen(mem_key)
        if count > 0:
            self._redis.delete(mem_key)
            logger.info(
                f"[Capacity] Cleared {count} in-memory queued items for '{agent_name}'"
            )
        return count

    async def run_maintenance(self, max_age_hours: float = 24) -> None:
        """Periodic maintenance for the persistent overflow store.

        Expires queued rows older than `max_age_hours` to FAILED, then drains
        any orphan backlog that didn't trigger a release callback (e.g. after
        a backend restart between enqueue and drain). Called from main.py's
        60s loop.

        Then runs the #526 breaker-aware backstop (re-fails queued rows for any
        agent whose dispatch breaker is still open) so a lost inline drain
        recovers in ~60s rather than waiting out the 24h `expire_stale` window.

        On success — and only on success — writes a `canary:drain_tick_at`
        Redis key with the current unix timestamp. The canary's B-02
        invariant (no queued without slots-full) reads this to distinguish
        "queue stuck waiting for drain" from "drain just hasn't run yet".
        Written at the END of the sweep so a crash mid-sweep doesn't
        falsely claim a successful tick — leaving the cursor stale and
        letting B-02 catch the stuck drain. `__init__` seeds this key
        with a fresh timestamp so the on-demand canary path never sees a
        missing-heartbeat state during the boot window.
        """
        await self._backlog.expire_stale(max_age_hours=max_age_hours)

        # #1085: while the re-delivery governor's shared-cause pause is armed,
        # skip the active orphan drain + the breaker-backstop re-fail. Both push
        # backlog forward / fail queued rows — during a fleet outage we hold that
        # pressure back so a throttled-then-resumed callback still lands. The 24h
        # `expire_stale` above is the slow safety net and always runs. Fail-open:
        # governor degrades to not-paused, so a Redis blip resumes normal drain.
        paused = False
        try:
            import config

            if config.REDELIVERY_GOVERNOR_ENABLED:
                from services.redelivery_governor import get_redelivery_governor

                paused = get_redelivery_governor().should_hold_reaper()
        except Exception as e:  # noqa: BLE001 — never block maintenance on a gate error
            logger.debug("[Capacity] governor drain-gate skipped: %s", e)

        if paused:
            logger.info("[Capacity] orphan drain + breaker-backstop held — re-delivery paused (#1085)")
        else:
            await self._backlog.drain_orphans_all()
            await self._backstop_open_breaker_backlog()
        try:
            self._redis.set("canary:drain_tick_at", str(time.time()))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                f"[Capacity] failed to write canary drain-tick heartbeat: {e}"
            )

    async def _backstop_open_breaker_backlog(self) -> None:
        """#526 breaker-aware backstop. If the inline drain on a breaker →open
        transition was lost (task dropped, or `fail_queued_for_agent` threw),
        re-fail the queued backlog for any agent whose dispatch breaker is still
        open. Bounded to agents that actually have queued rows; one pipelined
        breaker-state read. Gated on the global master switch, so it's a no-op
        when the feature is off. Never raises — must not break the sweep."""
        try:
            from config import DISPATCH_BREAKER_ENABLED
            if not DISPATCH_BREAKER_ENABLED:
                return
            from database import db
            from services.dispatch_breaker import get_dispatch_states_for

            queued_agents = db.list_agents_with_queued()
            if not queued_agents:
                return
            for name, state in get_dispatch_states_for(queued_agents).items():
                if state.get("state") != "open":
                    continue
                failed = db.fail_queued_for_agent(
                    name, reason="circuit_open: maintenance backstop"
                )
                if failed:
                    await self.clear_in_memory_queue(name)
                    logger.warning(
                        "[Capacity] breaker-backstop failed %d queued row(s) for "
                        "open-breaker agent '%s'",
                        failed,
                        name,
                    )
        except Exception as e:
            logger.warning(f"[Capacity] breaker-aware backstop sweep failed: {e}")

    async def cancel_all_overflow(self, agent_name: str, reason: str) -> int:
        """Cancel all queued work (in-memory + persistent) for an agent.

        Used on agent deletion so dangling 'queued' rows don't point at a dead
        agent. Returns the count of persistent rows cancelled (the in-memory
        queue is best-effort).
        """
        # In-memory: best-effort delete.
        await self.clear_in_memory_queue(agent_name)
        # Persistent: real cancellation with audit reason.
        return await self._backlog.cancel_all_backlog(agent_name, reason=reason)

    # ------------------------------------------------------------------
    # In-memory queue helpers (private)
    # ------------------------------------------------------------------

    def _mem_queue_key(self, agent_name: str) -> str:
        return f"{self._MEM_QUEUE_PREFIX}{agent_name}"

    def _mem_enqueue(
        self,
        *,
        agent_name: str,
        execution_id: str,
        source: ExecutionSource,
        source_agent: Optional[str],
        source_user_id: Optional[str],
        source_user_email: Optional[str],
        message: str,
    ) -> int:
        """LPUSH an Execution into the in-memory queue. Returns 1-based position.

        Bounded by IN_MEMORY_DEPTH so queue cannot grow unbounded; raises
        CapacityFull when at the bound. (Bound preserved from the original
        ExecutionQueue MAX_QUEUE_SIZE.)
        """
        queue_key = self._mem_queue_key(agent_name)
        depth = self._redis.llen(queue_key)
        if depth >= IN_MEMORY_DEPTH:
            raise CapacityFull(
                agent_name, IN_MEMORY_DEPTH, "in_memory_full", depth=depth
            )
        execution = Execution(
            id=execution_id,
            agent_name=agent_name,
            source=source,
            source_agent=source_agent,
            source_user_id=source_user_id,
            source_user_email=source_user_email,
            message=message,
            queued_at=datetime.utcnow(),
            status=QueueItemStatus.QUEUED,
        )
        self._redis.lpush(queue_key, execution.model_dump_json())
        # Position is 1-based and human-readable: oldest queued = position 1.
        return depth + 1

    def _mem_pop(self, agent_name: str) -> None:
        """RPOP the oldest in-memory queued entry. Bookkeeping only — the
        caller proceeded with the request when it was queued; this just
        drops the record so the status endpoint stays accurate."""
        queue_key = self._mem_queue_key(agent_name)
        self._redis.rpop(queue_key)

    def _mem_list(self, agent_name: str) -> List[Execution]:
        """List in-memory queued executions, oldest first (FIFO order)."""
        queue_key = self._mem_queue_key(agent_name)
        items = self._redis.lrange(queue_key, 0, -1)
        executions = [Execution.model_validate_json(item) for item in items]
        # LPUSH/RPOP means LRANGE returns newest first; reverse for FIFO.
        executions.reverse()
        return executions


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_capacity_manager: Optional[CapacityManager] = None


def get_capacity_manager() -> CapacityManager:
    """Get the global CapacityManager instance."""
    global _capacity_manager
    if _capacity_manager is None:
        _capacity_manager = CapacityManager()
    return _capacity_manager


def reset_capacity_manager() -> None:
    """Reset the singleton — used by tests to inject mocked services."""
    global _capacity_manager
    _capacity_manager = None
