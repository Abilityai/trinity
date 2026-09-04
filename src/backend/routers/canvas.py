"""Agent canvas API (ent#438).

A **canvas** is a durable surface an agent renders onto and keeps current —
addressed by ``(agent, canvas_id)``, updated in place, and rendered by the
Workspace and Agent Detail through the shared ``components/reports/`` dispatch.
Reports (#918) stay the immutable half: a thing published once and accumulated.

Thin by contract (Invariant #1): validation and the derived staleness live in
``services/canvas_service.py``; this module is auth, the HTTP error map, and
the audience projection.

**Write is self-gated**, exactly as reports are: ``AuthorizedAgent`` proves the
key's owner can access the path agent, but does NOT stop an agent-scoped key
from writing as a *sibling* agent the same owner shares. So an agent-scoped
caller's bound ``agent_name`` must equal the path agent — otherwise one agent
could paint on another's canvas, which is a disclosure surface as well as a
correctness one because a `roster` canvas is client-visible.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status

from database import db
from dependencies import AuthorizedAgent, get_current_user
from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_RATE_LIMIT,
    CANVAS_RATE_WINDOW,
    Canvas,
    CanvasSummary,
    CanvasWrite,
    User,
)
from services import canvas_service, rate_limiter
from services.canvas_service import CanvasError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["canvas"])


def _map(exc: CanvasError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _require_self(current_user: User, name: str) -> None:
    """An agent-scoped key may only write its OWN canvas (the #918 rule)."""
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only write its own canvas",
        )


@router.get("/{name}/canvas", response_model=List[CanvasSummary])
async def list_canvases(name: AuthorizedAgent):
    """Every canvas this agent has, newest-updated first (operator surface).

    Unfiltered by audience on purpose: this is the operator read, and an
    operator seeing only the canvases their agent chose to publish to clients
    would be the inverse of the access model.
    """
    return canvas_service.decorate(db.list_agent_canvases(name), name)


@router.get("/{name}/canvas/{canvas_id}", response_model=Canvas)
async def get_canvas(name: AuthorizedAgent, canvas_id: str):
    """One canvas with its blocks (operator surface)."""
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except CanvasError as e:
        raise _map(e)
    canvas = db.get_agent_canvas(name, canvas_id)
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return canvas_service.decorate([canvas], name)[0]


@router.put("/{name}/canvas/{canvas_id}", response_model=Canvas)
async def write_canvas(
    name: AuthorizedAgent,
    canvas_id: str,
    data: CanvasWrite,
    # Bare `Request`, not `Optional[Request]` — FastAPI special-cases the bare
    # annotation as an ASGI injection and would otherwise try to build a
    # Pydantic field for it (the #1838 note on the reports route). Defaulted so
    # a direct in-process call keeps working.
    request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """Create or replace a canvas (called by the agent via MCP).

    PUT rather than POST because the operation is idempotent on
    ``(agent, canvas_id)`` — writing the same blocks twice leaves one canvas in
    one state, which is the whole point of the surface.
    """
    _require_self(current_user, name)
    rate_limiter.enforce(
        f"agent_canvas:{name}",
        CANVAS_RATE_LIMIT,
        CANVAS_RATE_WINDOW,
        detail="Canvas write rate limit exceeded for this agent.",
    )
    # Cheap header check before the parsed payload is re-serialized. A HINT,
    # not the enforcement — a missing or lying Content-Length falls through to
    # the exact byte check below, which is what actually bounds what reaches
    # the DB and every later read (the #1537 two-stage shape).
    declared = request.headers.get("content-length") if request else None
    if declared:
        try:
            if int(declared) > CANVAS_BLOCKS_MAX_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"canvas blocks exceed {CANVAS_BLOCKS_MAX_BYTES} bytes",
                )
        except ValueError:
            pass  # unparseable header — the exact check below still applies

    try:
        canvas_service.validate_canvas_id(canvas_id)
        blocks = [b.model_dump() for b in data.blocks]
        # Serialize once HERE purely to enforce the byte cap before anything
        # touches the DB; db/canvas.py serializes again for storage. The double
        # encode is deliberate — the alternative is a service that returns a
        # string and a db layer that trusts it, and the db layer is the one
        # every future caller reaches through.
        canvas_service.serialize_blocks(blocks)
        execution_id = canvas_service.resolve_execution_id(data.execution_id, name)
    except CanvasError as e:
        raise _map(e)

    canvas = db.upsert_agent_canvas(
        name,
        canvas_id,
        blocks=blocks,
        title=data.title,
        audience=data.audience,
        execution_id=execution_id,
    )
    return canvas_service.decorate([canvas], name)[0]


@router.delete("/{name}/canvas/{canvas_id}")
async def clear_canvas(
    name: AuthorizedAgent,
    canvas_id: str,
    current_user: User = Depends(get_current_user),
):
    """Remove a canvas. Idempotent — clearing an absent canvas is a success.

    Idempotent rather than 404-on-missing because the caller is an agent
    tidying up after itself: "make sure this surface is gone" has succeeded
    either way, and a 404 here would push every agent into a
    check-then-delete race with its own concurrent turns.
    """
    _require_self(current_user, name)
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except CanvasError as e:
        raise _map(e)
    deleted = db.delete_agent_canvas(name, canvas_id)
    return {"canvas_id": canvas_id, "deleted": bool(deleted)}
