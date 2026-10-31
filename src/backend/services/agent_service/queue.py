"""
Agent Service Queue - Execution queue operations.

Handles agent execution queue management. Funnels all capacity operations
through the unified CapacityManager (#428).
"""
import logging

from fastapi import HTTPException

from database import db
from dependencies import row_is_viewers, viewer_row_scope
from models import User
from services.capacity_manager import get_capacity_manager
from services.docker_service import get_agent_container

logger = logging.getLogger(__name__)


async def get_agent_queue_status_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get execution queue status for an agent.

    Returns:
    - is_busy: Whether the agent is currently executing a request
    - current_execution: Details of the currently running execution (if any)
    - queue_length: Number of requests waiting in the queue
    - queued_executions: Details of queued requests

    This is useful for checking if an agent is available before
    sending a chat request, or for monitoring agent workload.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        capacity = get_capacity_manager()
        # /chat path is serial (max_concurrent=1); status endpoint historically
        # reflected /chat queue state.
        status = await capacity.get_status(agent_name, max_concurrent=1)
        data = status.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get queue status: {str(e)}")
    return _narrow_queue_status(data, agent_name, current_user)


def _narrow_queue_status(data: dict, agent_name: str, current_user: User) -> dict:
    """A share holder sees the prompts of the turns they started and no one
    else's (trinity-enterprise#875). The running turn is shown only if it is
    theirs; queued items are kept by `source_user_id`, which the in-memory queue
    carries as a string. `is_busy` and `queue_length` are counts, not text, so
    the caller still learns the agent is busy and how long the line is.

    Owner, admin and key tiers get the status untouched."""
    scope = viewer_row_scope(current_user, agent_name)
    if scope is None:
        return data
    current = data.get("current_execution")
    if current:
        row = db.get_execution(str(current.get("id", "")))
        mine = bool(row) and getattr(row, "agent_name", None) == agent_name and row_is_viewers(
            scope, getattr(row, "source_user_id", None)
        )
        if not mine:
            data["current_execution"] = None
    data["queued_executions"] = [
        item for item in (data.get("queued_executions") or [])
        if row_is_viewers(scope, item.get("source_user_id"))
    ]
    return data


async def clear_agent_queue_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Clear all queued executions for an agent.

    This does NOT stop the currently running execution - only clears pending requests.
    Use this if you want to cancel all waiting requests for an agent.

    Returns the number of cleared executions.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        capacity = get_capacity_manager()
        cleared_count = await capacity.clear_in_memory_queue(agent_name)

        return {
            "status": "cleared",
            "agent": agent_name,
            "cleared_count": cleared_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear queue: {str(e)}")


async def force_release_agent_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Force release an agent from its running state.

    CAUTION: This is an emergency operation for when an agent is stuck.
    Use only if an execution is hung or the agent died without completing.

    This clears the "running" state in the queue, allowing new executions.
    It does NOT stop any actual agent process - just resets the queue state.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        capacity = get_capacity_manager()
        result = await capacity.force_release(agent_name)

        return {
            "status": "released",
            "agent": agent_name,
            "was_running": result.was_running,
            "slots_cleared": result.slots_cleared,
            "warning": "Agent queue state and capacity slots have been reset. Any in-progress execution may still be running."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to release agent: {str(e)}")
