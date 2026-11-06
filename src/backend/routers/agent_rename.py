# mcp: agents.ts (rename_agent)
"""Agent rename endpoint (RENAME-001).

The gates, the name sanitiser and the HTTP mapping live here; the rename
itself is `services/agent_service/rename.py::rename_agent_now`
(trinity-enterprise#1111, T3), so a platform caller acting with `actor=None`
runs the same steps rather than a second copy of them.
"""
import re
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from models import RenameAgentRequest, User
from database import db
from dependencies import get_current_user, reject_agent_principal, require_person
from services.docker_service import get_agent_container
from services.agent_service.rename import (
    AgentContainerMissing,
    RenameRefused,
    rename_agent_now,
)

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


@router.put("/{agent_name}/rename", dependencies=[Depends(require_person)])
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

    try:
        result = await rename_agent_now(
            agent_name,
            sanitized_name,
            actor=current_user,
            audit_ctx={
                "actor_ip": request.client.host if request.client else None,
                "endpoint": request.scope["path"],
                "request_id": getattr(request.state, "request_id", None),
            },
            ws_manager=manager,
            filtered_ws_manager=filtered_manager,
        )
    except AgentContainerMissing:
        raise HTTPException(status_code=404, detail="Agent not found")
    except RenameRefused:
        raise HTTPException(
            status_code=500,
            detail="Failed to update database. Agent name may already be taken."
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to rename agent {agent_name}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to rename agent: {str(e)}"
        )

    # The agent is left stopped: the next start recreates the container so the
    # new name reaches AGENT_NAME, the label and the auth token.
    return {
        "message": f"Agent renamed from '{agent_name}' to '{sanitized_name}'",
        "old_name": agent_name,
        "new_name": sanitized_name,
        "was_running": result.was_running,
        "note": "Agent needs to be restarted to apply all changes" if result.was_running else None
    }
