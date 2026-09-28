# mcp: none — a Workspace UI read computed for one human viewer; agents read the same signals through schedules/asks/decisions tools
"""FastAPI router for Workspace suggestions (trinity-enterprise#465).

Platform-authenticated only (ent#78's auth-path invariant): a verified-email
portal token gets the same uniform 404 as an agent outside the roster, before
any read. Order is access-first (OSS invariant #8) — door, then roster, then the
per-viewer rate limiter — so the limiter never keys on an unvalidated name.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from client_portal.portal_auth import PortalPrincipal, get_portal_principal

from . import service
from .models import PortalSuggestions, SuggestionFeedback, SuggestionFeedbackResult

router = APIRouter(
    prefix="/api/enterprise/client-portal/agents",
    tags=["client-portal"],
)


def _gate(agent_name: str, principal: PortalPrincipal, limiter_key: str, limit: int) -> None:
    if not principal.is_platform:
        raise HTTPException(status_code=404, detail="Not found")
    from client_portal import service as portal

    if not portal.agent_on_roster(agent_name, principal.email, True):
        raise HTTPException(status_code=404, detail="Agent not found")
    from services import rate_limiter

    rate_limiter.enforce(f"{limiter_key}:{principal.email}", limit, 60)


@router.get("/{agent_name}/suggestions", response_model=PortalSuggestions)
async def get_suggestions(
    agent_name: str,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Suggestions computed for this viewer and this agent (requirement §5.39)."""
    _gate(agent_name, principal, "portal_suggestions", 60)
    return await service.get_suggestions(agent_name, principal.email, is_admin=principal.is_admin)


@router.post("/{agent_name}/suggestions/feedback", response_model=SuggestionFeedbackResult)
async def post_feedback(
    agent_name: str,
    body: SuggestionFeedback,
    principal: PortalPrincipal = Depends(get_portal_principal),
):
    """Accept or dismiss one suggestion that is currently shown to this viewer.
    The key travels in the body (it can carry an agent-authored playbook name)."""
    _gate(agent_name, principal, "portal_suggestions_feedback", 60)
    try:
        await service.record_feedback(
            agent_name, principal.email, is_admin=principal.is_admin,
            key=body.key, action=body.action,
        )
    except service.SuggestionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return SuggestionFeedbackResult()
