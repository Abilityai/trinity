"""
Agent Service Metrics - Custom metrics operations.

Handles fetching custom metrics from agents.
"""
import logging

import httpx
from fastapi import HTTPException

from models import User
from database import db
from services.docker_service import get_agent_container

logger = logging.getLogger(__name__)


async def get_agent_metrics_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get agent custom metrics.

    Returns metric definitions from agent's template.yaml and current values
    from metrics.json in the agent's workspace.

    Metric types supported:
    - counter: Monotonically increasing value
    - gauge: Current value that can go up/down
    - percentage: 0-100 value with progress bar
    - status: Enum/state value with colored badge
    - duration: Time in seconds (formatted)
    - bytes: Size in bytes (formatted)

    Returns:
    - agent_name: Name of the agent
    - has_metrics: Whether agent has custom metrics defined
    - definitions: List of metric definitions from template.yaml
    - values: Current metric values from metrics.json
    - last_updated: Timestamp from metrics.json (if available)
    - status: Agent status (running/stopped)
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()

    # If agent is not running, return basic info
    if container.status != "running":
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "stopped",
            "message": "Agent must be running to read metrics"
        }

    # Agent is running - fetch metrics from agent's internal API
    try:
        agent_url = f"http://agent-{agent_name}:8000/api/metrics"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["agent_name"] = agent_name
                data["status"] = "running"
                return data
            else:
                return {
                    "agent_name": agent_name,
                    "has_metrics": False,
                    "status": "running",
                    "message": f"Failed to fetch metrics: HTTP {response.status_code}"
                }
    except httpx.TimeoutException:
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "running",
            "message": "Agent is starting up, please try again"
        }
    except Exception as e:
        return {
            "agent_name": agent_name,
            "has_metrics": False,
            "status": "running",
            "message": f"Failed to read metrics: {str(e)}"
        }
