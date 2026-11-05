# mcp: none — the terms a PERSON accepts on their own visit, and the admin editor of the instance's text (trinity-enterprise#1107); a human surface, not an agent capability
"""Terms of use per instance (trinity-enterprise#1107).

* ``GET /api/terms/current`` — a person (``assert_person``), marked
  ``@workspace_route`` (the terms screen is a member page) AND ``@terms_route``
  (the text must be readable before it can be accepted).
* ``POST /api/terms/accept`` — an interactive session
  (``reject_non_interactive_principal``: a person accepts on their own
  session, never through a key), both marks. Schedules the acceptance hooks
  after the response.

Both resolve the caller through ``terms_caller``: a platform principal
through ``get_optional_user`` (so the Workspace-route census still sees the
operator door) — or, when the bearer is a Workspace portal session token
(ruling A(1) on trinity-enterprise#1107: the emailed-code sign-in, no
``users`` row), a ``terms_service.PortalPerson`` for its verified email, the
client-block check applied as on every portal route. A credential that is
neither is refused by the platform resolver's own answer (401/403), never a
guess. The acceptance of a portal person is recorded by email.
* ``GET /api/terms/acceptances`` — ``require_admin``: the record (version,
  time, account).
* ``PUT /api/terms`` / ``POST /api/terms/restore-default`` — admin AND
  interactive: replacing the text every person on the instance must accept is
  a grant-shaped configuration write (Invariant #8), audited by version.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from typing import Optional, Union

from dependencies import (
    assert_person,
    decode_portal_session,
    get_current_user,
    get_optional_user,
    oauth2_scheme,
    reject_non_interactive_principal,
    require_admin,
    terms_route,
    workspace_route,
)
from models import TermsAccept, TermsAcceptance, TermsAccepted, TermsCurrent, TermsUpdate, User
from services import terms_gate, terms_service
from services.terms_service import PortalPerson, TermsError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/terms", tags=["terms"])

TermsPrincipal = Union[User, PortalPerson]


async def terms_caller(
    request: Request,
    token: str = Depends(oauth2_scheme),
    platform: Optional[User] = Depends(get_optional_user),
) -> TermsPrincipal:
    """The person reading or accepting: a platform principal, or the Workspace
    portal session the bearer token carries (module doc). The handlers apply
    their own person / interactive rule on what comes back."""
    if platform is not None:
        return platform
    email = decode_portal_session(token)
    if email:
        from client_portal.portal_auth import _reject_if_blocked
        _reject_if_blocked(email)
        return PortalPerson(email)
    # Neither: a bearer this door does not recognise. The same 401 whatever
    # the token was — an ops key included — so the door reads as one that
    # swallows (`get_optional_user`) to the #3180 census, never as a second
    # route into the ops fence.
    raise HTTPException(status_code=401, detail="Not authenticated",
                        headers={"WWW-Authenticate": "Bearer"})


def _raise(e: TermsError):
    raise HTTPException(status_code=e.status_code,
                        detail={"code": e.code, "message": e.message, "version": e.version})


def _with_accepted(current: dict, user: TermsPrincipal) -> dict:
    return {**current, "accepted": terms_service.accepted_current(user)}


@router.get("/current", response_model=TermsCurrent)
@workspace_route("the terms screen a person reads before accepting")
@terms_route("the text must be readable before it can be accepted")
def current_terms(current_user: TermsPrincipal = Depends(terms_caller)):
    assert_person(current_user)
    return _with_accepted(terms_service.current_terms(), current_user)


@router.post("/accept", response_model=TermsAccepted)
@workspace_route("the terms screen is where a person accepts")
@terms_route("accepting is what ends the refusal")
async def accept_terms(body: TermsAccept, request: Request, background: BackgroundTasks,
                       current_user: TermsPrincipal = Depends(terms_caller)):
    reject_non_interactive_principal(current_user)
    # `require_interactive`'s second half, for the platform branch: a vouched
    # (agent-sourced) session is no person's own.
    if getattr(current_user, "vouched_source_agent", None):
        from dependencies import HUMAN_ONLY_DETAIL
        raise HTTPException(status_code=403, detail=dict(HUMAN_ONLY_DETAIL))
    # W2: the door the person came through must still be open — a provider
    # naming a closed invitation refuses here with the same code the gate
    # would, so the OSS route is not a way around the module's door.
    terms_gate.assert_door_open(current_user)
    try:
        out = await terms_service.accept(
            current_user, body.version,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
            endpoint=request.scope["path"],
            door=terms_gate.provider_door(current_user),
        )
    except TermsError as e:
        _raise(e)
    if out["created"]:
        # After the response, inside this request: the consumers (an invite
        # door, a member's agent) never hold the acceptance up, once per new row.
        background.add_task(terms_service.run_acceptance_hooks, out["user_id"], out["email"],
                            out["version"], out["accepted_at"])
    return out


@router.get("/acceptances", response_model=list[TermsAcceptance])
def list_acceptances(email: str | None = Query(None, max_length=254),
                     limit: int = Query(200, ge=1, le=1000),
                     _: User = Depends(require_admin)):
    return terms_service.list_acceptances(limit, email=email)


@router.put("", response_model=TermsCurrent)
async def update_terms(body: TermsUpdate, request: Request,
                       current_user: User = Depends(require_admin)):
    reject_non_interactive_principal(current_user)
    try:
        current = terms_service.set_terms(
            title=body.title, text=body.text, required=body.required,
            author=current_user, actor_ip=request.client.host if request.client else None,
        )
    except TermsError as e:
        _raise(e)
    await terms_service.audit_change(
        "terms_updated", current_user,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
        endpoint=request.scope["path"], version=current["version"],
    )
    return _with_accepted(current, current_user)


@router.post("/restore-default", response_model=TermsCurrent)
async def restore_default_terms(request: Request, current_user: User = Depends(require_admin)):
    reject_non_interactive_principal(current_user)
    current = terms_service.restore_default()
    await terms_service.audit_change(
        "terms_restored", current_user,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
        endpoint=request.scope["path"], version=current["version"],
    )
    return _with_accepted(current, current_user)
