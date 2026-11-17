# mcp: none — single-use WebSocket auth tickets for the browser (#550); an MCP client never opens /ws
"""
WebSocket auth ticket endpoint (#550).

Browser clients call ``POST /api/ws/ticket`` (JWT in ``Authorization``
header) and receive a short-lived opaque ticket they then present on
``/ws?ticket=<ticket>``. See ``services/ws_ticket_service`` for the
single-use Redis-backed exchange.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from dependencies import require_person, workspace_route
from models import User
from services.ws_ticket_service import mint_ticket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ws", tags=["websocket"])


@router.post("/ticket")
@workspace_route("the Workspace stores take live updates over /ws; events are scoped to the agents the person can see (ent#467)")
async def create_ws_ticket(current_user: User = Depends(require_person)) -> dict:
    """Mint a single-use 30-second WebSocket auth ticket for the caller.

    A person only (trinity-enterprise#872): the ticket is minted for the
    username alone, so an agent key would get its owner's stream. An agent
    listens on `/ws/events`, which scopes a key to its reach.

    The ticket is opaque and unrelated to the JWT. Clients must call
    this endpoint immediately before opening the WebSocket; if the
    ticket isn't consumed within 30 seconds it expires and a new one
    must be requested.
    """
    try:
        ticket = mint_ticket(current_user.username, scope="user")
    except RuntimeError as exc:
        # Redis down — fail closed. Surface as 503 so the client retries.
        # #4284: fixed words, the text TicketStoreUnavailable already carries.
        # Class name only in the log, like the ticket service: a store error's
        # text can carry the store URL.
        logger.warning("WebSocket ticket mint failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Redis unavailable — cannot mint WebSocket ticket",
        )

    return {"ticket": ticket, "expires_in": 30}
