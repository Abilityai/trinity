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
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Request, Query
import httpx

from models import User
from database import db
from dependencies import get_current_user
from services.audit_service import log_audit_event
from services.docker_service import get_agent_container, docker_client, list_all_agents
from db.agents import SYSTEM_AGENT_NAME

router = APIRouter(prefix="/api/ops", tags=["operations"])
logger = logging.getLogger(__name__)


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


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

    Returns a comprehensive list of all agents with their:
    - Container status (running/stopped)
    - Context usage (if running)
    - Last activity time
    - System agent flag
    """
    agents = list_all_agents()

    fleet_status = []
    context_stats = {}

    # Try to get context stats for running agents
    try:
        # Get context stats from backend endpoint
        running_agents = [a for a in agents if a.status == "running"]
        for agent in running_agents:
            agent_name = agent.name
            try:
                agent_url = f"http://agent-{agent_name}:8000/api/chat/session"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(agent_url)
                    if response.status_code == 200:
                        session = response.json()
                        context_stats[agent_name] = {
                            "context_tokens": session.get("context_tokens", 0),
                            "context_window": session.get("context_window", 200000),
                            "context_percent": round(
                                session.get("context_tokens", 0) /
                                max(session.get("context_window", 200000), 1) * 100, 1
                            )
                        }
            except Exception:
                pass  # Agent not responding, skip context
    except Exception as e:
        logger.warning(f"Failed to get context stats: {e}")

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

    await log_audit_event(
        event_type="ops",
        action="fleet_status",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"total_agents": total, "running": running}
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
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

    Identifies unhealthy agents based on:
    - Context usage > 90%
    - Container errors
    - No activity for > 30 minutes (for running agents)
    """
    agents = list_all_agents()

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
            # Check context usage
            try:
                agent_url = f"http://agent-{agent_name}:8000/api/chat/session"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(agent_url)
                    if response.status_code == 200:
                        session = response.json()
                        context_tokens = session.get("context_tokens", 0)
                        context_window = session.get("context_window", 200000)
                        context_percent = round(context_tokens / max(context_window, 1) * 100, 1)

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

    await log_audit_event(
        event_type="ops",
        action="fleet_health",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={
            "overall": overall,
            "critical_count": len(critical_issues),
            "warning_count": len(warnings)
        }
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
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

    Admin-only. Excludes the system agent.
    """
    require_admin(current_user)

    agents = list_all_agents()

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

        # Restart the agent
        try:
            container = get_agent_container(agent_name)
            if container:
                container.stop(timeout=30)
                container.start()
                results.append({
                    "agent": agent_name,
                    "result": "success",
                    "previous_status": status
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

    await log_audit_event(
        event_type="ops",
        action="fleet_restart",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success" if failures == 0 else "partial",
        details={
            "successes": successes,
            "failures": failures,
            "skipped": skipped
        }
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "summary": {
            "total": len(agents),
            "successes": successes,
            "failures": failures,
            "skipped": skipped
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
    require_admin(current_user)

    agents = list_all_agents()

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
                container.stop(timeout=30)
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

    await log_audit_event(
        event_type="ops",
        action="fleet_stop",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success" if failures == 0 else "partial",
        details={
            "successes": successes,
            "failures": failures,
            "skipped": skipped
        }
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
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
    require_admin(current_user)

    # Get all enabled schedules
    schedules = db.list_all_enabled_schedules()

    # Filter by agent if specified
    if agent_name:
        schedules = [s for s in schedules if s.agent_name == agent_name]

    paused = 0
    for schedule in schedules:
        try:
            db.set_schedule_enabled(schedule.id, False)

            # Remove from scheduler
            from services.scheduler_service import scheduler_service
            scheduler_service.disable_schedule(schedule.id)

            paused += 1
        except Exception as e:
            logger.error(f"Failed to pause schedule {schedule.id}: {e}")

    await log_audit_event(
        event_type="ops",
        action="schedules_pause",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"paused": paused, "agent_name": agent_name}
    )

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
    require_admin(current_user)

    # Get all disabled schedules
    schedules = db.list_all_disabled_schedules() if hasattr(db, 'list_all_disabled_schedules') else []

    # If no specific method, get all schedules
    if not schedules:
        all_schedules = []
        agents = list_all_agents()
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

            # Add to scheduler
            from services.scheduler_service import scheduler_service
            schedule.enabled = True
            scheduler_service.add_schedule(schedule)

            resumed += 1
        except Exception as e:
            logger.error(f"Failed to resume schedule {schedule.id}: {e}")

    await log_audit_event(
        event_type="ops",
        action="schedules_resume",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success",
        details={"resumed": resumed, "agent_name": agent_name}
    )

    return {
        "success": True,
        "message": f"Resumed {resumed} schedule(s)",
        "resumed_count": resumed,
        "agent_filter": agent_name
    }


@router.post("/emergency-stop")
async def emergency_stop(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Emergency stop: Pause all schedules and stop all non-system agents.

    Admin-only. Use for runaway costs or critical issues.
    """
    require_admin(current_user)

    results = {
        "schedules_paused": 0,
        "agents_stopped": 0,
        "errors": []
    }

    # 1. Pause all schedules
    schedules = db.list_all_enabled_schedules()
    for schedule in schedules:
        try:
            db.set_schedule_enabled(schedule.id, False)
            from services.scheduler_service import scheduler_service
            scheduler_service.disable_schedule(schedule.id)
            results["schedules_paused"] += 1
        except Exception as e:
            results["errors"].append(f"Schedule {schedule.id}: {e}")

    # 2. Stop all non-system agents
    agents = list_all_agents()
    for agent in agents:
        agent_name = agent.name
        status = agent.status

        # Skip system agent
        owner = db.get_agent_owner(agent_name)
        is_system = owner.get("is_system", False) if owner else False
        if is_system:
            continue

        if status == "running":
            try:
                container = get_agent_container(agent_name)
                if container:
                    container.stop(timeout=10)  # Shorter timeout for emergency
                    results["agents_stopped"] += 1
            except Exception as e:
                results["errors"].append(f"Agent {agent_name}: {e}")

    await log_audit_event(
        event_type="ops",
        action="emergency_stop",
        user_id=current_user.username,
        ip_address=request.client.host if request.client else None,
        result="success",
        severity="warning",
        details=results
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

    Alerts are derived from audit logs with ops-related events.
    """
    # Get ops-related audit events
    # This would ideally be a dedicated alerts table, but for now use audit logs
    # TODO: Implement dedicated alerts table in future

    return {
        "timestamp": datetime.utcnow().isoformat(),
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
    """
    # TODO: Implement when alerts table is added
    return {
        "success": True,
        "message": "Alert acknowledgment feature coming soon"
    }


# ============================================================================
# Cost & Observability (powered by OTel)
# ============================================================================

# Import OTel configuration from observability module
OTEL_ENABLED = os.getenv("OTEL_ENABLED", "0") == "1"
OTEL_PROMETHEUS_ENDPOINT = os.getenv("OTEL_PROMETHEUS_ENDPOINT", "http://trinity-otel-collector:8889/metrics")


@router.get("/costs")
async def get_ops_costs(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get cost and usage metrics for platform operations.

    This endpoint provides an ops-focused view of OTel metrics:
    - Total platform cost with threshold analysis
    - Token usage by model
    - Productivity metrics (commits, PRs, lines)
    - Cost alerts if thresholds exceeded

    Returns {enabled: false} when OTel is not configured.
    """
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
                    "timestamp": datetime.utcnow().isoformat()
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
                "timestamp": datetime.utcnow().isoformat(),

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

            await log_audit_event(
                event_type="ops",
                action="costs_report",
                user_id=current_user.username,
                ip_address=request.client.host if request.client else None,
                result="success",
                details={"total_cost": total_cost, "alerts_count": len(alerts)}
            )

            return result

    except httpx.ConnectError:
        return {
            "enabled": True,
            "available": False,
            "error": "Cannot connect to OTel Collector. Is it running?",
            "timestamp": datetime.utcnow().isoformat()
        }
    except httpx.TimeoutException:
        return {
            "enabled": True,
            "available": False,
            "error": "OTel Collector request timed out",
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to fetch cost metrics: {e}")
        return {
            "enabled": True,
            "available": False,
            "error": f"Failed to fetch metrics: {str(e)}",
            "timestamp": datetime.utcnow().isoformat()
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
