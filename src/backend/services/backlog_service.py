"""
BacklogService — persistent async task backlog (BACKLOG-001).

When `async_mode=true` arrives at `POST /api/agents/{name}/task` and the
agent's parallel slots are full, the request is persisted as a QUEUED row on
`schedule_executions` instead of returning HTTP 429. When a slot frees up,
`SlotService.release_slot()` fires a callback that drains the oldest queued
item for the same agent in FIFO order. A 60s maintenance task owned by
main.py expires stale queued rows (>24h) and drains orphans after restarts.

Key invariants:
- Enqueue is idempotent per execution_id (the row already exists from
  create_task_execution; we flip its status to QUEUED).
- Drain acquires the slot BEFORE claiming the row. If slot acquisition fails,
  no row transition happens. If claim returns nothing (race with another
  drain), the slot we just acquired is immediately released.
- Claim uses a single atomic UPDATE ... WHERE id = (SELECT ... ORDER BY
  queued_at LIMIT 1) RETURNING so concurrent drains can't double-claim.
- Drain imports `_run_async_task_with_persistence` lazily to avoid a circular
  import with routers/chat.py. (#496: was `_execute_task_background`,
  deleted by #95; lazy-import target updated.)
- Credentials are never stored in backlog_metadata — only opaque references
  (subscription_id, user_id, mcp key id).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from models import ParallelTaskRequest, TaskExecutionStatus
from services.slot_service import get_slot_service
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


class BacklogService:
    """Service-level backlog operations (enqueue, drain, maintenance)."""

    def __init__(self) -> None:
        # Lazy-initialized to avoid import cycles at module load time.
        self._slot_service = None

    def _slots(self):
        if self._slot_service is None:
            self._slot_service = get_slot_service()
        return self._slot_service

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        agent_name: str,
        execution_id: str,
        request: ParallelTaskRequest,
        effective_timeout: int,
        user_id: Optional[int],
        user_email: Optional[str],
        subscription_id: Optional[str],
        x_source_agent: Optional[str],
        x_mcp_key_id: Optional[str],
        x_mcp_key_name: Optional[str],
        triggered_by: str,
        collaboration_activity_id: Optional[str],
        is_self_task: bool = False,
        self_task_activity_id: Optional[str] = None,
    ) -> bool:
        """Persist an async task request as a QUEUED backlog item.

        Returns False if the agent's backlog is already at its configured
        depth, otherwise True. The caller (chat router) must have already
        created the `schedule_executions` row via `create_task_execution`.
        """
        # Late import avoids a load-time cycle (database -> models -> ...).
        from database import db

        depth = db.get_queued_count(agent_name)
        cap = db.get_max_backlog_depth(agent_name)
        if depth >= cap:
            logger.info(
                f"[Backlog] Agent '{agent_name}' backlog full ({depth}/{cap}), rejecting"
            )
            return False

        metadata = {
            "message": request.message,
            "model": request.model,
            "allowed_tools": request.allowed_tools,
            "system_prompt": request.system_prompt,
            "timeout_seconds": effective_timeout,
            "max_turns": request.max_turns,
            "save_to_session": request.save_to_session,
            "user_message": request.user_message,
            "create_new_session": request.create_new_session,
            "chat_session_id": request.chat_session_id,
            "resume_session_id": request.resume_session_id,
            "inject_result": request.inject_result,
            "user_id": user_id,
            "user_email": user_email,
            "subscription_id": subscription_id,
            "x_source_agent": x_source_agent,
            "x_mcp_key_id": x_mcp_key_id,
            "x_mcp_key_name": x_mcp_key_name,
            "triggered_by": triggered_by,
            "collaboration_activity_id": collaboration_activity_id,
            "is_self_task": is_self_task,
            "self_task_activity_id": self_task_activity_id,
        }
        queued_at = utc_now_iso()
        ok = db.update_execution_to_queued(
            execution_id, json.dumps(metadata), queued_at
        )
        if not ok:
            # update_execution_to_queued is CAS-guarded on status == RUNNING
            # (#1082), so a False here means the row is gone or already terminal
            # (a stale/duplicate enqueue). Do not re-queue — there is no slot
            # held on this path (the slot acquire failed upstream), so returning
            # False is the clean rejection.
            logger.warning(
                f"[Backlog] Failed to transition execution {execution_id} "
                f"to queued for agent '{agent_name}' — row missing or already terminal"
            )
            return False

        logger.info(
            f"[Backlog] Agent '{agent_name}' queued execution {execution_id} "
            f"({depth + 1}/{cap})"
        )
        return True

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    async def drain_next(self, agent_name: str) -> bool:
        """Drain the oldest queued item for an agent, if any.

        Flow:
        1. Cheap COUNT to avoid spurious slot acquisitions.
        2. Acquire a slot up-front (using current agent capacity & timeout).
        3. Atomically claim the oldest queued row.
        4. On any failure after (2), release the slot we grabbed.
        5. Spawn `_run_async_task_with_persistence` on the reconstituted request.
           (#496: was `_execute_task_background`, deleted by #95.)

        Returns True if a row was drained, False otherwise.
        """
        from database import db

        if db.get_queued_count(agent_name) == 0:
            return False

        slots = self._slots()
        # #506: facade bypass — clamp to the fleet ceiling here (this path
        # calls slots.acquire_slot directly, skipping CapacityManager.acquire).
        # The same `max_parallel` local is reused by the real_slot re-acquire
        # at :193, so clamping once covers both.
        from services.settings_service import get_effective_max_parallel_tasks
        max_parallel = get_effective_max_parallel_tasks(agent_name)
        effective_timeout = db.get_execution_timeout(agent_name)

        # Sentinel execution_id for the slot — replaced by the real one after
        # we successfully claim a row. This avoids acquiring a slot under an
        # id that doesn't exist in the backlog (which would be confusing in
        # metrics) while still giving us a valid handle to release on failure.
        sentinel_id = f"drain-{agent_name}-{datetime.utcnow().timestamp()}"
        slot_ok = await slots.acquire_slot(
            agent_name=agent_name,
            execution_id=sentinel_id,
            max_parallel_tasks=max_parallel,
            message_preview="<backlog drain>",
            timeout_seconds=effective_timeout,
        )
        if not slot_ok:
            logger.debug(
                f"[Backlog] Drain skipped for '{agent_name}': no free slot"
            )
            return False

        claimed = db.claim_next_queued(agent_name)
        if not claimed:
            # Another drain beat us to it. Release the slot we grabbed.
            await slots.release_slot(agent_name, sentinel_id)
            return False

        execution_id = claimed["id"]
        # Swap the sentinel slot for the real execution_id so termination and
        # observability point at the right thing. Release sentinel, acquire
        # under the real id. On failure, fall back to releasing sentinel.
        await slots.release_slot(agent_name, sentinel_id)
        real_slot_ok = await slots.acquire_slot(
            agent_name=agent_name,
            execution_id=execution_id,
            max_parallel_tasks=max_parallel,
            message_preview=(claimed.get("message") or "")[:100],
            timeout_seconds=effective_timeout,
        )
        if not real_slot_ok:
            # Extremely rare: sentinel released and another request consumed
            # the free slot before we could re-acquire. Release the row back
            # to the queue so it drains on the next callback.
            logger.warning(
                f"[Backlog] Lost race re-acquiring slot for {execution_id}; "
                f"releasing row back to queue"
            )
            db.release_claim_to_queued(execution_id)
            return False

        try:
            metadata_json = claimed.get("backlog_metadata") or "{}"
            metadata = json.loads(metadata_json)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(
                f"[Backlog] Corrupt backlog_metadata on execution {execution_id}: {e}"
            )
            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=f"Backlog drain failed: corrupt metadata ({e})",
            )
            await slots.release_slot(agent_name, execution_id)
            return False

        try:
            await self._spawn_drain(agent_name, execution_id, metadata)
        except Exception as e:  # pragma: no cover - defensive
            # #496: stable log token "backlog_drain_spawn_failed" so log-based
            # detection (Vector / dashboards) can spot import drift or other
            # spawn-time regressions at fleet scale rather than per-row.
            logger.error(
                f"[Backlog] backlog_drain_spawn_failed agent='{agent_name}' "
                f"execution_id={execution_id} error={e!r}",
                exc_info=True,
            )
            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=f"Backlog drain spawn failed: {e}",
            )
            await slots.release_slot(agent_name, execution_id)
            return False

        return True

    async def _spawn_drain(
        self, agent_name: str, execution_id: str, metadata: Dict[str, Any]
    ) -> None:
        """Reconstruct a ParallelTaskRequest from metadata and spawn the
        existing background execution helper. Late-imported to avoid the
        chat.py <-> backlog_service.py cycle.

        #496: lazy-imports `_run_async_task_with_persistence` (post-#95
        replacement). The drain pre-acquires the slot under the real
        execution_id (drain_next), so the helper passes
        `slot_already_held=True` to TaskExecutionService, which releases the
        slot in its `finally` block.
        """
        from routers.chat import _run_async_task_with_persistence

        request = ParallelTaskRequest(
            message=metadata.get("message") or "",
            model=metadata.get("model"),
            allowed_tools=metadata.get("allowed_tools"),
            system_prompt=metadata.get("system_prompt"),
            timeout_seconds=metadata.get("timeout_seconds"),
            max_turns=metadata.get("max_turns"),
            async_mode=True,
            save_to_session=metadata.get("save_to_session") or False,
            user_message=metadata.get("user_message"),
            create_new_session=metadata.get("create_new_session") or False,
            chat_session_id=metadata.get("chat_session_id"),
            resume_session_id=metadata.get("resume_session_id"),
            inject_result=metadata.get("inject_result") or False,
        )

        task = asyncio.create_task(
            _run_async_task_with_persistence(
                agent_name=agent_name,
                request=request,
                execution_id=execution_id,
                collaboration_activity_id=metadata.get("collaboration_activity_id"),
                x_source_agent=metadata.get("x_source_agent"),
                user_id=metadata.get("user_id"),
                user_email=metadata.get("user_email"),
                subscription_id=metadata.get("subscription_id"),
                is_self_task=metadata.get("is_self_task") or False,
                self_task_activity_id=metadata.get("self_task_activity_id"),
            )
        )

        def _on_drain_done(t: asyncio.Task) -> None:
            if t.cancelled():
                logger.warning(
                    f"[Backlog] Drain task cancelled for {agent_name} execution {execution_id}"
                )
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    f"[Backlog] Drain task raised for {agent_name} "
                    f"execution {execution_id}: {exc}"
                )

        task.add_done_callback(_on_drain_done)
        logger.info(
            f"[Backlog] Drained execution {execution_id} for agent '{agent_name}'"
        )

    # ------------------------------------------------------------------
    # SlotService callback entry point
    # ------------------------------------------------------------------

    async def on_slot_released(self, agent_name: str) -> None:
        """Invoked by SlotService when a slot frees. Tries to drain one item."""
        try:
            await self.drain_next(agent_name)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"[Backlog] on_slot_released failed for '{agent_name}': {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Maintenance (called every 60s from main.py)
    # ------------------------------------------------------------------

    async def expire_stale(self, max_age_hours: float = 24) -> int:
        """Expire queued rows older than `max_age_hours` to FAILED."""
        from database import db

        n = db.expire_stale_queued(max_age_hours)
        if n:
            logger.info(f"[Backlog] Expired {n} stale queued rows (>{max_age_hours}h)")
        return n

    async def drain_orphans_all(self) -> int:
        """Drain one item per agent that still has queued work.

        Runs on the 60s safety-net tick. Picks up work that didn't trigger a
        release callback (e.g. after a restart, or a release path that fired
        outside an event loop). The subsequent drain cascade via callbacks
        handles the rest.
        """
        from database import db

        agents = db.list_agents_with_queued()
        drained = 0
        for agent_name in agents:
            try:
                if await self.drain_next(agent_name):
                    drained += 1
            except Exception as e:  # pragma: no cover - defensive
                logger.error(
                    f"[Backlog] orphan drain failed for '{agent_name}': {e}",
                    exc_info=True,
                )
        if drained:
            logger.info(f"[Backlog] orphan drain advanced {drained} agents")
        return drained

    async def cancel_all_backlog(self, agent_name: str, reason: str = "agent_deleted") -> int:
        """Cancel every queued row for an agent. Used on agent deletion."""
        from database import db

        n = db.cancel_queued_for_agent(agent_name, reason)
        if n:
            logger.info(
                f"[Backlog] Cancelled {n} queued rows for agent '{agent_name}' ({reason})"
            )
        return n


# Global singleton
_backlog_service: Optional[BacklogService] = None


def get_backlog_service() -> BacklogService:
    global _backlog_service
    if _backlog_service is None:
        _backlog_service = BacklogService()
    return _backlog_service
