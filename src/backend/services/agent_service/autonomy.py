"""
Agent Service Autonomy - Autonomy mode management.

Handles the agent autonomy mode toggle — the master gate for proactive work.
When autonomy is enabled, the agent's enabled schedules run automatically.
When autonomy is disabled, no cron trigger fires for the agent.

The toggle is a GATE, not a bulk edit (#1945): it writes only
``agent_ownership.autonomy_enabled`` and never rewrites the per-schedule
``enabled`` flag, which is owner intent and must survive a toggle in both
directions.
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

    Autonomy is a **gate, not a bulk edit** (#1945). The toggle writes exactly one
    row — ``agent_ownership.autonomy_enabled`` — and never touches the per-schedule
    ``enabled`` flag:

    - Autonomy off  → the scheduler refuses to fire ANY cron trigger for this agent
      (``src/scheduler/service.py`` ``_execute_schedule_with_lock``, cron-only gate).
      An enabled schedule stays enabled and simply does not run; the scheduler
      advances its ``next_run_at`` projection without recording an execution row
      (#1472), and the Schedules tab labels it "Will not fire — autonomy off" (#1796).
    - Autonomy on   → schedules resume with exactly the ``enabled`` state their owner
      left them in. A deliberately-disabled schedule stays disabled.

    Before #1945 this loop wrote ``set_schedule_enabled(id, enabled)`` over every
    schedule on the agent, unfiltered and in both directions, so the first toggle
    destroyed per-schedule intent: an owner-disabled (or template-authored
    ``enabled: false``) schedule was silently re-armed on the next autonomy-on, and
    autonomy-off was a set-all rather than a pause. With a template able to
    materialize up to 20 declared schedules, one unrelated toggle could arm all of
    them at once.

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

    # The ONLY write. The agent-level flag is the gate the scheduler consults on
    # every cron fire, so it is sufficient on its own to start/stop proactive work.
    # Do NOT re-add a per-schedule fan-out here (#1945): the per-schedule `enabled`
    # flag is owner intent and must survive an autonomy toggle in both directions.
    db.set_autonomy_enabled(agent_name, enabled)

    # #1557 — autonomy governs PROACTIVE work ONLY; it deliberately does NOT touch
    # the circuit breaker. The old #631 AC#5 hook forced the *transport* breaker
    # dormant on autonomy-off, conflating "administratively paused" with "transport
    # unhealthy": the execute_task gate consults the transport breaker for every
    # trigger, so a healthy paused agent fast-failed all inbound chat
    # (manual/Telegram/Slack/public) with "circuit breaker open — agent is
    # unhealthy". #631's flood protection does not depend on this hook — the
    # breaker's own failure-driven backoff/dormant path (fed by the pollers' real
    # probes), plus the #1464 leader lock and #1121 monitoring-default-off, already
    # throttle a genuinely down agent. Do NOT re-add a breaker write here.

    # Report-only: what the operator's schedules will actually do under the new gate.
    schedules = db.list_agent_schedules(agent_name)
    total_schedules = len(schedules)
    enabled_schedules = sum(1 for s in schedules if s.enabled)

    if enabled:
        if total_schedules == 0:
            message = "Autonomy enabled. This agent has no schedules."
        elif enabled_schedules == 0:
            message = (
                f"Autonomy enabled, but all {total_schedules} schedule(s) are disabled — "
                "nothing will run until you enable one."
            )
        else:
            message = (
                f"Autonomy enabled. {enabled_schedules} of {total_schedules} "
                "schedule(s) will run; per-schedule settings unchanged."
            )
    elif total_schedules == 0:
        message = "Autonomy disabled. This agent has no schedules."
    else:
        message = (
            f"Autonomy disabled. {total_schedules} schedule(s) paused; "
            "per-schedule settings preserved."
        )

    logger.info(
        f"Autonomy {'enabled' if enabled else 'disabled'} for agent {agent_name} "
        f"by {current_user.username}. {enabled_schedules}/{total_schedules} schedule(s) "
        f"enabled (per-schedule state untouched)."
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "autonomy_enabled": enabled,
        "total_schedules": total_schedules,
        "enabled_schedules": enabled_schedules,
        "message": message,
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
