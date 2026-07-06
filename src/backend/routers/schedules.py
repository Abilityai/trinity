"""
Schedule management routes for the Trinity backend.

Provides CRUD operations for agent schedules and execution history.

IMPORTANT: Route ordering matters! Static routes like /scheduler/status must be
defined BEFORE dynamic routes like /{name}/schedules to avoid FastAPI matching
"scheduler" as an agent name.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from typing import List
from datetime import datetime
import json
import os
import logging
import httpx

from models import (
    AgentSchedulesSummaryResponse,
    ExecutionResponse,
    ExecutionSummary,
    ScheduleAnalyticsResponse,
    ScheduleResponse,
    ScheduleUpdateRequest,
    User,
    WebhookStatusResponse,
)
from dependencies import get_current_user, get_authorized_agent, AuthorizedAgent, CurrentUser
from database import db, Schedule, ScheduleCreate, ScheduleExecution
from services.platform_audit_service import platform_audit_service, AuditEventType

_ANALYTICS_VALID_WINDOWS = frozenset({24, 168, 720})  # #868
# #1115: Overview/Schedules-tab scorecard windows → hours (matches the #1107
# Overview selector: 7 / 14 / 30 days).
_SUMMARY_WINDOW_HOURS = {"7d": 168, "14d": 336, "30d": 720}

logger = logging.getLogger(__name__)

# Dedicated scheduler URL (scheduler service runs on port 8001)
SCHEDULER_URL = os.getenv("SCHEDULER_URL", "http://scheduler:8001")

router = APIRouter(prefix="/api/agents", tags=["schedules"])


# =============================================================================
# SCHEDULER STATUS ENDPOINT (must be before /{name}/* routes!)
# =============================================================================

@router.get("/scheduler/status")
async def get_scheduler_status(
    current_user: User = Depends(get_current_user)
):
    """
    Get scheduler status (admin only).

    Returns information about the scheduler state and scheduled jobs
    from the dedicated scheduler service.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    # Call dedicated scheduler service for status
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SCHEDULER_URL}/status",
                timeout=10.0
            )

            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"Scheduler status failed: {response.status_code}")
                return {"running": False, "error": "Scheduler unavailable"}

    except httpx.ConnectError:
        logger.warning(f"Cannot connect to scheduler at {SCHEDULER_URL}")
        return {"running": False, "error": "Scheduler unavailable"}
    except httpx.TimeoutException:
        logger.warning(f"Timeout connecting to scheduler")
        return {"running": False, "error": "Scheduler timeout"}


# Schedule CRUD Endpoints


def _enforce_timeout_below_agent_cap(agent_name: str, requested_seconds: int) -> None:
    """#929 Approach A: refuse a schedule timeout above the agent cap.

    `agent_ownership.execution_timeout_seconds` is the hard ceiling.
    Raises HTTPException(400) with a structured `detail` dict so clients
    can branch on `detail["error"] == "schedule_timeout_exceeds_agent_cap"`.

    Callers must guard with `is not None` — after #913 both
    `ScheduleCreate.timeout_seconds` and `ScheduleUpdateRequest.timeout_seconds`
    are `Optional[int]`, and `None > int` raises TypeError in Python 3.
    """
    cap = db.get_execution_timeout(agent_name)
    if requested_seconds > cap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "schedule_timeout_exceeds_agent_cap",
                "message": (
                    f"Schedule timeout {requested_seconds}s exceeds agent "
                    f"execution_timeout_seconds {cap}s. Raise the agent cap "
                    f"first via PUT /api/agents/{agent_name}/timeout."
                ),
                "agent_cap_seconds": cap,
                "requested_seconds": requested_seconds,
            },
        )


@router.get("/{name}/schedules", response_model=List[ScheduleResponse])
async def list_agent_schedules(name: AuthorizedAgent):
    """List all schedules for an agent."""
    schedules = db.list_agent_schedules(name)
    return [ScheduleResponse(**s.model_dump()) for s in schedules]


@router.post("/{name}/schedules", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    name: str,
    schedule_data: ScheduleCreate,
    current_user: User = Depends(get_current_user)
):
    """Create a new schedule for an agent."""
    # Validate cron expression
    try:
        from croniter import croniter
        croniter(schedule_data.cron_expression)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cron expression: {str(e)}"
        )

    # #929: schedule timeout cannot exceed the agent cap. Validated here so
    # the operator gets the 400 at config time instead of a SIGKILL at run time.
    # After #913 the field is Optional — only enforce when the caller set it.
    if schedule_data.timeout_seconds is not None:
        _enforce_timeout_below_agent_cap(name, schedule_data.timeout_seconds)

    schedule = db.create_schedule(name, current_user.username, schedule_data)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create schedule - access denied or agent not found"
        )

    # Dedicated scheduler syncs from database automatically
    # No need to notify it directly - it will pick up new schedules on next sync cycle

    return ScheduleResponse(**schedule.model_dump())


@router.get(
    "/{name}/schedules/{schedule_id}/analytics",
    response_model=ScheduleAnalyticsResponse,
)
async def get_schedule_analytics(
    name: AuthorizedAgent,
    schedule_id: str,
    window_hours: int = 168,
):
    """Per-schedule execution analytics (#868).

    Counts, success rate, duration percentiles (p50/p95/p99),
    cost totals, tool-call distribution (top-5 by total wall time),
    and a daily timeline. UTC day boundaries.

    Args:
        window_hours: One of 24, 168 (7d), or 720 (30d). Default 7d.

    Authorisation:
        - `AuthorizedAgent` validates the caller can access this agent.
        - The DB layer additionally verifies `schedule_id` belongs to
          `name` (the path param) — caller cannot read analytics for
          another agent's schedule by guessing/stealing schedule_ids.
        - Soft-deleted schedules return 404 (matches `db.get_schedule`).

    Sampling:
        Counts and timeline use the full unsampled rowset. Percentiles
        and tool-call top-N are computed over the newest 5,000 success
        rows in the window — `sampled=true` in the response when the
        cap was hit. The UI surfaces this as a small badge.
    """
    if window_hours not in _ANALYTICS_VALID_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "window_hours must be one of "
                f"{sorted(_ANALYTICS_VALID_WINDOWS)}"
            ),
        )

    analytics = db.get_schedule_analytics(schedule_id, window_hours, name)
    if analytics is None:
        # Collapse missing / soft-deleted / cross-tenant into one 404
        # so existence of another agent's schedule can't be probed.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found",
        )
    return ScheduleAnalyticsResponse(**analytics)


@router.get(
    "/{name}/schedules/analytics-summary",
    response_model=AgentSchedulesSummaryResponse,
)
async def get_agent_schedules_summary(
    name: AuthorizedAgent,
    window: str = Query("7d", description="One of 7d, 14d, 30d"),
):
    """Per-schedule performance rollups for the agent over a window (#1115).

    ONE compact row per non-deleted schedule — consumed by BOTH the Overview
    "Schedules performance" section and the Schedules-tab inline stats from a
    single call (no N per-schedule round-trips). Zero-run schedules are
    included. Read-only / DB-sourced (renders when the agent is stopped). The
    #868 per-schedule deep view stays the drill-in target.

    NOTE: declared BEFORE `/{name}/schedules/{schedule_id}` so the literal
    `analytics-summary` segment isn't captured as a schedule_id (Invariant #4).
    """
    hours = _SUMMARY_WINDOW_HOURS.get(window)
    if hours is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"window must be one of {sorted(_SUMMARY_WINDOW_HOURS)}",
        )
    return AgentSchedulesSummaryResponse(**db.get_agent_schedules_summary(name, hours))


@router.get("/{name}/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    name: AuthorizedAgent,
    schedule_id: str
):
    """Get a specific schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    return ScheduleResponse(**schedule.model_dump())


@router.put("/{name}/schedules/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    name: str,
    schedule_id: str,
    updates: ScheduleUpdateRequest,
    current_user: User = Depends(get_current_user)
):
    """Update a schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    # Validate cron expression if being updated
    if updates.cron_expression:
        try:
            from croniter import croniter
            croniter(updates.cron_expression)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid cron expression: {str(e)}"
            )

    # #929: validate timeout against agent cap only when this PUT actually
    # touches `timeout_seconds`. exclude_unset semantics: a write that
    # doesn't include the field doesn't get re-checked (existing rows that
    # predate the validation stay editable).
    if updates.timeout_seconds is not None:
        _enforce_timeout_below_agent_cap(name, updates.timeout_seconds)

    # Build updates dict — use exclude_unset to distinguish "not provided" from "explicitly set to null"
    update_dict = updates.model_dump(exclude_unset=True)

    updated_schedule = db.update_schedule(schedule_id, current_user.username, update_dict)
    if not updated_schedule:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot update schedule - access denied"
        )

    # Dedicated scheduler syncs from database automatically
    # It will detect the update on next sync cycle

    return ScheduleResponse(**updated_schedule.model_dump())


@router.delete("/{name}/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    name: str,
    schedule_id: str,
    current_user: User = Depends(get_current_user)
):
    """Delete a schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    # Dedicated scheduler syncs from database automatically
    # It will detect the deletion on next sync cycle

    if not db.delete_schedule(schedule_id, current_user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete schedule - access denied"
        )


# Schedule Control Endpoints

@router.post("/{name}/schedules/{schedule_id}/enable")
async def enable_schedule(
    name: AuthorizedAgent,
    schedule_id: str
):
    """Enable a schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    # Update database - dedicated scheduler syncs automatically
    db.set_schedule_enabled(schedule_id, True)

    return {"status": "enabled", "schedule_id": schedule_id}


@router.post("/{name}/schedules/{schedule_id}/disable")
async def disable_schedule(
    name: AuthorizedAgent,
    schedule_id: str
):
    """Disable a schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    # Update database - dedicated scheduler syncs automatically
    db.set_schedule_enabled(schedule_id, False)

    return {"status": "disabled", "schedule_id": schedule_id}


@router.post("/{name}/schedules/{schedule_id}/trigger")
async def trigger_schedule(
    name: AuthorizedAgent,
    schedule_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Manually trigger a schedule execution.

    Delegates to the dedicated scheduler service which handles:
    - Distributed locking (prevents concurrent execution)
    - Execution record creation
    - Activity tracking (appears on Timeline)
    - Agent execution
    """
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    # Call dedicated scheduler service for manual trigger
    # The scheduler handles locking, activity tracking, and execution
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SCHEDULER_URL}/api/schedules/{schedule_id}/trigger",
                timeout=10.0
            )

            if response.status_code == 404:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Schedule not found in scheduler"
                )
            elif response.status_code == 503:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Scheduler service unavailable"
                )
            elif response.status_code != 200:
                logger.error(f"Scheduler trigger failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to trigger schedule"
                )

            result = response.json()
            logger.info(f"Manual trigger delegated to scheduler: {schedule_id}")

            await platform_audit_service.log(
                event_type=AuditEventType.EXECUTION,
                event_action="task_triggered",
                source="api",
                actor_user=current_user,
                target_type="agent",
                target_id=name,
                endpoint=f"/api/agents/{name}/schedules/{schedule_id}/trigger",
                details={
                    "schedule_id": schedule_id,
                    "schedule_name": result.get("schedule_name"),
                    "triggered_by": "manual",
                },
            )

            return {
                "status": "triggered",
                "schedule_id": schedule_id,
                "schedule_name": result.get("schedule_name"),
                "agent_name": result.get("agent_name"),
                "message": result.get("message", "Execution started")
            }

    except httpx.ConnectError:
        logger.error(f"Cannot connect to scheduler at {SCHEDULER_URL}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduler service unavailable"
        )
    except httpx.TimeoutException:
        logger.error(f"Timeout connecting to scheduler at {SCHEDULER_URL}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Scheduler service timeout"
        )


# Webhook Management Endpoints (WEBHOOK-001, #291)


def _build_webhook_url(request_base_url: str, token: str) -> str:
    """Construct the full webhook URL from base URL and token."""
    base = str(request_base_url).rstrip("/")
    return f"{base}/api/webhooks/{token}"


@router.post("/{name}/schedules/{schedule_id}/webhook", response_model=WebhookStatusResponse)
async def generate_webhook(
    name: AuthorizedAgent,
    schedule_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Generate (or regenerate) a webhook URL for a schedule.

    Creates an opaque 32-byte random token stored in the DB.
    Calling again replaces the old token, immediately invalidating the old URL.

    Agent access is validated by AuthorizedAgent first (uniform 404), so the
    schedule-not-found 404 below is only ever reached by an authorized accessor
    — never an existence oracle for a stranger (#186).
    """
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    token = db.generate_webhook_token(schedule_id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate webhook token"
        )

    webhook_url = _build_webhook_url(request.base_url, token)
    logger.info(f"Webhook token generated for schedule {schedule_id} by {current_user.username}")

    return WebhookStatusResponse(
        schedule_id=schedule_id,
        has_token=True,
        webhook_enabled=True,
        webhook_url=webhook_url,
    )


@router.get("/{name}/schedules/{schedule_id}/webhook", response_model=WebhookStatusResponse)
async def get_webhook_status(
    name: AuthorizedAgent,
    schedule_id: str,
    request: Request,
):
    """Get webhook configuration for a schedule.

    AuthorizedAgent gives a uniform 404 for a non-existent/inaccessible agent, so
    the schedule-not-found 404 is accessor-only — not an existence oracle (#186).
    """
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    status_data = db.get_webhook_status(schedule_id)
    if status_data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    token = status_data["webhook_token"]
    webhook_url = _build_webhook_url(request.base_url, token) if token else None

    return WebhookStatusResponse(
        schedule_id=schedule_id,
        has_token=status_data["has_token"],
        webhook_enabled=status_data["webhook_enabled"],
        webhook_url=webhook_url,
    )


@router.delete("/{name}/schedules/{schedule_id}/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_webhook(
    name: AuthorizedAgent,
    schedule_id: str,
    current_user: User = Depends(get_current_user)
):
    """Revoke the webhook token for a schedule, immediately invalidating the URL.

    AuthorizedAgent gives a uniform 404 for a non-existent/inaccessible agent, so
    the schedule-not-found 404 is accessor-only — not an existence oracle (#186).
    """
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    db.revoke_webhook_token(schedule_id)
    logger.info(f"Webhook token revoked for schedule {schedule_id} by {current_user.username}")


# Execution History Endpoints

@router.get("/{name}/schedules/{schedule_id}/executions", response_model=List[ExecutionResponse])
async def get_schedule_executions(
    name: AuthorizedAgent,
    schedule_id: str,
    limit: int = 50
):
    """Get execution history for a schedule."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Schedule not found"
        )

    executions = db.get_schedule_executions(schedule_id, limit=limit)
    return [ExecutionResponse(**e.model_dump()) for e in executions]


@router.get("/{name}/executions", response_model=List[ExecutionSummary])
async def get_agent_executions(
    name: AuthorizedAgent,
    limit: int = 50
):
    """Get execution summaries for an agent - optimized for list views.

    Returns lightweight ExecutionSummary objects that exclude large text fields:
    - response, error, tool_calls, execution_log

    For full execution details including response/error, use:
    GET /api/agents/{name}/executions/{id}

    PERF-001: Task List Performance Optimization
    Expected impact: 50-100x reduction in data transfer (10MB → 100KB)

    Note: The AuthorizedAgent dependency validates both agent existence (via db.get_agent_owner)
    and user access. This returns 404 if agent not found, 403 if no access, or execution list.
    """
    executions = db.get_agent_executions_summary(name, limit=limit)
    return executions


@router.get("/{name}/executions/{execution_id}", response_model=ExecutionResponse)
async def get_execution(
    name: AuthorizedAgent,
    execution_id: str
):
    """Get details of a specific execution."""
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found"
        )

    return ExecutionResponse(**execution.model_dump())


@router.get("/{name}/executions/{execution_id}/log")
async def get_execution_log(
    name: AuthorizedAgent,
    execution_id: str
):
    """
    Get the full execution log for a specific execution.

    Returns the raw Claude Code execution transcript as JSON array.
    This includes all tool calls, thinking, and responses.
    """
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found"
        )

    if not execution.execution_log:
        return {
            "execution_id": execution_id,
            "has_log": False,
            "log": None,
            "message": "No execution log available for this execution"
        }

    # Parse the JSON log for structured response
    try:
        log_data = json.loads(execution.execution_log)
    except json.JSONDecodeError:
        log_data = execution.execution_log

    return {
        "execution_id": execution_id,
        "agent_name": name,
        "has_log": True,
        "log": log_data,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "status": execution.status
    }
