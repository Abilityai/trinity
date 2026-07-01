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
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from typing import Optional, Dict
import logging

from database import db
from models import (
    ActivityCompleteRequest,
    ActivityTrackRequest,
    ActivityType,
    InternalAuditRequest,
    InternalTaskExecutionRequest,
    ShareFileRequest,
    ShareFileResponse,
    TaskExecutionStatus,
    ValidateExecutionRequest,
)
from services.activity_service import activity_service
from services.task_execution_service import get_task_execution_service
from services.platform_audit_service import platform_audit_service, AuditEventType
from services import idempotency_service

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


# ---------------------------------------------------------------------------
# Scheduler pre-check (#454, SCHED-COND-001)
# ---------------------------------------------------------------------------


@router.post("/agents/{agent_name}/pre-check")
async def internal_agent_pre_check(agent_name: str):
    """Run the agent's optional pre-check hook (SCHED-COND-001 / #454).

    Thin passthrough — all logic lives in
    ``services/pre_check_service.py`` (Invariant #1: Router → Service
    → DB). See that module for the full contract.
    """
    from services.pre_check_service import run_pre_check, AgentNotFound

    try:
        return await run_pre_check(agent_name)
    except AgentNotFound:
        raise HTTPException(status_code=404, detail="Agent not found")


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
async def execute_task_internal(
    request: InternalTaskExecutionRequest,
    idempotency_key: Optional[str] = Header(None),
):
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
    # #748: warming-up gate. Until startup orphan-recovery finishes, refuse new
    # task starts so we don't ZADD a capacity slot for an execution row that
    # recovery is about to flip to FAILED (which would leak the slot until
    # TTL). The scheduler treats 503 as transient and retries.
    from services.cleanup_service import is_startup_recovery_complete
    if not is_startup_recovery_complete():
        raise HTTPException(
            status_code=503,
            detail="Backend warming up — startup execution recovery still in progress. Retry.",
        )

    task_service = get_task_execution_service()

    # RELIABILITY-006 (#525): idempotency gate. The scheduler sends a
    # deterministic key (sched:{execution_id}); a network blip + resend of the
    # same dispatch resolves to the same key and is short-circuited here instead
    # of double-dispatching. Optional header — absent → no dedup.
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(request.agent_name), idempotency_key
    )
    if idem.replay:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="idempotent_replay",
            source="scheduler",
            target_type="agent",
            target_id=request.agent_name,
            endpoint="/api/internal/execute-task",
            details={
                "idempotency_key": idempotency_key,
                "execution_id": idem.execution_id,
                "in_flight": idem.in_flight,
            },
        )
        if idem.snapshot is not None:
            return idem.snapshot
        # Still in flight (or no snapshot stored) — acknowledge without
        # re-dispatching; the scheduler polls the DB by execution_id.
        return {
            "status": "accepted",
            "execution_id": idem.execution_id or request.execution_id,
            "async_mode": bool(request.async_mode),
            "idempotent_replay": True,
        }
    idempotency_service.attach_execution(idem, request.execution_id)

    # Audit schedule-triggered execution (source=scheduler, actor=system)
    if request.triggered_by == "schedule":
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="schedule_triggered",
            source="scheduler",
            target_type="agent",
            target_id=request.agent_name,
            endpoint="/api/internal/execute-task",
            details={
                "execution_id": request.execution_id,
                "schedule_id": getattr(request, "schedule_id", None),
                "schedule_name": getattr(request, "schedule_name", None),
                "async_mode": bool(request.async_mode),
                "attempt": request.attempt,
            },
        )

    if request.async_mode:
        # Fire-and-forget: spawn background task, return immediately
        asyncio.create_task(_execute_task_internal_background(
            task_service, request
        ))
        accepted = {
            "status": "accepted",
            "execution_id": request.execution_id,
            "async_mode": True,
        }
        # Mark the claim completed with the accepted ack so a resend replays it
        # rather than re-dispatching (the background task owns the real result).
        idempotency_service.complete(idem, request.execution_id, accepted)
        return accepted

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

        result_payload = {
            "execution_id": result.execution_id,
            "status": result.status,
            "response": result.response,
            "cost": result.cost,
            "context_used": result.context_used,
            "context_max": result.context_max,
            "session_id": result.session_id,
            "error": result.error,
        }
        idempotency_service.complete(idem, result.execution_id, result_payload)
        return result_payload

    except Exception as e:
        logger.error(f"Internal task execution failed for {request.agent_name}: {e}")
        idempotency_service.fail(idem)
        raise HTTPException(status_code=500, detail=str(e))


async def _execute_task_internal_background(task_service, request: InternalTaskExecutionRequest):
    """
    Background coroutine for async task execution (SCHED-ASYNC-001).

    TaskExecutionService handles all lifecycle: slot acquisition, activity
    tracking, DB updates, and cleanup. This wrapper logs outcomes and ensures
    execution status is updated on any uncaught exception (fixes issue #90).
    """
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
    except asyncio.CancelledError:
        # Python 3.11+: CancelledError is BaseException, bypasses except Exception.
        # On backend shutdown, in-flight background tasks are cancelled; close the
        # record synchronously so cleanup_service doesn't inflate duration (#767).
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
                        error="Execution cancelled (backend shutdown)",
                    )
                    logger.info(f"Updated execution {request.execution_id} to FAILED on cancel")
            except Exception as db_err:
                logger.error(f"Failed to update execution status on cancel: {db_err}")
        raise

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


# =============================================================================
# Audit Logging Endpoint (SEC-001 Phase 3 — MCP server integration)
# =============================================================================


@router.post("/audit")
async def log_audit_entry(request: InternalAuditRequest):
    """
    Log an audit entry from the MCP server (SEC-001 Phase 3).

    Called by the MCP server after each tool execution to record
    tool calls with full MCP auth context (key_id, scope, agent_name).
    Uses the internal shared-secret auth (C-003), not JWT.
    """
    try:
        event_type_map = {e.value: e for e in AuditEventType}
        event_type = event_type_map.get(request.event_type)
        if not event_type:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid event_type: {request.event_type}"
            )

        event_id = await platform_audit_service.log(
            event_type=event_type,
            event_action=request.event_action,
            source=request.source,
            mcp_key_id=request.mcp_key_id,
            mcp_key_name=request.mcp_key_name,
            mcp_scope=request.mcp_scope,
            actor_agent_name=request.actor_agent_name,
            target_type=request.target_type,
            target_id=request.target_id,
            request_id=request.request_id,
            details=request.details,
        )

        return {"event_id": event_id, "status": "logged"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to log audit entry: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Agent Shared Files (outbound — FILES-001 Step 3)
# =============================================================================

@router.post("/agent-files/share", response_model=ShareFileResponse)
async def agent_files_share(payload: ShareFileRequest):
    """
    Mint a public download URL for a file the agent wrote to its publish dir.

    Authentication: X-Internal-Secret (already enforced by router dependency).
    Agent identity: carried by `payload.agent_name`. The agent server is
    responsible for passing its own name here — same trust model as
    /internal/execute-task (forging requires the internal secret).
    """
    from services.agent_shared_files_service import create_share

    result = await create_share(
        agent_name=payload.agent_name,
        filename=payload.filename,
        display_name=payload.display_name,
        expires_in=payload.expires_in,
        created_by=payload.agent_name,
    )
    return ShareFileResponse(**result)


# ---------------------------------------------------------------------------
# MCP-exposed agents poll endpoint (#846)
# ---------------------------------------------------------------------------


@router.get("/mcp-exposed-agents")
async def mcp_exposed_agents():
    """Authoritative list of agents to expose as dedicated MCP tools (#846).

    Polled by the Trinity MCP server (over the existing X-Internal-Secret path)
    every ~20s. The backend is the single source of truth for the deterministic,
    collision-free ``tool_name`` per exposed agent (computed over the full set),
    so restarts/replicas of the MCP server agree and there is no client-side
    slug split-brain.

    ``description`` is intentionally **name-only** (see ``build_tool_description``):
    the dedicated tool's description is advertised globally to every non-connector
    MCP session, so it must not carry per-agent metadata (the agent's
    ``trinity.template`` label was a cross-tenant leak + injection surface, #846).
    """
    from services.agent_service.mcp_tool_names import (
        compute_tool_names,
        build_tool_description,
    )

    exposed = db.get_mcp_exposed_agents()
    names = [a["agent_name"] for a in exposed]
    tool_names = compute_tool_names(names)

    return {
        "agents": [
            {
                "agent_name": name,
                "tool_name": tool_names[name],
                "description": build_tool_description(name),
            }
            for name in names
        ]
    }
