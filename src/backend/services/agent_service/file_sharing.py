"""
Agent file-sharing (outbound) configuration service.

FILES-001 / amazing-file-outbound Step 2: per-agent opt-in for file sharing.

The toggle flips `agent_ownership.file_sharing_enabled`. Volume mount/unmount
happens on the next agent restart (the lifecycle flow calls
`check_public_folder_mount_matches` and triggers container recreation when
DB config and actual mounts disagree).

Actual file writing/reading is Step 3 territory.
"""

import logging

from fastapi import HTTPException

from database import db
from dependencies import agent_may_reach
from models import User
from services.docker_service import get_agent_container

logger = logging.getLogger(__name__)


# Per-agent storage quota (Step 5 wire-up will enforce; here for status only).
DEFAULT_QUOTA_BYTES = 500 * 1024 * 1024  # 500 MB


def _mount_present(container, destination: str) -> bool:
    """Whether the container has a mount with the given destination path."""
    for mount in container.attrs.get("Mounts", []):
        if mount.get("Destination") == destination:
            return True
    return False


def check_public_folder_mount_matches(container, agent_name: str) -> bool:
    """
    Check if the container's /home/developer/public mount matches the
    agent's file_sharing_enabled flag.

    Returns True when mounts are consistent with DB, False if recreation
    is needed. Called from the agent-start lifecycle alongside the other
    mount-match checks (shared-folders, resources, etc.).
    """
    enabled = db.get_file_sharing_enabled(agent_name)
    mount_path = db.get_public_mount_path()
    mount_present = _mount_present(container, mount_path)

    if enabled and not mount_present:
        return False
    if not enabled and mount_present:
        return False
    return True


async def get_file_sharing_status_logic(agent_name: str, current_user: User) -> dict:
    """Return the current file-sharing status for the agent."""
    if not (db.can_user_access_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name)):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    enabled = db.get_file_sharing_enabled(agent_name)
    volume_attached = _mount_present(container, db.get_public_mount_path())

    # Restart is required when config and actual mounts disagree.
    restart_required = enabled != volume_attached

    # Step 1 Note: file_count / total_bytes are placeholders until the
    # AgentSharedFilesOperations DB layer lands in Step 3. Returning zeros
    # here lets the UI render the quota bar immediately.
    file_count = 0
    total_bytes = 0

    return {
        "agent_name": agent_name,
        "enabled": enabled,
        "volume_attached": volume_attached,
        "restart_required": restart_required,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "quota_bytes": DEFAULT_QUOTA_BYTES,
        "status": container.status,
    }


async def set_file_sharing_status_logic(
    agent_name: str,
    body: dict,
    current_user: User,
) -> dict:
    """Enable/disable file sharing for an agent (owner-only)."""
    if not (db.can_user_share_agent(current_user.username, agent_name) and agent_may_reach(current_user, agent_name, manage=True)):
        raise HTTPException(status_code=403, detail="Only the owner can modify file-sharing settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="Cannot modify file sharing for the system agent")

    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = bool(body["enabled"])

    previous = db.get_file_sharing_enabled(agent_name)
    db.set_file_sharing_enabled(agent_name, enabled)

    changed = previous != enabled
    if changed:
        logger.info(
            "File sharing %s for agent %s by %s",
            "enabled" if enabled else "disabled",
            agent_name,
            current_user.username,
        )

    volume_attached = _mount_present(container, db.get_public_mount_path())
    restart_required = enabled != volume_attached

    return {
        "status": "updated",
        "agent_name": agent_name,
        "enabled": enabled,
        "restart_required": restart_required,
        "message": (
            "File sharing enabled. Restart the agent to mount the volume."
            if enabled and restart_required
            else "File sharing disabled. Restart the agent to detach the volume."
            if not enabled and restart_required
            else "File sharing updated."
        ),
    }
