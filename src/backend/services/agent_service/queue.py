"""
Agent Service Queue - Execution queue operations.

Handles agent execution queue management.
"""
import logging

from fastapi import HTTPException

from models import User
from services.docker_service import get_agent_container
from services.execution_queue import get_execution_queue
from services.audit_service import log_audit_event

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
        queue = get_execution_queue()
        status = await queue.get_status(agent_name)
        return status.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get queue status: {str(e)}")


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
        queue = get_execution_queue()
        cleared_count = await queue.clear_queue(agent_name)

        await log_audit_event(
            event_type="agent_queue",
            action="clear_queue",
            user_id=current_user.username,
            agent_name=agent_name,
            resource=f"agent-{agent_name}",
            result="success",
            details={"cleared_count": cleared_count}
        )

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
        queue = get_execution_queue()
        was_running = await queue.force_release(agent_name)

        await log_audit_event(
            event_type="agent_queue",
            action="force_release",
            user_id=current_user.username,
            agent_name=agent_name,
            resource=f"agent-{agent_name}",
            result="success",
            details={"was_running": was_running},
            severity="warning"
        )

        return {
            "status": "released",
            "agent": agent_name,
            "was_running": was_running,
            "warning": "Agent queue state has been reset. Any in-progress execution may still be running."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to release agent: {str(e)}")
