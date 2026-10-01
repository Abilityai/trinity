# mcp: none — Operating Room fleet ops (fleet restart, #1860) — an admin / ops-key surface, not an agent capability
"""
Fleet Operations routes for the Trinity backend.

Provides endpoints for platform-wide operations:
- Fleet status and health
- Fleet-wide start/stop/restart
- Schedule control (pause/resume)
- Emergency stop

These endpoints are admin-only and intended for platform operations.
"""
import os
import logging
import concurrent.futures
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request, Query
import httpx

from models import User
from database import db
from dependencies import get_current_user, assert_admin, reject_agent_principal
from services.docker_service import get_agent_container, docker_client, list_all_agents_fast
from services.docker_utils import container_stop
from services.agent_client import get_agent_client
from services.agent_service.lifecycle import restart_agent_internal
from services.agent_service.stats import invalidate_context_stats_cache
from db.agents import SYSTEM_AGENT_NAME
from redis_breaker_util import get_breaker_redis, SingleFlightLock, LeaseState
from utils.helpers import utc_now_iso
from services.platform_audit_service import platform_audit_service, AuditEventType

router = APIRouter(prefix="/api/ops", tags=["operations"])
from services import fleet_ops_service, ops_costs_service

logger = logging.getLogger(__name__)

# #1860: one fleet restart at a time. A client timeout (nginx 60s / Cloudflare
# ~100s) invites a retry while the first loop is still running server-side, and
# two loops interleaving stop/start on the same agent is the #799/#1817
# stop-racing-start wedge class. TTL bounds a crashed holder; the live loop
# refreshes its own lease each iteration, ownership-checked (#1919).
# #1919: the TTL must outlive the slowest SINGLE agent, or a mid-agent lapse is
# arithmetically guaranteed on the slow path — the refresh is per-iteration, and
# start_agent_internal runs skill injection, itself sized at
# skill_service._INJECT_LOCK_TTL_SECONDS (1800s worst case) + container-stop
# margin. Keep the two constants linked when either side changes.


# ============================================================================
# Fleet Status & Health
# ============================================================================

@router.get("/fleet/status")
async def get_fleet_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of all agents in the fleet.

    Admin-only. Returns a comprehensive list of all agents with their
    container status, context usage, last activity time, and system agent flag.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    assert_admin(current_user, allow_scopes={"ops"})

    agents = list_all_agents_fast()

    fleet_status = []
    context_stats = {}

    # Try to get context stats for running agents using AgentClient
    running_agents = [a for a in agents if a.status == "running"]
    for agent in running_agents:
        agent_name = agent.name
        try:
            client = get_agent_client(agent_name)
            session_info = await client.get_session()
            if session_info:
                context_stats[agent_name] = {
                    "context_tokens": session_info.context_tokens,
                    "context_window": session_info.context_window,
                    "context_percent": session_info.context_percent
                }
        except Exception:
            pass  # Agent not responding, skip context

    # Get last activity for each agent
    for agent in agents:
        agent_name = agent.name

        # Get owner info
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False

        # Get last activity from database
        last_activity = db.get_agent_last_activity(agent_name) if hasattr(db, 'get_agent_last_activity') else None

        agent_status = {
            "name": agent_name,
            "status": agent.status,
            "is_system": is_system,
            "created_at": agent.created.isoformat() if agent.created else None,
            "context": context_stats.get(agent_name),
            "last_activity": last_activity
        }

        fleet_status.append(agent_status)

    # Calculate summary
    total = len(agents)
    running = sum(1 for a in agents if a.status == "running")
    stopped = sum(1 for a in agents if a.status != "running")
    high_context = sum(
        1 for name, stats in context_stats.items()
        if stats.get("context_percent", 0) > 75
    )

    return {
        "timestamp": utc_now_iso(),
        "summary": {
            "total": total,
            "running": running,
            "stopped": stopped,
            "high_context": high_context
        },
        "agents": fleet_status
    }


@router.get("/fleet/health")
async def get_fleet_health(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Thin since #1028 — gate here, orchestration in fleet_ops_service."""
    assert_admin(current_user, allow_scopes={"ops"})
    return await fleet_ops_service.get_fleet_health_impl(request, current_user)


# ============================================================================
# Fleet Operations
# ============================================================================

@router.post("/fleet/restart")
async def restart_fleet(
    request: Request,
    current_user: User = Depends(get_current_user),
    filter_status: Optional[str] = Query(None, description="Only restart agents with this status"),
    system_prefix: Optional[str] = Query(None, description="Only restart agents matching this system prefix")
):
    """Thin since #1028 — gate here, orchestration in fleet_ops_service."""
    assert_admin(current_user)
    return await fleet_ops_service.restart_fleet_impl(request, current_user, filter_status, system_prefix)


@router.post("/fleet/stop")
async def stop_fleet(
    request: Request,
    current_user: User = Depends(get_current_user),
    system_prefix: Optional[str] = Query(None, description="Only stop agents matching this system prefix")
):
    """Thin since #1028 — gate here, orchestration in fleet_ops_service."""
    assert_admin(current_user)
    return await fleet_ops_service.stop_fleet_impl(request, current_user, system_prefix)


# ============================================================================
# Schedule Control
# ============================================================================

@router.get("/schedules")
async def list_all_schedules(
    request: Request,
    current_user: User = Depends(get_current_user),
    agent_name: Optional[str] = Query(None, description="Filter by agent name"),
    enabled_only: bool = Query(False, description="Only return enabled schedules")
):
    """
    List all schedules across all agents.

    Admin-only. Returns schedule information including next run times
    and recent execution status.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    assert_admin(current_user, allow_scopes={"ops"})

    schedules = db.list_all_schedules()

    # Apply filters
    if agent_name:
        schedules = [s for s in schedules if s.agent_name == agent_name]
    if enabled_only:
        schedules = [s for s in schedules if s.enabled]

    # #1265: one bulk query for the latest execution of every schedule, instead
    # of a get_schedule_executions() call per schedule (N+1 that grew with the
    # total schedule count across the fleet).
    latest_by_schedule = db.get_latest_execution_per_schedule([s.id for s in schedules])

    # Build response with schedule details
    schedule_list = []
    for schedule in schedules:
        last_execution = latest_by_schedule.get(schedule.id)

        schedule_data = {
            "id": schedule.id,
            "agent_name": schedule.agent_name,
            "name": schedule.name,
            "cron_expression": schedule.cron_expression,
            "message": schedule.message,
            "enabled": schedule.enabled,
            "timezone": schedule.timezone,
            "description": schedule.description,
            "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
            "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            # last_execution is the slim dict from get_latest_execution_per_schedule
            # (#1265): only dashboard fields, timestamps already ISO-normalised.
            "last_execution": last_execution
        }
        schedule_list.append(schedule_data)

    # Calculate summary
    total = len(schedules)
    enabled = sum(1 for s in schedules if s.enabled)
    disabled = total - enabled

    # Group by agent
    agents_with_schedules = len(set(s.agent_name for s in schedules))

    return {
        "timestamp": utc_now_iso(),
        "summary": {
            "total": total,
            "enabled": enabled,
            "disabled": disabled,
            "agents_with_schedules": agents_with_schedules
        },
        "schedules": schedule_list
    }


@router.post("/schedules/pause")
async def pause_all_schedules(
    request: Request,
    current_user: User = Depends(get_current_user),
    agent_name: Optional[str] = Query(None, description="Only pause schedules for this agent")
):
    """
    Pause all schedules (or schedules for a specific agent).

    Admin-only. Use for maintenance windows or incident response.
    """
    assert_admin(current_user)

    # Get all enabled schedules
    schedules = db.list_all_enabled_schedules()

    # Filter by agent if specified
    if agent_name:
        schedules = [s for s in schedules if s.agent_name == agent_name]

    paused = 0
    for schedule in schedules:
        try:
            db.set_schedule_enabled(schedule.id, False)
            # Dedicated scheduler syncs from database automatically
            paused += 1
        except Exception as e:
            logger.error(f"Failed to pause schedule {schedule.id}: {e}")

    return {
        "success": True,
        "message": f"Paused {paused} schedule(s)",
        "paused_count": paused,
        "agent_filter": agent_name
    }


@router.post("/schedules/resume")
async def resume_all_schedules(
    request: Request,
    current_user: User = Depends(get_current_user),
    agent_name: Optional[str] = Query(None, description="Only resume schedules for this agent")
):
    """
    Resume all paused schedules (or schedules for a specific agent).

    Admin-only.
    """
    assert_admin(current_user)

    # Get all disabled schedules
    schedules = db.list_all_disabled_schedules() if hasattr(db, 'list_all_disabled_schedules') else []

    # If no specific method, get all schedules
    if not schedules:
        all_schedules = []
        agents = list_all_agents_fast()
        for agent in agents:
            agent_schedules = db.list_agent_schedules(agent.name)
            all_schedules.extend([s for s in agent_schedules if not s.enabled])
        schedules = all_schedules

    # Filter by agent if specified
    if agent_name:
        schedules = [s for s in schedules if s.agent_name == agent_name]

    resumed = 0
    for schedule in schedules:
        try:
            db.set_schedule_enabled(schedule.id, True)
            # Dedicated scheduler syncs from database automatically
            resumed += 1
        except Exception as e:
            logger.error(f"Failed to resume schedule {schedule.id}: {e}")

    return {
        "success": True,
        "message": f"Resumed {resumed} schedule(s)",
        "resumed_count": resumed,
        "agent_filter": agent_name
    }




@router.post("/emergency-stop")
async def emergency_stop(
    request: Request,
    current_user: User = Depends(get_current_user),
    system_prefix: Optional[str] = Query(
        None,
        description="Optional: Only stop agents whose names start with this prefix"
    )
):
    """Thin since #1028 — gate here, orchestration in fleet_ops_service."""
    assert_admin(current_user)
    return await fleet_ops_service.emergency_stop_impl(request, current_user, system_prefix)


# ============================================================================
# Alerts
# ============================================================================

@router.get("/alerts")
async def list_alerts(
    request: Request,
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    severity: Optional[str] = Query(None, description="Filter by severity: critical, warning, info")
):
    """
    List recent operational alerts.

    Admin-only. Alerts are derived from platform events.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    assert_admin(current_user, allow_scopes={"ops"})

    # TODO: Implement dedicated alerts table
    # For now, return placeholder - check fleet health for issues

    return {
        "timestamp": utc_now_iso(),
        "alerts": [],
        "message": "Alerts feature coming soon. Check fleet health for current issues."
    }


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Acknowledge an alert.

    Admin-only.
    """
    assert_admin(current_user)

    # TODO: Implement when alerts table is added
    return {
        "success": True,
        "message": "Alert acknowledgment feature coming soon"
    }


# ============================================================================
# Cost & Observability (powered by OTel)
# ============================================================================

# Import OTel configuration from observability module (enabled by default)
# OTEL_* consts moved to services/fleet_ops_service.py with their only consumer (#1028)


@router.get("/costs")
async def get_ops_costs(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Thin since #1028 — gate here, orchestration in fleet_ops_service."""
    assert_admin(current_user, allow_scopes={"ops"})
    return await ops_costs_service.get_ops_costs_impl(request, current_user)






# ============================================================================
# Fleet Auth Report (SUB-001)
# ============================================================================

@router.get("/auth-report")
async def get_auth_report(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get authentication status report for all agents.

    Admin-only. Shows subscription/API key usage across the fleet.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    assert_admin(current_user, allow_scopes={"ops"})

    agents = list_all_agents_fast()

    # Track auth modes
    subscription_agents = []
    api_key_agents = []
    not_configured_agents = []

    # Get subscription info for all agents
    for agent in agents:
        agent_name = agent.name

        # Skip system agent
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False
        if is_system:
            continue

        # Check for subscription assignment
        subscription = db.get_agent_subscription(agent_name)
        has_api_key = db.get_use_platform_api_key(agent_name) or False

        agent_info = {
            "name": agent_name,
            "status": agent.status,
        }

        if subscription:
            agent_info["subscription_name"] = subscription.name
            subscription_agents.append(agent_info)
        elif has_api_key:
            api_key_agents.append(agent_info)
        else:
            not_configured_agents.append(agent_info)

    # Get subscription summary
    subscriptions = db.list_subscriptions_with_agents()
    subscription_summary = [
        {
            "name": s.name,
            "subscription_type": s.subscription_type,
            "agent_count": s.agent_count,
            "agents": s.agents,
        }
        for s in subscriptions
    ]

    return {
        "timestamp": utc_now_iso(),
        "summary": {
            "total_agents": len(agents) - len([a for a in agents if db.get_agent_owner(a.name) and db.get_agent_owner(a.name).get("is_system")]),
            "using_subscription": len(subscription_agents),
            "using_api_key": len(api_key_agents),
            "not_configured": len(not_configured_agents),
            "subscription_count": len(subscriptions),
        },
        "by_auth_mode": {
            "subscription": subscription_agents,
            "api_key": api_key_agents,
            "not_configured": not_configured_agents,
        },
        "subscriptions": subscription_summary,
    }
