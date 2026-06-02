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

from models import AgentConfig, AgentStatus, User, DeployLocalRequest, CircuitBreakerConfigUpdate
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
    # #73: one scoped query instead of an N+1 per-agent lookup.
    auto_sync_map = db.get_all_git_auto_sync_enabled(accessible)

    entries = []
    for name in sorted(accessible):
        row = by_name.get(name)
        entries.append({
            "agent_name": name,
            "auto_sync_enabled": auto_sync_map.get(name, False),
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
    from services.capacity_manager import get_capacity_manager
    from datetime import datetime

    # Get all agents with their capacities
    agent_capacities = db.get_all_agents_parallel_capacity()

    # CAPACITY-CONSOLIDATE (#428): bulk capacity meter via CapacityManager.
    capacity = get_capacity_manager()
    slot_states = await capacity.get_all_states(agent_capacities)

    # #526: surface non-closed dispatch breakers for the dashboard badge. Gated
    # on the global master switch (zero cost when off); pipelined HGETALL over
    # the known agent list (no keyspace SCAN); only OPEN breakers are returned.
    circuit_breakers: dict = {}
    from config import DISPATCH_BREAKER_ENABLED
    if DISPATCH_BREAKER_ENABLED:
        from services.dispatch_breaker import get_dispatch_states_for
        all_states = get_dispatch_states_for(list(agent_capacities.keys()))
        circuit_breakers = {
            n: s for n, s in all_states.items() if s.get("state") != "closed"
        }

    return BulkSlotState(
        agents=slot_states,
        timestamp=utc_now_iso(),
        circuit_breakers=circuit_breakers,
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
        # #834 Phase 1c: a just-recovered agent has a live
        # agent_ownership row (deleted_at cleared, so the auth
        # dependency above passed) but NO container — soft-delete
        # removed it and recovery is metadata-only. Without this
        # fallback the operator can't even see the recovered agent
        # to start it: recovery would be a dead-end. Synthesize a
        # DB-backed "stopped, no container" representation so the UI
        # can list it and offer a Start button. (Container recreate
        # on recover is deferred Phase 2.)
        owner_row = db.get_agent_owner(agent_name)
        if not owner_row:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent_dict = {
            "name": agent_name,
            "type": "",
            "status": "stopped",
            "port": 0,
            "created": owner_row.get("created_at"),
            "resources": db.get_resource_limits(agent_name) or {},
            "container_id": None,
            "template": None,
            "runtime": "claude-code",
            "base_image_version": None,
            # Signal to the UI that this agent has no container yet —
            # recovered (or otherwise container-less). Drives a
            # "Start to bring online" affordance instead of the normal
            # stopped-container controls.
            "needs_start": True,
        }
    else:
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

    # #526: embed the unified circuit-breaker block so the agent-detail header
    # badge needs no second round-trip. Gated on the global master switch — the
    # block costs two Redis reads, so when dispatch breaking is off fleet-wide
    # we skip it entirely (there's no dispatch badge to show anyway).
    from config import DISPATCH_BREAKER_ENABLED
    if DISPATCH_BREAKER_ENABLED:
        from services.circuit_breaker_view import build_circuit_breaker_block
        agent_dict["circuit_breaker"] = build_circuit_breaker_block(agent_name)
    else:
        agent_dict["circuit_breaker"] = None

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

    # Issue #834 Phase 1a: agent delete is now a SOFT-delete. We stop +
    # remove the container (Docker containers are ephemeral by design
    # per the issue), and mark `agent_ownership.deleted_at`. Everything
    # else — workspace/shared/public volumes, schedules, chat history,
    # sharing, permissions, MCP key, credentials, avatars — is
    # preserved until the retention sweep in cleanup_service.py runs
    # `purge_agent_ownership()` after `agent_soft_delete_retention_days`
    # (default 180). At that point the #816 cascade_delete primitive
    # tears down all the child rows and on-disk artifacts in one shot.

    try:
        await container_stop(container)
        await container_remove(container)
    except Exception as e:
        logger.warning(f"Error stopping/removing container: {e}")

    # BACKLOG-001: in-flight queued tasks can't be recovered (the
    # container is gone), so cancel them now rather than waiting for
    # the purge sweep — keeps the operator queue + Redis primitives
    # consistent immediately.
    try:
        from services.capacity_manager import get_capacity_manager
        await get_capacity_manager().cancel_all_overflow(
            agent_name, reason="agent_deleted"
        )
    except Exception as e:
        logger.warning(f"Failed to cancel backlog for agent {agent_name}: {e}")

    # RELIABILITY-004 / #307: clear the agent's heartbeat Redis keys now. The
    # `seen` marker has no TTL, so without this it would leak past delete; the
    # hb/misses keys self-expire but are cleared too for cleanliness. The
    # watch loop bounds itself to live agents, so a stopped container is never
    # read regardless — this just stops the slow key accumulation. Best-effort.
    try:
        from services import heartbeat_service
        heartbeat_service.clear_heartbeat(agent_name)
    except Exception as e:
        logger.warning(f"Failed to clear heartbeat keys for agent {agent_name}: {e}")

    # Mark the row as soft-deleted. Children stay until the retention
    # sweep; the unique constraint on agent_name naturally blocks reuse
    # during the retention window.
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


@router.get("/{agent_name}/token-stats")
async def get_agent_token_stats(
    agent_name: AuthorizedAgentByName,
    current_user: CurrentUser,
):
    """Get token usage statistics for an agent.

    Returns lifetime totals, 24h and 7d windows, a 7-day daily breakdown,
    and a trend percentage comparing today vs the 7-day daily average.
    Sourced entirely from the database — persists across agent restarts.
    """
    return db.get_agent_token_stats(agent_name)


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


@router.post("/{agent_name}/circuit-breaker/reset")
async def reset_circuit_breaker_endpoint(
    agent_name: AuthorizedAgentByName,
    current_user: User = Depends(require_role("admin")),
):
    """Force the agent's circuit breakers to closed (#921, #526).

    Admin only. Operator escape hatch — resets BOTH the transport breaker
    (#631/#921, ``agent:circuit:{name}``) and the dispatch breaker
    (#526, ``agent:dispatch:{name}``) so a single call fully un-trips an
    agent the operator knows is healthy, without waiting for either cooldown.
    """
    from services.agent_client import CircuitState, reset_circuit
    from services.dispatch_breaker import DispatchBreaker, reset_dispatch
    # Capture state-before for the response so postmortems can see what we
    # reset out of. Racey against the DEL but fine — best-effort observability.
    prior_transport = CircuitState(agent_name).state
    prior_dispatch = DispatchBreaker(agent_name).to_dict().get("state", "closed")
    reset_circuit(agent_name)
    reset_dispatch(agent_name)
    return {
        "agent_name": agent_name,
        "prior_state": prior_transport,  # back-compat field (transport breaker)
        "prior_transport_state": prior_transport,
        "prior_dispatch_state": prior_dispatch,
        "new_state": "closed",
        "reset_at": utc_now_iso(),
    }


@router.get("/{agent_name}/circuit-breaker")
async def get_circuit_breaker_endpoint(
    agent_name: AuthorizedAgentByName,
    current_user: CurrentUser,
):
    """Unified breaker state for an agent (#526).

    Returns both breakers plus a derived ``open`` boolean (true when EITHER the
    dispatch breaker is open or the transport breaker is open/dormant) and the
    per-agent + global config flags.
    """
    from services.circuit_breaker_view import build_circuit_breaker_block

    return {"agent_name": agent_name, **build_circuit_breaker_block(agent_name)}


@router.put("/{agent_name}/circuit-breaker")
async def set_circuit_breaker_endpoint(
    agent_name: OwnedAgentByName,
    body: CircuitBreakerConfigUpdate,
    current_user: CurrentUser,
):
    """Enable/disable the per-agent dispatch breaker (#526). Owner-only.

    Note: the global ``DISPATCH_BREAKER_ENABLED`` master switch must also be on
    for the breaker to actually engage (returned in ``global_enabled``).
    """
    from config import DISPATCH_BREAKER_ENABLED

    db.set_circuit_breaker_enabled(agent_name, body.enabled)
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="circuit_breaker_config",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={"enabled": body.enabled},
    )
    return {
        "agent_name": agent_name,
        "enabled": body.enabled,
        "global_enabled": bool(DISPATCH_BREAKER_ENABLED),
    }


# ============================================================================
# Heartbeat Endpoint (RELIABILITY-004 / #307)
# ============================================================================

class HeartbeatPayload(BaseModel):
    """Lightweight liveness payload POSTed by the agent every ~5s."""
    memory_mb: Optional[float] = None
    active_executions: Optional[int] = None
    uptime_s: Optional[float] = None


@router.post("/{agent_name}/heartbeat")
async def agent_heartbeat(agent_name: str, payload: HeartbeatPayload, request: Request):
    """Receive a liveness heartbeat from an agent (RELIABILITY-004 / #307).

    Authenticated with the agent's own ``TRINITY_MCP_API_KEY`` (Option B,
    least-privilege) — *not* the user JWT and not the master internal secret.
    An agent may only heartbeat itself: the key must be agent-scoped and its
    bound ``agent_name`` must equal the path. User/system/null keys → 403.

    Validation runs with ``track_usage=False`` so a 5s beat doesn't amplify the
    key's ``usage_count`` or write to SQLite on every beat. The write is
    best-effort: a Redis blip returns ``stored=false`` rather than 5xx, since
    the heartbeat is an additive signal and the 30s monitor stays authoritative.
    """
    from services import heartbeat_service

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="Heartbeat requires the agent's own MCP key")
    token = auth_header[7:]  # strip "Bearer "

    res = db.validate_mcp_api_key(token, track_usage=False)
    if not heartbeat_service.authorize_heartbeat(res, agent_name):
        raise HTTPException(status_code=403, detail="Heartbeat requires the agent's own MCP key")

    stored = heartbeat_service.record_heartbeat(
        agent_name, payload.model_dump(exclude_none=True)
    )
    return {"ok": True, "stored": stored}


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
