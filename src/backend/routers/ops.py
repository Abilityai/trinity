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
import uuid
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
from redis_breaker_util import get_breaker_redis
from utils.helpers import utc_now_iso
from services.platform_audit_service import platform_audit_service, AuditEventType

router = APIRouter(prefix="/api/ops", tags=["operations"])
logger = logging.getLogger(__name__)

# #1860: one fleet restart at a time. A client timeout (nginx 60s / Cloudflare
# ~100s) invites a retry while the first loop is still running server-side, and
# two loops interleaving stop/start on the same agent is the #799/#1817
# stop-racing-start wedge class. TTL bounds a crashed holder; the live loop
# refreshes its own lease each iteration so a long fleet never loses the lock.
_FLEET_RESTART_LOCK_KEY = "ops:fleet_restart"
_FLEET_RESTART_LOCK_TTL_SECONDS = 900


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
    assert_admin(current_user)

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
            "type": agent.type,
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
    """
    Get health check for all agents.

    Admin-only. Identifies unhealthy agents based on context usage,
    container errors, and idle time.
    """
    assert_admin(current_user)

    agents = list_all_agents_fast()

    # Health thresholds (from settings or defaults)
    context_warning = 75
    context_critical = 90
    idle_warning_minutes = 30
    idle_critical_minutes = 60

    critical_issues = []
    warnings = []
    healthy_agents = []

    for agent in agents:
        agent_name = agent.name
        status = agent.status

        # Skip system agent from health checks
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False
        if is_system:
            continue

        issues = []

        # Check container status
        if status not in ["running", "stopped"]:
            critical_issues.append({
                "agent": agent_name,
                "issue": f"Container in unexpected state: {status}",
                "recommendation": "Check container logs"
            })
            continue

        if status == "running":
            # Check context usage using AgentClient
            try:
                client = get_agent_client(agent_name)
                session_info = await client.get_session()
                if session_info:
                    context_percent = session_info.context_percent

                    if context_percent > context_critical:
                        critical_issues.append({
                            "agent": agent_name,
                            "issue": f"Critical context usage: {context_percent}%",
                            "recommendation": "Reset session to clear context"
                        })
                    elif context_percent > context_warning:
                        warnings.append({
                            "agent": agent_name,
                            "issue": f"High context usage: {context_percent}%",
                            "recommendation": "Consider resetting session soon"
                        })
                    else:
                        healthy_agents.append(agent_name)
                else:
                    warnings.append({
                        "agent": agent_name,
                        "issue": "Unable to get context info",
                        "recommendation": "Check agent server"
                    })
            except Exception as e:
                warnings.append({
                    "agent": agent_name,
                    "issue": f"Agent not responding: {str(e)[:50]}",
                    "recommendation": "Check if agent is stuck"
                })
        else:
            # Agent is stopped - not necessarily an issue
            pass

    # Determine overall health
    if critical_issues:
        overall = "critical"
    elif warnings:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "timestamp": utc_now_iso(),
        "overall": overall,
        "critical_issues": critical_issues,
        "warnings": warnings,
        "healthy_count": len(healthy_agents),
        "healthy_agents": healthy_agents
    }


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
    """
    Restart all agents in the fleet.

    Admin-only (human principals only). Excludes the system agent and
    ephemeral ghosts. Each agent is stopped, then started through the
    canonical lifecycle path (``restart_agent_internal``), so a fleet restart
    applies pending config drift and adopts a rebuilt base image
    (#1809/#1860) exactly like a manual stop → start.

    The loop is sequential; a large fleet can outlast proxy timeouts. A
    client timeout (504/524) does NOT mean the restart failed — the loop
    keeps running server-side and the outcome lands in the audit log
    (``fleet_restart`` entry). Use ``system_prefix`` / ``filter_status`` to
    restart in chunks. 409 while another fleet restart is in flight.
    """
    assert_admin(current_user)
    # #1860: this endpoint replaces containers, not just restarts them —
    # operator-scale destructiveness re-prices principals (Invariant #8, the
    # #1816 escalation rule). No-op for human/system principals.
    reject_agent_principal(current_user)

    # Single-flight lock (SETNX + TTL, fail-open when Redis is down).
    lock_client = get_breaker_redis()
    lock_token = uuid.uuid4().hex
    lock_held = False
    if lock_client is not None:
        try:
            lock_held = bool(lock_client.set(
                _FLEET_RESTART_LOCK_KEY, lock_token,
                nx=True, ex=_FLEET_RESTART_LOCK_TTL_SECONDS,
            ))
            if not lock_held:
                raise HTTPException(status_code=409, detail="fleet_restart_in_progress")
        except HTTPException:
            raise
        except Exception:
            lock_client = None  # fail-open: proceed unlocked

    agents = list_all_agents_fast()

    results = []
    successes = 0
    failures = 0
    skipped = 0
    recreated_count = 0
    recreated_map = {}
    failed_agents = []

    try:
        for agent in agents:
            agent_name = agent.name
            status = agent.status

            # Refresh our own lease so a long fleet never loses the lock
            # mid-run while a crashed holder still expires within the TTL.
            if lock_held and lock_client is not None:
                try:
                    lock_client.expire(_FLEET_RESTART_LOCK_KEY, _FLEET_RESTART_LOCK_TTL_SECONDS)
                except Exception:
                    pass

            # Skip system agent
            owner = db.get_agent_owner(agent_name)
            is_system = owner.get("is_system", False) if owner else False
            if is_system:
                results.append({
                    "agent": agent_name,
                    "result": "skipped",
                    "reason": "system agent"
                })
                skipped += 1
                continue

            # Skip ephemeral ghosts (#1860): the config-drift predicates are
            # not ephemeral-gated, so a cold start could recreate a drifted
            # ghost — destroying its volume-less workspace mid-budget
            # (trinity-enterprise#69 "ghosts never recreate"). Restarting a
            # disposable ghost has no adoption upside; GC owns its lifecycle.
            try:
                eph = db.get_agent_ephemeral_info(agent_name)
            except Exception:
                eph = None
            if isinstance(eph, dict) and eph.get("is_ephemeral"):
                results.append({
                    "agent": agent_name,
                    "result": "skipped",
                    "reason": "ephemeral"
                })
                skipped += 1
                continue

            # Apply filters
            if filter_status and status != filter_status:
                results.append({
                    "agent": agent_name,
                    "result": "skipped",
                    "reason": f"status is {status}, not {filter_status}"
                })
                skipped += 1
                continue

            if system_prefix and not agent_name.startswith(system_prefix + "-"):
                results.append({
                    "agent": agent_name,
                    "result": "skipped",
                    "reason": f"doesn't match prefix {system_prefix}"
                })
                skipped += 1
                continue

            # Skip stopped agents
            if status != "running":
                results.append({
                    "agent": agent_name,
                    "result": "skipped",
                    "reason": "not running"
                })
                skipped += 1
                continue

            # Restart the agent through the canonical lifecycle path.
            try:
                container = get_agent_container(agent_name)
                if container:
                    start_result = await restart_agent_internal(agent_name)
                    # Explicit field copy — this router builds its own result
                    # dicts, so service fields do NOT flow through on their
                    # own (#1809's allowlist learning, routers/agents.py).
                    entry = {
                        "agent": agent_name,
                        "result": "success",
                        "previous_status": status,
                        "recreated": bool(start_result.get("recreated")),
                        "recreate_reason": start_result.get("recreate_reason"),
                        "credentials_injection": start_result.get("credentials_injection"),
                        "skills_injection": start_result.get("skills_injection"),
                    }
                    if entry["recreated"]:
                        recreated_count += 1
                        recreated_map[agent_name] = entry["recreate_reason"] or "unknown"
                    results.append(entry)
                    successes += 1
                    logger.info(
                        f"Fleet restart: {agent_name} restarted "
                        f"(recreated={entry['recreated']}, reason={entry['recreate_reason']})"
                    )
                else:
                    results.append({
                        "agent": agent_name,
                        "result": "failed",
                        "error": "container not found"
                    })
                    failed_agents.append(agent_name)
                    failures += 1
            except Exception as e:
                if isinstance(e, HTTPException):
                    error_text = f"{e.status_code}: {e.detail}"
                else:
                    error_text = str(e) or e.__class__.__name__
                # A recreate that failed after removing the old container
                # leaves the agent with NO container — it vanishes from fleet
                # listings (Docker-as-truth). Name the recovery path (#1559).
                try:
                    if get_agent_container(agent_name) is None:
                        error_text += (
                            f" (no container present — recover via "
                            f"POST /api/agents/{agent_name}/start)"
                        )
                except Exception:
                    pass
                results.append({
                    "agent": agent_name,
                    "result": "failed",
                    "error": error_text
                })
                failed_agents.append(agent_name)
                failures += 1
                logger.warning(f"Fleet restart: {agent_name} failed: {error_text}")
    finally:
        # Sync cleanup FIRST, awaited audit LAST: a CancelledError raised at
        # the audit await (backend shutdown mid-loop) is a BaseException the
        # try below doesn't catch — anything after it in this block would be
        # skipped, leaving the lock held for a full TTL after a deploy.
        invalidate_context_stats_cache()
        if lock_held and lock_client is not None:
            try:
                # Release only our own lock (compare-and-delete).
                current = lock_client.get(_FLEET_RESTART_LOCK_KEY)
                if current == lock_token.encode() or current == lock_token:
                    lock_client.delete(_FLEET_RESTART_LOCK_KEY)
            except Exception:
                pass
        # The audit row is the durable record — a client that timed out finds
        # the outcome (incl. partial completion) here. Never fails the op.
        # Restores the fleet_restart entry dropped in 0ec3a7fc; SYSTEM event
        # type matches this router's emergency_stop precedent.
        try:
            await platform_audit_service.log(
                event_type=AuditEventType.SYSTEM,
                event_action="fleet_restart",
                source="api",
                actor_user=current_user,
                actor_ip=request.client.host if request.client else None,
                endpoint=str(request.url.path),
                request_id=getattr(request.state, "request_id", None),
                details={
                    "total": len(agents),
                    "successes": successes,
                    "failures": failures,
                    "skipped": skipped,
                    # {agent: reason} for recreated agents only — preserves
                    # #1809's changed-container-id traceability at fleet scale.
                    "recreated": recreated_map or None,
                    "failed_agents": failed_agents or None,
                    "filter_status": filter_status,
                    "system_prefix": system_prefix,
                },
            )
        except Exception:
            logger.warning("Fleet restart: audit write failed", exc_info=True)

    return {
        "timestamp": utc_now_iso(),
        "summary": {
            "total": len(agents),
            "successes": successes,
            "failures": failures,
            "skipped": skipped,
            "recreated": recreated_count
        },
        "results": results
    }


@router.post("/fleet/stop")
async def stop_fleet(
    request: Request,
    current_user: User = Depends(get_current_user),
    system_prefix: Optional[str] = Query(None, description="Only stop agents matching this system prefix")
):
    """
    Stop all agents in the fleet.

    Admin-only. Excludes the system agent.
    """
    assert_admin(current_user)

    agents = list_all_agents_fast()

    results = []
    successes = 0
    failures = 0
    skipped = 0

    for agent in agents:
        agent_name = agent.name
        status = agent.status

        # Skip system agent
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False
        if is_system:
            results.append({
                "agent": agent_name,
                "result": "skipped",
                "reason": "system agent"
            })
            skipped += 1
            continue

        # Apply prefix filter
        if system_prefix and not agent_name.startswith(system_prefix + "-"):
            results.append({
                "agent": agent_name,
                "result": "skipped",
                "reason": f"doesn't match prefix {system_prefix}"
            })
            skipped += 1
            continue

        # Skip already stopped agents
        if status != "running":
            results.append({
                "agent": agent_name,
                "result": "skipped",
                "reason": "already stopped"
            })
            skipped += 1
            continue

        # Stop the agent
        try:
            container = get_agent_container(agent_name)
            if container:
                await container_stop(container, timeout=30)
                results.append({
                    "agent": agent_name,
                    "result": "success"
                })
                successes += 1
            else:
                results.append({
                    "agent": agent_name,
                    "result": "failed",
                    "error": "container not found"
                })
                failures += 1
        except Exception as e:
            results.append({
                "agent": agent_name,
                "result": "failed",
                "error": str(e)
            })
            failures += 1

    return {
        "timestamp": utc_now_iso(),
        "summary": {
            "total": len(agents),
            "successes": successes,
            "failures": failures,
            "skipped": skipped
        },
        "results": results
    }


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
    assert_admin(current_user)

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


def _stop_agent_container(agent_name: str, timeout: int = 10) -> dict:
    """
    Stop a single agent container. Used for parallel stopping.

    Returns a dict with result info.
    """
    try:
        container = get_agent_container(agent_name)
        if container:
            container.stop(timeout=timeout)
            return {"agent": agent_name, "result": "stopped"}
        return {"agent": agent_name, "result": "not_found"}
    except Exception as e:
        return {"agent": agent_name, "result": "error", "error": str(e)}


@router.post("/emergency-stop")
async def emergency_stop(
    request: Request,
    current_user: User = Depends(get_current_user),
    system_prefix: Optional[str] = Query(
        None,
        description="Optional: Only stop agents whose names start with this prefix"
    )
):
    """
    Emergency stop: Pause all schedules and stop all non-system agents.

    Admin-only. Use for runaway costs or critical issues.

    Agents are stopped in parallel using a thread pool for faster response time.

    Args:
        system_prefix: Optional filter to only stop agents with names starting with this prefix
    """
    assert_admin(current_user)

    results = {
        "schedules_paused": 0,
        "agents_stopped": 0,
        "errors": []
    }

    # 1. Pause all schedules (optionally filtered by prefix)
    schedules = db.list_all_enabled_schedules()
    for schedule in schedules:
        # If prefix filter is set, only pause schedules for matching agents
        if system_prefix and not schedule.agent_name.startswith(system_prefix):
            continue
        try:
            db.set_schedule_enabled(schedule.id, False)
            # Dedicated scheduler syncs from database automatically
            results["schedules_paused"] += 1
        except Exception as e:
            results["errors"].append(f"Schedule {schedule.id}: {e}")

    # 2. Stop all non-system agents IN PARALLEL
    agents = list_all_agents_fast()

    # Filter to running non-system agents
    agents_to_stop = []
    for agent in agents:
        agent_name = agent.name
        status = agent.status

        # If prefix filter is set, only process matching agents
        if system_prefix and not agent_name.startswith(system_prefix):
            continue

        # Skip system agent
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False
        if is_system:
            continue

        if status == "running":
            agents_to_stop.append(agent_name)

    # Stop agents in parallel using thread pool (Docker SDK is synchronous)
    if agents_to_stop:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(_stop_agent_container, agent_name, 10): agent_name
                for agent_name in agents_to_stop
            }

            # Wait for all with a timeout (60 seconds should be plenty for parallel stops)
            for future in concurrent.futures.as_completed(futures, timeout=60):
                agent_name = futures[future]
                try:
                    result = future.result()
                    if result["result"] == "stopped":
                        results["agents_stopped"] += 1
                    elif result["result"] == "error":
                        results["errors"].append(f"Agent {agent_name}: {result.get('error', 'unknown error')}")
                except Exception as e:
                    results["errors"].append(f"Agent {agent_name}: {e}")

    await platform_audit_service.log(
        event_type=AuditEventType.SYSTEM,
        event_action="emergency_stop",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={
            "schedules_paused": results["schedules_paused"],
            "agents_stopped": results["agents_stopped"],
            "system_prefix": system_prefix,
            "errors": results["errors"] or None,
        },
    )

    return {
        "success": True,
        "message": "Emergency stop completed",
        "schedules_paused": results["schedules_paused"],
        "agents_stopped": results["agents_stopped"],
        "errors": results["errors"] if results["errors"] else None
    }


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
    assert_admin(current_user)

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
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "1") == "1"
OTEL_PROMETHEUS_ENDPOINT = os.getenv("OTEL_PROMETHEUS_ENDPOINT", "http://trinity-otel-collector:8889/metrics")


@router.get("/costs")
async def get_ops_costs(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get cost and usage metrics for platform operations.

    Admin-only. Returns OTel metrics including cost breakdown,
    token usage, and productivity metrics.
    """
    assert_admin(current_user)

    if not OTEL_ENABLED:
        return {
            "enabled": False,
            "message": "OpenTelemetry is not enabled. Set OTEL_ENABLED=1 in your environment to enable cost tracking.",
            "setup_instructions": [
                "1. Set OTEL_ENABLED=1 in .env file",
                "2. Deploy the OTel collector (docker-compose up otel-collector)",
                "3. Restart agents to begin collecting metrics",
                "4. Wait 60 seconds for initial metrics to appear"
            ]
        }

    # Get ops settings for thresholds
    daily_cost_limit = float(db.get_setting("ops_cost_limit_daily_usd") or 50.0)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                OTEL_PROMETHEUS_ENDPOINT,
                timeout=5.0
            )

            if response.status_code != 200:
                return {
                    "enabled": True,
                    "available": False,
                    "error": f"OTel Collector returned status {response.status_code}",
                    "timestamp": utc_now_iso()
                }

            # Parse Prometheus metrics
            from routers.observability import parse_prometheus_metrics, calculate_totals
            metrics = parse_prometheus_metrics(response.text)
            totals = calculate_totals(metrics)

            # Calculate alerts based on thresholds
            alerts = []
            total_cost = totals.get("total_cost", 0)

            if daily_cost_limit > 0 and total_cost >= daily_cost_limit:
                alerts.append({
                    "severity": "critical",
                    "type": "cost_limit_exceeded",
                    "message": f"Daily cost limit exceeded: ${total_cost:.4f} >= ${daily_cost_limit:.2f}",
                    "recommendation": "Consider pausing schedules or stopping non-essential agents"
                })
            elif daily_cost_limit > 0 and total_cost >= daily_cost_limit * 0.8:
                alerts.append({
                    "severity": "warning",
                    "type": "cost_limit_approaching",
                    "message": f"Approaching daily cost limit: ${total_cost:.4f} (limit: ${daily_cost_limit:.2f})",
                    "recommendation": "Monitor closely and prepare to reduce activity if needed"
                })

            # Format cost breakdown by model
            cost_by_model = []
            for model, cost in sorted(metrics.get("cost", {}).items(), key=lambda x: x[1], reverse=True):
                # Get token counts for this model
                model_tokens = metrics.get("tokens", {}).get(model, {})
                cost_by_model.append({
                    "model": _format_model_name(model),
                    "model_id": model,
                    "cost": round(cost, 4),
                    "input_tokens": int(model_tokens.get("input", 0)),
                    "output_tokens": int(model_tokens.get("output", 0)),
                    "cache_read_tokens": int(model_tokens.get("cacheRead", 0)),
                    "cache_creation_tokens": int(model_tokens.get("cacheCreation", 0))
                })

            # Build response
            result = {
                "enabled": True,
                "available": True,
                "timestamp": utc_now_iso(),

                # Summary
                "summary": {
                    "total_cost": round(total_cost, 4),
                    "total_tokens": totals.get("total_tokens", 0),
                    "daily_limit": daily_cost_limit if daily_cost_limit > 0 else None,
                    "cost_percent_of_limit": round(total_cost / daily_cost_limit * 100, 1) if daily_cost_limit > 0 else None
                },

                # Alerts
                "alerts": alerts,

                # Detailed breakdown
                "cost_by_model": cost_by_model,

                # Token breakdown by type
                "tokens_by_type": totals.get("tokens_by_type", {}),

                # Productivity metrics
                "productivity": {
                    "sessions": totals.get("sessions", 0),
                    "active_time_seconds": totals.get("active_time_seconds", 0),
                    "active_time_formatted": _format_duration(totals.get("active_time_seconds", 0)),
                    "commits": totals.get("commits", 0),
                    "pull_requests": totals.get("pull_requests", 0),
                    "lines_added": metrics.get("lines", {}).get("added", 0),
                    "lines_removed": metrics.get("lines", {}).get("removed", 0)
                }
            }

            return result

    except httpx.ConnectError:
        return {
            "enabled": True,
            "available": False,
            "error": "Cannot connect to OTel Collector. Is it running?",
            "timestamp": utc_now_iso()
        }
    except httpx.TimeoutException:
        return {
            "enabled": True,
            "available": False,
            "error": "OTel Collector request timed out",
            "timestamp": utc_now_iso()
        }
    except Exception as e:
        logger.error(f"Failed to fetch cost metrics: {e}")
        return {
            "enabled": True,
            "available": False,
            "error": f"Failed to fetch metrics: {str(e)}",
            "timestamp": utc_now_iso()
        }


def _format_model_name(model_id: str) -> str:
    """Format a model ID into a human-readable name."""
    if not model_id:
        return "Unknown"

    # Remove date suffixes like -20250514
    import re
    clean = re.sub(r'-\d{8}$', '', model_id)

    # Map common model IDs
    mappings = {
        "claude-sonnet-4": "Claude Sonnet 4",
        "claude-opus-4": "Claude Opus 4",
        "claude-haiku-4": "Claude Haiku 4",
        "claude-3-5-sonnet": "Claude 3.5 Sonnet",
        "claude-3-sonnet": "Claude 3 Sonnet",
        "claude-3-haiku": "Claude 3 Haiku",
        "claude-3-opus": "Claude 3 Opus",
    }

    for prefix, name in mappings.items():
        if clean.startswith(prefix):
            return name

    # Fallback: Title case with hyphens as spaces
    return clean.replace("-", " ").title()


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes}m"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        if minutes > 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h"


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
    assert_admin(current_user)

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
            "type": agent.type,
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
