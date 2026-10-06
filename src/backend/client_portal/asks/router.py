# mcp: none — the Workspace's own door (portal tokens and platform sessions via get_portal_principal); an agent answers nothing here and the operator's asks surface is operator_queue.ts
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

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from client_portal.portal_auth import PortalPrincipal, get_portal_principal
from dependencies import PERSON_REQUIRED_DETAIL

from . import service
from .models import WorkspaceAsk, WorkspaceAskAnswer, WorkspaceAskContext, WorkspaceAskDiscussion
from .service import AskError, AsksUnavailable

# The 503 `asks_unavailable` Retry-After, in seconds (trinity-enterprise#610,
# PR A0). It is the Workspace's asks poll interval (`Portal.vue::ASKS_POLL_MS`):
# its next read is the retry anyway.
ASKS_RETRY_AFTER_SECONDS = "20"

router = APIRouter(
    prefix="/api/enterprise/client-portal/asks",
    tags=["client-portal"],
)


def _raise(e: AskError):
    # #2376: merge any structured fields the refusal carries (the offered
    # options, today) beside the code and message rather than only in prose.
    raise HTTPException(status_code=e.status_code,
                        detail={"code": e.code, "message": e.detail, **e.data})


@router.get("", response_model=List[WorkspaceAsk])
def list_asks(
    response: Response,
    agent_name: Optional[str] = Query(default=None),
    include_ended: bool = Query(default=False),
    # #3059 — opt-in paging. The body stays a list so no current caller
    # changes; the total and the next cursor travel as headers.
    limit: int = Query(default=service.PAGE_MAX, ge=1, le=service.PAGE_MAX),
    cursor: Optional[str] = Query(default=None, max_length=64),
    # trinity-enterprise#610: one chat's chat-turn asks, ended ones with no window.
    chat_id: Optional[str] = Query(default=None, min_length=1, max_length=128),
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Open asks addressed to the caller. `agent_name` narrows to the agent page.

    One endpoint for all three renderings (sidebar count, agent page, inline in
    chat) — three bespoke queries is how "answering anywhere clears it everywhere"
    stops being true. `include_ended` (trinity-enterprise#611) adds the asks that
    ended in the last 7 days, so a person sees how an ask ended instead of
    watching it vanish; the sidebar count stays pending-only on the client.

    An unreadable queue or roster is **503 `asks_unavailable`**, never `[]`
    (trinity-enterprise#610, PR A0): an empty list is a claim that nothing is
    waiting, and the client keeps its last good list on this answer.
    """
    try:
        page = service.list_asks_page(principal.email, principal.is_platform, agent_name,
                                      include_ended=include_ended, limit=limit, cursor=cursor,
                                      chat_id=chat_id)
    except AsksUnavailable:
        raise HTTPException(status_code=503, detail={
            "code": "asks_unavailable",
            "message": "Couldn't load your asks — try again.",
        }, headers={"Retry-After": ASKS_RETRY_AFTER_SECONDS})
    except AskError as e:
        _raise(e)
    response.headers["X-Total-Count"] = str(page.total)
    if page.next_cursor:
        response.headers["X-Next-Cursor"] = page.next_cursor
    return page.items


@router.post("/{item_id}/answer", response_model=WorkspaceAsk)
def answer_ask(
    item_id: str,
    body: WorkspaceAskAnswer,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Answer one ask. 404 covers missing / not-mine / off-roster alike.

    The uniform 404 is deliberate (Invariant #8): a 403 for "exists but not yours"
    would let any client enumerate which ask ids exist.

    Only a person ends an ask (trinity-enterprise#611): a platform principal
    that is not one — a system-scoped key keeps #2198's read breadth here — is
    refused before the row is read, as the operator routes refuse it.
    """
    if not principal.is_person:
        raise HTTPException(status_code=403, detail=dict(PERSON_REQUIRED_DETAIL))
    try:
        return service.answer_ask(
            item_id, principal.email, principal.is_platform,
            body.response, body.response_text,
            acknowledge_divergence=body.acknowledge_divergence,
        )
    except AskError as e:
        _raise(e)


@router.post("/{item_id}/dismiss", response_model=WorkspaceAsk)
def dismiss_ask(
    item_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """End an ask without answering it (trinity-enterprise#748).

    Only the person it was addressed to: the same person gate and the same
    uniform 404 as the answer route. An ask that already ended — or ends first
    in a race — comes back as it stands with a 200: dismissing something that
    no longer waits is a no-op, not an error. Rate-limited like the portal's
    other mutating routes.
    """
    from services import rate_limiter

    if not principal.is_person:
        raise HTTPException(status_code=403, detail=dict(PERSON_REQUIRED_DETAIL))
    rate_limiter.enforce(f"portal_ask_dismiss:{principal.email}", 60, 60)
    try:
        return service.dismiss_ask(item_id, principal.email, principal.is_platform)
    except AskError as e:
        _raise(e)


@router.post("/{item_id}/discuss", response_model=WorkspaceAskDiscussion)
def discuss_ask(
    item_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Open — or continue — the chat in which the addressee talks an ask through
    with its agent before deciding (trinity-enterprise#747). One chat per ask;
    the ask stays pending and is still answered on its own row.

    Same person gate and uniform 404 as the answer route. Rate-limited: each
    first call creates a chat.
    """
    from services import rate_limiter

    if not principal.is_person:
        raise HTTPException(status_code=403, detail=dict(PERSON_REQUIRED_DETAIL))
    rate_limiter.enforce(f"portal_ask_discuss:{principal.email}", 30, 60)
    try:
        return service.discuss_ask(item_id, principal.email, principal.is_platform)
    except AskError as e:
        _raise(e)


@router.get("/{item_id}/context", response_model=WorkspaceAskContext)
def get_ask_context(
    item_id: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Where an ask came from, the run that raised it, and how you answered this
    agent lately (trinity-enterprise#610 §3g L7, E1). One lazy read per selected
    ask — never folded into the polled list.

    404 covers missing / not-mine / off-roster / a kind the Workspace never shows
    alike (Invariant #8). An unreadable roster or context is **503
    `asks_unavailable`**, never an empty context. No `cost` and no
    `execution_id` in the body. Rate-limited per viewer after the principal is
    resolved: a context read is a handful of queries, and a client that polled
    it would multiply them.
    """
    from services import rate_limiter

    rate_limiter.enforce(f"portal_ask_context:{principal.email}", 120, 60)
    try:
        return service.get_ask_context(item_id, principal.email, principal.is_platform)
    except AsksUnavailable:
        raise HTTPException(status_code=503, detail={
            "code": "asks_unavailable",
            "message": "Couldn't load this ask's context — try again.",
        }, headers={"Retry-After": ASKS_RETRY_AFTER_SECONDS})
    except AskError as e:
        _raise(e)
