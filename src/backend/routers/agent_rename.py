"""Agent rename endpoint (RENAME-001)."""
import re
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from models import User
from database import db
from dependencies import get_current_user
from services.docker_service import get_agent_container
from services.docker_utils import container_stop, container_rename
from services.image_generation_prompts import AVATAR_EMOTIONS
from services.platform_audit_service import platform_audit_service, AuditEventType

router = APIRouter(prefix="/api/agents", tags=["agents"])

manager = None
filtered_manager = None

logger = logging.getLogger(__name__)


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


def set_filtered_websocket_manager(ws_manager):
    """Set the filtered WebSocket manager for /ws/events (Trinity Connect)."""
    global filtered_manager
    filtered_manager = ws_manager


class RenameAgentRequest(BaseModel):
    """Request body for agent rename."""
    new_name: str


@router.put("/{agent_name}/rename")
async def rename_agent_endpoint(
    agent_name: str,
    body: RenameAgentRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Rename an agent.

    Changes the agent name in all database references and renames the Docker
    container and volume. System agents cannot be renamed.

    Body:
    - new_name: The new name for the agent

    Returns:
    - message: Success message
    - old_name: Previous agent name
    - new_name: New agent name

    Note: The agent will be briefly stopped and restarted during rename.
    """
    # Check if user can rename this agent
    if not db.can_user_rename_agent(current_user.username, agent_name):
        # Check if it's a system agent for better error message
        if db.is_system_agent(agent_name):
            raise HTTPException(
                status_code=403,
                detail="System agents cannot be renamed"
            )
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to rename this agent"
        )

    # Validate new name format
    new_name = body.new_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="New name cannot be empty")

    # Sanitize name for Docker compatibility (same as agent creation)
    sanitized_name = re.sub(r'[^a-zA-Z0-9_-]', '-', new_name.lower())
    sanitized_name = re.sub(r'-+', '-', sanitized_name).strip('-')

    if not sanitized_name:
        raise HTTPException(status_code=400, detail="Invalid agent name after sanitization")

    if len(sanitized_name) > 63:
        raise HTTPException(status_code=400, detail="Agent name too long (max 63 characters)")

    if sanitized_name == agent_name:
        raise HTTPException(status_code=400, detail="New name is the same as current name")

    # Check if new name is already taken
    existing = get_agent_container(sanitized_name)
    if existing:
        raise HTTPException(status_code=409, detail=f"Agent with name '{sanitized_name}' already exists")

    # Get the container
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if agent is running - we need to stop it to rename
    was_running = container.status == "running"

    try:
        # Stop container if running
        if was_running:
            await container_stop(container)

        # Rename Docker container
        old_container_name = f"agent-{agent_name}"
        new_container_name = f"agent-{sanitized_name}"
        await container_rename(container, new_container_name)

        # Update container labels (need to recreate for label changes)
        # For now, we'll update just the database and handle labels on next start
        #
        # #1159: the running container still carries TRINITY_AGENT_AUTH_TOKEN=
        # derive(old_name); under the new name that token is stale and would 401
        # once enforcement is on (Codex #4). No extra work here — the rename
        # leaves the agent stopped, and the next start_agent_internal recreate is
        # forced by check_agent_auth_token_env_matches (token mismatch) and
        # re-injects derive(new_name). Same recreate-on-next-start path the label/
        # volume changes above already depend on.

        # Rename Docker volume
        # Docker doesn't support renaming volumes directly
        # We need to create a new volume, copy data, and remove old one
        # For simplicity in this implementation, we'll keep the volume name
        # and update the container mount on next start
        # This is handled by recreate_container_with_updated_config

        # Update database references
        if not db.rename_agent(agent_name, sanitized_name):
            # Rollback container rename
            await container_rename(container, old_container_name)
            raise HTTPException(
                status_code=500,
                detail="Failed to update database. Agent name may already be taken."
            )

        # RELIABILITY-004 / #307: the heartbeat `seen` marker has no TTL and is
        # keyed by agent name, so a rename would orphan the old name's key
        # forever. Clear the old name's heartbeat keys; the renamed container
        # re-sets `seen` under the new name on its next beat. Best-effort.
        try:
            from services import heartbeat_service
            heartbeat_service.clear_heartbeat(agent_name)
        except Exception as e:
            logger.warning(f"Failed to clear heartbeat keys for old name {agent_name}: {e}")

        # Rename cached avatar, reference, and emotion image files (AVATAR-001, AVATAR-002)
        try:
            for ext in (".webp", ".png"):
                old_path = Path("/data/avatars") / f"{agent_name}{ext}"
                new_path = Path("/data/avatars") / f"{sanitized_name}{ext}"
                if old_path.exists():
                    old_path.rename(new_path)
            # Reference stays .png
            old_ref = Path("/data/avatars") / f"{agent_name}_ref.png"
            new_ref = Path("/data/avatars") / f"{sanitized_name}_ref.png"
            if old_ref.exists():
                old_ref.rename(new_ref)
            for emotion in AVATAR_EMOTIONS:
                for ext in (".webp", ".png"):
                    old_path = Path("/data/avatars") / f"{agent_name}_emotion_{emotion}{ext}"
                    new_path = Path("/data/avatars") / f"{sanitized_name}_emotion_{emotion}{ext}"
                    if old_path.exists():
                        old_path.rename(new_path)
        except Exception as e:
            logger.warning(f"Failed to rename avatar for agent {agent_name}: {e}")

        # Broadcast rename event
        event = {
            "event": "agent_renamed",
            "type": "agent_renamed",
            "name": sanitized_name,
            "data": {
                "old_name": agent_name,
                "new_name": sanitized_name
            }
        }
        if manager:
            await manager.broadcast(json.dumps(event))
        if filtered_manager:
            await filtered_manager.broadcast_filtered(event)

        # SEC-001: audit rename
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="rename",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=sanitized_name,
            endpoint=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            details={"old_name": agent_name, "new_name": sanitized_name},
        )

        # Restart agent if it was running
        # Note: Container needs to be recreated for new volume mount
        # This will be done on explicit start

        return {
            "message": f"Agent renamed from '{agent_name}' to '{sanitized_name}'",
            "old_name": agent_name,
            "new_name": sanitized_name,
            "was_running": was_running,
            "note": "Agent needs to be restarted to apply all changes" if was_running else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to rename agent {agent_name}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to rename agent: {str(e)}"
        )
