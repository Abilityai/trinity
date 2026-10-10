"""
Agent Service Permissions - Agent-to-agent permission management.

Handles permission operations for agent collaboration.
"""
import logging

from fastapi import HTTPException, Request

from models import User
from database import db
from dependencies import agent_may_reach
from services.docker_service import agent_container_states, get_agent_container
from .helpers import get_accessible_agents

logger = logging.getLogger(__name__)


async def get_agent_permissions_logic(
    agent_name: str,
    current_user: User,
    strict: bool = False,
) -> dict:
    """
    Get permissions for an agent.

    Returns:
    - source_agent: The agent name
    - permitted_agents: List of agents this agent can communicate with
    - available_agents: List of all other accessible agents with permission status

    `strict` (trinity-enterprise#815): never answer a Docker fault with a
    confident, smaller set — see `_strict_permissions`. Without it the
    behaviour is unchanged (the frontend and the lenient MCP callers).
    """
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    if strict:
        return _strict_permissions(agent_name, current_user)

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


def _strict_permissions(agent_name: str, current_user: User) -> dict:
    """The permissions answer built from ONE tri-state Docker snapshot
    (trinity-enterprise#815).

    The lenient path reads Docker twice through helpers that swallow faults:
    `get_agent_container` turns any error into a 404, and `get_accessible_agents`
    → `list_all_agents_fast` turns one into an empty fleet, so the answer is
    "200, no peers". A caller that must not mistake that for "no peers" — the
    MCP deciding whether a broad queue read is complete — asks for this mode:
    `agent_container_states()` is read once; `None` (Docker unreadable) is a
    503; otherwise both "the agent exists" and "which permitted peers have a
    container" come from that one snapshot plus the same DB access rules
    `get_accessible_agents` applies (admin ⇒ every container, orphans included;
    else owned or shared).
    """
    states = agent_container_states()
    if states is None:
        raise HTTPException(
            status_code=503,
            detail="Docker could not be read; the permitted agents cannot be verified",
        )
    if agent_name not in states:
        raise HTTPException(status_code=404, detail="Agent not found")

    permitted_list = db.get_permitted_agents(agent_name)
    user_data = db.get_user_by_username(current_user.username)
    is_admin = bool(user_data) and user_data["role"] == "admin"
    all_metadata = db.get_all_agent_metadata(user_data.get("email")) if user_data else {}

    permitted_agents = []
    available_agents = []
    for name in sorted(states):
        if name == agent_name or not user_data:
            continue
        metadata = all_metadata.get(name)
        if metadata:
            visible = (is_admin
                       or metadata.get("owner_username") == current_user.username
                       or bool(metadata.get("is_shared_with_user")))
        else:
            visible = is_admin  # an orphan container: admin only, as today
        if not visible:
            continue
        agent_info = {"name": name, "status": states[name], "permitted": name in permitted_list}
        if agent_info["permitted"]:
            permitted_agents.append(agent_info)
        available_agents.append(agent_info)

    return {
        "source_agent": agent_name,
        "permitted_agents": permitted_agents,
        "available_agents": available_agents,
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
    if not (db.can_user_share_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name, manage=True)):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    permitted_agents = body.get("permitted_agents", [])

    # Validate all target agents exist and are accessible
    for target in permitted_agents:
        if not (db.can_user_access_agent(current_user.username, target) and agent_may_reach(current_user, target)):
            raise HTTPException(
                status_code=400,
                detail=f"Agent '{target}' does not exist or is not accessible"
            )

    # Set permissions
    db.set_agent_permissions(agent_name, permitted_agents, current_user.username)

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
    if not (db.can_user_share_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name, manage=True)):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Verify target agent exists and is accessible
    if not (db.can_user_access_agent(current_user.username, target_agent) and agent_may_reach(current_user, target_agent)):
        raise HTTPException(status_code=400, detail=f"Target agent '{target_agent}' does not exist or is not accessible")

    # Can't permit self
    if agent_name == target_agent:
        raise HTTPException(status_code=400, detail="Agent cannot be permitted to call itself")

    # Add permission
    result = db.add_agent_permission(agent_name, target_agent, current_user.username)

    if result:
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
    if not (db.can_user_share_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name, manage=True)):
        raise HTTPException(status_code=403, detail="Only the owner can modify agent permissions")

    # Verify source agent exists
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Remove permission
    removed = db.remove_agent_permission(agent_name, target_agent)

    if removed:
        return {"status": "removed", "source_agent": agent_name, "target_agent": target_agent}
    else:
        return {"status": "not_found", "source_agent": agent_name, "target_agent": target_agent}
