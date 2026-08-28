"""
Subscription Service (SUB-002)

Manages Claude Max/Pro subscription token assignment and auth mode detection.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time.

No file injection is needed — the token is part of the container environment.
"""

import importlib
import logging
from typing import Optional

from database import db
from db_models import AgentAuthStatus, SubscriptionCredential

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


def select_subscription_for_new_agent() -> Optional[SubscriptionCredential]:
    """The subscription a NEW Claude agent is auto-assigned to (#74), chosen
    the way auto-switch chooses an alternative (#2409): the db lists every
    subscription that has not failed recently (kind-blind, #2352) in
    load-balance order, the cached provider headroom ranks them furthest from
    the nearest wall first and drops any the provider is currently refusing,
    and the first candidate whose token still decrypts (#340) wins — a
    viability filter walked in RANKED order, so the common case costs one
    decrypt instead of one per subscription.

    `database` is resolved at CALL time on purpose: the agent-creation test
    harnesses stub it per test, and a module-level binding taken on first
    import would answer the previous test's stub (learnings 2026-08-12).
    Fail-open on the ranking half only — Redis down or a bad import degrades
    to load-balance order, which is exactly the pre-#2409 round-robin — and
    loudly, so an inert ranker cannot pass for a working one.
    """
    from database import db as _db

    candidates = _db.list_assignable_subscriptions()
    if not candidates:
        return None
    try:
        headroom = importlib.import_module("services.subscription_headroom_service")
        readings = headroom.cached_headroom_readings([c.id for c in candidates])
        ranked = headroom.rank_subscriptions(candidates, readings)
    except Exception as e:  # noqa: BLE001 — the ranking may fail; assignment may not
        logger.warning(
            "[#2409] headroom ranking unavailable for new-agent assignment "
            "(%s: %s) — using load-balance order", type(e).__name__, e,
        )
        ranked = list(candidates)
    for sub in ranked:
        # #340: skip invalid/legacy tokens. Order-neutral, so it runs AFTER the
        # ranking rather than decrypting every candidate up front.
        if _db.get_subscription_token(sub.id):
            return sub
    return None

