"""
Schedule management routes for the Trinity backend.

Provides CRUD operations for agent schedules and execution history.

IMPORTANT: Route ordering matters! Static routes like /scheduler/status must be
defined BEFORE dynamic routes like /{name}/schedules to avoid FastAPI matching
"scheduler" as an agent name.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
import json
import os
import logging
import httpx

from models import User
from dependencies import get_current_user, get_authorized_agent, AuthorizedAgent, CurrentUser
from database import db, Schedule, ScheduleCreate, ScheduleExecution
from services.platform_audit_service import platform_audit_service, AuditEventType

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


# Request/Response Models

class ScheduleUpdateRequest(BaseModel):
    """Request model for updating a schedule."""
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    message: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    description: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Validation configuration (VALIDATE-001)
    validation_enabled: Optional[bool] = None
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: Optional[int] = None


class ScheduleResponse(BaseModel):
    """Response model for schedule data."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]
    timeout_seconds: int = 900
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: int = 120

    class Config:
        from_attributes = True


class ExecutionSummary(BaseModel):
    """Lightweight execution response for list views - excludes large text fields.

    Used by GET /api/agents/{name}/executions for fast list loading.
    Full details available via GET /api/agents/{name}/executions/{id}.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    triggered_by: str
    # Observability fields (small)
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    # Origin tracking (small) - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume (small) - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection (small) - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage (small) - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking (small) - VALIDATE-001
    business_status: Optional[str] = None  # pending_validation, validated, failed_validation, skipped
    validation_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B) - small JSON list
    compact_metadata: Optional[str] = None

    # EXCLUDED (large fields - fetch via /executions/{id}):
    # - response: Optional[str]      # Full response text
    # - error: Optional[str]         # Full error text
    # - tool_calls: Optional[str]    # JSON array of tool calls
    # - execution_log: Optional[str] # Full Claude Code transcript

    class Config:
        from_attributes = True


class ExecutionResponse(BaseModel):
    """Full response model for execution data - includes all fields.

    Used by GET /api/agents/{name}/executions/{id} for single execution details.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    response: Optional[str]
    error: Optional[str]
    triggered_by: str
    # Observability fields
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None  # Full Claude Code execution transcript (JSON)
    # Origin tracking - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking - VALIDATE-001
    business_status: Optional[str] = None
    validated_at: Optional[datetime] = None
    validation_execution_id: Optional[str] = None
    validates_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B)
    compact_metadata: Optional[str] = None

    class Config:
        from_attributes = True


# Schedule CRUD Endpoints

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

    schedule = db.create_schedule(name, current_user.username, schedule_data)
    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create schedule - access denied or agent not found"
        )

    # Dedicated scheduler syncs from database automatically
    # No need to notify it directly - it will pick up new schedules on next sync cycle

    return ScheduleResponse(**schedule.model_dump())


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

class WebhookStatusResponse(BaseModel):
    """Webhook configuration for a schedule."""
    schedule_id: str
    has_token: bool
    webhook_enabled: bool
    webhook_url: Optional[str] = None


def _build_webhook_url(request_base_url: str, token: str) -> str:
    """Construct the full webhook URL from base URL and token."""
    base = str(request_base_url).rstrip("/")
    return f"{base}/api/webhooks/{token}"


@router.post("/{name}/schedules/{schedule_id}/webhook", response_model=WebhookStatusResponse)
async def generate_webhook(
    name: str,
    schedule_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Generate (or regenerate) a webhook URL for a schedule.

    Creates an opaque 32-byte random token stored in the DB.
    Calling again replaces the old token, immediately invalidating the old URL.
    """
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

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
    """Get webhook configuration for a schedule."""
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
    name: str,
    schedule_id: str,
    current_user: User = Depends(get_current_user)
):
    """Revoke the webhook token for a schedule, immediately invalidating the URL."""
    schedule = db.get_schedule(schedule_id)
    if not schedule or schedule.agent_name != name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    if not db.can_user_access_agent(current_user.username, name):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

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
