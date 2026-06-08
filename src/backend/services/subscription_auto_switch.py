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

import logging
from typing import Optional

from database import db
from db_models import NotificationCreate

logger = logging.getLogger(__name__)


# Substrings that indicate an auth-class subscription failure. Mirrors the
# scheduler's classification at `src/scheduler/service.py` (which now imports
# this same list to keep the two surfaces from drifting).
AUTH_INDICATORS = [
    "credit balance",
    "unauthorized",
    "authentication",
    "credentials",
    "forbidden",
    "401",
    "403",
    "oauth",
    "token expired",
    "not authenticated",
]

# #904: unambiguous signal-kill / OOM / timeout markers. When the error
# message contains any of these we short-circuit `is_auth_failure` to
# False even if an AUTH_INDICATOR also happens to match — a SIGKILL is
# evidence the subprocess died from outside, not from a real auth
# response on the wire, and triggering SUB-003 burns the 2h skip-list
# slot for the alternative subscription without fixing anything.
NON_AUTH_KILL_MARKERS = [
    "sigkill",
    "sigterm",
    "sigint",
    "exit code -9",
    "exit code -15",
    "exit code -2",
    "exit code 137",   # 128 + 9 (shell-encoded SIGKILL)
    "exit code 143",   # 128 + 15 (shell-encoded SIGTERM)
    "exit code 130",   # 128 + 2 (shell-encoded SIGINT)
    "terminated by",
    "killed by",
    "out of memory",
    "oom",
    "memory cgroup",
]


def is_auth_failure(error_message: str) -> bool:
    """Return True if `error_message` contains any AUTH_INDICATORS substring
    AND does not also contain an unambiguous signal-kill / OOM / timeout
    marker (#904)."""
    if not error_message:
        return False
    error_lower = error_message.lower()
    if any(marker in error_lower for marker in NON_AUTH_KILL_MARKERS):
        return False
    return any(ind in error_lower for ind in AUTH_INDICATORS)


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
    # 1. Check if auto-switch is enabled (default: on, #441)
    enabled = db.get_setting_value("auto_switch_subscriptions", default="true") == "true"
    if not enabled:
        return None

    # 2. Check if agent has a subscription assigned
    current_sub_id = db.get_agent_subscription_id(agent_name)
    if not current_sub_id:
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

    # 5. Perform the switch
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

    # Restart agent container to apply new subscription token
    restart_result = await _restart_agent(agent_name)

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
