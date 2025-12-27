"""
Agent management routes for the Trinity backend.

This module provides a thin router layer over the agent service.
All business logic has been moved to services/agent_service/.
"""
import os
import json
import docker
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Query, WebSocket

from models import AgentConfig, AgentStatus, User, DeployLocalRequest
from database import db
from dependencies import get_current_user, decode_token
from services.audit_service import log_audit_event
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_agent_by_name,
)
from services.scheduler_service import scheduler_service
from services import git_service
from credentials import CredentialManager

# Import service layer functions
from services.agent_service import (
    # Helpers - re-exported for external modules
    get_accessible_agents,
    get_agents_by_prefix,
    get_next_version_name,
    get_latest_version,
    check_shared_folder_mounts_match,
    check_api_key_env_matches,
    # Lifecycle
    inject_trinity_meta_prompt,
    start_agent_internal,
    recreate_container_with_updated_config,
    # CRUD
    create_agent_internal as _create_agent_internal,
    # Deploy
    deploy_local_agent_logic,
    # Terminal
    TerminalSessionManager,
    # Permissions
    get_agent_permissions_logic,
    set_agent_permissions_logic,
    add_agent_permission_logic,
    remove_agent_permission_logic,
    # Folders
    get_agent_folders_logic,
    update_agent_folders_logic,
    get_available_shared_folders_logic,
    get_folder_consumers_logic,
    # Files
    list_agent_files_logic,
    download_agent_file_logic,
    # Queue
    get_agent_queue_status_logic,
    clear_agent_queue_logic,
    force_release_agent_logic,
    # Metrics
    get_agent_metrics_logic,
    # Stats
    get_agents_context_stats_logic,
    get_agent_stats_logic,
    # API Key
    get_agent_api_key_setting_logic,
    update_agent_api_key_setting_logic,
)

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Initialize credential manager
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
credential_manager = CredentialManager(REDIS_URL)

# WebSocket manager will be injected from main.py
manager = None

# Logger for terminal sessions
logger = logging.getLogger(__name__)

# Terminal session manager
_terminal_manager = TerminalSessionManager()


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


# ============================================================================
# Facade function for create_agent_internal
# Passes module-level dependencies to service layer
# ============================================================================

async def create_agent_internal(
    config: AgentConfig,
    current_user: User,
    request: Request,
    skip_name_sanitization: bool = False
) -> AgentStatus:
    """
    Internal function to create an agent.

    Facade that delegates to service layer with module-level dependencies.
    """
    return await _create_agent_internal(
        config=config,
        current_user=current_user,
        request=request,
        skip_name_sanitization=skip_name_sanitization,
        credential_manager=credential_manager,
        ws_manager=manager
    )


# ============================================================================
# CRUD Endpoints
# ============================================================================

@router.get("")
async def list_agents_endpoint(request: Request, current_user: User = Depends(get_current_user)):
    """List all agents accessible to the current user."""
    await log_audit_event(
        event_type="agent_access",
        action="list",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success"
    )
    return get_accessible_agents(current_user)


@router.get("/context-stats")
async def get_agents_context_stats(current_user: User = Depends(get_current_user)):
    """Get context window stats and activity state for all accessible agents."""
    return await get_agents_context_stats_logic(current_user)


@router.get("/{agent_name}")
async def get_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Get details of a specific agent."""
    agent = get_agent_by_name(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    await log_audit_event(
        event_type="agent_access",
        action="get",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    agent_dict = agent.dict() if hasattr(agent, 'dict') else dict(agent)
    user_data = db.get_user_by_username(current_user.username)
    is_admin = user_data and user_data["role"] == "admin"

    owner = db.get_agent_owner(agent_name)
    agent_dict["owner"] = owner["owner_username"] if owner else None
    agent_dict["is_owner"] = owner and owner["owner_username"] == current_user.username
    agent_dict["is_shared"] = not agent_dict["is_owner"] and not is_admin and \
                               db.is_agent_shared_with_user(agent_name, current_user.username)
    agent_dict["is_system"] = owner.get("is_system", False) if owner else False
    agent_dict["can_share"] = db.can_user_share_agent(current_user.username, agent_name)
    agent_dict["can_delete"] = db.can_user_delete_agent(current_user.username, agent_name)

    if agent_dict["can_share"]:
        shares = db.get_agent_shares(agent_name)
        agent_dict["shares"] = [s.dict() for s in shares]
    else:
        agent_dict["shares"] = []

    return agent_dict


@router.post("")
async def create_agent_endpoint(config: AgentConfig, request: Request, current_user: User = Depends(get_current_user)):
    """Create a new agent."""
    return await create_agent_internal(config, current_user, request, skip_name_sanitization=False)


@router.post("/deploy-local")
async def deploy_local_agent(
    body: DeployLocalRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Deploy a Trinity-compatible local agent."""
    return await deploy_local_agent_logic(
        body=body,
        current_user=current_user,
        request=request,
        create_agent_fn=create_agent_internal,
        credential_manager=credential_manager
    )


@router.delete("/{agent_name}")
async def delete_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Delete an agent."""
    # Check for system agent first - no one can delete these
    if db.is_system_agent(agent_name):
        await log_audit_event(
            event_type="agent_management",
            action="delete",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="forbidden",
            severity="warning",
            details={"reason": "system_agent_protected"}
        )
        raise HTTPException(
            status_code=403,
            detail="System agents cannot be deleted. Use re-initialization to reset to clean state."
        )

    if not db.can_user_delete_agent(current_user.username, agent_name):
        await log_audit_event(
            event_type="agent_management",
            action="delete",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="unauthorized",
            severity="warning"
        )
        raise HTTPException(status_code=403, detail="You don't have permission to delete this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        container.stop()
        container.remove()
    except Exception as e:
        logger.warning(f"Error stopping/removing container: {e}")

    # Delete per-agent persistent volume
    try:
        agent_volume_name = f"agent-{agent_name}-workspace"
        volume = docker_client.volumes.get(agent_volume_name)
        volume.remove()
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.warning(f"Failed to delete workspace volume for agent {agent_name}: {e}")

    # Delete all schedules for this agent
    schedules = db.list_agent_schedules(agent_name)
    for schedule in schedules:
        scheduler_service.remove_schedule(schedule.id)
    db.delete_agent_schedules(agent_name)

    # Delete git config if exists
    git_service.delete_agent_git_config(agent_name)

    # Delete agent's MCP API key
    try:
        db.delete_agent_mcp_api_key(agent_name)
    except Exception as e:
        logger.warning(f"Failed to delete MCP API key for agent {agent_name}: {e}")

    # Delete agent permissions
    try:
        db.delete_agent_permissions(agent_name)
    except Exception as e:
        logger.warning(f"Failed to delete permissions for agent {agent_name}: {e}")

    # Delete shared folder config and shared volume
    try:
        db.delete_shared_folder_config(agent_name)
        shared_volume_name = db.get_shared_volume_name(agent_name)
        try:
            shared_volume = docker_client.volumes.get(shared_volume_name)
            shared_volume.remove()
        except docker.errors.NotFound:
            pass
    except Exception as e:
        logger.warning(f"Failed to delete shared folder config for agent {agent_name}: {e}")

    db.delete_agent_ownership(agent_name)

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_deleted",
            "data": {"name": agent_name}
        }))

    await log_audit_event(
        event_type="agent_management",
        action="delete",
        user_id=current_user.username,
        agent_name=agent_name,
        ip_address=request.client.host if request.client else None,
        result="success"
    )

    return {"message": f"Agent {agent_name} deleted"}


# ============================================================================
# Lifecycle Endpoints
# ============================================================================

@router.post("/{agent_name}/start")
async def start_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Start an agent."""
    try:
        result = await start_agent_internal(agent_name)
        trinity_status = result.get("trinity_injection", "unknown")

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_started",
                "data": {"name": agent_name, "trinity_injection": trinity_status}
            }))

        await log_audit_event(
            event_type="agent_management",
            action="start",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"trinity_injection": trinity_status}
        )

        return {"message": f"Agent {agent_name} started", "trinity_injection": trinity_status}
    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="start",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to start agent: {str(e)}")


@router.post("/{agent_name}/stop")
async def stop_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Stop an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        container.stop()

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_stopped",
                "data": {"name": agent_name}
            }))

        await log_audit_event(
            event_type="agent_management",
            action="stop",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {"message": f"Agent {agent_name} stopped"}
    except Exception as e:
        await log_audit_event(
            event_type="agent_management",
            action="stop",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to stop agent: {str(e)}")


# ============================================================================
# Logs and Stats Endpoints
# ============================================================================

@router.get("/{agent_name}/logs")
async def get_agent_logs_endpoint(
    agent_name: str,
    request: Request,
    tail: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get agent container logs."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        logs = container.logs(tail=tail).decode('utf-8')

        await log_audit_event(
            event_type="agent_access",
            action="view_logs",
            user_id=current_user.username,
            agent_name=agent_name,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")


@router.get("/{agent_name}/stats")
async def get_agent_stats_endpoint(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get live container stats (CPU, memory, network) for an agent."""
    return await get_agent_stats_logic(agent_name, current_user)


# ============================================================================
# Queue Endpoints
# ============================================================================

@router.get("/{agent_name}/queue")
async def get_agent_queue_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get execution queue status for an agent."""
    return await get_agent_queue_status_logic(agent_name, current_user)


@router.post("/{agent_name}/queue/clear")
async def clear_agent_queue(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Clear all queued executions for an agent."""
    return await clear_agent_queue_logic(agent_name, current_user)


@router.post("/{agent_name}/queue/release")
async def force_release_agent(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Force release an agent from its running state."""
    return await force_release_agent_logic(agent_name, current_user)


# ============================================================================
# Info and Files Endpoints
# ============================================================================

@router.get("/{agent_name}/info")
async def get_agent_info_endpoint(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get template info and metadata for an agent."""
    import httpx

    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    container.reload()

    if container.status != "running":
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "stopped",
            "message": "Agent is stopped. Start the agent to see full template info."
        }

    try:
        agent_url = f"http://agent-{agent_name}:8000/api/template/info"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                data = response.json()
                data["status"] = "running"
                return data
            else:
                labels = container.labels
                return {
                    "has_template": bool(labels.get("trinity.template")),
                    "agent_name": agent_name,
                    "template_name": labels.get("trinity.template", ""),
                    "type": labels.get("trinity.agent-type", ""),
                    "resources": {
                        "cpu": labels.get("trinity.cpu", ""),
                        "memory": labels.get("trinity.memory", "")
                    },
                    "status": "running",
                    "message": "Template info endpoint not available in this agent version"
                }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except Exception as e:
        labels = container.labels
        return {
            "has_template": bool(labels.get("trinity.template")),
            "agent_name": agent_name,
            "template_name": labels.get("trinity.template", ""),
            "type": labels.get("trinity.agent-type", ""),
            "resources": {
                "cpu": labels.get("trinity.cpu", ""),
                "memory": labels.get("trinity.memory", "")
            },
            "status": "running",
            "message": f"Could not fetch template info: {str(e)}"
        }


@router.get("/{agent_name}/files")
async def list_agent_files_endpoint(
    agent_name: str,
    request: Request,
    path: str = "/home/developer",
    current_user: User = Depends(get_current_user)
):
    """List files in the agent's workspace directory."""
    return await list_agent_files_logic(agent_name, path, current_user, request)


@router.get("/{agent_name}/files/download")
async def download_agent_file_endpoint(
    agent_name: str,
    request: Request,
    path: str,
    current_user: User = Depends(get_current_user)
):
    """Download a file from the agent's workspace."""
    return await download_agent_file_logic(agent_name, path, current_user, request)


# ============================================================================
# Activity Stream Endpoints
# ============================================================================

@router.get("/{agent_name}/activities")
async def get_agent_activities(
    agent_name: str,
    activity_type: Optional[str] = None,
    activity_state: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get activity history for a specific agent."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    activities = db.get_agent_activities(
        agent_name=agent_name,
        activity_type=activity_type,
        activity_state=activity_state,
        limit=limit
    )

    return {
        "agent_name": agent_name,
        "count": len(activities),
        "activities": activities
    }


@router.get("/activities/timeline")
async def get_activity_timeline(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    activity_types: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """Get cross-agent activity timeline."""
    types_list = activity_types.split(",") if activity_types else None

    all_activities = db.get_activities_in_range(
        start_time=start_time,
        end_time=end_time,
        activity_types=types_list,
        limit=limit * 2
    )

    filtered_activities = []
    for activity in all_activities:
        agent_name = activity.get("agent_name")
        if db.can_user_access_agent(current_user.username, agent_name):
            filtered_activities.append(activity)
            if len(filtered_activities) >= limit:
                break

    return {
        "count": len(filtered_activities),
        "start_time": start_time,
        "end_time": end_time,
        "activity_types": types_list,
        "timeline": filtered_activities
    }


# ============================================================================
# Agent Permissions Endpoints
# ============================================================================

@router.get("/{agent_name}/permissions")
async def get_agent_permissions(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get permissions for an agent."""
    return await get_agent_permissions_logic(agent_name, current_user)


@router.put("/{agent_name}/permissions")
async def set_agent_permissions(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Set permissions for an agent (full replacement)."""
    return await set_agent_permissions_logic(agent_name, body, current_user, request)


@router.post("/{agent_name}/permissions/{target_agent}")
async def add_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Add permission for an agent to communicate with another agent."""
    return await add_agent_permission_logic(agent_name, target_agent, current_user, request)


@router.delete("/{agent_name}/permissions/{target_agent}")
async def remove_agent_permission(
    agent_name: str,
    target_agent: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Remove permission for an agent to communicate with another agent."""
    return await remove_agent_permission_logic(agent_name, target_agent, current_user, request)


# ============================================================================
# Custom Metrics Endpoints
# ============================================================================

@router.get("/{agent_name}/metrics")
async def get_agent_metrics(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get agent custom metrics."""
    return await get_agent_metrics_logic(agent_name, current_user)


# ============================================================================
# Shared Folders Endpoints
# ============================================================================

@router.get("/{agent_name}/folders")
async def get_agent_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get shared folder configuration for an agent."""
    return await get_agent_folders_logic(agent_name, current_user)


@router.put("/{agent_name}/folders")
async def update_agent_folders(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Update shared folder configuration for an agent."""
    return await update_agent_folders_logic(agent_name, body, current_user, request)


@router.get("/{agent_name}/folders/available")
async def get_available_shared_folders(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get list of shared folders available for this agent to mount."""
    return await get_available_shared_folders_logic(agent_name, current_user)


@router.get("/{agent_name}/folders/consumers")
async def get_folder_consumers(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get list of agents that can consume this agent's shared folder."""
    return await get_folder_consumers_logic(agent_name, current_user)


# ============================================================================
# API Key Settings Endpoints
# ============================================================================

@router.get("/{agent_name}/api-key-setting")
async def get_agent_api_key_setting(
    agent_name: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Get the API key setting for an agent."""
    return await get_agent_api_key_setting_logic(agent_name, current_user)


@router.put("/{agent_name}/api-key-setting")
async def update_agent_api_key_setting(
    agent_name: str,
    request: Request,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """Update the API key setting for an agent."""
    return await update_agent_api_key_setting_logic(agent_name, body, current_user, request)


# ============================================================================
# Terminal WebSocket Endpoint
# ============================================================================

@router.websocket("/{agent_name}/terminal")
async def agent_terminal(
    websocket: WebSocket,
    agent_name: str,
    mode: str = Query(default="claude")
):
    """Interactive terminal WebSocket for any agent."""
    await _terminal_manager.handle_terminal_session(
        websocket=websocket,
        agent_name=agent_name,
        mode=mode,
        decode_token_fn=decode_token
    )
