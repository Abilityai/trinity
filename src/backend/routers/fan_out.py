"""
Fan-out router — parallel task dispatch and result collection (FANOUT-001).

POST /api/agents/{name}/fan-out
    Dispatches N independent tasks to an agent in parallel, waits for results,
    and returns aggregated per-task results.
"""

import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from dependencies import get_current_user, get_authorized_agent
from models import User
from services.fan_out_service import (
    FanOutService,
    FanOutTaskInput,
    get_fan_out_service,
)
from services import idempotency_service
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["fan-out"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_TASKS = 50
MAX_CONCURRENCY = 10


class FanOutTask(BaseModel):
    """A single task in a fan-out request."""
    id: str
    message: str = Field(..., min_length=1, max_length=100_000)

    @field_validator("id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(
                f"Task ID must be 1-64 alphanumeric characters, hyphens, or underscores: '{v}'"
            )
        return v


class FanOutRequest(BaseModel):
    """Request model for fan-out parallel task execution."""
    tasks: List[FanOutTask]
    agent: str = "self"
    # Optional overall fan-out deadline. When None, no outer deadline is
    # applied — each sub-task is still bounded by the target agent's
    # configured execution_timeout_seconds (TIMEOUT-001).
    timeout_seconds: Optional[int] = None
    max_concurrency: int = 3
    policy: str = "best-effort"
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("tasks")
    @classmethod
    def validate_tasks(cls, v: List[FanOutTask]) -> List[FanOutTask]:
        if len(v) == 0:
            raise ValueError("At least one task is required")
        if len(v) > MAX_TASKS:
            raise ValueError(f"Maximum {MAX_TASKS} tasks per fan-out")
        # Check for duplicate IDs
        ids = [t.id for t in v]
        if len(ids) != len(set(ids)):
            dupes = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"Duplicate task IDs: {set(dupes)}")
        return v

    @field_validator("max_concurrency")
    @classmethod
    def validate_concurrency(cls, v: int) -> int:
        if v < 1 or v > MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 10 or v > 3600:
            raise ValueError("timeout_seconds must be between 10 and 3600")
        return v

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, v: str) -> str:
        if v != "best-effort":
            raise ValueError("Only 'best-effort' policy is supported")
        return v


class FanOutTaskResponse(BaseModel):
    """Result of a single fan-out subtask."""
    id: str
    status: str
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_id: Optional[str] = None
    cost: Optional[float] = None
    context_used: Optional[int] = None
    duration_ms: Optional[int] = None


class FanOutResponse(BaseModel):
    """Aggregated fan-out result."""
    fan_out_id: str
    status: str
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResponse]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/{name}/fan-out", response_model=FanOutResponse)
async def fan_out(
    request: FanOutRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
    x_mcp_key_id: Optional[str] = Header(None),
    x_mcp_key_name: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None),
):
    """
    Fan out N independent tasks to an agent in parallel and collect results.

    Each subtask follows the standard execution path — all executions appear
    on the dashboard with full observability (cost, tokens, logs, origin).

    The `agent` field must be "self" or match the path agent name for v1.
    """
    # Validate agent targeting (v1: self-only)
    if request.agent not in ("self", name):
        raise HTTPException(
            status_code=400,
            detail=f"Fan-out target must be 'self' or '{name}'. Cross-agent fan-out is not yet supported.",
        )

    # RELIABILITY-006 (#525): idempotency over the whole batch — a duplicate
    # fan-out replays the original aggregated result instead of re-dispatching
    # all N subtasks. Optional header; absent → no dedup.
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(name), idempotency_key
    )
    if idem.replay:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="idempotent_replay",
            source="mcp" if x_via_mcp else "api",
            actor_user=current_user if not x_source_agent else None,
            actor_agent_name=x_source_agent,
            target_type="agent",
            target_id=name,
            endpoint=f"/api/agents/{name}/fan-out",
            details={"idempotency_key": idempotency_key, "in_flight": idem.in_flight},
        )
        if idem.in_flight:
            raise HTTPException(
                status_code=409,
                detail="A fan-out with this Idempotency-Key is still being processed.",
            )
        if idem.snapshot is not None:
            return JSONResponse(
                content=idem.snapshot, headers={"X-Idempotent-Replay": "true"}
            )

    service = get_fan_out_service()

    # Convert to service-layer task inputs
    task_inputs = [
        FanOutTaskInput(id=t.id, message=t.message)
        for t in request.tasks
    ]

    # Determine source agent for origin tracking
    source_agent = x_source_agent or (name if request.agent == "self" else None)

    try:
        result = await service.execute(
            agent_name=name,
            tasks=task_inputs,
            max_concurrency=request.max_concurrency,
            timeout_seconds=request.timeout_seconds,
            model=request.model,
            system_prompt=request.system_prompt,
            allowed_tools=request.allowed_tools,
            source_user_id=current_user.id,
            source_user_email=current_user.email,
            source_agent_name=source_agent,
            source_mcp_key_id=x_mcp_key_id,
            source_mcp_key_name=x_mcp_key_name,
        )
    except Exception:
        idempotency_service.fail(idem)
        raise

    response = FanOutResponse(
        fan_out_id=result.fan_out_id,
        status=result.status,
        total=result.total,
        completed=result.completed,
        failed=result.failed,
        results=[
            FanOutTaskResponse(
                id=r.id,
                status=r.status,
                response=r.response,
                error=r.error,
                error_code=r.error_code,
                execution_id=r.execution_id,
                cost=r.cost,
                context_used=r.context_used,
                duration_ms=r.duration_ms,
            )
            for r in result.results
        ],
    )

    # Store the aggregated batch result so a duplicate replays it (#525).
    idempotency_service.complete(idem, result.fan_out_id, response.model_dump())
    return response
