"""FastAPI router for shared sessions / rooms (ent#169).

Platform-wide prefix (Invariant #15): a room spans several agents, so it does
NOT nest under ``/api/agents/{name}``.

OSS core since ent#443: no entitlement dependency — these routes answer on
every build. (They used to carry ``requires_entitlement("shared_sessions")``,
which 404'd in community builds while the frontend and MCP tools shipped
anyway.)

Auth shape: every route is open to any authenticated principal, and the ACTING
identity is resolved from the auth context inside the service (an agent-scoped
key acts as its agent, everyone else as the user). Visibility is membership, and
a non-member gets a uniform 404 — never a 403 that confirms the room exists
(Invariant #8).
"""
from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from dependencies import (
    get_current_user,
    oauth2_scheme,
    reject_agent_principal,
    require_admin,
)
from models import User

from . import service
from .models import (
    RoomBudgetDefaults,
    RoomBudgetDefaultsUpdate,
    RoomCloseRequest,
    RoomCreate,
    RoomMessageCreate,
    RoomParticipantAdd,
)
from .service import RoomError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/rooms",
    tags=["rooms"],
)


@dataclass(frozen=True)
class WorkspacePrincipal:
    """A workspace client acting in a room (ent#362).

    Deliberately NOT a `User`. It carries an email and nothing else — no `role`,
    so it can never satisfy an admin bypass, and no `agent_name`, so it can
    never act as an agent. `is_portal` is the marker the service resolves the
    participant kind from.
    """
    email: str
    is_platform: bool
    is_portal: bool = True


async def get_room_principal(
    request: Request,
    response: Response,
    token: str = Depends(oauth2_scheme),
):
    """Platform principal if there is one, else a workspace client (ent#362).

    A FALLBACK, not a replacement: platform auth is attempted first, so every
    existing caller — JWT, MCP key, agent-scoped key — resolves exactly as
    before and keeps acting as `user`/`agent`. Only a credential the platform
    rejects gets the portal path.

    This does widen an entitled platform surface to portal tokens, which the OSS
    fence in `get_current_user` otherwise prevents. That is bounded by what a
    `WorkspacePrincipal` can reach: it holds no role and no agent name, the
    service resolves its agent access through the portal roster (so it can only
    room with agents shared to it), and room visibility is membership with a
    uniform 404. A portal token still cannot touch any other platform endpoint.
    """
    try:
        return await get_current_user(request, token)
    except HTTPException:
        pass

    # The portal resolver owns the blocked-client check and the ent#375 sliding
    # renewal, so it is called rather than re-implemented — and it raises 401
    # itself when the credential is neither kind.
    from client_portal.portal_auth import get_portal_principal
    principal = await get_portal_principal(request, response, token)
    return WorkspacePrincipal(email=principal.email, is_platform=principal.is_platform)


def _raise(e: RoomError):
    raise HTTPException(status_code=e.status_code,
                        detail={"code": e.code, "message": e.detail, **e.extra})


# --- Operator budget defaults (ent#387) -------------------------------------
#
# A SEPARATE router, deliberately: `/api/rooms` is membership-scoped and reachable
# by a workspace client, and a `/budget-defaults` path under it would sit beside
# `/{room_id}` — one ordering slip away from being read as a room id (Invariant
# #4), on the one surface where the reader must never be a client.
#
# Double-gated since ent#443 removed the entitlement layer: `require_admin` per
# route, and `reject_agent_principal` on the setter. The second is the one that is
# easy to omit and expensive to miss — an agent-scoped MCP key resolves to its
# owner CARRYING the owner's role, so on a default admin-owned install
# `require_admin` alone would admit an agent to widen the budget that bounds it.
# The `/api/enterprise/` path segment is retained history, exactly like the
# `enterprise_` table prefix: the OSS frontend panel already calls this URL
# (`components/settings/RoomBudgetDefaultsPanel.vue`), so renaming it would
# break a shipped client for no gain. It is provenance, not a licensing claim.
budget_router = APIRouter(
    prefix="/api/enterprise/room-budget-defaults",
    tags=["rooms"],
)


@budget_router.get("", response_model=RoomBudgetDefaults)
def get_budget_defaults(_: User = Depends(require_admin)):
    """The defaults applied to a room started without an explicit budget."""
    return _budget_defaults_response()


@budget_router.put("", response_model=RoomBudgetDefaults)
def set_budget_defaults(
    body: RoomBudgetDefaultsUpdate, current_user: User = Depends(require_admin)
):
    reject_agent_principal(current_user)
    unknown = set(body.clear) - {
        service.ROOM_DEFAULT_MAX_MESSAGES_KEY,
        service.ROOM_DEFAULT_MAX_COST_KEY,
        service.ROOM_DEFAULT_TTL_HOURS_KEY,
    }
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown key(s) in clear: {', '.join(sorted(unknown))}",
        )
    service.set_budget_defaults(
        max_messages=body.max_messages,
        max_cost_usd=body.max_cost_usd,
        ttl_hours=body.ttl_hours,
        clear=body.clear,
    )
    logger.info(
        "[rooms] budget defaults updated by %s", getattr(current_user, "email", "?")
    )
    return _budget_defaults_response()


def _budget_defaults_response() -> RoomBudgetDefaults:
    state = service.budget_defaults()
    return RoomBudgetDefaults(
        max_messages=state["max_messages"],
        max_cost_usd=state["max_cost_usd"],
        ttl_hours=state["ttl_hours"],
        sources=state["sources"],
        max_messages_ceiling=service.MAX_MESSAGES_CEILING,
        max_ttl_hours=service.MAX_TTL_HOURS,
    )


@router.post("")
async def create_room(body: RoomCreate, current_user=Depends(get_room_principal)):
    try:
        return service.create_room(
            current_user, body.name, body.agents, topic=body.topic,
            max_messages=body.max_messages, max_cost_usd=body.max_cost_usd,
            ttl_hours=body.ttl_hours, scribe=body.scribe,
        )
    except RoomError as e:
        _raise(e)


@router.get("")
async def list_rooms(current_user=Depends(get_room_principal)):
    return service.list_rooms(current_user)


# NOTE: no static sub-routes exist under /api/rooms today, but if one is added
# it MUST be declared above `/{room_id}` or it will be captured as an id
# (Invariant #4).

@router.get("/{room_id}")
async def get_room(room_id: str, since: int = Query(default=0, ge=0),
                   current_user=Depends(get_room_principal)):
    try:
        return service.get_room(current_user, room_id, since_seq=since)
    except RoomError as e:
        _raise(e)


@router.post("/{room_id}/messages")
async def post_message(room_id: str, body: RoomMessageCreate,
                       idempotency_key: Optional[str] = Header(default=None,
                                                               alias="Idempotency-Key"),
                       current_user=Depends(get_room_principal)):
    """Post to the room; @mentioned agents are woken (Invariant #18: accepts an
    Idempotency-Key so a retried post creates one message, not two)."""
    from services import idempotency_service

    decision = idempotency_service.begin(f"room:{room_id}", idempotency_key)
    if decision.replay:
        return {**(decision.snapshot or {}), "replayed": True}
    if decision.in_flight:
        raise HTTPException(status_code=409,
                            detail={"code": "in_flight",
                                    "message": "An identical post is already in flight"})
    try:
        result = await service.post_message(current_user, room_id, body.content)
    except RoomError as e:
        idempotency_service.fail(decision)
        _raise(e)
    except Exception:
        idempotency_service.fail(decision)
        raise
    idempotency_service.complete(decision, None, result)
    return result


@router.post("/{room_id}/close")
async def close_room(room_id: str, body: RoomCloseRequest | None = None,
                     current_user=Depends(get_room_principal)):
    try:
        return service.close_room(current_user, room_id,
                                  (body.reason if body else None) or "user_closed")
    except RoomError as e:
        _raise(e)


@router.post("/{room_id}/participants")
async def add_participant(room_id: str, body: RoomParticipantAdd,
                          current_user=Depends(get_room_principal)):
    try:
        return service.add_participant(current_user, room_id, body.agent_name, body.role)
    except RoomError as e:
        _raise(e)


@router.delete("/{room_id}/participants/{agent_name}")
async def remove_participant(room_id: str, agent_name: str,
                             current_user=Depends(get_room_principal)):
    try:
        return service.remove_participant(current_user, room_id, agent_name)
    except RoomError as e:
        _raise(e)
