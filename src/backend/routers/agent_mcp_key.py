# mcp: none — credential-lifecycle routes are human-only by construction (reject_non_interactive_principal, #1854)
"""Per-agent Trinity MCP key — read, verify, rotate (#1854).

Thin HTTP shell over ``services/agent_mcp_key_service.py`` (Invariant #1): no
business logic here, and every 4xx/5xx the service raises maps 1:1.

Named ``mcp-key``, not ``platform-key``: "platform key" already means the
**Anthropic** platform API key throughout the codebase
(``agent_ownership.use_platform_api_key``, the ``agent_ownership_platform_key``
migration, ``services/agent_service/api_key.py``, ``GET/PUT
/api/agents/{name}/api-key-setting``).

Auth on all three routes:
  * ``OwnedAgentByName`` — owner/admin, uniform 404 for both "no such agent" and
    "not yours" (Invariant #8 / #186; no 404-then-403 oracle).
  * ``reject_non_interactive_principal`` — an ALLOWlist over
    ``User.mcp_scope``. The older ``reject_agent_principal`` +
    ``_reject_connector_principal`` pair is a two-item denylist over a
    five-value free-text column, and ``scope='system'`` walks through BOTH while
    still resolving to an owner who, on a default admin-owned install, owns the
    whole fleet.
  * a sliding-window rate limit per agent AND per actor — for an admin,
    ownership is fleet-wide, so an unthrottled rotate loop is a scripted
    fleet-wide container-recreate storm.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from dependencies import (
    OwnedAgentByName,
    get_current_user,
    reject_non_interactive_principal,
)
from models import (
    AgentMcpKeyRegenerateResult,
    AgentMcpKeyStatus,
    AgentMcpKeyVerifyResult,
    User,
)
from services import rate_limiter
from services.agent_mcp_key_service import (
    get_agent_mcp_key_status,
    regenerate_agent_mcp_key,
    verify_agent_mcp_key,
)

router = APIRouter(prefix="/api/agents", tags=["agent_mcp_key"])

# The probe is a docker exec; rotation replaces a container. Both are cheap to
# ask for and expensive to serve, so both are limited — rotation hard.
_VERIFY_LIMIT, _VERIFY_WINDOW_S = 20, 60
# Per-actor too, not just per-agent: ownership is fleet-wide for an admin, so a
# per-agent-only limit bounds nothing an operator actually does — a scripted
# sweep of N agents is N × 20/min docker execs against one daemon, each holding
# a 15s timeout. Same reasoning that gives rotation both limits.
_VERIFY_ACTOR_LIMIT, _VERIFY_ACTOR_WINDOW_S = 60, 60
_REGEN_AGENT_LIMIT, _REGEN_AGENT_WINDOW_S = 3, 300
_REGEN_ACTOR_LIMIT, _REGEN_ACTOR_WINDOW_S = 10, 300


def _actor_key(current_user: User) -> str:
    return str(getattr(current_user, "id", None) or getattr(current_user, "username", "?"))


@router.get("/{agent_name}/mcp-key", response_model=AgentMcpKeyStatus)
async def get_agent_mcp_key_endpoint(
    agent_name: OwnedAgentByName,
    current_user: User = Depends(get_current_user),
):
    """Metadata + health for the agent's Trinity MCP key. Never the secret."""
    reject_non_interactive_principal(current_user)
    return get_agent_mcp_key_status(agent_name)


@router.post("/{agent_name}/mcp-key/verify", response_model=AgentMcpKeyVerifyResult)
async def verify_agent_mcp_key_endpoint(
    agent_name: OwnedAgentByName,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Ask the running container what it is ACTUALLY configured with.

    A separate explicit route rather than part of the GET: it is a `docker exec`,
    far too heavy for every panel load. Degrades to an ``unavailable`` verdict
    when the agent is stopped — never a 500.
    """
    reject_non_interactive_principal(current_user)
    rate_limiter.enforce(
        f"mcp_key_verify:{agent_name}", _VERIFY_LIMIT, _VERIFY_WINDOW_S,
        detail="Too many verification checks for this agent.",
    )
    rate_limiter.enforce(
        f"mcp_key_verify_actor:{_actor_key(current_user)}",
        _VERIFY_ACTOR_LIMIT, _VERIFY_ACTOR_WINDOW_S,
        detail="Too many verification checks.",
    )
    return await verify_agent_mcp_key(agent_name)


@router.post("/{agent_name}/mcp-key/regenerate", response_model=AgentMcpKeyRegenerateResult)
async def regenerate_agent_mcp_key_endpoint(
    agent_name: OwnedAgentByName,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Rotate the agent's MCP key and deliver it. Returns metadata only."""
    reject_non_interactive_principal(current_user)
    rate_limiter.enforce(
        f"mcp_key_regen:{agent_name}", _REGEN_AGENT_LIMIT, _REGEN_AGENT_WINDOW_S,
        detail="Too many key rotations for this agent.",
    )
    rate_limiter.enforce(
        f"mcp_key_regen_actor:{_actor_key(current_user)}",
        _REGEN_ACTOR_LIMIT, _REGEN_ACTOR_WINDOW_S,
        detail="Too many key rotations.",
    )
    return await regenerate_agent_mcp_key(
        agent_name,
        current_user,
        actor_ip=request.client.host if request.client else None,
        request_id=getattr(request.state, "request_id", None),
        endpoint=str(request.url.path),
    )
