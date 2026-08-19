"""
Subscription Service (SUB-002)

Manages Claude Max/Pro subscription token assignment and auth mode detection.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time.

No file injection is needed — the token is part of the container environment.
"""

import logging
from typing import Optional

from database import db
from db_models import AgentAuthStatus

logger = logging.getLogger(__name__)


def derive_auth_mode(has_subscription: bool, has_api_key: bool) -> str:
    """The ONE auth-mode enum derivation (#471) — shared by the per-agent
    `AgentAuthStatus` resolver below and the fleet subscription-pressure batch
    endpoint, so the two surfaces use one vocabulary by construction
    ("subscription" | "api_key" | "not_configured")."""
    if has_subscription:
        return "subscription"
    if has_api_key:
        return "api_key"
    return "not_configured"


async def get_agent_auth_mode(agent_name: str) -> AgentAuthStatus:
    """
    Detect the authentication mode for an agent.

    Determines auth purely from DB state:
    1. If agent has a subscription assigned → "subscription"
    2. If agent has use_platform_api_key enabled → "api_key"
    3. Otherwise → "not_configured"

    Args:
        agent_name: Name of the agent

    Returns:
        AgentAuthStatus with detected mode
    """
    # Check for subscription assignment
    subscription = db.get_agent_subscription(agent_name)
    has_subscription = subscription is not None

    # Check for platform API key setting
    has_api_key = db.get_use_platform_api_key(agent_name) or False

    # Determine auth mode (shared derivation, #471)
    auth_mode = derive_auth_mode(has_subscription, has_api_key)

    return AgentAuthStatus(
        agent_name=agent_name,
        auth_mode=auth_mode,
        subscription_name=subscription.name if subscription else None,
        subscription_id=subscription.id if subscription else None,
        has_api_key=has_api_key,
    )
