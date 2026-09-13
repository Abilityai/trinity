# mcp: canvas.ts (set_canvas / patch_canvas / get_canvas / list_canvases / clear_canvas → /api/agents/{name}/canvas); deliberately NOT exposed (ent#553): PUT .../canvas/{id}/pin — pin is the reader's decision, never the agent's — and POST .../canvas/bulk-delete — an agent's bulk delete is clear_canvas per id under the #918 self-gate, so the human-only verb has no agent caller
"""Agent canvas API (ent#438, widened by ent#536).

A **canvas** is a durable surface an agent renders onto and keeps current —
addressed by ``(agent, canvas_id)``, updated in place, and rendered by the
Workspace and Agent Detail through the shared ``components/reports/`` dispatch.
Reports (#918) stay the immutable half: a thing published once and accumulated.

Thin by contract (Invariant #1): validation, the derived staleness, and the
one write path live in ``services/canvas_service.py``; this module is auth,
the HTTP error map, and the audience projection.

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
from dependencies import AuthorizedAgent, assert_agent_owner, get_current_user
from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_RATE_LIMIT,
    CANVAS_RATE_WINDOW,
    Canvas,
    CanvasBulkDelete,
    CanvasShare,
    CanvasShareCreate,
    CanvasBulkDeleteResult,
    CanvasPatch,
    CanvasPinRequest,
    CanvasWriteResult,
    CanvasSummary,
    CanvasWrite,
    User,
)
from services import canvas_service, rate_limiter
from services.canvas_service import CanvasError
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["canvas"])


def _share_url(token: str) -> str:
    """The address a share link is handed out as.

    Relative on purpose: the frontend resolves it against its own origin, so a
    link keeps working behind whatever hostname the instance is reached by
    (Tailscale name, tunnel, localhost) instead of baking one in at mint time.
    """
    return f"/canvas/s/{token}"


def _map(exc: CanvasError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _require_self(current_user: User, name: str) -> None:
    """An agent-scoped key may only write its OWN canvas (the #918 rule)."""
    if current_user.agent_name and current_user.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only write its own canvas",
        )


def _gate_human_removal(current_user: User, name: str) -> None:
    """Who may delete or pin a canvas (ent#553).

    Two principals, two rules, and the split is the decision:

    * an **agent-scoped key** may only touch its OWN canvas — unchanged, the
      #918 self-gate. `clear_canvas` is an agent tidying up after itself.
    * a **human** must be the agent's owner (or an admin). This mirrors the
      answer ent#548 gives for files — the owner deletes the shared artifact —
      and it NARROWS the previous behaviour, where any user the agent was
      shared with could delete through this route. Safe to narrow: no UI called
      it, so no workflow depended on the wider gate.

    A canvas is one shared surface with no per-user copy, so there is no
    "remove it from my list only" middle ground to offer a non-owner; per the
    issue's AC #2 they see no control at all rather than one that 403s.

    `assert_agent_owner` is the right helper despite its docstring warning:
    that warning is about deleting an AGENT (where the `is_system` guard
    matters), not about a row belonging to one.
    """
    if current_user.agent_name:
        _require_self(current_user, name)
        return
    assert_agent_owner(current_user, name, detail="Only the agent's owner may change its canvases")


def _gate_pin(current_user: User, name: str) -> None:
    """Who may PIN a canvas (ent#553 review) — humans only, and the owner.

    Deliberately NOT `_gate_human_removal`. That gate lets an agent-scoped key
    act on its own canvases, which is right for delete ("an agent tidying up
    after itself") and wrong for pin: a pin decides which canvas a whole roster
    sees first, so an agent that could set it could promote itself up its own
    list. Nothing in the agent-facing surface offers it — `pinned` is absent
    from the MCP tools by design — but "no tool exposes it" is a property of the
    client, and this route is reachable with the agent's own key.

    `docs/user-docs/agents/agent-canvas.md` already tells users "the agent
    cannot pin its own canvas". This is what makes that sentence true rather
    than aspirational; the alternative was editing the doc to admit it could.
    """
    if current_user.agent_name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A pin is a human's ordering; an agent may not pin a canvas",
        )
    assert_agent_owner(current_user, name, detail="Only the agent's owner may change its canvases")


def _gate_write(current_user: User, name: str, request: Request) -> None:
    """The three checks every canvas write shares: self-gate, rate, size hint."""
    _require_self(current_user, name)
    rate_limiter.enforce(
        f"agent_canvas:{name}",
        CANVAS_RATE_LIMIT,
        CANVAS_RATE_WINDOW,
        detail="Canvas write rate limit exceeded for this agent.",
    )
    # Cheap header check before the parsed payload is re-serialized. A HINT,
    # not the enforcement — a missing or lying Content-Length falls through to
    # the exact byte check in the service, which is what actually bounds what
    # reaches the DB and every later read (the #1537 two-stage shape).
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


@router.get("/{name}/canvas", response_model=List[CanvasSummary])
async def list_canvases(name: AuthorizedAgent):
    """Every canvas this agent has, newest-updated first (operator surface).

    Unfiltered by audience on purpose: this is the operator read, and an
    operator seeing only the canvases their agent chose to publish to clients
    would be the inverse of the access model.
    """
    return canvas_service.decorate(db.list_agent_canvases(name), name)


@router.post("/{name}/canvas/bulk-delete", response_model=CanvasBulkDeleteResult)
async def bulk_delete_canvases(
    name: AuthorizedAgent,
    body: CanvasBulkDelete,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Remove several canvases in one action (ent#553).

    Declared BEFORE the parameterized routes (Invariant #4). Nothing collides
    on `POST` today, but the ordering is what keeps that true if one is added.

    A POST rather than a `DELETE` carrying a body: request bodies on DELETE are
    permitted-but-unreliable across proxies and clients, and this one is not
    optional — dropping it would turn "delete these five" into a syntax error
    at best and, on a route shaped `DELETE /canvas`, a delete-everything at
    worst. The verb is worth less than that guarantee.

    Reports what actually went, not what was asked for, so the UI can say
    "3 of 5 removed" rather than implying success for ids that were already
    gone.
    """
    _gate_human_removal(current_user, name)
    for canvas_id in body.canvas_ids:
        try:
            canvas_service.validate_canvas_id(canvas_id)
        except CanvasError as e:
            raise _map(e)

    deleted = db.delete_agent_canvases(name, body.canvas_ids)

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="canvas_bulk_delete",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        # Ids only. A canvas's blocks are agent-authored free-form content and
        # the audit log is broadly readable (the G-04 rule).
        details={"requested": len(body.canvas_ids), "deleted": deleted,
                 "surface": "agent_detail"},
    )
    return CanvasBulkDeleteResult(
        agent_name=name, requested=len(body.canvas_ids), deleted=deleted
    )


@router.get("/{name}/canvas/shares", response_model=List[CanvasShare])
async def list_canvas_shares(
    name: AuthorizedAgent,
    current_user: User = Depends(get_current_user),
    canvas_id: str | None = None,
):
    """Every live share link for this agent, or for one canvas (ent#554).

    Declared ABOVE `/{name}/canvas/{canvas_id}` (Invariant #4) — "shares" is a
    valid canvas id shape, so this route would otherwise be captured by it and
    answer "canvas not found" forever.

    Owner-gated like the mutations: a link's TOKEN is in the payload, so this
    read hands over the capability itself and cannot be wider than the write.
    """
    _gate_human_removal(current_user, name)
    rows = db.list_canvas_shares(name, canvas_id)
    return [CanvasShare(**r, url=_share_url(r["token"])) for r in rows]


@router.post("/{name}/canvas/{canvas_id}/share", response_model=CanvasShare)
async def create_canvas_share(
    name: AuthorizedAgent,
    canvas_id: str,
    body: CanvasShareCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Mint a share link for one canvas (ent#554).

    Owner-or-admin and human-only via `_gate_human_removal`: a share is a
    GRANT, not a use, and the agent that writes a canvas does not decide who
    outside the platform may read it.

    `public` is audited distinctly from `authorized` because it is the one that
    widens the audience — the audit trail should make "who made this readable
    by anyone with the URL" answerable without reading the payload.
    """
    _gate_human_removal(current_user, name)
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except CanvasError as e:
        raise _map(e)
    if not db.get_agent_canvas(name, canvas_id):
        raise HTTPException(status_code=404, detail="Canvas not found")

    share = db.create_canvas_share(
        name, canvas_id, scope=body.scope,
        created_by=str(getattr(current_user, "id", "")) or None,
        expires_at=body.expires_at,
    )
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="canvas_share_public" if body.scope == "public" else "canvas_share",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        # No token: the audit log is broadly readable and the token IS the
        # capability (the G-04 rule).
        details={"canvas_id": canvas_id, "scope": share["scope"], "share_id": share["id"]},
    )
    return CanvasShare(**share, url=_share_url(share["token"]))


@router.delete("/{name}/canvas/shares/{share_id}")
async def revoke_canvas_share(
    name: AuthorizedAgent,
    share_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Turn a share link off (ent#554). The row is kept, not deleted, so the
    link can say it was revoked instead of 404-ing blankly."""
    _gate_human_removal(current_user, name)
    revoked = db.revoke_canvas_share(name, share_id)
    if revoked:
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action="canvas_share_revoke",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"share_id": share_id},
        )
    return {"share_id": share_id, "revoked": bool(revoked)}


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


@router.put("/{name}/canvas/{canvas_id}", response_model=CanvasWriteResult)
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
    _gate_write(current_user, name, request)
    try:
        canvas = canvas_service.write_canvas(
            name,
            canvas_id,
            [b.model_dump() for b in data.blocks],
            title=data.title,
            audience=data.audience,
            execution_id=data.execution_id,
            template=data.template,
        )
    except CanvasError as e:
        raise _map(e)
    decorated = canvas_service.decorate([canvas], name)[0]
    # #2577: say whether the audience just chosen reaches the session that
    # asked. Resolved in the service (Invariant #1) off the STORED row, so the
    # verdict describes the canvas that exists rather than the value requested.
    visible, note = canvas_service.visibility_for_canvas(canvas, name)
    return {**decorated, "visible_to_requester": visible, "visibility_note": note}


@router.patch("/{name}/canvas/{canvas_id}", response_model=Canvas)
async def patch_canvas(
    name: AuthorizedAgent,
    canvas_id: str,
    data: CanvasPatch,
    request: Request = None,
    current_user: User = Depends(get_current_user),
):
    """Replace only the named blocks of an existing canvas (ent#536).

    The agent names what changes; order is kept; an id the canvas does not
    hold is refused by name rather than appended, because a partial write that
    could append would be the append tool the surface deliberately lacks.
    Title and audience are untouched — this is a content edit, not a
    republish.
    """
    _gate_write(current_user, name, request)
    try:
        canvas = canvas_service.patch_canvas(
            name,
            canvas_id,
            [b.model_dump() for b in data.blocks],
            execution_id=data.execution_id,
        )
    except CanvasError as e:
        raise _map(e)
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    return canvas_service.decorate([canvas], name)[0]


@router.put("/{name}/canvas/{canvas_id}/pin", response_model=CanvasSummary)
async def pin_canvas(
    name: AuthorizedAgent,
    canvas_id: str,
    body: CanvasPinRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Pin or unpin one canvas so it sorts to the top (ent#553).

    Gated like delete, not like write: a pin is the READER's ordering and is
    stored once for everyone, so it is a human decision about a shared surface.

    HUMANS ONLY — `_gate_pin`, not `_gate_human_removal`. That gate lets an
    agent-scoped key act on its OWN canvases, which is right for delete (an
    agent tidying up after itself) and wrong here: an agent that could pin would
    promote itself to the top of its own list for everyone. `pinned` being
    absent from the MCP tools is a property of the CLIENT, not of this route,
    and the user documentation already promises the stronger thing.

    Audited on both surfaces, because the Workspace twin is.
    """
    _gate_pin(current_user, name)
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except CanvasError as e:
        raise _map(e)
    if not db.set_agent_canvas_pinned(name, canvas_id, body.pinned):
        raise HTTPException(status_code=404, detail="Canvas not found")
    canvas = db.get_agent_canvas(name, canvas_id)
    if not canvas:
        raise HTTPException(status_code=404, detail="Canvas not found")
    # Audited like delete (ent#553 review). A pin decides which canvas an entire
    # roster sees first, so it is an administrative act on a shared surface, not
    # a per-viewer preference — and its Workspace twin is audited, which is only
    # worth anything if both surfaces record the same act.
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="canvas_pin",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"canvas_id": canvas_id, "pinned": bool(body.pinned),
                 "surface": "agent_detail"},
    )
    return canvas_service.decorate([canvas], name)[0]


@router.delete("/{name}/canvas/{canvas_id}")
async def clear_canvas(
    name: AuthorizedAgent,
    canvas_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Remove a canvas. Idempotent — clearing an absent canvas is a success.

    Idempotent rather than 404-on-missing because one caller is an agent
    tidying up after itself: "make sure this surface is gone" has succeeded
    either way, and a 404 here would push every agent into a
    check-then-delete race with its own concurrent turns. The human caller
    inherits that idempotence, which is also right for a UI whose list may be
    one poll behind.

    ent#553: the gate widened from "agents may delete their own" to that PLUS
    "the owner may delete any of this agent's", and narrowed for everyone else
    — see `_gate_human_removal`.
    """
    _gate_human_removal(current_user, name)
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except CanvasError as e:
        raise _map(e)
    deleted = db.delete_agent_canvas(name, canvas_id)

    # Audited like the bulk route (AC #1). Only a delete that REMOVED something
    # is logged: this route is idempotent, so a repeat click is a no-op, and
    # logging those would fill the trail with events where nothing happened.
    if deleted:
        await platform_audit_service.log(
            event_type=AuditEventType.CONFIGURATION,
            event_action="canvas_delete",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"canvas_id": canvas_id, "surface": "agent_detail"},
        )
    return {"canvas_id": canvas_id, "deleted": bool(deleted)}
