"""
Agent Service Folders - Shared folder management.

Handles agent shared folder configuration and operations.
"""
import logging

from fastapi import HTTPException, Request

from models import User
from database import db
from services.docker_service import get_agent_container
from services.audit_service import log_audit_event

logger = logging.getLogger(__name__)


async def get_agent_folders_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get shared folder configuration for an agent.

    Returns:
    - expose_enabled: Whether this agent exposes a shared folder
    - consume_enabled: Whether this agent mounts other agents' shared folders
    - exposed_volume: Volume name if exposing
    - consumed_folders: List of mounted folders from other agents
    - restart_required: Whether config changed and restart is needed
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get config from database (or defaults if none)
    config = db.get_shared_folder_config(agent_name)
    expose_enabled = config.expose_enabled if config else False
    consume_enabled = config.consume_enabled if config else False

    # Get actual mounted volumes from container
    container.reload()
    mounts = container.attrs.get("Mounts", [])

    # Check if agent is exposing (has /home/developer/shared-out mount)
    shared_out_mounted = any(
        m.get("Destination") == "/home/developer/shared-out"
        for m in mounts
    )

    # Build list of consumed folders
    consumed_folders = []
    if consume_enabled:
        available = db.get_available_shared_folders(agent_name)
        for source_agent in available:
            mount_path = db.get_shared_mount_path(source_agent)
            source_volume = db.get_shared_volume_name(source_agent)

            # Check if this volume is actually mounted
            is_mounted = any(
                m.get("Destination") == mount_path
                for m in mounts
            )

            consumed_folders.append({
                "source_agent": source_agent,
                "mount_path": mount_path,
                "access_mode": "rw",
                "currently_mounted": is_mounted
            })

    # Determine if restart is required
    # Restart needed if config says expose but not mounted (or vice versa)
    restart_required = (expose_enabled != shared_out_mounted)

    # Also check consume status
    if consume_enabled and consumed_folders:
        # Check if any expected mounts are missing
        for folder in consumed_folders:
            if not folder["currently_mounted"]:
                restart_required = True
                break

    return {
        "agent_name": agent_name,
        "expose_enabled": expose_enabled,
        "consume_enabled": consume_enabled,
        "exposed_volume": db.get_shared_volume_name(agent_name) if expose_enabled else None,
        "exposed_path": "/home/developer/shared-out",
        "consumed_folders": consumed_folders,
        "restart_required": restart_required,
        "status": container.status
    }


async def update_agent_folders_logic(
    agent_name: str,
    body: dict,
    current_user: User,
    request: Request
) -> dict:
    """
    Update shared folder configuration for an agent.

    Body:
    - expose_enabled: (optional) Whether to expose a shared folder
    - consume_enabled: (optional) Whether to mount other agents' shared folders

    Note: Changes require agent restart to take effect.
    """
    # Only owner can modify folder sharing
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify folder sharing")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    expose_enabled = body.get("expose_enabled")
    consume_enabled = body.get("consume_enabled")

    # Update config
    config = db.upsert_shared_folder_config(
        agent_name,
        expose_enabled=expose_enabled,
        consume_enabled=consume_enabled
    )

    await log_audit_event(
        event_type="agent_folders",
        action="update_config",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "expose_enabled": config.expose_enabled,
            "consume_enabled": config.consume_enabled
        }
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "expose_enabled": config.expose_enabled,
        "consume_enabled": config.consume_enabled,
        "restart_required": True,
        "message": "Configuration updated. Restart the agent to apply changes."
    }


async def get_available_shared_folders_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get list of shared folders available for this agent to mount.

    Returns agents that:
    1. Have expose_enabled=True
    2. This agent has permission to communicate with

    Useful for showing which folders can be mounted.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    available = db.get_available_shared_folders(agent_name)

    # Build detailed list with mount info
    available_folders = []
    for source_agent in available:
        source_container = get_agent_container(source_agent)
        available_folders.append({
            "source_agent": source_agent,
            "volume_name": db.get_shared_volume_name(source_agent),
            "mount_path": db.get_shared_mount_path(source_agent),
            "source_status": source_container.status if source_container else "not_found"
        })

    return {
        "agent_name": agent_name,
        "available_folders": available_folders,
        "count": len(available_folders)
    }


async def get_folder_consumers_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get list of agents that can consume this agent's shared folder.

    Returns agents that:
    1. Have consume_enabled=True
    2. Have permission to communicate with this agent

    Useful for understanding who will see exposed files.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get agents that can consume from this agent
    consumers = db.get_consuming_agents(agent_name)

    consumer_list = []
    for consumer_agent in consumers:
        consumer_container = get_agent_container(consumer_agent)
        consumer_list.append({
            "agent_name": consumer_agent,
            "mount_path": db.get_shared_mount_path(agent_name),
            "status": consumer_container.status if consumer_container else "not_found"
        })

    return {
        "source_agent": agent_name,
        "consumers": consumer_list,
        "count": len(consumer_list)
    }
