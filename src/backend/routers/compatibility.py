"""
Agent compatibility validation routes (#668).

Server-side compatibility checks against an agent's workspace, surfaced
(non-blocking) in the Agent Detail Overview tab and via an MCP tool. Read is
agent-scoped (owner/shared/admin); the auto-fix mutates the workspace so it is
owner/admin-only.

Three-layer: this router → services/compatibility/ → db/compatibility.py.

NOTE: these endpoints create NO execution, so the Idempotency-Key invariant
(#18) does not apply. The fix endpoint is naturally idempotent (gitignore append
matches exact lines; the blanket-removal is a no-op once gone) and is serialised
per agent by a Redis lock in the service layer.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from models import (
    User,
    CompatibilityReport,
    CompatibilityFixRequest,
    CompatibilityFixResponse,
)
from dependencies import get_current_user, AuthorizedAgentByName, OwnedAgentByName
from services import compatibility
from services.compatibility import FixError, FixBusy
from services.rate_limiter import enforce

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["compatibility"])


@router.get("/{agent_name}/compatibility", response_model=CompatibilityReport)
async def get_compatibility(
    agent_name: AuthorizedAgentByName,
    include_ai: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Compatibility report for an agent.

    STATIC checks recompute live; AI checks return the last persisted verdicts
    unless ``include_ai=true`` forces a fresh (cost-incurring) evaluation. The
    frontend fetches STATIC-only first (instant paint), then AI.
    """
    # The include_ai path fans out ~8 Anthropic calls; bound it per user+agent so
    # a loop can't amplify LLM spend on the platform key (CSO #668 finding).
    # The default (cached) path is free and unthrottled.
    if include_ai:
        enforce(
            f"compat_ai:{current_user.username}:{agent_name}",
            limit=5,
            window_seconds=60,
            detail="Too many compatibility AI re-runs.",
        )
    report = await compatibility.build_report(agent_name, include_ai=include_ai)
    return CompatibilityReport(**report)


@router.post("/{agent_name}/compatibility/fix", response_model=CompatibilityFixResponse)
async def fix_compatibility(
    agent_name: OwnedAgentByName,
    body: CompatibilityFixRequest,
    current_user: User = Depends(get_current_user),
):
    """Apply an auto-fix for a correctable (gitignore) check. Owner/admin only."""
    try:
        result = await compatibility.apply_fix(agent_name, body.check_id)
    except FixError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FixBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return CompatibilityFixResponse(**result)
