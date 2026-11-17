"""
Activity tracking service for unified activity stream.

This service provides centralized activity tracking with:
- Database persistence via DatabaseManager
- WebSocket broadcasting for real-time updates
- Subscriber pattern for extensibility
- Activity lifecycle management (start, complete, fail)
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Callable, Any, Set
from datetime import datetime
from models import (
    ActivityType,
    ActivityState,
    ActivityCreate,
    ActivityCloseOutcome,
    TaskExecutionStatus,
    activity_state_for_terminal,
)
from database import db
from services.sync_waiter import TERMINAL_TASK_STATUSES
from db.activities import queued_activity_ids
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Terminals whose activity state is *authoritative* (#1804). Only these may
# upgrade a provisionally-FAILED activity, and only these widen the lookup —
# see db/activities.py::_close_predicate for the lattice.
_AUTHORITATIVE_TERMINALS = (
    TaskExecutionStatus.SUCCESS,
    TaskExecutionStatus.CANCELLED,
)

# Strong refs for fire-and-forget closes spawned from sync call sites, so the
# event loop cannot garbage-collect a pending task (same guard as
# event_dispatch_service._inflight_emit_tasks).
_inflight_close_tasks: Set["asyncio.Task"] = set()


class ActivityService:
    """
    Centralized service for tracking agent activities.

    Handles:
    - Activity creation and completion
    - WebSocket broadcasting
    - Subscriber notifications
    - Current activity queries
    """

    def __init__(self):
        self.websocket_manager = None
        self.filtered_websocket_manager = None  # For /ws/events (Trinity Connect)
        self.subscribers: List[Callable] = []

    def set_websocket_manager(self, manager):
        """Set the WebSocket manager for broadcasting."""
        self.websocket_manager = manager

    def set_filtered_websocket_manager(self, manager):
        """Set the filtered WebSocket manager for /ws/events (Trinity Connect)."""
        self.filtered_websocket_manager = manager

    def subscribe(self, callback: Callable):
        """Register a callback to be notified of all activity events."""
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        """Unregister a callback."""
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    async def track_activity(
        self,
        agent_name: str,
        activity_type: ActivityType,
        user_id: Optional[int] = None,
        triggered_by: str = "user",
        parent_activity_id: Optional[str] = None,
        related_chat_message_id: Optional[str] = None,
        related_execution_id: Optional[str] = None,
        details: Optional[Dict] = None
    ) -> str:
        """
        Track the start of a new activity.

        Args:
            agent_name: Name of the agent
            activity_type: Type of activity (from ActivityType enum)
            user_id: User who triggered the activity
            triggered_by: Source of trigger (user, schedule, agent, system)
            parent_activity_id: ID of parent activity (for tool_call → chat_start linkage)
            related_chat_message_id: Link to chat_messages table
            related_execution_id: Link to schedule_executions table
            details: Activity-specific details as dict

        Returns:
            activity_id: UUID of created activity
        """
        # Create activity in database
        activity = ActivityCreate(
            agent_name=agent_name,
            activity_type=activity_type,
            activity_state=ActivityState.STARTED,
            parent_activity_id=parent_activity_id,
            user_id=user_id,
            triggered_by=triggered_by,
            related_chat_message_id=related_chat_message_id,
            related_execution_id=related_execution_id,
            details=details
        )

        activity_id = db.create_activity(activity)

        # Broadcast via WebSocket
        await self._broadcast_activity_event(
            agent_name=agent_name,
            activity_id=activity_id,
            activity_type=activity_type.value,
            activity_state="started",
            action=self._get_action_description(activity_type, details),
            details=details
        )

        # Notify subscribers
        await self._notify_subscribers({
            "event": "activity_started",
            "activity_id": activity_id,
            "agent_name": agent_name,
            "activity_type": activity_type.value,
            "details": details
        })

        return activity_id

    async def complete_activity(
        self,
        activity_id: str,
        status: str = ActivityState.COMPLETED,
        details: Optional[Dict] = None,
        error: Optional[str] = None,
        *,
        completed_at: Optional[datetime] = None,
    ) -> bool:
        """
        Mark an activity as completed or failed.

        Args:
            activity_id: UUID of the activity
            status: ActivityState value ("completed" or "failed")
            details: Additional details to merge with existing details
            error: Error message if failed
            completed_at: #3589 — the instant the producer says the work
                ended (the watchdog claim's `retained_at`); None stamps now

        Returns:
            bool: True if activity was found and updated
        """
        # Get activity to broadcast details
        activity = db.get_activity(activity_id)
        if not activity:
            return False

        # Update in database. #1804: the db layer is a lattice CAS returning a
        # tri-state outcome — ALREADY_CLOSED is a DESIGNED no-op (a second
        # closer refused, nothing clobbered), not a failure.
        outcome = db.complete_activity(activity_id, status, details, error,
                                       completed_at=completed_at)

        if outcome is ActivityCloseOutcome.UPDATED:
            # Broadcast via WebSocket
            await self._broadcast_activity_event(
                agent_name=activity["agent_name"],
                activity_id=activity_id,
                activity_type=activity["activity_type"],
                activity_state=status,
                action=f"Completed: {activity['activity_type']}",
                details=details,
                error=error
            )

            # Notify subscribers
            await self._notify_subscribers({
                "event": "activity_completed",
                "activity_id": activity_id,
                "agent_name": activity["agent_name"],
                "activity_type": activity["activity_type"],
                "status": status,
                "error": error,
                "details": details
            })

        # The bool answers "does this activity exist and is it closed" — the
        # question `routers/internal.py` 404s on. An idempotent refusal is a
        # satisfied caller, so only NOT_FOUND is False (a row deleted between
        # the read above and the CAS).
        return outcome is not ActivityCloseOutcome.NOT_FOUND

    async def close_execution_activity(
        self,
        execution_id: str,
        terminal_status,
        *,
        error: Optional[str] = None,
        activity_id: Optional[str] = None,
    ) -> bool:
        """Close the dispatch activity paired with a just-written execution
        terminal. **The single owner of the #1804 contract.**

        Call this from EVERY writer that wins a terminal CAS on
        ``schedule_executions``. Before #1804 the close was gated on holding the
        ``activity_id`` local *and* winning the CAS in the same coroutine, so
        every out-of-band terminal writer left the row ``started`` until a
        120-minute backstop closed it with a fabricated duration::

          CAS-WON TERMINAL WRITERS                          ONE OWNER
          ------------------------                          ---------
          _write_terminal_and_gate (won AND lost)   |
          apply_result (success + failure)          |
          watchdog  _recover_execution              |       activity_service
          startup   _recover_execution              |--->   .close_execution_activity()
          CancelledError x 2  (backend shutdown)    |              |
          lease reaper (park + requeue)             |              | lattice-aware lookup
          terminate_execution                       |              | activity_state_for_terminal()
          pull sink apply_task_result --spawn wrap--|              v
                                                          db.complete_activity()  <- CAS
                                                          -> ActivityCloseOutcome

          BULK (N rows) ------------------------------>  db.close_open_activities_for_executions()
                                                          (1 transaction, no per-row WS)

        ``terminal_status`` is an execution status (``TaskExecutionStatus`` or
        its bare string); the activity state comes from the shared
        ``models.activity_state_for_terminal`` (#1332) — never a second mapping.

        ``activity_id`` skips the lookup when the caller already holds it.
        Otherwise the lookup variant is chosen by the terminal: an
        *authoritative* terminal (SUCCESS/CANCELLED) searches ``started|failed``
        so it can upgrade a provisional FAILED; a *provisional* terminal
        (FAILED) searches ``started`` only. Callers stay ignorant of the lattice.

        Fail-open: returns False and swallows on any error. It runs AFTER a
        committed terminal write and must never affect it.
        """
        try:
            activity_state = activity_state_for_terminal(terminal_status)
            if execution_id:
                # #3651: companions are closed HERE, for every terminal writer,
                # and from the persisted row — not from `backlog_metadata`,
                # which a direct-dispatch `/task` hop never has. Opting in per
                # writer is how #3588 fixed one recovery path and left five.
                try:
                    await self.close_companion_activities(execution_id)
                except Exception as e:  # noqa: BLE001 — never block the dispatch close
                    logger.warning(
                        "[#3651] companion close failed for execution %s: %s",
                        execution_id,
                        e,
                    )
            if not activity_id:
                if not execution_id:
                    return False
                activity_id = db.get_open_activity_id_for_execution(
                    execution_id,
                    include_failed=terminal_status in _AUTHORITATIVE_TERMINALS,
                )
                if not activity_id:
                    return False
            return await self.complete_activity(
                activity_id=activity_id,
                status=activity_state,
                error=error,
            )
        except Exception as e:  # noqa: BLE001 — never affect a committed terminal
            logger.warning(
                "[#1804] close_execution_activity failed for execution %s: %s",
                execution_id,
                e,
            )
            return False

    @staticmethod
    def _companion_close_details(execution, meta: Dict) -> Dict:
        """The detail keys the push closers write (``complete_collaboration_activity``
        / ``finalize_self_task``) — ONE builder for every companion closer, so
        whichever closer wins the CAS persists the same shape (#2329, #3588)."""
        details = {
            "execution_id": execution.id,
            "response_length": len(getattr(execution, "response", None) or ""),
            "execution_time_ms": getattr(execution, "duration_ms", None),
        }
        if meta.get("is_self_task"):
            # finalize_self_task records this; keep it whichever closer wins.
            details["inject_result"] = meta.get("inject_result")
        return details

    async def close_companion_activities(
        self, execution_id: str, *, details_extra: Optional[Dict] = None
    ) -> int:
        """#3588: close every open companion (collaboration / self-task) activity
        of a TERMINAL execution, deriving state AND instant from the persisted
        row. Returns the number of companions closed.

        Called by ``close_execution_activity`` for every terminal writer
        (#3651), and directly by the watchdog claim to stamp ``recovered_by``.
        Companions may have NO ``backlog_metadata`` at all (a direct-dispatch
        ``/task`` hop), so the lookup is type-scoped on ``related_execution_id``
        with the metadata ids as a belt.

        Re-queue safety: a row that is not terminal NOW is left alone. The lease
        reaper closes the dead attempt's dispatch activity as CANCELLED and
        re-queues the row; its companions belong to the re-delivered attempt,
        and a CANCELLED close can never be upgraded.

        Push path: the owner runs before ``run_post_turn_delivery``, so it wins
        the companion CAS and the push closers' writes are no-ops. Result
        injection (``finalize_self_task``) runs regardless of that CAS. On a
        recovery-closed self-task the injection never runs (the coroutine that
        held the request died) — accepted loss; recovery writes FAILED and
        injection is SUCCESS-only, so it is the claim-to-SUCCESS case alone.

        **Everything is derived from the ROW, never from the caller's envelope**:
        ``apply_result``'s failure branch returns the same result whether its
        CAS won or lost (only the SUCCESS branch says RECONCILED), so "the claim
        won" is not observable at the call site. Reading the row makes a lost
        failure-style claim close companions in the terminal that actually
        stands (e.g. CANCELLED from the cancel that won, at the cancel's
        instant) — a close that can never disagree with the row. ``completed_at``
        is the row's persisted instant (#3589: the claim threads ``retained_at``
        into the row write, so row and companions agree by construction; when
        ``retained_at`` was unreadable both carry the writer's now-stamp).

        Fail-open: returns 0 and swallows on any error — it runs after a
        committed terminal and must never affect it. Per-row ``complete_activity``
        (WS broadcast per close) is deliberate: an execution has 0–2 companions
        and every caller already broadcasts its dispatch close; the bulk sweeps
        keep their broadcast-free ``close_open_activities_for_executions``.
        """
        try:
            execution = db.get_execution(execution_id)
            # Terminal NOW — the re-queue guard above.
            if execution is None or execution.status not in TERMINAL_TASK_STATUSES:
                return 0
            activity_state = activity_state_for_terminal(execution.status)
            raw = getattr(execution, "backlog_metadata", None)
            ids = db.get_open_companion_activity_ids_for_execution(
                execution_id,
                extra_ids=queued_activity_ids(raw),
                include_failed=execution.status in _AUTHORITATIVE_TERMINALS,
            )
            if not ids:
                return 0
            try:
                meta = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                meta = {}
            details = self._companion_close_details(execution, meta)
            if details_extra:
                details.update(details_extra)
            error = (
                getattr(execution, "error", None)
                if activity_state == ActivityState.FAILED
                else None
            )
            closed = 0
            for aid in ids:
                if await self.complete_activity(
                    activity_id=aid,
                    status=activity_state,
                    details=details,
                    error=error,
                    completed_at=getattr(execution, "completed_at", None),
                ):
                    closed += 1
            return closed
        except Exception as e:  # noqa: BLE001 — never affect a committed terminal
            logger.warning(
                "[#3588] companion close failed for execution %s: %s",
                execution_id,
                e,
            )
            return 0

    def spawn_close_execution_activity(
        self,
        execution_id: str,
        terminal_status,
        *,
        error: Optional[str] = None,
        activity_id: Optional[str] = None,
    ) -> None:
        """Fire ``close_execution_activity`` fire-and-forget from a SYNC site.

        Mirrors ``event_dispatch_service.spawn_task_terminal_event``: the pull
        sink (``pull_coordination_service.apply_task_result``) is synchronous but
        runs inside an async router handler, so a running loop exists. Fail-open:
        with no running loop the close is skipped (logged), never raised — the
        120-minute backstop still covers it until #429.
        """
        coro = self.close_execution_activity(
            execution_id, terminal_status, error=error, activity_id=activity_id
        )
        try:
            task = asyncio.create_task(coro)
            _inflight_close_tasks.add(task)
            task.add_done_callback(_inflight_close_tasks.discard)
        except RuntimeError as e:
            coro.close()
            logger.debug("[#1804] spawn_close_execution_activity skipped (no loop): %s", e)

    async def get_current_activities(self, agent_name: str) -> List[Dict]:
        """Get all in-progress activities for an agent."""
        return db.get_current_activities(agent_name)

    async def _broadcast_activity_event(
        self,
        agent_name: str,
        activity_id: str,
        activity_type: str,
        activity_state: str,
        action: str,
        details: Optional[Dict] = None,
        error: Optional[str] = None
    ):
        """Broadcast activity event via WebSocket."""
        if not self.websocket_manager:
            return

        event = {
            "type": "agent_activity",
            "agent_name": agent_name,
            "activity_id": activity_id,
            "activity_type": activity_type,
            "activity_state": activity_state,
            "action": action,
            "timestamp": utc_now_iso(),
            "details": details or {},
            "error": error
        }

        # Add context info if present in details
        if details:
            if "context_used" in details and "context_max" in details:
                event["details"]["context"] = {
                    "used": details["context_used"],
                    "max": details["context_max"],
                    "percentage": round((details["context_used"] / details["context_max"]) * 100, 2)
                }

        # Broadcast to main WebSocket manager (UI)
        # Note: ConnectionManager.broadcast expects a string, so serialize to JSON
        await self.websocket_manager.broadcast(json.dumps(event))

        # Also broadcast to filtered manager (Trinity Connect /ws/events)
        if self.filtered_websocket_manager:
            await self.filtered_websocket_manager.broadcast_filtered(event)

    async def _notify_subscribers(self, event: Dict):
        """Notify all subscribers of an activity event."""
        for callback in self.subscribers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                # Log error but don't fail the activity tracking
                print(f"Subscriber callback error: {e}")

    def _get_action_description(self, activity_type: ActivityType, details: Optional[Dict] = None) -> str:
        """Generate human-readable action description."""
        if activity_type == ActivityType.CHAT_START:
            if details and "message_preview" in details:
                preview = details["message_preview"][:50]
                return f"Processing: {preview}..."
            return "Processing chat"

        elif activity_type == ActivityType.TOOL_CALL:
            if details and "tool_name" in details:
                return f"Using tool: {details['tool_name']}"
            return "Executing tool"

        elif activity_type == ActivityType.SCHEDULE_START:
            if details and "schedule_name" in details:
                return f"Running: {details['schedule_name']}"
            return "Running scheduled task"

        elif activity_type == ActivityType.AGENT_COLLABORATION:
            if details and "target_agent" in details:
                return f"Collaborating with: {details['target_agent']}"
            return "Agent collaboration"

        elif activity_type == ActivityType.CHAT_END:
            return "Chat completed"

        elif activity_type == ActivityType.SCHEDULE_END:
            return "Schedule completed"

        else:
            return f"Activity: {activity_type.value}"


# Global activity service instance
activity_service = ActivityService()
