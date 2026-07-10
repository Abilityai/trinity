"""
Agent Service Autonomy - Autonomy mode management.

Handles agent autonomy mode toggle which enables/disables all scheduled tasks.
When autonomy is enabled, the agent's schedules run automatically.
When autonomy is disabled, schedules are paused.
"""
import logging
from typing import Dict

from fastapi import HTTPException

from models import User
from database import db
from services.docker_service import get_agent_container

logger = logging.getLogger(__name__)


async def get_autonomy_status_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get the autonomy status for an agent.

    Returns whether autonomy mode is enabled and schedule counts.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    autonomy_enabled = db.get_autonomy_enabled(agent_name)

    # Get schedule counts
    schedules = db.list_agent_schedules(agent_name)
    total_schedules = len(schedules)
    enabled_schedules = sum(1 for s in schedules if s.enabled)

    return {
        "agent_name": agent_name,
        "autonomy_enabled": autonomy_enabled,
        "total_schedules": total_schedules,
        "enabled_schedules": enabled_schedules,
        "status": container.status
    }


async def set_autonomy_status_logic(
    agent_name: str,
    body: dict,
    current_user: User
) -> dict:
    """
    Set the autonomy status for an agent.

    When enabling autonomy:
    - All schedules for the agent are enabled
    - The scheduler will pick them up automatically

    When disabling autonomy:
    - All schedules for the agent are disabled
    - No scheduled tasks will run until autonomy is re-enabled

    Body:
    - enabled: True to enable autonomy, False to disable
    """
    # Only owner can modify autonomy
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify autonomy settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Don't allow autonomy for system agent from this endpoint
    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="Cannot modify autonomy for system agent")

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")

    enabled = bool(enabled)

    # Update the autonomy flag
    db.set_autonomy_enabled(agent_name, enabled)

    # Enable/disable all schedules for this agent
    # NOTE: Dedicated scheduler syncs from database automatically on next sync cycle
    schedules = db.list_agent_schedules(agent_name)
    updated_count = 0
    for schedule in schedules:
        schedule_id = schedule.id
        if schedule_id:
            db.set_schedule_enabled(schedule_id, enabled)
            updated_count += 1

    # #1557 — autonomy governs PROACTIVE work ONLY (the schedules disabled above);
    # it deliberately does NOT touch the circuit breaker. The old #631 AC#5 hook
    # forced the *transport* breaker dormant on autonomy-off, conflating
    # "administratively paused" with "transport unhealthy": the execute_task gate
    # consults the transport breaker for every trigger, so a healthy paused agent
    # fast-failed all inbound chat (manual/Telegram/Slack/public) with
    # "circuit breaker open — agent is unhealthy". #631's flood protection does not
    # depend on this hook — the breaker's own failure-driven backoff/dormant path
    # (fed by the pollers' real probes), plus the #1464 leader lock and #1121
    # monitoring-default-off, already throttle a genuinely down agent. Do NOT re-add
    # a breaker write here; pause the agent's proactive work via its schedules.

    logger.info(
        f"Autonomy {'enabled' if enabled else 'disabled'} for agent {agent_name} "
        f"by {current_user.username}. Updated {updated_count} schedules."
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "autonomy_enabled": enabled,
        "schedules_updated": updated_count,
        "message": f"Autonomy {'enabled' if enabled else 'disabled'}. {updated_count} schedule(s) updated."
    }


async def get_all_autonomy_status_logic(
    current_user: User
) -> Dict[str, dict]:
    """
    Get autonomy status for all agents accessible to the user.

    Returns a dict mapping agent_name to autonomy info.
    Used for dashboard display.
    """
    # Get all autonomy statuses
    all_status = db.get_all_agents_autonomy_status()

    # Filter to agents the user can access
    result = {}
    for agent_name, autonomy_enabled in all_status.items():
        if db.can_user_access_agent(current_user.username, agent_name):
            # Skip system agent
            if db.is_system_agent(agent_name):
                continue
            result[agent_name] = {
                "autonomy_enabled": autonomy_enabled
            }

    return result
