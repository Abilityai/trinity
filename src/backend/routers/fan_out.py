# mcp: chat.ts (fan_out)
"""
Fan-out router — parallel task dispatch and result collection (FANOUT-001).

POST /api/agents/{name}/fan-out
    Dispatches N independent tasks to an agent in parallel and returns
    aggregated per-task results. With `async_mode` the batch is accepted and the
    caller polls the status endpoint instead of holding the connection (#2524).

GET /api/agents/{name}/fan-out/{fan_out_id}
    Aggregate for a batch, rebuilt from its execution rows. Answers after the
    dispatching request is gone, which is what makes `async_mode` usable — and
    is the source of truth after a deadline, since a deadline stops the WAIT,
    not the subtasks (#2524).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import JSONResponse

from dependencies import get_current_user, get_authorized_agent
from models import FanOutRequest, FanOutResponse, FanOutTaskResponse, User
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
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/{name}/fan-out", response_model=FanOutResponse)
async def fan_out(
    request: FanOutRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
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
            async_mode=bool(request.async_mode),
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
            # #2389: the credential actually presented, never the forgeable X-MCP-Key-* headers.
            source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
            source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
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

    # Store the aggregated batch result so a duplicate replays it (#525). On the
    # async path that snapshot is the ACCEPTED receipt, not the outcome — which
    # is the right thing to replay, since re-dispatching N subtasks is exactly
    # what the key exists to prevent.
    idempotency_service.complete(idem, result.fan_out_id, response.model_dump())
    return response


@router.get("/{name}/fan-out/{fan_out_id}", response_model=FanOutResponse)
async def fan_out_status(
    fan_out_id: str,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Aggregate for one fan-out batch, rebuilt from its execution rows (#2524).

    Authorization is the agent's — `get_authorized_agent` has already checked
    the caller may act on `{name}` — plus an explicit check that the batch
    belongs to that agent, so a valid `fan_out_id` from another agent cannot be
    read through an agent the caller happens to own.
    """
    service = get_fan_out_service()
    result = service.get_status(fan_out_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Fan-out batch not found")
    if not service.batch_belongs_to(fan_out_id, name):
        raise HTTPException(status_code=404, detail="Fan-out batch not found")

    return FanOutResponse(
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
