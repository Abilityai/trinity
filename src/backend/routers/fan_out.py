# mcp: chat.ts (fan_out)
"""
Fan-out router — parallel task dispatch and result collection (FANOUT-001).

POST /api/agents/{name}/fan-out
    Dispatches N independent tasks to an agent in parallel, waits for results,
    and returns aggregated per-task results.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import JSONResponse

from dependencies import get_current_user, get_authorized_agent
from database import db
from models import (
    FanOutBatchStatus,
    FanOutRequest,
    FanOutResponse,
    FanOutTaskResponse,
    User,
)
from services.fan_out_service import (
    FanOutService,
    build_fan_out_batch_status,
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
            # #2670: the SAME shape `/chat` and `/task` return, not a bare
            # string. Those two carry `{error, message, execution_id}` and the
            # MCP client reads the id off it (#2661) to answer with a receipt
            # instead of `API error (409)`; a string here meant `fan_out` could
            # never benefit from the machinery that already exists. The id is
            # the BATCH id — attached below the moment it is minted — because
            # that is what `GET /{name}/fan-out/{fan_out_id}` resolves.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A fan-out with this Idempotency-Key is still being processed.",
                    "execution_id": idem.execution_id,
                },
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
            # #2389: the credential actually presented, never the forgeable X-MCP-Key-* headers.
            source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
            source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
            # #2670: record the batch id on the idempotency claim as soon as it
            # exists rather than only at `complete()`. A fan-out runs longer
            # than any single task by construction, so the window in which a
            # concurrent duplicate can arrive — and in which this very call's
            # own gateway can give up — is the whole batch. Attaching at the end
            # records it exactly when nobody needs it any more.
            on_started=lambda fid: idempotency_service.attach_execution(idem, fid),
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


# ---------------------------------------------------------------------------
# #2670 — the batch's read surface
# ---------------------------------------------------------------------------

# Server-minted (`fo_` + `secrets.token_urlsafe(12)`), so this is a shape check
# and not an authorization one: it stops a malformed id from reaching the DB,
# nothing more. The access decision is `get_authorized_agent` on the path.
_FAN_OUT_ID_RE = re.compile(r"^fo_[A-Za-z0-9_-]{1,64}$")


@router.get("/{name}/fan-out/{fan_out_id}", response_model=FanOutBatchStatus)
async def get_fan_out_status(
    fan_out_id: str,
    name: str = Depends(get_authorized_agent),
):
    """Read one fan-out batch back from its execution rows (#2670).

    `POST /fan-out` builds its aggregate in memory and returns it exactly once.
    A caller whose HTTP call was killed by its own gateway timeout therefore had
    nothing to poll while N executions kept running — the third route of the
    #914 class, and the one that exceeds the ceiling most reliably, since a
    batch runs longer than any single task in it by construction.

    Deliberately reads `schedule_executions`, NOT the idempotency snapshot. The
    snapshot is written by `complete()` — i.e. only once the whole batch has
    finished — so it cannot answer the question a timed-out caller is actually
    asking, which is *what is happening right now*. The rows can, because
    `fan_out_id` is stamped on each of them at dispatch (FANOUT-001).

    Enumeration-safe (Invariant #8): a malformed id, an unknown id, and an id
    belonging to another agent are one uniform 404. The id is unguessable, so
    this costs a caller nothing it could otherwise have had.
    """
    if not _FAN_OUT_ID_RE.match(fan_out_id or ""):
        raise HTTPException(status_code=404, detail="Fan-out not found")

    rows = db.get_fan_out_executions(name, fan_out_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Fan-out not found")

    return build_fan_out_batch_status(name, fan_out_id, rows)
