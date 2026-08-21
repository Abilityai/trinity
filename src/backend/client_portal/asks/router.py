"""FastAPI router for Workspace asks (ent#364).

Portal-scoped: the caller is a **workspace user**, resolved through
`get_portal_principal` — the same resolver the rest of the Workspace uses, so a
portal token works and a platform JWT resolves to that user's email (ent#357).
That is why this cannot hang off `/api/operator-queue`, whose every route takes
`get_current_user` and reads `current_user.role`: a portal token is fenced out of
that dependency entirely.

OSS core since ent#428, and **not** admin-gated: the audience is the person the
ask was addressed to. Authorisation is the addressee match plus a read-time
roster re-check, both in the service — never a role, and never an entitlement.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from client_portal.portal_auth import PortalPrincipal, get_portal_principal

from . import service
from .models import WorkspaceAsk, WorkspaceAskAnswer
from .service import AskError

router = APIRouter(
    prefix="/api/enterprise/client-portal/asks",
    tags=["client-portal"],
)


def _raise(e: AskError):
    raise HTTPException(status_code=e.status_code,
                        detail={"code": e.code, "message": e.detail})


@router.get("", response_model=List[WorkspaceAsk])
def list_asks(
    agent_name: Optional[str] = Query(default=None),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Open asks addressed to the caller. `agent_name` narrows to the agent page.

    One endpoint for all three renderings (sidebar count, agent page, inline in
    chat) — three bespoke queries is how "answering anywhere clears it everywhere"
    stops being true.
    """
    return service.list_asks(principal.email, principal.is_platform, agent_name)


@router.post("/{item_id}/answer", response_model=WorkspaceAsk)
def answer_ask(
    item_id: str,
    body: WorkspaceAskAnswer,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Answer one ask. 404 covers missing / not-mine / off-roster alike.

    The uniform 404 is deliberate (Invariant #8): a 403 for "exists but not yours"
    would let any client enumerate which ask ids exist.
    """
    try:
        return service.answer_ask(
            item_id, principal.email, principal.is_platform,
            body.response, body.response_text,
        )
    except AskError as e:
        _raise(e)
