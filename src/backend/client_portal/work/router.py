# mcp: none — a Workspace UI read over the executions ledger; agents have `list_recent_executions` / `get_execution_result` (executions.ts) and the #919 pipeline tools for the same data
"""FastAPI router for Workspace work (trinity-enterprise#525).

Portal-scoped like `asks/`: the caller resolves through `get_portal_principal`.
Unlike `asks/`, this surface is **platform-authenticated only** — ent#78's
auth-path invariant, restated by the 2026-09-06 ruling — so a verified-email
portal token gets a uniform 404 here before any read, the same answer it gets
for an agent outside its roster. The frontend's door gate (`visibleTabs`) is
UX; this line is the containment.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from client_portal.portal_auth import PortalPrincipal, get_portal_principal

from . import service
from .models import PortalWork
from .service import MAX_AGENTS, WorkError, parse_agents

router = APIRouter(
    prefix="/api/enterprise/client-portal/work",
    tags=["client-portal"],
)


@router.get("", response_model=PortalWork)
async def get_work(
    agents: str = Query(..., description="Comma-separated participant names"),
    chat_id: Optional[str] = Query(None, description="The open thread, for its delegated children"),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """What the chat's participants are doing now, what they did recently, and
    the steps of the job under the message that started it.

    Invariant #8: no agent path parameter; an unknown or off-roster name in
    `agents` is dropped rather than answered; `chat_id` is honoured only when it
    names a thread this caller holds with one of those agents.
    """
    if not principal.is_platform:
        # Uniform with the roster miss: never confirm that the surface exists
        # to a credential that may not use it.
        raise HTTPException(status_code=404, detail="Not found")

    names = parse_agents(agents)
    if len(names) > MAX_AGENTS:
        # Raised BEFORE the limiter, named, never truncated (the `/briefings` rule).
        raise HTTPException(status_code=422, detail=f"agents: at most {MAX_AGENTS} names per request")

    from services import rate_limiter
    # Per viewer, like every other bounded portal read: the Work tab polls
    # every 12 s only while something is running, and pushes are debounced.
    rate_limiter.enforce(f"portal_work:{principal.email}", 120, 60)

    try:
        return await service.get_work(principal.email, names, chat_id)
    except WorkError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
