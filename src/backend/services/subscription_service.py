"""
Subscription Service (SUB-002)

Manages Claude Max/Pro subscription token assignment and auth mode detection.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time.

No file injection is needed — the token is part of the container environment.
"""

import logging
from collections.abc import Mapping
from typing import Any, Optional

from database import db
from services.agent_service import is_claude_runtime
from services.docker_service import get_agent_runtime
from db_models import (
    AgentAuthStatus,
    AgentUsagePresentation,
    ApiMeteredUsagePresentation,
    ClaudeSubscriptionIdentity,
    SubscriptionUsagePresentation,
    SubscriptionUtilizationUnavailable,
    UnconfiguredUsagePresentation,
)

logger = logging.getLogger(__name__)


def _subscription_value(subscription: Any, key: str) -> Any:
    if isinstance(subscription, Mapping):
        return subscription.get(key)
    return getattr(subscription, key, None)


def build_agent_usage_presentation(
    *, subscription: Optional[Any], has_api_key: bool
) -> AgentUsagePresentation:
    """Build the billing-mode discriminator consumed by every agent UI.

    Trinity has no provider-backed Claude allowance counter today. Subscription
    auth therefore returns an explicit unavailable state, with every percentage,
    window, reset, and freshness field null. Estimated Claude Code dollar values
    must never be used as a substitute for that missing signal.
    """
    if subscription is not None:
        return SubscriptionUsagePresentation(
            subscription=ClaudeSubscriptionIdentity(
                id=_subscription_value(subscription, "id"),
                name=_subscription_value(subscription, "name"),
                plan=_subscription_value(subscription, "subscription_type"),
            ),
            utilization=SubscriptionUtilizationUnavailable(),
        )
    if has_api_key:
        return ApiMeteredUsagePresentation()
    return UnconfiguredUsagePresentation()


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

    # Determine auth mode
    if has_subscription:
        auth_mode = "subscription"
    elif has_api_key:
        auth_mode = "api_key"
    else:
        auth_mode = "not_configured"

    return AgentAuthStatus(
        agent_name=agent_name,
        auth_mode=auth_mode,
        subscription_name=subscription.name if subscription else None,
        subscription_id=subscription.id if subscription else None,
        has_api_key=has_api_key,
        usage=(
            build_agent_usage_presentation(
                subscription=subscription,
                has_api_key=has_api_key,
            )
            if is_claude_runtime(get_agent_runtime(agent_name))
            else None
        ),
    )
