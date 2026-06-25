"""
Subscription credential management routes (SUB-002).

Provides endpoints for registering Claude Max/Pro subscription tokens
(from `claude setup-token`) and assigning them to agents.

Tokens are injected as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers.
Claude Code prioritizes ANTHROPIC_API_KEY over the OAuth token, so when a
subscription is assigned, ANTHROPIC_API_KEY is removed from the container.
"""

import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from models import User
from database import db
from dependencies import get_current_user
from db_models import (
    SubscriptionCredentialCreate,
    SubscriptionCredential,
    SubscriptionUsage,
    SubscriptionWithAgents,
    AgentAuthStatus,
)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])
logger = logging.getLogger(__name__)


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


# ============================================================================
# Subscription CRUD
# ============================================================================

@router.get("/encryption-status")
async def get_encryption_status(
    current_user: User = Depends(get_current_user)
):
    """Check if credential encryption is configured for subscriptions."""
    require_admin(current_user)
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    return {"configured": bool(key and len(key) >= 64)}


@router.post("", response_model=SubscriptionCredential)
async def register_subscription(
    request: SubscriptionCredentialCreate,
    current_user: User = Depends(get_current_user)
):
    """
    Register a new subscription token.

    Admin-only. Takes a long-lived token from `claude setup-token` and
    encrypts it for storage. Use upsert semantics - if a subscription with
    the same name exists, it will be updated.

    Token must start with `sk-ant-oat01-` (Claude Code OAuth access token).
    """
    require_admin(current_user)

    # Validate encryption key before attempting storage
    encryption_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not encryption_key:
        raise HTTPException(
            status_code=503,
            detail="Subscription registration requires the CREDENTIAL_ENCRYPTION_KEY environment variable. "
                   "Add it to your .env file (generate with: openssl rand -hex 32) and restart the backend."
        )

    try:
        # Get the user's ID
        user = db.get_user_by_username(current_user.username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        subscription = db.create_subscription(
            name=request.name,
            token=request.token,
            owner_id=user["id"],
            subscription_type=request.subscription_type,
            rate_limit_tier=request.rate_limit_tier,
        )

        logger.info(f"Registered subscription '{request.name}' by {current_user.username}")

        # #1089 (F1): a re-register (upsert) is a key rollover — fan a best-effort
        # hot-reload out to every running agent on this subscription so they pick
        # up the new token without a recreate. Swallowed on failure: the fan-out
        # must never fail the registration. (No-op on first registration — no
        # agents are assigned yet.)
        try:
            from services.subscription_auto_switch import reload_subscription_for_all_agents
            await reload_subscription_for_all_agents(subscription.id)
        except Exception as e:
            logger.error(
                f"[#1089] key-rollover hot-reload fan-out failed for "
                f"subscription '{request.name}': {e}"
            )

        return subscription

    except HTTPException:
        raise  # Let HTTP exceptions propagate as-is
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to register subscription: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to register subscription: {str(e)}")


@router.get("", response_model=List[SubscriptionWithAgents])
async def list_subscriptions(
    current_user: User = Depends(get_current_user)
):
    """
    List all subscriptions with their assigned agents.

    Admin-only. Returns subscription metadata and agent assignments.
    Never returns the encrypted credentials.
    """
    require_admin(current_user)

    return db.list_subscriptions_with_agents()


@router.get("/{subscription_id}/usage", response_model=SubscriptionUsage)
async def get_subscription_usage(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get rolling usage statistics for a subscription (SUB-004).

    Admin-only. Returns token and cost aggregates across two rolling windows:
    - window_5h: last 5 hours
    - window_7d: last 7 days

    Covers both chat messages and schedule executions attributed to this subscription.
    """
    require_admin(current_user)

    # Resolve by ID or name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        return db.get_subscription_usage(subscription.id)
    except Exception as e:
        logger.error(f"Failed to get usage for subscription {subscription_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve usage data")


@router.get("/{subscription_id}", response_model=SubscriptionWithAgents)
async def get_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get details for a specific subscription.

    Admin-only. Returns subscription metadata and assigned agents.
    """
    require_admin(current_user)

    # Try by ID first, then by name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Get assigned agents
    agents = db.get_agents_by_subscription(subscription.id)

    return SubscriptionWithAgents(
        **subscription.model_dump(),
        agents=agents
    )


@router.delete("/{subscription_id}")
async def delete_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a subscription.

    Admin-only. Cascade clears all agent assignments - agents will fall back
    to API key authentication.
    """
    require_admin(current_user)

    # Try by ID first, then by name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Get agents that will be affected
    affected_agents = db.get_agents_by_subscription(subscription.id)

    deleted = db.delete_subscription(subscription.id)

    if deleted:
        logger.info(
            f"Deleted subscription '{subscription.name}' by {current_user.username}, "
            f"cleared {len(affected_agents)} agent assignments"
        )
        return {
            "success": True,
            "message": f"Subscription '{subscription.name}' deleted",
            "agents_cleared": affected_agents
        }

    raise HTTPException(status_code=500, detail="Failed to delete subscription")


# ============================================================================
# Agent Subscription Assignment
# ============================================================================

@router.put("/agents/{agent_name}")
async def assign_subscription_to_agent(
    agent_name: str,
    subscription_name: str = Query(..., description="Name of subscription to assign"),
    current_user: User = Depends(get_current_user)
):
    """
    Assign a subscription to an agent.

    Owner access required. The token is applied to a running agent based on the
    kind of change (#1089):
      - sub → sub swap: hot-reloaded in place via /api/credentials/reload-token,
        so in-flight turns survive (no container recreate);
      - none/api-key → subscription (an auth-MODE change): the container is
        recreated so `ANTHROPIC_API_KEY` is dropped and `CLAUDE_CODE_OAUTH_TOKEN`
        is baked into Config.Env.
    Both run under the #799 per-agent switch lock so a manual reassignment can't
    interleave with a concurrent auto-switch on the same agent.
    """
    # Owner or admin only — shared users must not mutate subscription assignments
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the agent owner or an admin can manage subscriptions")

    # Get subscription by name
    subscription = db.get_subscription_by_name(subscription_name)
    if not subscription:
        raise HTTPException(status_code=404, detail=f"Subscription '{subscription_name}' not found")

    try:
        from services.subscription_auto_switch import (
            agent_switch_lock,
            _hot_reload_subscription_token,
        )

        # #799/#1089: serialize the assign + apply window per agent so a manual
        # reassignment can't interleave with a concurrent auto-switch.
        async with await agent_switch_lock(agent_name):
            # #1089: snapshot the agent's CURRENT subscription under the lock,
            # before reassigning, so a concurrent auto-switch can't change it
            # between the read and the assign (TOCTOU). A sub→sub swap
            # (old_sub_id is not None) hot-reloads the token in place; an
            # auth-mode change (old_sub_id is None) still needs the container
            # recreated.
            old_sub_id = db.get_agent_subscription_id(agent_name)
            db.assign_subscription_to_agent(agent_name, subscription.id)

            logger.info(
                f"Assigned subscription '{subscription_name}' to agent '{agent_name}' "
                f"by {current_user.username}"
            )

            restart_result = None
            injection_result = None

            if old_sub_id is not None:
                # sub → sub: hot-reload the token without recreating the container.
                # The helper itself short-circuits ("not_running"/"no_container")
                # for stopped agents and falls back to a recreate on 404 / transport
                # failure / missing token.
                restart_result = await _hot_reload_subscription_token(agent_name)
                injection_result = {"status": restart_result}
            else:
                # none/api-key → subscription: an auth-MODE change still requires a
                # recreate so ANTHROPIC_API_KEY is dropped and the OAuth token is
                # baked into Config.Env.
                from services.docker_service import get_agent_container, get_agent_status_from_container
                from services.docker_utils import container_stop
                from services.agent_service import start_agent_internal

                container = get_agent_container(agent_name)
                if container:
                    agent_status = get_agent_status_from_container(container)
                    if agent_status.status == "running":
                        try:
                            await container_stop(container)
                            await start_agent_internal(agent_name)
                            restart_result = "success"
                            injection_result = {"status": "success"}
                            logger.info(
                                f"Restarted agent '{agent_name}' to apply subscription token"
                            )
                        except Exception as e:
                            logger.error(f"Failed to restart agent '{agent_name}' for subscription: {e}")
                            restart_result = f"failed: {e}"
                            injection_result = {"status": "failed", "error": str(e)}
                    else:
                        injection_result = {"status": "agent_not_running"}
                else:
                    injection_result = {"status": "agent_not_running"}

        return {
            "success": True,
            "message": f"Subscription '{subscription_name}' assigned to agent '{agent_name}'",
            "agent_name": agent_name,
            "subscription_name": subscription_name,
            "restart_result": restart_result,
            "injection_result": injection_result,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/agents/{agent_name}")
async def clear_agent_subscription(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Clear subscription assignment from an agent.

    Owner access required. Agent will fall back to API key authentication.
    """
    # Owner or admin only — shared users must not mutate subscription assignments
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the agent owner or an admin can manage subscriptions")

    # Get current subscription for logging
    current_sub = db.get_agent_subscription(agent_name)

    db.clear_agent_subscription(agent_name)

    if current_sub:
        logger.info(
            f"Cleared subscription '{current_sub.name}' from agent '{agent_name}' "
            f"by {current_user.username}"
        )

    # Restart running agent so ANTHROPIC_API_KEY is restored (if use_platform_api_key=1)
    restart_result = None
    from services.docker_service import get_agent_container, get_agent_status_from_container
    from services.docker_utils import container_stop
    from services.agent_service import start_agent_internal
    container = get_agent_container(agent_name)
    if container:
        agent_status = get_agent_status_from_container(container)
        if agent_status.status == "running":
            try:
                await container_stop(container)
                await start_agent_internal(agent_name)
                restart_result = "success"
                logger.info(f"Restarted agent '{agent_name}' to restore API key after subscription removal")
            except Exception as e:
                logger.error(f"Failed to restart agent '{agent_name}' after subscription removal: {e}")
                restart_result = f"failed: {e}"

    return {
        "success": True,
        "message": f"Subscription cleared from agent '{agent_name}'",
        "agent_name": agent_name,
        "previous_subscription": current_sub.name if current_sub else None,
        "restart_result": restart_result,
    }


@router.get("/agents/{agent_name}/auth", response_model=AgentAuthStatus)
async def get_agent_auth_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get the authentication status for an agent.

    Returns whether the agent is using subscription, API key, or not configured.
    Owner access required.
    """
    # Check agent access
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied to this agent")

    try:
        from services.subscription_service import get_agent_auth_mode
        return await get_agent_auth_mode(agent_name)
    except Exception as e:
        logger.error(f"Failed to get auth status for agent {agent_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Auto-Switch Setting (SUB-003)
# =========================================================================

@router.get("/settings/auto-switch")
async def get_auto_switch_setting(
    current_user: User = Depends(get_current_user)
):
    """Get the auto-switch subscriptions setting."""
    require_admin(current_user)
    # #441: default flipped to "true" (opt-out). Must match the default in
    # services/subscription_auto_switch.handle_subscription_failure so the UI
    # toggle and the runtime gate read the same value on a clean install.
    enabled = db.get_setting_value("auto_switch_subscriptions", default="true") == "true"
    return {"enabled": enabled}


@router.put("/settings/auto-switch")
async def set_auto_switch_setting(
    enabled: bool,
    current_user: User = Depends(get_current_user)
):
    """Enable or disable automatic subscription switching on rate-limit errors."""
    require_admin(current_user)
    db.set_setting("auto_switch_subscriptions", "true" if enabled else "false")
    logger.info(f"Auto-switch subscriptions {'enabled' if enabled else 'disabled'} by {current_user.username}")
    return {"enabled": enabled}
