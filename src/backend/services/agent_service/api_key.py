"""
Agent Service API Key - API key settings management.

Handles agent API key configuration.
"""
import logging

from fastapi import HTTPException, Request

from models import User
from database import db
from services.docker_service import get_agent_container
from services.audit_service import log_audit_event
from .helpers import check_api_key_env_matches

logger = logging.getLogger(__name__)


async def get_agent_api_key_setting_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """
    Get the API key setting for an agent.

    Returns whether the agent uses the platform API key or relies on
    terminal-based authentication.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = db.get_use_platform_api_key(agent_name)

    # Check if current container matches the setting
    container.reload()
    env_matches = check_api_key_env_matches(container, agent_name)

    return {
        "agent_name": agent_name,
        "use_platform_api_key": use_platform_key,
        "restart_required": not env_matches,
        "status": container.status
    }


async def update_agent_api_key_setting_logic(
    agent_name: str,
    body: dict,
    current_user: User,
    request: Request
) -> dict:
    """
    Update the API key setting for an agent.

    Body:
    - use_platform_api_key: True to use Trinity's platform key, False to require terminal auth

    Note: Changes require agent restart to take effect.
    """
    # Only owner can modify this setting
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify API key settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    use_platform_key = body.get("use_platform_api_key")
    if use_platform_key is None:
        raise HTTPException(status_code=400, detail="use_platform_api_key is required")

    # Update setting
    db.set_use_platform_api_key(agent_name, bool(use_platform_key))

    await log_audit_event(
        event_type="agent_settings",
        action="update_api_key_setting",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "use_platform_api_key": use_platform_key
        }
    )

    return {
        "status": "updated",
        "agent_name": agent_name,
        "use_platform_api_key": use_platform_key,
        "restart_required": True,
        "message": "Setting updated. Restart the agent to apply changes."
    }
