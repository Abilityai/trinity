"""
Subscription Auto-Switch Service (SUB-003).

Automatically switches an agent to a different subscription when it
encounters repeated rate-limit errors (2+ consecutive).

Preconditions (all must be true):
1. Setting "auto_switch_subscriptions" is enabled
2. Agent has a subscription assigned (not API key)
3. 2+ consecutive rate-limit errors on this (agent, subscription)
4. At least one alternative subscription is available and not rate-limited
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import db
from db_models import NotificationCreate

logger = logging.getLogger(__name__)


def _parse_rate_limit_reset(error_message: str) -> Optional[str]:
    """Parse 'resets 8pm (America/New_York)' from a 429 body → UTC ISO string, or None.

    Anthropic includes the reset time in the error message. Persisting it lets
    is_subscription_rate_limited() block the subscription until it actually resets
    rather than relying on the 2-hour rolling window (which is too short).
    """
    match = re.search(
        r'resets\s+(\d{1,2}(?::\d{2})?(?:am|pm)?)\s+\(([^)]+)\)',
        error_message, re.IGNORECASE
    )
    if not match:
        return None
    time_str, tz_str = match.group(1), match.group(2)
    try:
        tz = ZoneInfo(tz_str)
        today = datetime.now(tz).date()
        fmt = "%I:%M%p" if ":" in time_str else "%I%p"
        t = datetime.strptime(time_str.upper(), fmt)
        reset_local = datetime.combine(today, t.time(), tzinfo=tz)
        if reset_local <= datetime.now(tz):
            reset_local += timedelta(days=1)
        return reset_local.astimezone(ZoneInfo("UTC")).isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return None


async def handle_rate_limit_error(
    agent_name: str,
    error_message: str = "",
) -> Optional[dict]:
    """
    Called when a 429 rate-limit error is received from an agent.

    Records the event and triggers auto-switch if all preconditions are met.

    Returns:
        dict with switch details if auto-switch occurred, None otherwise.
    """
    # 1. Check if auto-switch is enabled
    enabled = db.get_setting_value("auto_switch_subscriptions", default="false") == "true"
    if not enabled:
        return None

    # 2. Check if agent has a subscription assigned
    current_sub_id = db.get_agent_subscription_id(agent_name)
    if not current_sub_id:
        return None

    # 3. Record the rate-limit event and persist the authoritative reset time if parseable
    consecutive_count = db.record_rate_limit_event(
        agent_name=agent_name,
        subscription_id=current_sub_id,
        error_message=error_message
    )
    reset_until = _parse_rate_limit_reset(error_message)
    if reset_until:
        db.set_subscription_rate_limited_until(current_sub_id, reset_until)
        logger.info(f"[SUB-003] Subscription {current_sub_id} rate-limited until {reset_until}")

    if consecutive_count < 2:
        logger.info(
            f"[SUB-003] Rate-limit event #{consecutive_count} for agent '{agent_name}' "
            f"on subscription {current_sub_id} — waiting for 2+ before auto-switch"
        )
        return None

    # 4. Find a viable alternative subscription
    alternative = db.select_best_alternative_subscription(current_sub_id)
    if not alternative:
        logger.warning(
            f"[SUB-003] Agent '{agent_name}' has {consecutive_count} consecutive rate-limit errors "
            f"but no viable alternative subscription found"
        )
        return None

    # Get current subscription name for logging
    current_sub = db.get_subscription(current_sub_id)
    old_name = current_sub.name if current_sub else current_sub_id

    # 5. Perform the switch
    return await _perform_auto_switch(
        agent_name=agent_name,
        old_subscription_id=current_sub_id,
        old_subscription_name=old_name,
        new_subscription=alternative,
        consecutive_count=consecutive_count,
    )


async def _perform_auto_switch(
    agent_name: str,
    old_subscription_id: str,
    old_subscription_name: str,
    new_subscription,
    consecutive_count: int,
) -> dict:
    """
    Execute the subscription switch: DB update, container restart, log, notify.
    """
    logger.info(
        f"[SUB-003] Auto-switching agent '{agent_name}' from '{old_subscription_name}' "
        f"to '{new_subscription.name}' after {consecutive_count} consecutive rate-limit errors"
    )

    # Switch subscription in DB
    db.assign_subscription_to_agent(agent_name, new_subscription.id)

    # NOTE: Do NOT clear rate-limit events for the old subscription here. The
    # events are the signal that the old subscription is still rate-limited —
    # `is_subscription_rate_limited()` counts them over a 2h window, and
    # `select_best_alternative_subscription()` uses that to filter candidates.
    # Clearing here causes a ping-pong between exhausted subscriptions because
    # the old sub looks viable on the next cycle (issue #444). Events age out
    # naturally via the 2h query window and the 24h cleanup job.

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
            "consecutive_errors": consecutive_count,
            "restart_result": restart_result,
        }
    )
    await activity_service.complete_activity(
        activity_id=activity_id,
        status=ActivityState.COMPLETED,
        details={"message": f"Auto-switched from '{old_subscription_name}' to '{new_subscription.name}'"}
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
                    f"'{old_subscription_name}' to '{new_subscription.name}' after "
                    f"{consecutive_count} consecutive rate-limit errors."
                ),
                priority="high",
                category="subscription",
                metadata={
                    "old_subscription": old_subscription_name,
                    "new_subscription": new_subscription.name,
                    "consecutive_errors": consecutive_count,
                }
            )
        )
    except Exception as e:
        logger.error(f"[SUB-003] Failed to send auto-switch notification for '{agent_name}': {e}")

    result = {
        "switched": True,
        "agent_name": agent_name,
        "old_subscription": old_subscription_name,
        "new_subscription": new_subscription.name,
        "consecutive_errors": consecutive_count,
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
