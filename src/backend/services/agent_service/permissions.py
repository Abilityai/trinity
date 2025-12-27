"""
Agent Service Permissions - Agent-to-agent permission management.

Handles permission operations for agent collaboration.
"""
import logging

from fastapi import HTTPException, Request

from models import User
from database import db
from services.docker_service import get_agent_container
from services.audit_service import log_audit_event
from .helpers import get_accessible_agents

logger = logging.getLogger(__name__)


async def get_agent_permissions_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get permissions for an agent.

    Returns:
    - source_agent: The agent name
    - permitted_agents: List of agents this agent can communicate with
    - available_agents: List of all other accessible agents with permission status
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get permitted agents
    permitted_list = db.get_permitted_agents(agent_name)

    # Get all agents accessible to this user
    accessible_agents = get_accessible_agents(current_user)

    # Build available agents list with permission status
    available_agents = []
    permitted_agents = []

    for agent in accessible_agents:
        if agent["name"] == agent_name:
            continue  # Skip self

        agent_info = {
            "name": agent["name"],
            "status": agent["status"],
            "type": agent.get("type", ""),
            "permitted": agent["name"] in permitted_list
        }

        if agent_info["permitted"]:
            permitted_agents.append(agent_info)
        available_agents.append(agent_info)

    return {
        "source_agent": agent_name,
        "permitted_agents": permitted_agents,
        "available_agents": available_agents
    }


async def set_agent_permissions_logic(
    agent_name: str,
    body: dict,
    current_user: User,
    request: Request
) -> dict:
    """
    Set permissions for an agent (full replacement).

    Body:
    - permitted_agents: List of agent names to permit
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    permitted_agents = body.get("permitted_agents", [])

    # Validate all target agents exist and are accessible
    for target in permitted_agents:
        if not db.can_user_access_agent(current_user.username, target):
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{target}' does not exist or is not accessible"
            )

    # Set permissions
    db.set_agent_permissions(agent_name, permitted_agents, current_user.username)

    await log_audit_event(
        event_type="agent_permissions",
        action="set_permissions",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"permitted_count": len(permitted_agents)}
    )

    return {
        "status": "updated",
        "source_agent": agent_name,
        "permitted_count": len(permitted_agents)
    }


async def add_agent_permission_logic(
    agent_name: str,
    target_agent: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Add permission for an agent to communicate with another agent.
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify target agent exists and is accessible
    if not db.can_user_access_agent(current_user.username, target_agent):
        raise HTTPException(status_code=400, detail=f"Target agent '{target_agent}' does not exist or is not accessible")

    # Can't permit self
    if agent_name == target_agent:
        raise HTTPException(status_code=400, detail="Agent cannot be permitted to call itself")

    # Add permission
    result = db.add_agent_permission(agent_name, target_agent, current_user.username)

    if result:
        await log_audit_event(
            event_type="agent_permissions",
            action="add_permission",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"target_agent": target_agent}
        )
        return {"status": "added", "source_agent": agent_name, "target_agent": target_agent}
    else:
        return {"status": "already_exists", "source_agent": agent_name, "target_agent": target_agent}


async def remove_agent_permission_logic(
    agent_name: str,
    target_agent: str,
    current_user: User,
    request: Request
) -> dict:
    """
    Remove permission for an agent to communicate with another agent.
    """
    # Only owner or admin can modify permissions
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Remove permission
    removed = db.remove_agent_permission(agent_name, target_agent)

    if removed:
        await log_audit_event(
            event_type="agent_permissions",
            action="remove_permission",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"target_agent": target_agent}
        )
        return {"status": "removed", "source_agent": agent_name, "target_agent": target_agent}
    else:
        return {"status": "not_found", "source_agent": agent_name, "target_agent": target_agent}
