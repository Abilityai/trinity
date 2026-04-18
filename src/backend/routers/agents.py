"""
Agent management routes for the Trinity backend.

This module provides a thin router layer over the agent service.
All business logic has been moved to services/agent_service/.

Related routers (same /api/agents prefix):
- agent_config.py  — per-agent settings (autonomy, read-only, resources, capabilities, capacity, timeout, api-key)
- agent_files.py   — file management, info, playbooks, permissions, metrics, folders
- agent_rename.py  — rename endpoint
- agent_ssh.py     — SSH access
"""
import json
import docker
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Query, WebSocket
from pydantic import BaseModel

from models import AgentConfig, AgentStatus, User, DeployLocalRequest
from database import db
from dependencies import get_current_user, decode_token, require_role, AuthorizedAgentByName, OwnedAgentByName, CurrentUser
from services.docker_service import (
    docker_client,
    get_agent_container,
    get_agent_by_name,
)
from services.docker_utils import (
    container_stop, container_remove, container_reload,
    volume_get, volume_remove
)
from services import git_service
from services.image_generation_prompts import AVATAR_EMOTIONS
from services.platform_audit_service import (
    platform_audit_service,
    AuditEventType,
)

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
    start_agent_internal,
    recreate_container_with_updated_config,
    # CRUD
    create_agent_internal as _create_agent_internal,
    # Deploy
    deploy_local_agent_logic,
    # Terminal
    TerminalSessionManager,
    # Queue
    get_agent_queue_status_logic,
    clear_agent_queue_logic,
    force_release_agent_logic,
    # Stats
    get_agents_context_stats_logic,
    get_agent_stats_logic,
    invalidate_context_stats_cache,
    # Autonomy (global view)
    get_all_autonomy_status_logic,
)
from utils.helpers import utc_now_iso

router = APIRouter(prefix="/api/agents", tags=["agents"])

# WebSocket manager will be injected from main.py
manager = None
filtered_manager = None  # For Trinity Connect /ws/events

# Logger for terminal sessions
logger = logging.getLogger(__name__)

# Terminal session manager
_terminal_manager = TerminalSessionManager()


def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


def set_filtered_websocket_manager(ws_manager):
    """Set the filtered WebSocket manager for /ws/events (Trinity Connect)."""
    global filtered_manager
    filtered_manager = ws_manager


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
        ws_manager=manager
    )


# ============================================================================
# CRUD Endpoints
# ============================================================================

@router.get("")
async def list_agents_endpoint(
    request: Request,
    tags: str = None,
    current_user: User = Depends(get_current_user)
):
    """
    List all agents accessible to the current user.

    Args:
        tags: Optional comma-separated list of tags to filter by (OR logic).
              Example: ?tags=due-diligence,content-ops

    Returns:
        List of agents with their metadata including tags.
    """
    from database import db

    agents = get_accessible_agents(current_user)

    # If tags filter specified, filter agents
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
        if tag_list:
            # Get agents that have any of the specified tags
            matching_agents = set(db.get_agents_by_tags(tag_list))
            agents = [a for a in agents if a.get("name") in matching_agents]

    # Add tags to each agent in response
    agent_names = [a.get("name") for a in agents]
    all_tags = db.get_tags_for_agents(agent_names)

    for agent in agents:
        agent["tags"] = all_tags.get(agent.get("name"), [])

    return agents


@router.get("/context-stats")
async def get_agents_context_stats(current_user: User = Depends(get_current_user)):
    """Get context window stats and activity state for all accessible agents."""
    return await get_agents_context_stats_logic(current_user)


@router.get("/execution-stats")
async def get_agents_execution_stats(
    hours: int = 24,
    include_7d: bool = False,
    current_user: User = Depends(get_current_user)
):
    """Get execution statistics for all accessible agents.

    Returns task counts, success rates, costs, last execution times,
    and schedule counts for all agents the user can access.

    Args:
        hours: Time window in hours (default: 24)
        include_7d: If true, include 7-day stats alongside 24h stats
    """
    # Get all stats from database
    if include_7d:
        all_stats = db.get_all_agents_execution_stats_dual()
    else:
        all_stats = db.get_all_agents_execution_stats(hours=hours)

    # Get schedule counts for all agents
    schedule_counts = db.get_all_agents_schedule_counts()

    # Filter to only agents the user can access
    accessible_agents = {a['name'] for a in get_accessible_agents(current_user)}

    filtered_stats = []
    for stat in all_stats:
        if stat["name"] in accessible_agents:
            # Add schedule counts to each stat
            agent_schedules = schedule_counts.get(stat["name"], {"total": 0, "enabled": 0})
            stat["schedules_total"] = agent_schedules["total"]
            stat["schedules_enabled"] = agent_schedules["enabled"]
            filtered_stats.append(stat)

    # Also include agents with schedules but no executions in the time window
    stats_agents = {s["name"] for s in filtered_stats}
    for agent_name in accessible_agents:
        if agent_name not in stats_agents:
            agent_schedules = schedule_counts.get(agent_name, {"total": 0, "enabled": 0})
            if agent_schedules["total"] > 0:
                empty_stat = {
                    "name": agent_name,
                    "task_count_24h": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "running_count": 0,
                    "success_rate": 0,
                    "total_cost": 0,
                    "last_execution_at": None,
                    "schedules_total": agent_schedules["total"],
                    "schedules_enabled": agent_schedules["enabled"]
                }
                if include_7d:
                    empty_stat.update({
                        "task_count_7d": 0,
                        "success_count_7d": 0,
                        "failed_count_7d": 0,
                        "running_count_7d": 0,
                        "success_rate_7d": 0,
                        "total_cost_7d": 0,
                        "last_execution_at_7d": None
                    })
                filtered_stats.append(empty_stat)

    return {"agents": filtered_stats}


@router.get("/autonomy-status")
async def get_all_autonomy_status(
    current_user: User = Depends(get_current_user)
):
    """Get autonomy status for all accessible agents (for dashboard display)."""
    return await get_all_autonomy_status_logic(current_user)


@router.get("/sync-health")
async def get_all_sync_health(
    current_user: User = Depends(get_current_user)
):
    """Dashboard batch endpoint for sync-health dots (#389).

    Returns one entry per accessible agent. Entries join `agent_sync_state`
    with the per-agent auto-sync flag so the UI can colour dots and badge
    agents that have auto-sync off.
    """
    accessible = {a["name"] for a in get_accessible_agents(current_user)}
    rows = db.list_sync_states()
    by_name = {r["agent_name"]: r for r in rows if r["agent_name"] in accessible}

    entries = []
    for name in sorted(accessible):
        row = by_name.get(name)
        auto_sync = db.get_git_auto_sync_enabled(name)
        entries.append({
            "agent_name": name,
            "auto_sync_enabled": bool(auto_sync),
            "last_sync_at": (row or {}).get("last_sync_at"),
            "last_sync_status": (row or {}).get("last_sync_status") or "never",
            "consecutive_failures": (row or {}).get("consecutive_failures") or 0,
            "last_error_summary": (row or {}).get("last_error_summary"),
            "behind_working": (row or {}).get("behind_working") or 0,
            "behind_main": (row or {}).get("behind_main") or 0,
            "ahead_working": (row or {}).get("ahead_working") or 0,
            "ahead_main": (row or {}).get("ahead_main") or 0,
        })
    return {"agents": entries}


@router.get("/slots")
async def get_all_agent_slots(
    current_user: User = Depends(get_current_user)
):
    """
    Get slot state for all agents (bulk endpoint for Dashboard polling).

    Returns:
    - agents: Dict mapping agent_name to {"max": N, "active": M}
    - timestamp: ISO timestamp of response
    """
    from db_models import BulkSlotState
    from services.slot_service import get_slot_service
    from datetime import datetime

    # Get all agents with their capacities
    agent_capacities = db.get_all_agents_parallel_capacity()

    # Get slot states from Redis
    slot_service = get_slot_service()
    slot_states = await slot_service.get_all_slot_states(agent_capacities)

    return BulkSlotState(
        agents=slot_states,
        timestamp=utc_now_iso()
    )


@router.get("/permissions-edges")
async def get_all_permission_edges(
    current_user: User = Depends(get_current_user)
):
    """
    Get all permission edges for dashboard graph visualization (bulk endpoint).

    Returns all agent-to-agent permission edges in a single query,
    filtered to only include edges where the user can access both agents.

    This replaces N per-agent permission calls with 1 bulk call.
    """
    # Get accessible agents once (not N times)
    accessible = {a['name'] for a in get_accessible_agents(current_user)}

    # Single DB query filtered at SQL level
    edges = db.get_all_permission_edges(accessible)

    return {"edges": edges}


@router.get("/{agent_name}")
async def get_agent_endpoint(agent_name: AuthorizedAgentByName, request: Request, current_user: CurrentUser):
    """Get details of a specific agent."""
    agent = get_agent_by_name(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

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
    agent_dict["autonomy_enabled"] = db.get_autonomy_enabled(agent_name)
    read_only_data = db.get_read_only_mode(agent_name)
    agent_dict["read_only_enabled"] = read_only_data["enabled"]

    # Avatar URL (AVATAR-001)
    identity = db.get_avatar_identity(agent_name)
    if identity and identity.get("updated_at"):
        agent_dict["avatar_url"] = f"/api/agents/{agent_name}/avatar?v={identity['updated_at']}"
    else:
        agent_dict["avatar_url"] = None

    if agent_dict["can_share"]:
        shares = db.get_agent_shares(agent_name)
        agent_dict["shares"] = [s.dict() for s in shares]
    else:
        agent_dict["shares"] = []

    return agent_dict


@router.post("")
async def create_agent_endpoint(config: AgentConfig, request: Request, current_user: User = Depends(require_role("creator"))):
    """Create a new agent. Requires creator role or above."""
    result = await create_agent_internal(config, current_user, request, skip_name_sanitization=False)
    # SEC-001: audit after successful creation. Failures here swallowed by the service.
    await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="create",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=config.name,
        endpoint=str(request.url.path),
        details={
            "template": getattr(config, "template", None),
            "base_image": getattr(config, "base_image", None),
            "agent_type": getattr(config, "agent_type", None),
        },
    )
    return result


@router.post("/deploy-local")
async def deploy_local_agent(
    body: DeployLocalRequest,
    request: Request,
    current_user: User = Depends(require_role("creator"))
):
    """Deploy a Trinity-compatible local agent. Requires creator role or above."""
    return await deploy_local_agent_logic(
        body=body,
        current_user=current_user,
        request=request,
        create_agent_fn=create_agent_internal
    )


@router.delete("/{agent_name}")
async def delete_agent_endpoint(agent_name: str, request: Request, current_user: User = Depends(get_current_user)):
    """Delete an agent."""
    # Check for system agent first - no one can delete these
    if db.is_system_agent(agent_name):
        raise HTTPException(
            status_code=403,
            detail="System agents cannot be deleted. Use re-initialization to reset to clean state."
        )

    if not db.can_user_delete_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to delete this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        await container_stop(container)
        await container_remove(container)
    except Exception as e:
        logger.warning(f"Error stopping/removing container: {e}")

    # Delete per-agent persistent volume
    try:
        agent_volume_name = f"agent-{agent_name}-workspace"
        volume = await volume_get(agent_volume_name)
        await volume_remove(volume)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.warning(f"Failed to delete workspace volume for agent {agent_name}: {e}")

    # Delete all schedules for this agent
    # Dedicated scheduler syncs from database automatically
    db.delete_agent_schedules(agent_name)

    # BACKLOG-001: Cancel any queued backlog items before deleting the agent
    # so they don't sit around in schedule_executions pointing at a dead agent.
    try:
        from services.backlog_service import get_backlog_service
        await get_backlog_service().cancel_all_backlog(
            agent_name, reason="agent_deleted"
        )
    except Exception as e:
        logger.warning(f"Failed to cancel backlog for agent {agent_name}: {e}")

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

    # Delete agent event subscriptions (EVT-001)
    try:
        db.delete_agent_event_subscriptions(agent_name)
    except Exception as e:
        logger.warning(f"Failed to delete event subscriptions for agent {agent_name}: {e}")

    # Delete agent skills
    try:
        db.delete_agent_skills(agent_name)
    except Exception as e:
        logger.warning(f"Failed to delete skills for agent {agent_name}: {e}")

    # Delete shared folder config and shared volume
    try:
        db.delete_shared_folder_config(agent_name)
        shared_volume_name = db.get_shared_volume_name(agent_name)
        try:
            shared_volume = await volume_get(shared_volume_name)
            await volume_remove(shared_volume)
        except docker.errors.NotFound:
            pass
    except Exception as e:
        logger.warning(f"Failed to delete shared folder config for agent {agent_name}: {e}")

    # Delete agent tags (ORG-001)
    try:
        db.delete_agent_tags(agent_name)
    except Exception as e:
        logger.warning(f"Failed to delete tags for agent {agent_name}: {e}")

    # Delete cached avatar, reference, and emotion images (AVATAR-001, AVATAR-002)
    try:
        for ext in (".webp", ".png"):
            p = Path("/data/avatars") / f"{agent_name}{ext}"
            if p.exists():
                p.unlink()
        ref = Path("/data/avatars") / f"{agent_name}_ref.png"
        if ref.exists():
            ref.unlink()
        for emotion in AVATAR_EMOTIONS:
            for ext in (".webp", ".png"):
                p = Path("/data/avatars") / f"{agent_name}_emotion_{emotion}{ext}"
                if p.exists():
                    p.unlink()
    except Exception as e:
        logger.warning(f"Failed to delete avatar for agent {agent_name}: {e}")

    db.delete_agent_ownership(agent_name)

    # SEC-001: audit delete after all cleanup and ownership removal committed.
    await platform_audit_service.log(
        event_type=AuditEventType.AGENT_LIFECYCLE,
        event_action="delete",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
    )

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_deleted",
            "data": {"name": agent_name}
        }))

    return {"message": f"Agent {agent_name} deleted"}


# ============================================================================
# Lifecycle Endpoints
# ============================================================================

@router.post("/{agent_name}/start")
async def start_agent_endpoint(agent_name: AuthorizedAgentByName, request: Request, current_user: CurrentUser):
    """Start an agent."""
    try:
        result = await start_agent_internal(agent_name)
        invalidate_context_stats_cache()  # PERF-269
        credentials_status = result.get("credentials_injection", "unknown")
        credentials_result = result.get("credentials_result", {})

        # SEC-001: audit after container reports running and credentials are injected.
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="start",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
            details={"credentials_injection": credentials_status},
        )

        event = {
            "event": "agent_started",
            "type": "agent_started",  # Normalized type field for filtering
            "name": agent_name,
            "data": {"name": agent_name, "credentials_injection": credentials_status}
        }
        if manager:
            await manager.broadcast(json.dumps(event))
        # Also broadcast to filtered manager (Trinity Connect /ws/events)
        if filtered_manager:
            await filtered_manager.broadcast_filtered(event)

        return {
            "message": f"Agent {agent_name} started",
            "credentials_injection": credentials_status,
            "credentials_result": credentials_result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start agent: {str(e)}")


@router.post("/{agent_name}/stop")
async def stop_agent_endpoint(agent_name: AuthorizedAgentByName, request: Request, current_user: CurrentUser):
    """Stop an agent."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        await container_stop(container)
        invalidate_context_stats_cache()  # PERF-269

        # SEC-001: audit after container_stop returns cleanly.
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_LIFECYCLE,
            event_action="stop",
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=str(request.url.path),
        )

        event = {
            "event": "agent_stopped",
            "type": "agent_stopped",  # Normalized type field for filtering
            "name": agent_name,
            "data": {"name": agent_name}
        }
        if manager:
            await manager.broadcast(json.dumps(event))
        # Also broadcast to filtered manager (Trinity Connect /ws/events)
        if filtered_manager:
            await filtered_manager.broadcast_filtered(event)

        return {"message": f"Agent {agent_name} stopped"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop agent: {str(e)}")


# ============================================================================
# Logs and Stats Endpoints
# ============================================================================

@router.get("/{agent_name}/logs")
async def get_agent_logs_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request,
    tail: int = 100
):
    """Get agent container logs."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        logs = container.logs(tail=tail).decode('utf-8')

        return {"logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")


@router.get("/{agent_name}/stats")
async def get_agent_stats_endpoint(
    agent_name: AuthorizedAgentByName,
    request: Request,
    current_user: CurrentUser,
):
    """Get live container stats (CPU, memory, network) for an agent."""
    return await get_agent_stats_logic(agent_name, current_user)


# ============================================================================
# Queue Endpoints
# ============================================================================

@router.get("/{agent_name}/queue")
async def get_agent_queue_status(
    agent_name: AuthorizedAgentByName,
    current_user: CurrentUser,
):
    """Get execution queue status for an agent."""
    return await get_agent_queue_status_logic(agent_name, current_user)


@router.post("/{agent_name}/queue/clear")
async def clear_agent_queue(
    agent_name: OwnedAgentByName,
    current_user: CurrentUser,
):
    """Clear all queued executions for an agent. Owner-only."""
    return await clear_agent_queue_logic(agent_name, current_user)


@router.post("/{agent_name}/queue/release")
async def force_release_agent(
    agent_name: OwnedAgentByName,
    current_user: CurrentUser,
):
    """Force release an agent from its running state. Owner-only."""
    return await force_release_agent_logic(agent_name, current_user)


# ============================================================================
# Activity Stream Endpoints
# ============================================================================

@router.get("/{agent_name}/activities")
async def get_agent_activities(
    agent_name: AuthorizedAgentByName,
    activity_type: Optional[str] = None,
    activity_state: Optional[str] = None,
    limit: int = 100
):
    """Get activity history for a specific agent."""
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
        "activities": filtered_activities  # Frontend expects "activities" (fixed 2026-01-15)
    }


# ============================================================================
# Terminal WebSocket Endpoint
# ============================================================================

@router.websocket("/{agent_name}/terminal")
async def agent_terminal(
    websocket: WebSocket,
    agent_name: str,
    mode: str = Query(default="claude"),
    model: str = Query(default=None)
):
    """Interactive terminal WebSocket for any agent."""
    await _terminal_manager.handle_terminal_session(
        websocket=websocket,
        agent_name=agent_name,
        mode=mode,
        decode_token_fn=decode_token,
        model=model
    )
