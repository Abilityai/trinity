"""
Internal endpoints with shared-secret authentication (C-003).

These endpoints are called by:
- Agent containers on the Docker network to communicate back to the backend
- Dedicated scheduler service (trinity-scheduler) for task execution and activity tracking

Security: Requires X-Internal-Secret header matching INTERNAL_API_SECRET env var.
Falls back to SECRET_KEY if INTERNAL_API_SECRET is not set.
"""
import asyncio
import os
import hmac
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
import logging

from models import ActivityState, ActivityType
from services.activity_service import activity_service
from services.task_execution_service import get_task_execution_service

logger = logging.getLogger(__name__)


def _get_internal_secret() -> str:
    """Get the internal API shared secret."""
    from config import SECRET_KEY
    return os.getenv("INTERNAL_API_SECRET") or SECRET_KEY


async def verify_internal_secret(request: Request):
    """
    Dependency to verify internal API shared secret (C-003).

    Checks the X-Internal-Secret header against the configured secret.
    """
    secret = _get_internal_secret()
    provided = request.headers.get("X-Internal-Secret", "")
    if not provided or not hmac.compare_digest(provided, secret):
        logger.warning(f"Internal API request rejected: invalid or missing X-Internal-Secret from {request.client.host}")
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing internal API secret"
        )


router = APIRouter(
    prefix="/api/internal",
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)


@router.get("/health")
async def internal_health():
    """Internal health check for agent containers."""
    return {"status": "ok"}


@router.get("/agents/{agent_name}/sync-health-status")
async def internal_agent_sync_health(agent_name: str):
    """#389: lightweight read used by the dedicated scheduler before dispatching.

    Returns both the per-agent `freeze_schedules_if_sync_failing` flag and
    whether the current sync state would trip it. The scheduler multiplies
    the two to decide whether to skip the fire.
    """
    from database import db as _db
    freeze_flag = _db.get_freeze_schedules_if_sync_failing(agent_name)
    state = _db.get_sync_state(agent_name) or {}
    failing = (
        state.get("last_sync_status") == "failed"
        and (state.get("consecutive_failures") or 0) >= 3
    )
    return {
        "agent_name": agent_name,
        "freeze_schedules_if_sync_failing": bool(freeze_flag),
        "sync_failing": bool(failing),
        "should_freeze": bool(freeze_flag and failing),
        "consecutive_failures": state.get("consecutive_failures") or 0,
    }


# =============================================================================
# Activity Tracking Endpoints (for dedicated scheduler)
# =============================================================================

class ActivityTrackRequest(BaseModel):
    """Request model for tracking activity start."""
    agent_name: str
    activity_type: str  # e.g., "schedule_start"
    user_id: Optional[int] = None
    triggered_by: str = "schedule"  # schedule, manual, user, agent, system
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None


class ActivityCompleteRequest(BaseModel):
    """Request model for completing an activity."""
    status: str = ActivityState.COMPLETED  # ActivityState: completed, failed
    details: Optional[Dict] = None
    error: Optional[str] = None


@router.post("/activities/track")
async def track_activity(request: ActivityTrackRequest):
    """
    Track the start of a new activity.

    Called by the dedicated scheduler when a cron-triggered execution starts.
    Creates an activity record and broadcasts via WebSocket.

    Returns:
        activity_id: UUID of the created activity
    """
    try:
        # Map string to ActivityType enum
        activity_type_map = {
            "schedule_start": ActivityType.SCHEDULE_START,
            "schedule_end": ActivityType.SCHEDULE_END,
            "chat_start": ActivityType.CHAT_START,
            "chat_end": ActivityType.CHAT_END,
            "agent_collaboration": ActivityType.AGENT_COLLABORATION,
        }

        activity_type = activity_type_map.get(request.activity_type)
        if not activity_type:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid activity_type: {request.activity_type}"
            )

        activity_id = await activity_service.track_activity(
            agent_name=request.agent_name,
            activity_type=activity_type,
            user_id=request.user_id,
            triggered_by=request.triggered_by,
            related_execution_id=request.related_execution_id,
            details=request.details
        )

        logger.info(f"Activity tracked: {activity_id} for agent {request.agent_name} ({request.activity_type})")

        return {
            "activity_id": activity_id,
            "agent_name": request.agent_name,
            "activity_type": request.activity_type
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to track activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/activities/{activity_id}/complete")
async def complete_activity(activity_id: str, request: ActivityCompleteRequest):
    """
    Mark an activity as completed or failed.

    Called by the dedicated scheduler when an execution completes.
    Updates the activity record and broadcasts via WebSocket.
    """
    try:
        success = await activity_service.complete_activity(
            activity_id=activity_id,
            status=request.status,
            details=request.details,
            error=request.error
        )

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Activity not found: {activity_id}"
            )

        logger.info(f"Activity completed: {activity_id} ({request.status})")

        return {
            "activity_id": activity_id,
            "status": request.status,
            "completed": True
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to complete activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Task Execution Endpoint (for dedicated scheduler)
# =============================================================================

class InternalTaskExecutionRequest(BaseModel):
    """Request model for internal task execution via TaskExecutionService."""
    agent_name: str
    message: str
    triggered_by: str = "schedule"
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None  # TIMEOUT-001: None = use agent's config (default 15 min)
    allowed_tools: Optional[List[str]] = None
    execution_id: Optional[str] = None
    async_mode: bool = False
    # #171: optional schedule metadata surfaced in the agent's execution context block.
    schedule_name: Optional[str] = None
    schedule_cron: Optional[str] = None
    schedule_next_run: Optional[str] = None
    attempt: Optional[int] = None


def _schedule_context_from(request: "InternalTaskExecutionRequest") -> Optional[Dict]:
    """Build the schedule_context dict passed to TaskExecutionService, or None."""
    if not (request.schedule_name or request.schedule_cron or request.schedule_next_run):
        return None
    return {
        "name": request.schedule_name,
        "cron": request.schedule_cron,
        "next_run": request.schedule_next_run,
    }


@router.post("/execute-task")
async def execute_task_internal(request: InternalTaskExecutionRequest):
    """
    Execute a task via the unified TaskExecutionService.

    Called by the dedicated scheduler for cron-triggered and manually-triggered
    schedule executions. Routes through the same code path as authenticated
    /task and public chat endpoints, ensuring consistent slot management,
    activity tracking, credential sanitization, and dashboard visibility.

    The scheduler creates the execution record before calling this endpoint
    and passes the execution_id so the service skips record creation.

    When async_mode=True (SCHED-ASYNC-001), the endpoint spawns a background
    task and returns immediately with {"status": "accepted"}. The scheduler
    then polls the DB for completion instead of holding the HTTP connection.
    """
    task_service = get_task_execution_service()

    if request.async_mode:
        # Fire-and-forget: spawn background task, return immediately
        asyncio.create_task(_execute_task_internal_background(
            task_service, request
        ))
        return {
            "status": "accepted",
            "execution_id": request.execution_id,
            "async_mode": True,
        }

    # Synchronous mode (default, backward compatible)
    try:
        result = await task_service.execute_task(
            agent_name=request.agent_name,
            message=request.message,
            triggered_by=request.triggered_by,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            allowed_tools=request.allowed_tools,
            execution_id=request.execution_id,
            schedule_context=_schedule_context_from(request),
            attempt=request.attempt,
        )

        return {
            "execution_id": result.execution_id,
            "status": result.status,
            "response": result.response,
            "cost": result.cost,
            "context_used": result.context_used,
            "context_max": result.context_max,
            "session_id": result.session_id,
            "error": result.error,
        }

    except Exception as e:
        logger.error(f"Internal task execution failed for {request.agent_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_task_internal_background(task_service, request: InternalTaskExecutionRequest):
    """
    Background coroutine for async task execution (SCHED-ASYNC-001).

    TaskExecutionService handles all lifecycle: slot acquisition, activity
    tracking, DB updates, and cleanup. This wrapper logs outcomes and ensures
    execution status is updated on any uncaught exception (fixes issue #90).
    """
    from database import db
    from models import TaskExecutionStatus

    try:
        result = await task_service.execute_task(
            agent_name=request.agent_name,
            message=request.message,
            triggered_by=request.triggered_by,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            allowed_tools=request.allowed_tools,
            execution_id=request.execution_id,
            schedule_context=_schedule_context_from(request),
            attempt=request.attempt,
        )
        logger.info(
            f"Async task completed for {request.agent_name}: "
            f"status={result.status}, execution_id={result.execution_id}"
        )
    except Exception as e:
        # If an exception escapes TaskExecutionService, ensure execution is marked failed
        # to prevent stuck 'running' status (fixes issue #90)
        error_msg = f"Background execution failed: {e}"
        logger.error(
            f"Async task failed for {request.agent_name}: {e}"
        )
        if request.execution_id:
            try:
                existing = db.get_execution(request.execution_id)
                if existing and existing.status not in (
                    TaskExecutionStatus.SUCCESS,
                    TaskExecutionStatus.FAILED,
                    TaskExecutionStatus.CANCELLED,
                ):
                    db.update_execution_status(
                        execution_id=request.execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                    )
                    logger.info(f"Updated execution {request.execution_id} to FAILED")
            except Exception as db_err:
                logger.error(f"Failed to update execution status: {db_err}")


# =============================================================================
# Validation Endpoints (VALIDATE-001)
# =============================================================================

class ValidateExecutionRequest(BaseModel):
    """Request model for triggering execution validation."""
    execution_id: str
    agent_name: str
    schedule_id: str
    original_message: str
    execution_response: str
    custom_prompt: Optional[str] = None
    timeout_seconds: int = 120


@router.post("/validate-execution")
async def validate_execution(request: ValidateExecutionRequest):
    """Trigger validation for a completed execution.

    Called by the scheduler service after a successful execution
    when validation is enabled for the schedule.

    Returns:
        dict with validation status and result.
    """
    from services.validation_service import get_validation_service

    logger.info(
        f"Received validation request for execution {request.execution_id} "
        f"on agent '{request.agent_name}'"
    )

    validation_service = get_validation_service()

    # Run validation in background to not block the scheduler
    asyncio.create_task(
        _run_validation_background(
            validation_service=validation_service,
            execution_id=request.execution_id,
            agent_name=request.agent_name,
            schedule_id=request.schedule_id,
            original_message=request.original_message,
            execution_response=request.execution_response,
            custom_prompt=request.custom_prompt,
            timeout_seconds=request.timeout_seconds,
        )
    )

    return {
        "status": "accepted",
        "message": f"Validation triggered for execution {request.execution_id}",
    }


async def _run_validation_background(
    validation_service,
    execution_id: str,
    agent_name: str,
    schedule_id: str,
    original_message: str,
    execution_response: str,
    custom_prompt: str = None,
    timeout_seconds: int = 120,
):
    """Run validation in background.

    This allows the internal endpoint to return immediately while
    validation runs asynchronously.
    """
    try:
        result = await validation_service.validate_execution(
            execution_id=execution_id,
            agent_name=agent_name,
            schedule_id=schedule_id,
            original_message=original_message,
            execution_response=execution_response,
            custom_prompt=custom_prompt,
            timeout_seconds=timeout_seconds,
        )
        logger.info(
            f"Validation completed for execution {execution_id}: "
            f"status={result.status.value}, summary={result.summary}"
        )
    except Exception as e:
        logger.error(f"Validation failed for execution {execution_id}: {e}")
