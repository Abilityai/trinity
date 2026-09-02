# mcp: agents.ts (rename_agent)
"""Agent rename endpoint (RENAME-001)."""
import re
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from models import RenameAgentRequest, User
from database import db
from dependencies import get_current_user, reject_agent_principal
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

    trinity-enterprise#69 Part 2: rename is human-only (agent-scoped keys 403).

    Returns:
    - message: Success message
    - old_name: Previous agent name
    - new_name: New agent name

    Note: The agent will be briefly stopped and restarted during rename.
    """
    # trinity-enterprise#69 Part 2: rename is a human-only operation.
    reject_agent_principal(current_user)
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

    # #1671: a free NAME does not mean a free VOLUME BASE. Rename keeps the
    # agent's volumes under its existing base, so a previously-renamed agent
    # still claims its old name's base — and renaming a second agent into that
    # base gives it two claimants. That is the #1667 silent-adopt disclosure via
    # the one producer #1664 left ungated (`get_public_volume_name` names off
    # the LIVE name, so the new holder get-then-creates onto the old agent's
    # `agent-{name}-public`), and it strands both bases: with two claimants the
    # purge guard skips them and the orphan sweep never reclaims them.
    #
    # `exclude_agent` = this agent: only ANOTHER row's claim blocks. Renaming an
    # agent back to a name it already owns the base of (`B`->`A`->`B`) is
    # legitimate and leaves a single claimant.
    #
    # Raised BEFORE the container is stopped/renamed — nothing is half-done on
    # refusal. `db.rename_agent` re-checks inside its transaction (the
    # chokepoint that closes the check-then-write gap, #1445 pattern); this gate
    # exists so the caller gets an actionable 409 instead of that path's generic
    # 500.
    if db.is_volume_base_reserved(sanitized_name, exclude_agent=agent_name):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Name '{sanitized_name}' is unavailable: its data volumes still "
                f"belong to another agent (that agent was renamed and kept them). "
                f"Pick a different name."
            ),
        )

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

        # Docker volumes are NOT renamed: Docker supports neither renaming a
        # volume nor editing its (immutable) labels, so copying gigabytes of
        # `/home/developer` on every rename is the only alternative. The agent
        # keeps its existing `agent-{old_name}-*` volumes and the container
        # carries the same mounts forward (recreate_container_with_updated_config
        # rebuilds the mount set from the old container's Mounts).
        #
        # #1664: that makes the volume's own identity (name + `trinity.agent-name`
        # label) permanently stale, which the #1581 orphan sweep once read as
        # "this volume's agent no longer exists" — and force-removed the LIVE
        # agent's home volume during a recreate gap. `db.rename_agent` therefore
        # pins `agent_ownership.volume_base_name = old_name` atomically with the
        # rename; the sweep resolves ownership from that, never from the volume.
        # Anything that needs this agent's volume names must ask
        # `db.get_volume_base_name(agent)` — NOT f"agent-{agent_name}-workspace".

        # Update database references
        if not db.rename_agent(agent_name, sanitized_name):
            # Rollback container rename
            await container_rename(container, old_container_name)
            raise HTTPException(
                status_code=500,
                detail="Failed to update database. Agent name may already be taken."
            )

        # #1560 / RELIABILITY-004 (#307): every per-agent Redis keyspace is keyed
        # by name, so a rename orphans all of them under the old name — the
        # heartbeat `seen` marker (no TTL), both circuit breakers, and the slot
        # ZSET. The new name is swept too: it may have been used by an agent that
        # the retention purge has since removed, whose breaker verdict would
        # otherwise be inherited here. The container is stopped for the whole
        # rename, so neither sweep can race an in-flight execution's slot. The
        # renamed container re-establishes its own state on next beat/dispatch.
        # Best-effort.
        try:
            from services.agent_runtime_state import clear_agent_runtime_state
            await clear_agent_runtime_state(agent_name)
            await clear_agent_runtime_state(sanitized_name)
        except Exception as e:
            logger.warning(f"Failed to clear Redis runtime state on rename {agent_name} -> {sanitized_name}: {e}")

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
