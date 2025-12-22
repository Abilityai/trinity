"""
System Agent routes for the Trinity backend.

Provides endpoints for managing the Trinity system agent:
- Status check
- Re-initialization (reset to clean state)

The system agent is auto-deployed on platform startup and cannot be deleted.
"""
import os
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
import httpx

from models import User
from database import db
from dependencies import get_current_user
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container, docker_client
from db.agents import SYSTEM_AGENT_NAME

router = APIRouter(prefix="/api/system-agent", tags=["system-agent"])
logger = logging.getLogger(__name__)

# Reference to the agents router's functions - will be imported at runtime to avoid circular imports
_inject_trinity_meta_prompt = None


def set_inject_trinity_meta_prompt(func):
    """Set the inject_trinity_meta_prompt function from agents router."""
    global _inject_trinity_meta_prompt
    _inject_trinity_meta_prompt = func


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/status")
async def get_system_agent_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the status of the system agent.

    Returns health information including:
    - Container status (running/stopped/not found)
    - Agent details if running
    - Last activity timestamp
    """
    container = get_agent_container(SYSTEM_AGENT_NAME)

    if not container:
        return {
            "exists": False,
            "status": "not_found",
            "message": "System agent container not found",
            "name": SYSTEM_AGENT_NAME
        }

    container.reload()
    status = container.status

    result = {
        "exists": True,
        "status": status,
        "name": SYSTEM_AGENT_NAME,
        "container_id": container.short_id
    }

    # Get additional info from database
    owner = db.get_agent_owner(SYSTEM_AGENT_NAME)
    if owner:
        result["owner"] = owner.get("owner_username")
        result["created_at"] = owner.get("created_at")
        result["is_system"] = owner.get("is_system", True)

    # If running, try to get health info from agent
    if status == "running":
        try:
            agent_url = f"http://agent-{SYSTEM_AGENT_NAME}:8000/api/health"
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(agent_url)
                if response.status_code == 200:
                    result["health"] = response.json()
        except Exception as e:
            result["health_error"] = str(e)

    await log_audit_event(
        event_type="system_agent",
        action="status",
        user_id=current_user.username,
        agent_name=SYSTEM_AGENT_NAME,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    return result


@router.post("/reinitialize")
async def reinitialize_system_agent(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Re-initialize the system agent.

    Admin-only endpoint that:
    1. Stops the system agent if running
    2. Clears workspace content
    3. Re-starts the agent
    4. Re-injects Trinity meta-prompt

    Does NOT delete database records or MCP API key.
    This is a "reset to clean state" operation.
    """
    require_admin(current_user)

    container = get_agent_container(SYSTEM_AGENT_NAME)
    if not container:
        raise HTTPException(
            status_code=404,
            detail="System agent container not found. It may not have been deployed yet."
        )

    steps_completed = []
    errors = []

    try:
        # Step 1: Stop the container
        container.reload()
        if container.status == "running":
            container.stop(timeout=30)
            steps_completed.append("stopped")
            logger.info(f"System agent {SYSTEM_AGENT_NAME} stopped for re-initialization")

        # Step 2: Clear workspace content (not the volume, just files)
        try:
            container.start()
            container.reload()

            # Execute cleanup command inside container
            cleanup_result = container.exec_run(
                "bash -c 'rm -rf /home/developer/workspace/* /home/developer/.claude /home/developer/.trinity'",
                user="developer"
            )
            if cleanup_result.exit_code == 0:
                steps_completed.append("workspace_cleared")
            else:
                errors.append(f"Workspace cleanup warning: {cleanup_result.output.decode()}")
        except Exception as e:
            errors.append(f"Workspace cleanup error: {str(e)}")

        # Step 3: Container is already running from step 2
        steps_completed.append("started")
        logger.info(f"System agent {SYSTEM_AGENT_NAME} started after re-initialization")

        # Step 4: Re-copy template files (.claude and CLAUDE.md)
        try:
            copy_result = container.exec_run(
                "bash -c 'cp -r /template/.claude /home/developer/ 2>/dev/null; cp /template/CLAUDE.md /home/developer/ 2>/dev/null; true'",
                user="developer"
            )
            if copy_result.exit_code == 0:
                steps_completed.append("template_copied")
            else:
                errors.append(f"Template copy warning: {copy_result.output.decode()}")
        except Exception as e:
            errors.append(f"Template copy error: {str(e)}")

        # Step 5: Re-inject Trinity meta-prompt
        if _inject_trinity_meta_prompt:
            try:
                injection_result = await _inject_trinity_meta_prompt(SYSTEM_AGENT_NAME)
                if injection_result.get("status") == "success":
                    steps_completed.append("trinity_injected")
                else:
                    errors.append(f"Trinity injection issue: {injection_result}")
            except Exception as e:
                errors.append(f"Trinity injection error: {str(e)}")
        else:
            errors.append("Trinity injection function not available")

        await log_audit_event(
            event_type="system_agent",
            action="reinitialize",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={
                "steps_completed": steps_completed,
                "errors": errors if errors else None
            }
        )

        return {
            "success": True,
            "message": "System agent re-initialized successfully",
            "name": SYSTEM_AGENT_NAME,
            "steps_completed": steps_completed,
            "warnings": errors if errors else None
        }

    except Exception as e:
        logger.error(f"Failed to re-initialize system agent: {e}")
        await log_audit_event(
            event_type="system_agent",
            action="reinitialize",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e), "steps_completed": steps_completed}
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to re-initialize system agent: {str(e)}"
        )


@router.post("/restart")
async def restart_system_agent(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Restart the system agent.

    Admin-only endpoint that stops and starts the system agent.
    Does NOT clear workspace or re-initialize - just a simple restart.
    """
    require_admin(current_user)

    container = get_agent_container(SYSTEM_AGENT_NAME)
    if not container:
        raise HTTPException(
            status_code=404,
            detail="System agent container not found. It may not have been deployed yet."
        )

    try:
        container.reload()
        was_running = container.status == "running"

        if was_running:
            container.stop(timeout=30)

        container.start()
        container.reload()

        # Re-inject Trinity meta-prompt
        trinity_result = None
        if _inject_trinity_meta_prompt:
            try:
                trinity_result = await _inject_trinity_meta_prompt(SYSTEM_AGENT_NAME)
            except Exception as e:
                logger.warning(f"Trinity injection after restart failed: {e}")

        await log_audit_event(
            event_type="system_agent",
            action="restart",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {
            "success": True,
            "message": "System agent restarted successfully",
            "name": SYSTEM_AGENT_NAME,
            "status": container.status,
            "trinity_injection": trinity_result.get("status") if trinity_result else None
        }

    except Exception as e:
        logger.error(f"Failed to restart system agent: {e}")
        await log_audit_event(
            event_type="system_agent",
            action="restart",
            user_id=current_user.username,
            agent_name=SYSTEM_AGENT_NAME,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to restart system agent: {str(e)}"
        )
