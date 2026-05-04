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
from typing import Dict, Iterable, List, Optional

from canary import collect_snapshot, run_invariants
from canary.snapshot import ViolationReport
from database import db
from db_models import NotificationCreate

logger = logging.getLogger(__name__)


# Five-minute cadence per the design doc. Deliberately the same as
# cleanup_service to share the operator's mental model (both are "every
# 5 min the backend reconciles state").
CANARY_INTERVAL_SECONDS = 300

# Synthetic agent_name used as the source for canary notifications.
# Not a real agent — `agent_notifications.agent_name` is a free-text TEXT
# column with no FK, so this just identifies notifications as canary-sourced.
CANARY_NOTIFIER_NAME = "canary-harness"

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
    ) -> Dict[str, List[ViolationReport]]:
        """Run one canary cycle. Public so it can be invoked from tests
        or by the operator via `POST /api/canary/run-cycle`.

        Returns the per-invariant violation lists from this cycle.
        """
        if self._lock.locked():
            logger.debug("canary: cycle already in progress, skipping")
            return {}
        async with self._lock:
            return await self._run_cycle_inner(invariant_ids)

    async def _run_cycle_inner(
        self,
        invariant_ids: Optional[Iterable[str]],
    ) -> Dict[str, List[ViolationReport]]:
        # Capture pre-cycle state for green→red transition detection.
        # Done BEFORE persistence so the "previous" comparison sees the
        # state as of the prior cycle, not this one.
        previous_latest = db.get_latest_canary_violation_per_invariant()

        # Heavy work — synchronous SQLite + Redis reads. Offload to a thread
        # so we don't block the asyncio loop while sqlite3 is blocking.
        snapshot = await asyncio.to_thread(collect_snapshot)
        results = await asyncio.to_thread(run_invariants, snapshot, invariant_ids)

        persisted_count = 0
        for inv_id, vlist in results.items():
            for v in vlist:
                try:
                    db.insert_canary_violation(
                        invariant_id=v.invariant_id,
                        tier=v.tier,
                        severity=v.severity,
                        snapshot_time=snapshot.snapshot_time,
                        observed_state=v.observed_state,
                        signal_query=v.signal_query,
                    )
                    persisted_count += 1
                except Exception:
                    logger.exception(
                        "canary: failed to persist violation %s; continuing",
                        v.invariant_id,
                    )

        # Detect green→red transitions and emit one notification per.
        transition_count = 0
        for inv_id, vlist in results.items():
            if not vlist:
                continue
            prev = previous_latest.get(inv_id)
            # Transition rule: previous violation is older than this snapshot
            # (or absent entirely) → green→red. Same-snapshot rows from a
            # manual rerun are continuations, not transitions.
            if prev is not None and prev["snapshot_time"] >= snapshot.snapshot_time:
                continue
            try:
                await self._emit_transition(inv_id, vlist, snapshot.snapshot_time)
                transition_count += 1
            except Exception:
                logger.exception(
                    "canary: failed to emit transition notification for %s",
                    inv_id,
                )

        # Update counters + last-run.
        self.cumulative_cycles += 1
        self.cumulative_violations += persisted_count
        self.cumulative_transitions += transition_count
        self.last_run_at = snapshot.snapshot_time

        if persisted_count or snapshot.sources_unavailable:
            logger.info(
                "canary cycle: violations=%d transitions=%d unavailable=%s",
                persisted_count,
                transition_count,
                snapshot.sources_unavailable,
            )

        return results

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def _emit_transition(
        self,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> None:
        """Create + broadcast one notification for a green→red transition.

        Severity is the max of the cycle's violations for this invariant —
        operators should hear about the worst one, not the first found.
        """
        worst = max(violations, key=lambda v: _severity_rank(v.severity))
        priority = SEVERITY_TO_PRIORITY.get(worst.severity, "normal")

        title = f"{invariant_id} violated"
        if len(violations) > 1:
            title += f" ({len(violations)}× this cycle)"

        message = self._render_message(invariant_id, violations, snapshot_time)

        data = NotificationCreate(
            notification_type="alert",
            title=title[:200],
            message=message,
            priority=priority,
            category="canary",
            metadata={
                "invariant_id": invariant_id,
                "tier": worst.tier,
                "severity": worst.severity,
                "snapshot_time": snapshot_time,
                "violations_in_cycle": len(violations),
                # Cap sample for the bell tooltip to keep the row small.
                "sample_observed_state": [
                    v.observed_state for v in violations[:3]
                ],
            },
        )

        notification = db.create_notification(CANARY_NOTIFIER_NAME, data)
        await self._broadcast(notification)

    @staticmethod
    def _render_message(
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> str:
        """Human-readable one-liner for the notification body."""
        if invariant_id == "S-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"Slot–row bijection broke on {len(agents)} agent(s) "
                f"({', '.join(agents)[:120]}) at {snapshot_time}."
            )
        if invariant_id == "E-02":
            return (
                f"{len(violations)} execution(s) reverted from terminal to "
                f"non-terminal status at {snapshot_time}."
            )
        if invariant_id == "L-03":
            ghosts = sorted(
                {v.observed_state.get("ghost_agent_name") for v in violations}
            )
            return (
                f"{len(ghosts)} ghost agent(s) referenced by orphan rows: "
                f"{', '.join(ghosts)[:120]} at {snapshot_time}."
            )
        return f"{invariant_id} fired {len(violations)} violation(s) at {snapshot_time}."

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
