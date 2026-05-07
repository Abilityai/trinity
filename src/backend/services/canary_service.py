"""
Canary watcher service (CANARY-001 / Issue #411 — Phase 1).

Runs in the backend process. Every 5 minutes:

1. `collect_snapshot()` — read Redis, SQLite, agent registries.
2. `run_invariants(snapshot)` — apply S-01 / E-02 / L-03 (Phase 1 set).
3. Persist any violations to `canary_violations`.
4. Detect green→red transitions per invariant against the previously-stored
   latest violation; emit one notification per transition (the bell).

Modeled on `services/cleanup_service.py` — single asyncio task, idempotent
start/stop, lock-guarded re-entrancy. Disabled by default; enable per
deployment with `CANARY_ENABLED=1`. Production deployment is staging/dev.

Why a service and not a Trinity agent (Issue #411 design discussion):
the watcher does no LLM reasoning — it's a deterministic library invocation
on a 5-minute timer. Running it as a Trinity agent would add an LLM call
per cycle and a separate container for no benefit. Deterministic checks
belong in the backend; the agents are the *fleet* (load generators), which
are deployed via the canary-fleet manifest.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from canary import collect_snapshot, run_invariants
from canary.snapshot import ViolationReport
from database import db
from db_models import NotificationCreate


@dataclass
class CycleResult:
    """Outcome of one canary cycle.

    `violations` is the per-invariant list of `ViolationReport`s the
    deterministic library produced (now persisted to `canary_violations`).

    `transition_invariant_ids` is the subset that fired a notification
    this cycle — i.e. invariants the service decided were a real
    green→red flip, not a continuation of an already-known violation.
    The router exposes this directly to operators so the on-demand
    `/api/canary/run-cycle` response matches what the bell received.

    `persisted_violation_ids` is index-aligned with `violations`: for
    each `ViolationReport` in `violations[inv_id][i]`, the row id
    returned by `insert_canary_violation` is at
    `persisted_violation_ids[inv_id][i]` — or `None` if the insert
    failed. Lets the router surface row ids without re-querying.
    """

    violations: Dict[str, List[ViolationReport]] = field(default_factory=dict)
    persisted_violation_ids: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    transition_invariant_ids: List[str] = field(default_factory=list)
    snapshot_time: str = ""

logger = logging.getLogger(__name__)


# Five-minute cadence per the design doc. Deliberately the same as
# cleanup_service to share the operator's mental model (both are "every
# 5 min the backend reconciles state").
CANARY_INTERVAL_SECONDS = 300

# Synthetic agent_name used as the source for canary notifications.
# Not a real agent — `agent_notifications.agent_name` is a free-text TEXT
# column with no FK, so this just identifies notifications as canary-sourced.
CANARY_NOTIFIER_NAME = "canary-harness"

# Redis key holding the snapshot_time of the most recent cycle that ran.
# Used by transition detection so a continuously-red invariant fires a
# notification once (on the first cycle that catches it) rather than every
# cycle thereafter — see `_run_cycle_inner` for the rule.
REDIS_KEY_LAST_CYCLE = "canary:last_cycle_at"

# Map invariant severity → notification priority (NotificationCreate enum).
SEVERITY_TO_PRIORITY = {
    "critical": "urgent",
    "major": "high",
    "minor": "normal",
}

# WebSocket manager (injected from main.py) — broadcasts notifications live
# so the bell lights up without a poll. Mirrors cleanup_service / notifications
# router pattern.
_ws_manager = None
_filtered_ws_manager = None


def set_canary_ws_manager(manager):
    """Inject the main WebSocket manager (called from main.py on startup)."""
    global _ws_manager
    _ws_manager = manager


def set_canary_filtered_ws_manager(manager):
    """Inject the filtered WebSocket manager (Trinity Connect)."""
    global _filtered_ws_manager
    _filtered_ws_manager = manager


class CanaryService:
    """Background watcher loop for the canary invariant harness."""

    def __init__(self, interval_seconds: int = CANARY_INTERVAL_SECONDS):
        self.interval = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        # Counters surface in /api/health-style monitoring; useful for
        # confirming the service is actually firing on deployed instances.
        self.cumulative_cycles: int = 0
        self.cumulative_violations: int = 0
        self.cumulative_transitions: int = 0
        self.last_run_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background loop. No-op if already running or disabled."""
        if not self._is_enabled():
            logger.info("canary: disabled (set CANARY_ENABLED=1 to enable)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"canary watcher started (interval={self.interval}s)")

    def stop(self):
        """Stop the background loop cleanly."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("canary watcher stopped")

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv("CANARY_ENABLED", "0") == "1"

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _loop(self):
        """Loop forever: cycle → sleep → cycle."""
        # Short delay so the backend is fully ready before the first cycle —
        # avoids a noisy "sources_unavailable" log line during cold start.
        await asyncio.sleep(30)
        while self._running:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a cycle exception kill the loop. Log and retry
                # next interval. Mirrors cleanup_service.
                logger.exception("canary cycle raised; will retry next interval")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self,
        invariant_ids: Optional[Iterable[str]] = None,
    ) -> CycleResult:
        """Run one canary cycle. Public so it can be invoked from tests
        or by the operator via `POST /api/canary/run-cycle`.

        Returns a `CycleResult` carrying the per-invariant violation
        lists this cycle produced *and* the subset that fired a
        notification (i.e. green→red transitions). Both pieces of
        truth come from the same code path the background loop uses,
        so the on-demand endpoint cannot disagree with the bell.
        """
        if self._lock.locked():
            logger.debug("canary: cycle already in progress, skipping")
            return CycleResult()
        async with self._lock:
            return await self._run_cycle_inner(invariant_ids)

    async def _run_cycle_inner(
        self,
        invariant_ids: Optional[Iterable[str]],
    ) -> CycleResult:
        # Capture pre-cycle state for green→red transition detection.
        # Two reads BEFORE the cycle runs:
        #   1. previous_latest — the most recent persisted violation per
        #      invariant id. Tells us "when was this invariant last red".
        #   2. prev_cycle_at — snapshot_time of the prior cycle (any cycle,
        #      regardless of outcome). Tells us "when did we last look".
        #
        # An invariant transition (green→red) fires only when (a) the
        # invariant has violations this cycle AND (b) the previous cycle
        # was green for it — i.e. the latest stored violation predates
        # the previous cycle's snapshot. This is the only rule that
        # silences continuously-red invariants without losing real
        # green→red flips, including red→green→red.
        previous_latest = db.get_latest_canary_violation_per_invariant()
        prev_cycle_at = self._read_prev_cycle_at()

        # Heavy work — synchronous SQLite + Redis reads. Offload to a thread
        # so we don't block the asyncio loop while sqlite3 is blocking.
        snapshot = await asyncio.to_thread(collect_snapshot)
        results = await asyncio.to_thread(run_invariants, snapshot, invariant_ids)

        persisted_count = 0
        # Index-aligned with `results[inv_id]` — `None` slot means insert
        # failed. The router uses these ids directly instead of re-querying
        # by (invariant_id, snapshot_time).
        persisted_ids: Dict[str, List[Optional[int]]] = {}
        for inv_id, vlist in results.items():
            inv_ids: List[Optional[int]] = []
            for v in vlist:
                try:
                    row_id = db.insert_canary_violation(
                        invariant_id=v.invariant_id,
                        tier=v.tier,
                        severity=v.severity,
                        snapshot_time=snapshot.snapshot_time,
                        observed_state=v.observed_state,
                        signal_query=v.signal_query,
                    )
                    inv_ids.append(row_id)
                    persisted_count += 1
                except Exception:
                    inv_ids.append(None)
                    logger.exception(
                        "canary: failed to persist violation %s; continuing",
                        v.invariant_id,
                    )
            persisted_ids[inv_id] = inv_ids

        # Detect green→red transitions and emit one notification per.
        transition_ids: List[str] = []
        for inv_id, vlist in results.items():
            if not vlist:
                continue
            if not self._is_green_to_red(inv_id, previous_latest, prev_cycle_at):
                continue
            try:
                await self._emit_transition(inv_id, vlist, snapshot.snapshot_time)
                transition_ids.append(inv_id)
            except Exception:
                logger.exception(
                    "canary: failed to emit transition notification for %s",
                    inv_id,
                )

        # Update counters + last-run.
        self.cumulative_cycles += 1
        self.cumulative_violations += persisted_count
        self.cumulative_transitions += len(transition_ids)
        self.last_run_at = snapshot.snapshot_time
        # Persist this cycle's snapshot_time for the NEXT cycle's transition
        # check. Done AFTER notifications so a crash mid-emit doesn't
        # advance the cursor and silence a real transition on retry.
        self._write_prev_cycle_at(snapshot.snapshot_time)

        if persisted_count or snapshot.sources_unavailable:
            logger.info(
                "canary cycle: violations=%d transitions=%d unavailable=%s",
                persisted_count,
                len(transition_ids),
                snapshot.sources_unavailable,
            )

        return CycleResult(
            violations=results,
            persisted_violation_ids=persisted_ids,
            transition_invariant_ids=transition_ids,
            snapshot_time=snapshot.snapshot_time,
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _emit_transition(
        self,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> None:
        """Fire alerts for a green→red transition.

        Phase 1 ships with no alert sink wired — dashboard notifications
        were dropped per product call (vybe), and Slack is the planned
        replacement in a follow-up PR. The transition is still detected
        and counted so behavior stays stable; this hook is the single
        seam the next PR plugs into.

        The original notification helpers — `_render_message`, `_broadcast`,
        and `_severity_rank` — were intentionally retained alongside the
        ws-manager setters so the next PR is a one-file change.
        """
        await self._notify_transition_stub(invariant_id, violations, snapshot_time)

    async def _notify_transition_stub(
        self,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> None:
        """Placeholder alert sink — replaced by Slack in the next PR."""
        worst = max(violations, key=lambda v: _severity_rank(v.severity))
        logger.info(
            "canary transition (alert sink not wired): %s severity=%s "
            "violations_in_cycle=%d snapshot_time=%s",
            invariant_id,
            worst.severity,
            len(violations),
            snapshot_time,
        )

    @staticmethod
    def _render_message(
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> str:
        """Human-readable one-liner for the notification body.

        Time is intentionally omitted — the panel renders a relative
        "just now / 4m ago" badge from the row's `created_at` column,
        and the precise ISO `snapshot_time` is preserved in
        `metadata.snapshot_time` for forensic correlation back to the
        `canary_violations` row. Embedding it in the message text would
        be redundant and crowd the bell.
        """
        if invariant_id == "S-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"Slot–row bijection broke on {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        if invariant_id == "E-02":
            return (
                f"{len(violations)} execution(s) reverted from terminal "
                f"to non-terminal status."
            )
        if invariant_id == "L-03":
            ghosts = sorted(
                {v.observed_state.get("ghost_agent_name") for v in violations}
            )
            return (
                f"{len(ghosts)} ghost agent(s) referenced by orphan rows: "
                f"{', '.join(ghosts)[:160]}."
            )
        return f"{invariant_id} fired {len(violations)} violation(s)."

    # ------------------------------------------------------------------
    # Cycle-state side-table (Redis)
    # ------------------------------------------------------------------

    @staticmethod
    def _redis():
        """Redis client shared with the slot service. Lazy import so this
        module stays loadable in tests without a live Redis."""
        from services.slot_service import get_slot_service

        return get_slot_service().redis

    def _read_prev_cycle_at(self) -> Optional[str]:
        """Snapshot_time of the prior cycle, or None on first ever run.

        Falls back to None on any Redis error — that turns the next
        cycle's transition detection into "all violations are transitions"
        for that single cycle, which is verbose but never misses a real
        flip. We chose verbose-on-failure over silent-on-failure because
        the canary's whole reason to exist is catching transitions.
        """
        try:
            return self._redis().get(REDIS_KEY_LAST_CYCLE)
        except Exception:
            logger.exception("canary: failed to read previous-cycle marker")
            return None

    def _write_prev_cycle_at(self, snapshot_time: str) -> None:
        """Advance the previous-cycle cursor to this cycle's snapshot_time."""
        try:
            self._redis().set(REDIS_KEY_LAST_CYCLE, snapshot_time)
        except Exception:
            logger.exception("canary: failed to persist previous-cycle marker")

    @staticmethod
    def _is_green_to_red(
        invariant_id: str,
        previous_latest: dict,
        prev_cycle_at: Optional[str],
    ) -> bool:
        """Decide whether this cycle's violation is a fresh transition.

        Green→red iff the latest persisted violation for this invariant
        predates the previous cycle's snapshot_time. Cases:

        - First-ever cycle (prev_cycle_at is None): every violation is a
          transition. Operators expect to be told once when the harness
          first starts seeing problems.
        - First-ever violation (previous_latest entry absent): transition.
        - Continuing-red (latest violation timestamp == prev_cycle_at):
          continuation, no notification — the previous cycle saw it too.
        - Red→green→red (latest violation predates prev_cycle_at):
          transition — there was at least one clean cycle in between.
        """
        prev = previous_latest.get(invariant_id)
        if prev is None:
            return True
        if prev_cycle_at is None:
            return True
        # `>=` so a same-snapshot replay from an immediate manual rerun
        # is treated as a continuation rather than re-firing.
        return prev["snapshot_time"] < prev_cycle_at

    # ------------------------------------------------------------------
    # WebSocket broadcast
    # ------------------------------------------------------------------

    @staticmethod
    async def _broadcast(notification) -> None:
        """Push the notification to UI clients via WebSocket.

        Mirrors the routers/notifications._broadcast_notification flow but
        is in-process (the canary service has no HTTP entry point of its
        own). Failures are logged and swallowed — the row is already
        persisted, so the bell will pick it up on next render.
        """
        if _ws_manager is None and _filtered_ws_manager is None:
            return  # WS not wired (e.g. in tests)
        event = {
            "type": "agent_notification",
            "notification_id": notification.id,
            "agent_name": notification.agent_name,
            "notification_type": notification.notification_type,
            "title": notification.title,
            "priority": notification.priority,
            "category": notification.category,
            "timestamp": notification.created_at,
        }
        try:
            if _ws_manager is not None:
                await _ws_manager.broadcast(json.dumps(event))
            if _filtered_ws_manager is not None:
                await _filtered_ws_manager.broadcast_filtered(event)
        except Exception:
            logger.exception("canary: ws broadcast failed; row persisted, ignoring")


def _severity_rank(severity: str) -> int:
    """Higher = worse. Used to pick the loudest violation for a transition."""
    return {"minor": 1, "major": 2, "critical": 3}.get(severity, 0)


# Module-level singleton, mirrors cleanup_service.
canary_service = CanaryService()
