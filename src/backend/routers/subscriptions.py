"""
Subscription credential management routes (SUB-002).

Provides endpoints for registering Claude Max/Pro subscription tokens
(from `claude setup-token`) and assigning them to agents.

Tokens are injected as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers.
Claude Code prioritizes ANTHROPIC_API_KEY over the OAuth token, so when a
subscription is assigned, ANTHROPIC_API_KEY is removed from the container.
"""

import asyncio
import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from models import SubscriptionHeadroomHistory, User
from database import db
from dependencies import get_current_user, assert_admin, assert_agent_access, assert_agent_owner
from db_models import (
    SubscriptionCredentialCreate,
    SubscriptionCredential,
    SubscriptionUsage,
    SubscriptionUsageBreakdown,
    SubscriptionWithAgents,
    AgentAuthStatus,
)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])
logger = logging.getLogger(__name__)


# ============================================================================
# Subscription CRUD
# ============================================================================

@router.get("/encryption-status")
async def get_encryption_status(
    current_user: User = Depends(get_current_user)
):
    """Check if credential encryption is configured for subscriptions."""
    assert_admin(current_user)
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
    assert_admin(current_user)

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
    List subscriptions with their assigned agents. Admin sees the fleet; every
    other caller sees only their own. Never returns the encrypted credentials.

    ent#293 review: this was `assert_admin`, which broke the subscription
    workflow asymmetrically once admin gates stopped accepting agent-scoped
    keys — `assign_subscription` / `clear_agent_subscription` / `get_agent_auth`
    all kept working (owner gates), so an agent could ASSIGN a subscription it
    could no longer ENUMERATE. A half-working workflow is worse than a closed
    door.

    Scoped rather than un-gated, deliberately. Dropping the gate entirely was
    the first attempt and it was wrong: the payload carries `owner_email` and
    the full agent-name list of EVERY subscription, so an ungated read hands any
    `role=user` account a fleet-wide owner-email and agent-name enumeration
    oracle — the disclosure class Invariant #8 exists to prevent, reintroduced
    by a fix for a different disclosure. The `owner_id` filter is already part
    of the accessor's contract, so scoping costs nothing and restores exactly
    the read the workflow needs: an agent key resolves to its owner and sees
    that owner's subscriptions, which are the only ones it can assign anyway.
    """
    if current_user.role == "admin" and not getattr(current_user, "agent_name", None):
        return db.list_subscriptions_with_agents()
    return db.list_subscriptions_with_agents(owner_id=current_user.id)


@router.get("/{subscription_id}/usage", response_model=SubscriptionUsage)
async def get_subscription_usage(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get rolling usage statistics for a subscription (SUB-004).

    Returns token and cost aggregates across two rolling windows (the docstring
    said "Admin-only" while the gate has always been `get_current_user` — noted
    while auditing this module for ent#293):
    - window_5h: last 5 hours
    - window_7d: last 7 days

    Covers both chat messages and schedule executions attributed to this subscription.

    #471: additionally carries failure-event counts (24h, per-kind), the
    one-gate `rate_limited_now`, and — when available — the provider-truth
    `headroom` block (`source: "anthropic"`); the DB-derived windows are always
    populated regardless (`source: "observed"` fallback). The ambient probe
    behind `headroom` is governed by `subscription_headroom_auto_refresh`
    (default ON) and fail-closed rules — see subscription_headroom_service.
    """
    # #2323 — the ops fence admits this route, so the gate must too. It did
    # not: `ADMIN_GATE_SCOPES` excludes "ops", so an ops key passed
    # `_enforce_ops_key_fence` and was then refused here — the subscription-
    # pressure read the fence was measured for could not work. Caught in
    # review; the admit-set test exercised only the fence, never the gate.
    assert_admin(current_user, allow_scopes={"ops"})

    # Resolve by ID or name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        from services.subscription_headroom_service import decorate_usage
        usage = db.get_subscription_usage(subscription.id)
        return await decorate_usage(usage)
    except Exception as e:
        logger.error(f"Failed to get usage for subscription {subscription_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve usage data")


@router.get(
    "/{subscription_id}/headroom/history",
    response_model=SubscriptionHeadroomHistory,
)
async def get_subscription_headroom_history(
    subscription_id: str,
    window: str = "7d",
    current_user: User = Depends(get_current_user)
):
    """Windowed headroom utilization series for a subscription (ent#433).

    Answers "how close did we run to the 5h wall this week", which the #471
    live snapshot structurally cannot: it keeps exactly one reading per
    subscription and overwrites it on every probe.

    Read-only — this never probes, so viewing a trend costs no subscription
    quota. Admin-only, mirroring `/usage` (`assert_admin` also rejects agent
    principals, #1890), and resolves by id OR name for parity with it: an
    operator who just used a name on `/usage` must not find it rejected here,
    and a typo must 404 rather than returning an empty series that reads as
    "no data yet".

    Each bucket carries the LAST probe in it, plus BOTH its logical
    `bucket_start` and the real `fetched_at` — the pair is what makes a gap
    distinguishable from sample jitter. Absent buckets are absent; nothing is
    interpolated or zero-filled.
    """
    assert_admin(current_user)

    from services.subscription_headroom_service import HISTORY_WINDOWS, get_history

    if window not in HISTORY_WINDOWS:
        # Named validation, never a generic 500 — and deliberately a hard
        # reject rather than a silent fall back to the default: this parameter
        # is the chart's AXIS, and quietly redrawing a window the caller never
        # asked for is the wrong kind of forgiving.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid window '{window}'. "
                f"Expected one of: {', '.join(sorted(HISTORY_WINDOWS))}"
            ),
        )

    # Resolve by ID or name (parity with /usage)
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        return get_history(subscription.id, window)
    except Exception as e:
        logger.error(
            f"Failed to get headroom history for subscription {subscription_id}: {e}"
        )
        raise HTTPException(
            status_code=500, detail="Failed to retrieve headroom history"
        )


@router.get("/{subscription_id}/usage/breakdown", response_model=SubscriptionUsageBreakdown)
async def get_subscription_usage_breakdown(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Per-agent consumption breakdown for a subscription, 5h + 7d windows (#471
    Tier 2). Rows are ranked by `cost_usd` desc — cost is model-weighted by
    construction, the honest "who burns the quota" ordering on a mixed-model
    subscription. Admin-only (mirrors `/usage`; revisited when ent#351's
    agent-facing tools land).
    """
    assert_admin(current_user)

    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        return db.get_subscription_usage_breakdown(subscription.id)
    except Exception as e:
        logger.error(f"Failed to get usage breakdown for subscription {subscription_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve usage breakdown")


@router.post("/{subscription_id}/usage/refresh", response_model=SubscriptionUsage)
async def refresh_subscription_headroom(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Click-to-refresh the provider headroom snapshot (#471): fires ONE probe for
    this subscription (floored at 60s apart — a re-click inside the floor
    serves the cached snapshot with its honest age) and returns the decorated
    usage. Admin-only. The probe consumes ~a dozen Haiku tokens of the
    subscription's own quota and appears in the Anthropic console.
    """
    assert_admin(current_user)

    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    try:
        from services.subscription_headroom_service import decorate_usage
        usage = db.get_subscription_usage(subscription.id)
        return await decorate_usage(usage, force=True)
    except Exception as e:
        logger.error(f"Headroom refresh failed for subscription {subscription_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to refresh headroom")


@router.get("/{subscription_id}", response_model=SubscriptionWithAgents)
async def get_subscription(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get details for a specific subscription.

    Admin-only. Returns subscription metadata and assigned agents.
    """
    assert_admin(current_user)

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
    assert_admin(current_user)

    # Try by ID first, then by name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # Get agents that will be affected
    affected_agents = db.get_agents_by_subscription(subscription.id)

    deleted = db.delete_subscription(subscription.id)

    # #471: best-effort headroom-snapshot cleanup (transient Redis telemetry)
    try:
        from services.subscription_headroom_service import clear_snapshot
        clear_snapshot(subscription.id)
    except Exception:
        pass

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
    assert_agent_owner(current_user, agent_name, detail="Only the agent owner or an admin can manage subscriptions")

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
    assert_agent_owner(current_user, agent_name, detail="Only the agent owner or an admin can manage subscriptions")

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
    assert_agent_access(current_user, agent_name, detail="Access denied to this agent")

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
    assert_admin(current_user)
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
    assert_admin(current_user)
    db.set_setting("auto_switch_subscriptions", "true" if enabled else "false")
    logger.info(f"Auto-switch subscriptions {'enabled' if enabled else 'disabled'} by {current_user.username}")
    return {"enabled": enabled}


# =========================================================================
# Headroom Auto-Refresh Setting (#471)
# =========================================================================

@router.get("/settings/headroom-auto-refresh")
async def get_headroom_auto_refresh_setting(
    current_user: User = Depends(get_current_user)
):
    """Whether the platform ambiently refreshes provider headroom snapshots
    (#471). Default ON (operator ruling at the build gate); must match the
    default in `subscription_headroom_service.is_auto_refresh_enabled` so the
    UI toggle and the runtime gate read the same value on a clean install."""
    assert_admin(current_user)
    from services.subscription_headroom_service import (
        is_auto_refresh_enabled,
        REFRESH_SECONDS,
        SAMPLE_INTERVAL_SECONDS,
    )
    from services import subscription_headroom_alerts as alerts

    # Off the event loop: the sweep already threads the same `list_subscriptions`
    # call, and this handler is `async`. Admin-only and low-traffic, so the cost
    # is small either way — but a sync DB read on an async path is the shape
    # `_record_history` documents at length, and there is no reason to add one.
    # One threaded call, not three: the status helper already resolves the
    # toggle and the threshold, and reading them twice invites the two answers
    # to disagree across the gap.
    status = await asyncio.to_thread(_weekly_alert_status_blocking)
    enabled = status.pop("_auto_refresh_enabled")
    return {
        "enabled": enabled,
        "refresh_seconds": REFRESH_SECONDS,
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        # ent#434 — the weekly-headroom alert rides this same toggle, so its
        # state belongs on this payload rather than a second endpoint.
        "weekly_alert": status,
    }


def _weekly_alert_status_blocking() -> dict:
    """Report the weekly-headroom alert's real state, including WHY it is off.

    AC #4 (and #2217's lesson) is that "no alerts" must be distinguishable from
    "not checking". A bare boolean cannot say that, so every inactive path
    names itself. The reasons are ordered by how much they dominate: no
    subscriptions is silence by design, a disabled threshold is an explicit
    operator choice, the toggle governs all autonomous probing, and an
    unreachable Redis means the sampler fails closed and evaluates nothing.
    """
    from redis_breaker_util import get_breaker_redis
    from services import subscription_headroom_alerts as alerts
    from services.subscription_headroom_service import is_auto_refresh_enabled

    auto_refresh_enabled = is_auto_refresh_enabled()
    threshold = alerts.effective_threshold_pct()

    reason = None
    try:
        subscription_count = len(db.list_subscriptions() or [])
    except Exception:  # noqa: BLE001
        subscription_count = None

    redis_ok = False
    try:
        client = get_breaker_redis()
        # A client object existing is not Redis being reachable
        # (learnings 2026-08-19) — ping, do not just check for None.
        redis_ok = bool(client is not None and client.ping())
    except Exception:  # noqa: BLE001
        redis_ok = False

    if subscription_count is None:
        # `None` means the list could not be READ, which is not zero and is
        # certainly not health. Without its own arm it fell through every
        # branch below (`None == 0` is False) and returned `active: true` —
        # this function's whole job is naming why it is off, so claiming
        # health from a failed read was the one dishonest path in it.
        reason = "count_unavailable"
    elif subscription_count == 0:
        reason = "no_subscriptions"
    elif threshold == 0:
        reason = "threshold_disabled"
    elif not auto_refresh_enabled:
        reason = "auto_refresh_off"
    elif not redis_ok:
        reason = "redis_unavailable"

    return {
        "active": reason is None,
        "inactive_reason": reason,
        "threshold_pct": threshold,
        "escalation_pct": alerts.escalation_pct(threshold) if threshold else None,
        "default_pct": alerts.DEFAULT_THRESHOLD_PCT,
        "min_pct": alerts.MIN_THRESHOLD_PCT,
        "max_pct": alerts.MAX_THRESHOLD_PCT,
        "subscription_count": subscription_count,
        # consumed by the caller for the top-level `enabled` field, so the
        # toggle and the alert status can never disagree about it
        "_auto_refresh_enabled": auto_refresh_enabled,
    }


@router.put("/settings/headroom-auto-refresh")
async def set_headroom_auto_refresh_setting(
    enabled: bool,
    current_user: User = Depends(get_current_user)
):
    """Enable/disable autonomous headroom probing.

    Each probe sends one minimal (~a dozen tokens) message on the
    subscription's own token, floored per subscription. It governs ALL
    autonomous probing, not just the dashboard-driven kind: the ambient
    refresh behind an open dashboard (#471), the recovery probe that notices a
    rate-limited subscription coming back (#447), and the weekly-window
    sampler behind the headroom alert (ent#434) — the last two run whether or
    not anyone is watching. An earlier version of this docstring said probing
    happened "only while a dashboard is watching", which stopped being true at
    #447. Click-to-refresh works regardless of this toggle."""
    assert_admin(current_user)
    from services.subscription_headroom_service import AUTO_REFRESH_SETTING
    db.set_setting(AUTO_REFRESH_SETTING, "true" if enabled else "false")
    logger.info(
        f"Subscription headroom auto-refresh {'enabled' if enabled else 'disabled'} "
        f"by {current_user.username}"
    )
    return {"enabled": enabled}


@router.put("/settings/headroom-alert-threshold")
async def set_headroom_alert_threshold(
    threshold_pct: int,
    current_user: User = Depends(get_current_user)
):
    """Set the weekly-window alert threshold (ent#434).

    `0` disables the alerts (the `operator_queue_retention_days` idiom).
    Otherwise the value must be in `[MIN, MAX]` — below the minimum a weekly
    window is barely started and every fleet would alarm; at 100 the alert is
    unreachable given the provider's 1-decimal rounding.

    There is deliberately ONE knob: the escalation tier is derived from it
    (`escalation_pct`). Two independently-settable thresholds are an
    oscillator — a fixed escalation under a higher threshold fires below the
    warning — and `validate_ops_setting` is per-key, so a cross-field
    invariant could not be expressed where the other write path enforces it.
    """
    assert_admin(current_user)
    from services import subscription_headroom_alerts as alerts

    if threshold_pct != 0 and not (
        alerts.MIN_THRESHOLD_PCT <= threshold_pct <= alerts.MAX_THRESHOLD_PCT
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"threshold_pct must be 0 (disabled) or between "
                f"{alerts.MIN_THRESHOLD_PCT} and {alerts.MAX_THRESHOLD_PCT}; "
                f"got {threshold_pct}"
            ),
        )
    db.set_setting(alerts.THRESHOLD_SETTING, str(threshold_pct))
    logger.info(
        "Subscription weekly-headroom alert threshold set to %s%% by %s",
        threshold_pct, current_user.username,
    )
    return {
        "threshold_pct": threshold_pct,
        "escalation_pct": alerts.escalation_pct(threshold_pct) if threshold_pct else None,
        "enabled": threshold_pct > 0,
    }
