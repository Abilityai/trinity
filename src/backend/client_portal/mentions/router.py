# mcp: none — the tagged person's own Inbox door (portal tokens and platform sessions via get_portal_principal); agents cannot tag people, so no agent reads or writes here
"""FastAPI router for Workspace person tags (trinity-enterprise#631).

Portal-scoped, like asks: the caller is resolved by `get_portal_principal`, so a
portal token works and a platform session resolves to that user's email. The
authorization is the ADDRESSEE match in the service — a tag that is not yours is
a uniform 404, the same as one that does not exist (Invariant #8). Opening one
shows the tagged message only to a reader who can already see the conversation;
the admin's every-room visibility rides the platform door only (#78).
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from client_portal.portal_auth import PortalPrincipal, get_portal_principal
from models import PersonMention, PersonMentionDetail
from services import person_mention_service as pms

router = APIRouter(
    prefix="/api/enterprise/client-portal/mentions",
    tags=["client-portal"],
)


def _reader(principal: PortalPrincipal) -> pms.Reader:
    return pms.Reader(email=principal.email, is_platform=principal.is_platform)


def _raise(e: pms.TagError):
    raise HTTPException(status_code=e.status_code,
                        detail={"code": e.code, "message": e.detail, **e.extra})


@router.get("", response_model=List[PersonMention])
def list_mentions(principal: PortalPrincipal = Depends(get_portal_principal)):
    """The tags addressed to the caller, newest first — the Inbox's Unread
    (state `unread`) and All rows. Never Action: a tag needs no answer."""
    return pms.list_for_reader(_reader(principal))


@router.get("/{item_id}", response_model=PersonMentionDetail)
def open_mention(item_id: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """One tag, opened: the tagged message with a little around it when the
    caller can already see the conversation; otherwise `can_see: false`, no
    content at all, and who can let them in."""
    try:
        return pms.open_for_reader(_reader(principal), item_id)
    except pms.TagError as e:
        _raise(e)


@router.post("/{item_id}/read", response_model=PersonMention)
def read_mention(item_id: str, principal: PortalPrincipal = Depends(get_portal_principal)):
    """Mark a tag read — what the tagger then sees. A person only: a system
    key reading the Inbox must not tell the tagger a person read it."""
    if not principal.is_person:
        raise HTTPException(status_code=403, detail={
            "code": "person_required",
            "message": "Only a person can mark a tag read.",
        })
    try:
        return pms.mark_read(_reader(principal), item_id)
    except pms.TagError as e:
        _raise(e)
