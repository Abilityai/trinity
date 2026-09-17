# mcp: none — single-use WebSocket auth tickets for the browser (#550); an MCP client never opens /ws
"""
WebSocket auth ticket endpoint (#550).

Browser clients call ``POST /api/ws/ticket`` (JWT in ``Authorization``
header) and receive a short-lived opaque ticket they then present on
``/ws?ticket=<ticket>``. See ``services/ws_ticket_service`` for the
single-use Redis-backed exchange.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_current_user
from models import User
from services.ws_ticket_service import mint_ticket

router = APIRouter(prefix="/api/ws", tags=["websocket"])


@router.post("/ticket")
async def create_ws_ticket(current_user: User = Depends(get_current_user)) -> dict:
    """Mint a single-use 30-second WebSocket auth ticket for the caller.

    The ticket is opaque and unrelated to the JWT. Clients must call
    this endpoint immediately before opening the WebSocket; if the
    ticket isn't consumed within 30 seconds it expires and a new one
    must be requested.
    """
    try:
        ticket = mint_ticket(current_user.username, scope="user")
    except RuntimeError as exc:
        # Redis down — fail closed. Surface as 503 so the client retries.
        raise HTTPException(status_code=503, detail=str(exc))

    return {"ticket": ticket, "expires_in": 30}
