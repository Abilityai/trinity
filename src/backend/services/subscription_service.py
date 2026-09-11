"""
Subscription Service (SUB-002)

Manages Claude Max/Pro subscription token assignment and auth mode detection.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time.

No file injection is needed — the token is part of the container environment.
"""

import asyncio
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


# =============================================================================
# The install's first Claude credential (trinity-enterprise#582)
# =============================================================================

def is_claude_auth_configured() -> bool:
    """Can ANY agent on this instance authenticate to Claude?

    A platform Anthropic key (settings or env) OR any registered subscription.
    The one definition behind the `claude_auth_configured` feature flag and the
    first-credential check below, so "configured" cannot mean two things.
    """
    from database import db as _db
    from services.settings_service import get_anthropic_api_key

    return bool(get_anthropic_api_key()) or _db.has_any_subscription()


# Strong refs for the fire-and-forget restarts — the event loop holds only a
# WEAK reference to a bare `create_task` (the #1083 `_inflight` footgun).
_inflight_connect_tasks: "set[asyncio.Task]" = set()


def connect_agents_to_first_credential(subscription_id: Optional[str] = None) -> list:
    """Bring agents that were created with NO Claude credential onto the first one.

    Call only when a write took the install from not-configured to configured.
    Agents created before any credential existed — the ent#124 seeded fleet,
    Cornelius, `trinity-system` — were baked with no Claude auth: #74 auto-assign
    runs only at create, and nothing re-bakes a running container's env. Without
    this the operator finishes the Claude step and the fleet on the dashboard
    still cannot run.

    Scope is exactly the agents that could not authenticate anyway: Claude
    runtime, not ephemeral (ghosts never recreate — ent#69), no subscription,
    and `use_platform_api_key` on. Off means the owner chose the agent's own
    `.env` key or a terminal login; a subscription would shadow that (#2114),
    so those agents are left alone.

    `subscription_id` given → assign it to each (DB, now). Either way, running
    agents are restarted in the background so the new env is baked — an
    auth-mode change is a recreate (the assign route's rule); stopped agents
    pick it up on their next start via `check_api_key_env_matches`. Returns the
    names acted on. Never raises: the credential is already saved.
    """
    from database import db as _db
    from services.agent_service.helpers import is_claude_runtime
    from services.docker_service import list_all_agents_fast

    try:
        agents = list_all_agents_fast()
    except Exception as e:  # noqa: BLE001 — never fail the credential save
        logger.warning("[ent#582] could not list agents to connect: %s", e)
        return []

    connected, to_restart = [], []
    for agent in agents:
        try:
            if agent.ephemeral or not is_claude_runtime(agent.runtime):
                continue
            if _db.get_agent_subscription_id(agent.name) is not None:
                continue
            if not _db.get_use_platform_api_key(agent.name):
                continue
            if subscription_id:
                _db.assign_subscription_to_agent(agent.name, subscription_id)
            connected.append(agent.name)
            if agent.status == "running":
                to_restart.append(agent.name)
        except Exception as e:  # noqa: BLE001 — one bad agent must not stop the rest
            logger.warning("[ent#582] could not connect agent '%s': %s", agent.name, e)

    if to_restart:
        try:
            task = asyncio.create_task(_restart_connected_agents(to_restart))
            _inflight_connect_tasks.add(task)
            task.add_done_callback(_inflight_connect_tasks.discard)
        except RuntimeError:  # no running loop — they pick it up on next start
            logger.info("[ent#582] no event loop; %d agent(s) connect on next start", len(to_restart))
    if connected:
        logger.info("[ent#582] first Claude credential connected %d agent(s): %s",
                    len(connected), ", ".join(connected))
    return connected


async def _restart_connected_agents(agent_names: list) -> None:
    """Recreate each running agent so the new credential is in its env.

    Sequential, under the #799 per-agent switch lock so a restart cannot
    interleave with a concurrent SUB-003 auto-switch on the same agent.
    """
    from services.subscription_auto_switch import _restart_agent, agent_switch_lock

    for name in agent_names:
        try:
            async with await agent_switch_lock(name):
                result = await _restart_agent(name)
            logger.info("[ent#582] restarted '%s' onto the first Claude credential: %s", name, result)
        except Exception as e:  # noqa: BLE001
            logger.error("[ent#582] restart of '%s' failed: %s", name, e)

