"""
Subscription Auto-Switch Service (SUB-003).

Automatically switches an agent to a different subscription on the first
subscription failure — either a rate-limit (429) or an auth-class error
(401/403/credit balance/expired token, etc.).

Preconditions (all must be true):
1. Setting "auto_switch_subscriptions" is enabled (default: on, opt-out)
2. Agent has a subscription assigned (not API key)
3. At least one rate-limit / auth event recorded for this (agent, subscription)
4. At least one alternative subscription is available and not rate-limited

Threshold note (#441): pre-#441 we required 2+ consecutive 429s before
switching. That guaranteed at least one user-visible failure on long-running
schedules and never fired on auth-class breakage at all. The 2h skip-list on
alternative selection (`select_best_alternative_subscription` +
`is_subscription_rate_limited`) is what prevents thrashing — see
`tests/unit/test_subscription_auto_switch_pingpong.py` for the regression
tests pinning that contract.
"""

import asyncio
import logging
from typing import Optional

from database import db
from db_models import NotificationCreate

# Re-export the shared SUB-003 auth-class classifier (#1088) so existing
# consumers (routers/chat.py, services/task_execution_service.py) and their
# test patch targets keep importing `is_auth_failure` from this module
# unchanged. The redundant alias makes this an explicit re-export (recognised
# by ruff F401 + mypy --no-implicit-reexport).
from services.failure_classifier import is_auth_failure as is_auth_failure

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-agent switch lock (#799)
# ---------------------------------------------------------------------------
#
# Concurrent subscription failures on the SAME agent (two chat requests, or a
# chat overlapping a scheduled task) both enter `handle_subscription_failure`
# and, without mutual exclusion, both pick the same alternative, both
# `assign_subscription_to_agent`, and both fire `_restart_agent` — the second
# `container_stop` racing the first `start_agent_internal` wedges the container,
# duplicates the switch notification, or trips the #421 `was_already_running`
# ambiguity. A per-agent lock serializes the read→decide→assign→restart window.
#
# Mirrors the event-loop-safe lazy pattern in `services/agent_call_limiter.py`
# (module dict + a lazily-created guard) rather than `defaultdict(asyncio.Lock)`:
# a defaultdict binds each lock to whatever event loop is current at first key
# access and persists it, which breaks across pytest's per-test loops
# ("Future attached to a different loop"). Creating the locks lazily on the
# running loop avoids that.
#
# INVARIANT: process-local. Correct only while (a) the backend runs a single
# process and (b) the scheduler delegates execution to the backend via
# `/api/internal/execute-task` rather than calling this module in its own
# process — both true today. If the backend ever runs multiple workers, escalate
# to a Redis `SETNX` lock keyed `auto_switch:{agent_name}` (TTL ≥ longest
# plausible container start, ~60s).
_AGENT_SWITCH_LOCKS: dict[str, asyncio.Lock] = {}
_AGENT_SWITCH_LOCKS_GUARD: Optional[asyncio.Lock] = None


async def agent_switch_lock(agent_name: str) -> asyncio.Lock:
    """Return the per-agent switch lock, creating it lazily on the running loop."""
    global _AGENT_SWITCH_LOCKS_GUARD
    if _AGENT_SWITCH_LOCKS_GUARD is None:
        _AGENT_SWITCH_LOCKS_GUARD = asyncio.Lock()
    lock = _AGENT_SWITCH_LOCKS.get(agent_name)
    if lock is None:
        async with _AGENT_SWITCH_LOCKS_GUARD:
            lock = _AGENT_SWITCH_LOCKS.setdefault(agent_name, asyncio.Lock())
    return lock


def _reset_locks_for_test() -> None:
    """Test hook: drop all per-agent locks + the guard so each test's event loop
    starts clean (locks are loop-bound)."""
    global _AGENT_SWITCH_LOCKS_GUARD
    _AGENT_SWITCH_LOCKS.clear()
    _AGENT_SWITCH_LOCKS_GUARD = None


async def handle_subscription_failure(
    agent_name: str,
    error_message: str = "",
    failure_kind: str = "rate_limit",
) -> Optional[dict]:
    """
    Called when a subscription-backed agent fails with either a rate-limit (429)
    or an auth-class error.

    Records the event and triggers auto-switch on the first occurrence (subject
    to the alternative being viable per the 2h skip-list).

    Args:
        agent_name: name of the agent that failed
        error_message: server-side error string for audit + notification text
        failure_kind: "rate_limit" (429) or "auth" (401/403/credit/etc.)

    Returns:
        dict with switch details if auto-switch occurred, None otherwise.
    """
    # 1. Check if auto-switch is enabled (default: on, #441). Cheap, lock-free —
    # a disabled platform never contends for the per-agent lock.
    enabled = db.get_setting_value("auto_switch_subscriptions", default="true") == "true"
    if not enabled:
        return None

    # 2. Snapshot the agent's subscription BEFORE acquiring the lock. This is the
    # subscription our failure was (approximately) about. If a concurrent failure
    # switches the agent off it while we wait for the lock, our failure is stale.
    sub_at_entry = db.get_agent_subscription_id(agent_name)
    if not sub_at_entry:
        return None

    # #799: serialize the read→decide→assign→restart window per agent so two
    # concurrent failures on the same agent can't both switch + restart it.
    async with await agent_switch_lock(agent_name):
        # Re-read under the lock. If another coroutine already switched the agent
        # off `sub_at_entry`, this failure is stale — return rather than switch
        # again. This is what makes the fix correct for 3+ subscriptions: without
        # it, a loser whose failure was about sub-A would attribute it to the new
        # current sub-B and cascade A→B→C (#799 / Codex C8).
        current_sub_id = db.get_agent_subscription_id(agent_name)
        if current_sub_id != sub_at_entry:
            logger.info(
                f"[SUB-003] Agent '{agent_name}' already switched off subscription "
                f"{sub_at_entry} (now {current_sub_id}) before this {failure_kind} "
                f"failure acquired the lock — stale failure, skipping"
            )
            return None

        # 3. Record the failure event in the rate-limit table. Auth-class events
        # share the same table — the table tracks "subscription failure events"
        # generically; `is_subscription_rate_limited` treats any event in the 2h
        # window as a reason to skip the subscription as a candidate, which is the
        # behavior we want for both kinds of failure.
        consecutive_count = db.record_rate_limit_event(
            agent_name=agent_name,
            subscription_id=current_sub_id,
            error_message=error_message,
        )

        # 4. Find a viable alternative subscription
        alternative = db.select_best_alternative_subscription(current_sub_id)
        if not alternative:
            logger.warning(
                f"[SUB-003] Agent '{agent_name}' hit a {failure_kind} failure on "
                f"subscription {current_sub_id} (event #{consecutive_count}) "
                f"but no viable alternative subscription is available"
            )
            return None

        # Get current subscription name for logging / notification
        current_sub = db.get_subscription(current_sub_id)
        old_name = current_sub.name if current_sub else current_sub_id

        # 5. Perform the switch (still under the lock — the assign + restart must
        # not interleave with a concurrent switch for this agent).
        return await _perform_auto_switch(
            agent_name=agent_name,
            old_subscription_id=current_sub_id,
            old_subscription_name=old_name,
            new_subscription=alternative,
            failure_kind=failure_kind,
            event_count=consecutive_count,
        )


async def handle_rate_limit_error(
    agent_name: str,
    error_message: str = "",
) -> Optional[dict]:
    """Backward-compatible shim — delegates to `handle_subscription_failure`
    with `failure_kind="rate_limit"`. Existing 429 callers don't need to
    migrate atomically.
    """
    return await handle_subscription_failure(
        agent_name=agent_name,
        error_message=error_message,
        failure_kind="rate_limit",
    )


def _failure_phrase(failure_kind: str) -> str:
    """Notification + log wording per failure kind."""
    if failure_kind == "auth":
        return "an authentication failure"
    return "a rate-limit error"


async def _perform_auto_switch(
    agent_name: str,
    old_subscription_id: str,
    old_subscription_name: str,
    new_subscription,
    failure_kind: str,
    event_count: int,
) -> dict:
    """
    Execute the subscription switch: DB update, container restart, log, notify.
    """
    phrase = _failure_phrase(failure_kind)
    logger.info(
        f"[SUB-003] Auto-switching agent '{agent_name}' from '{old_subscription_name}' "
        f"to '{new_subscription.name}' after {phrase}"
    )

    # Switch subscription in DB
    db.assign_subscription_to_agent(agent_name, new_subscription.id)

    # NOTE: Do NOT clear rate-limit events for the old subscription here. The
    # events are the signal that the old subscription is still rate-limited —
    # `is_subscription_rate_limited()` counts them over a 2h window, and
    # `select_best_alternative_subscription()` uses that to filter candidates.
    # Clearing here causes a ping-pong between exhausted subscriptions because
    # the old sub looks viable on the next cycle (issue #444). Events age out
    # naturally via the 2h query window (enforced by iso_cutoff — see
    # utils/helpers.py, issue #476) and the 24h cleanup in
    # services/cleanup_service.py removes them from disk.

    # Rotate the subscription token on the running container via hot-reload so
    # in-flight turns survive the switch (#1089). Falls back to a full restart on
    # a 404 (old base image without the endpoint), transport failure, or when no
    # token is resolvable — identical to the previous recreate behavior.
    restart_result = await _hot_reload_subscription_token(agent_name)

    # Log activity event
    from services.activity_service import activity_service
    from models import ActivityType, ActivityState

    activity_id = await activity_service.track_activity(
        agent_name=agent_name,
        activity_type=ActivityType.SCHEDULE_END,  # System event
        triggered_by="system",
        details={
            "action": "subscription_auto_switch",
            "old_subscription": old_subscription_name,
            "new_subscription": new_subscription.name,
            "failure_kind": failure_kind,
            "event_count": event_count,
            "restart_result": restart_result,
        },
    )
    await activity_service.complete_activity(
        activity_id=activity_id,
        status=ActivityState.COMPLETED,
        details={"message": f"Auto-switched from '{old_subscription_name}' to '{new_subscription.name}'"},
    )

    # Send notification to agent owner
    try:
        db.create_notification(
            agent_name=agent_name,
            data=NotificationCreate(
                notification_type="alert",
                title=f"Subscription auto-switched to '{new_subscription.name}'",
                message=(
                    f"Agent '{agent_name}' was automatically switched from subscription "
                    f"'{old_subscription_name}' to '{new_subscription.name}' after {phrase}."
                ),
                priority="high",
                category="subscription",
                metadata={
                    "old_subscription": old_subscription_name,
                    "new_subscription": new_subscription.name,
                    "failure_kind": failure_kind,
                    "event_count": event_count,
                },
            )
        )
    except Exception as e:
        logger.error(f"[SUB-003] Failed to send auto-switch notification for '{agent_name}': {e}")

    result = {
        "switched": True,
        "agent_name": agent_name,
        "old_subscription": old_subscription_name,
        "new_subscription": new_subscription.name,
        "failure_kind": failure_kind,
        "event_count": event_count,
        "restart_result": restart_result,
    }

    logger.info(f"[SUB-003] Auto-switch complete: {result}")
    return result


async def _restart_agent(agent_name: str) -> str:
    """Restart an agent container to apply the new subscription token."""
    try:
        from services.docker_service import get_agent_container, get_agent_status_from_container
        from services.docker_utils import container_stop
        from services.agent_service import start_agent_internal

        container = get_agent_container(agent_name)
        if not container:
            return "no_container"

        agent_status = get_agent_status_from_container(container)
        if agent_status.status != "running":
            return "not_running"

        await container_stop(container)
        await start_agent_internal(agent_name)
        return "success"
    except Exception as e:
        logger.error(f"[SUB-003] Failed to restart agent '{agent_name}': {e}")
        return f"failed: {e}"


async def _hot_reload_subscription_token(agent_name: str) -> str:
    """Push the agent's current DB subscription token to the running container
    via ``POST /api/credentials/reload-token`` (#1089).

    The agent server mutates its own ``os.environ["CLAUDE_CODE_OAUTH_TOKEN"]``,
    so the NEXT claude subprocess uses the rotated token while in-flight turns
    keep their already-inherited old token and finish — "rotate a credential"
    is no longer the same operation as "kill every running turn".

    Falls back to the full ``_restart_agent`` recreate path (today's behavior,
    no regression) on:
      - a 404 — an old base image that predates the endpoint,
      - any transport / circuit failure (``AgentClientError`` family), or
      - no resolvable token for the agent's current subscription.
    Returns ``"no_container"`` / ``"not_running"`` when the agent is not a
    running container, mirroring ``_restart_agent``.
    """
    try:
        from services.docker_service import (
            get_agent_container,
            get_agent_status_from_container,
        )
        from services.agent_client import get_agent_client, AgentClientError

        container = get_agent_container(agent_name)
        if not container:
            return "no_container"
        if get_agent_status_from_container(container).status != "running":
            return "not_running"

        sub_id = db.get_agent_subscription_id(agent_name)
        token = db.get_subscription_token(sub_id) if sub_id else None
        if not token:
            # No token to push (e.g. assignment cleared mid-flight). Fall back to
            # the recreate path, which re-bakes Config.Env from the DB.
            return await _restart_agent(agent_name)

        client = get_agent_client(agent_name)
        try:
            # remove_api_key=False is intentional: subscription-backed agents never
            # carry ANTHROPIC_API_KEY in env (popped at create time, lifecycle.py).
            # The param is kept for a future mode-change-via-hot-reload caller.
            resp = await client.post(
                "/api/credentials/reload-token",
                json={"token": token, "remove_api_key": False},
                timeout=10.0,
            )
        except AgentClientError as e:
            logger.warning(
                f"[SUB-003] hot-reload transport failure for '{agent_name}': {e}; "
                f"falling back to restart"
            )
            return await _restart_agent(agent_name)

        if resp.status_code >= 400:  # 404 = old base image without the endpoint
            logger.info(
                f"[SUB-003] hot-reload returned HTTP {resp.status_code} for "
                f"'{agent_name}'; falling back to restart"
            )
            return await _restart_agent(agent_name)

        logger.info(f"[SUB-003] Hot-reloaded subscription token for '{agent_name}' (no recreate)")
        return "hot_reloaded"
    except Exception as e:
        logger.error(
            f"[SUB-003] hot-reload error for '{agent_name}': {e}; falling back to restart"
        )
        return await _restart_agent(agent_name)


async def reload_subscription_for_all_agents(subscription_id: str) -> dict[str, str]:
    """Hot-reload the subscription token on every running agent assigned to
    `subscription_id` (#1089 key rollover — re-registering a subscription's
    token via the `/api/subscriptions` upsert).

    Best-effort per agent, each under the #799 per-agent switch lock so a
    rollout can't interleave with a concurrent auto-switch: a failure on one
    agent is logged and does NOT abort the fan-out or block the others. Stopped
    agents are skipped by the helper (`not_running`) — they pick up the new
    token on next start (Config.Env is re-baked from the DB on recreate).
    Returns ``{agent_name: result}`` for observability.
    """
    results: dict[str, str] = {}
    for agent_name in db.get_agents_by_subscription(subscription_id):
        try:
            async with await agent_switch_lock(agent_name):
                results[agent_name] = await _hot_reload_subscription_token(agent_name)
        except Exception as e:
            logger.error(
                f"[SUB-003] key-rollover hot-reload failed for '{agent_name}': {e}"
            )
            results[agent_name] = f"failed: {e}"
    return results
