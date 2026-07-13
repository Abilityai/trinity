"""
Monitoring API Router (MON-001).

Provides endpoints for agent health monitoring:
- Fleet-wide health status
- Individual agent health details
- Health history and trends
- Configuration management
- Manual health check triggers
"""

import json
import logging
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks

from database import db
from dependencies import get_current_user, require_admin, AuthorizedAgentByName
from db_models import (
    User,
    MonitoringConfig,
    FleetHealthStatus,
    FleetHealthSummary,
    AgentHealthDetail,
    AgentHealthSummary,
)
from services.monitoring_service import (
    perform_health_check,
    perform_fleet_health_check,
    get_monitoring_service,
    start_monitoring_service,
    stop_monitoring_service,
    DEFAULT_CONFIG,
)
from services.agent_service import get_accessible_agents


router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])

logger = logging.getLogger(__name__)

# Sort key: lower number = higher severity, surfaces first.
_STATUS_SORT_ORDER = {"critical": 0, "unhealthy": 1, "degraded": 2, "unknown": 3, "healthy": 4}

# WebSocket manager for broadcasting health events
_websocket_manager = None
_filtered_websocket_manager = None

_MONITORING_CONFIG_KEY = "monitoring_config"


def load_persisted_monitoring_config() -> MonitoringConfig:
    """Load the persisted monitoring config, or DEFAULT_CONFIG (default-OFF).

    #1121: single reader used by GET /config, enable/disable, and the
    lifespan resume path so they can't drift. A corrupt persisted blob
    (or one with an out-of-range interval that fails validation) falls
    back to the default rather than raising.
    """
    setting = db.get_setting(_MONITORING_CONFIG_KEY)
    if setting and setting.value:
        try:
            return MonitoringConfig(**json.loads(setting.value))
        except Exception:
            logger.warning("Invalid persisted monitoring_config; using default", exc_info=True)
    return DEFAULT_CONFIG


def _persist_monitoring_enabled(enabled: bool) -> MonitoringConfig:
    """Persist the `enabled` flag onto the stored config so the choice
    survives backend restarts (#1121). Preserves all other fields."""
    config = load_persisted_monitoring_config().model_copy(update={"enabled": enabled})
    db.set_setting(_MONITORING_CONFIG_KEY, json.dumps(config.model_dump()))
    return config


# ============================================================================
# Builders (extracted for unit-test coverage — see #669)
# ============================================================================

def _coerce_status(raw: Any) -> str:
    """Map any persisted status value (incl. NULL / non-str) to a known label.

    The DB column is nullable, and partial health-check rows can land with
    `status = NULL`. Without coercion, building `AgentHealthSummary(status=...)`
    fails Pydantic validation (status is a required str) and the whole
    fleet-status endpoint returns 500. See #669.
    """
    if isinstance(raw, str) and raw:
        return raw
    return "unknown"


def _build_agent_summary(name: str, check: Optional[Dict[str, Any]]) -> AgentHealthSummary:
    """Build an `AgentHealthSummary` from a (possibly absent / partial) row.

    Tolerates `check is None` (no row at all), missing keys, NULL `status`,
    and NULL `error_message`. All defects in stored data degrade to
    `status="unknown"` instead of bubbling as a 500.
    """
    if not check:
        return AgentHealthSummary(name=name, status="unknown", issues=["No health check data"])

    error_message = check.get("error_message") or ""
    issues = error_message.split("; ") if error_message else []

    return AgentHealthSummary(
        name=name,
        status=_coerce_status(check.get("status")),
        docker_status=check.get("container_status"),
        network_reachable=check.get("reachable"),
        runtime_available=check.get("runtime_available"),
        last_check_at=check.get("checked_at"),
        issues=issues,
    )


def _status_sort_key(summary: AgentHealthSummary) -> int:
    """Sort key for fleet status — most severe first, unknowns in the middle."""
    return _STATUS_SORT_ORDER.get(summary.status, 3)


def set_websocket_manager(manager):
    """Set the WebSocket manager for broadcasting health events."""
    global _websocket_manager
    _websocket_manager = manager


def set_filtered_websocket_manager(manager):
    """Set the filtered WebSocket manager for Trinity Connect."""
    global _filtered_websocket_manager
    _filtered_websocket_manager = manager


async def _broadcast_health_change(
    agent_name: str,
    previous_status: str,
    current_status: str,
    issues: List[str]
):
    """Broadcast a health status change event via WebSocket."""
    from utils.helpers import utc_now_iso

    event = {
        "type": "agent_health_changed",
        "agent_name": agent_name,
        "previous_status": previous_status,
        "current_status": current_status,
        "issues": issues,
        "timestamp": utc_now_iso()
    }
    event_json = json.dumps(event)

    if _websocket_manager:
        await _websocket_manager.broadcast(event_json)

    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(event)


# ============================================================================
# Fleet Status Endpoints
# ============================================================================

@router.get("/status", response_model=FleetHealthStatus)
async def get_fleet_status(
    current_user: User = Depends(get_current_user)
):
    """
    Get fleet-wide health summary.

    Returns health status for all agents the user can access.
    Admins see all agents; regular users see owned and shared agents.
    """
    # Get accessible agents
    from services.docker_service import list_all_agents_fast

    all_agents = list_all_agents_fast()
    all_agent_names = [a.name for a in all_agents]

    # Filter to accessible agents (unless admin)
    if current_user.role != "admin":
        accessible = get_accessible_agents(current_user)
        accessible_names = {a["name"] for a in accessible}
        agent_names = [n for n in all_agent_names if n in accessible_names]
    else:
        agent_names = all_agent_names

    if not agent_names:
        return FleetHealthStatus(
            enabled=get_monitoring_service().is_running,
            last_check_at=None,
            summary=FleetHealthSummary(total_agents=0),
            agents=[]
        )

    # #669: aggregator-side defects (NULL status, schema drift, partial rows)
    # must not propagate as 500. Degrade to an "unknown" payload so MCP clients
    # and the UI can render *something*.
    try:
        latest_checks = db.get_all_latest_health_checks(agent_names, "aggregate")
        summary = db.get_health_summary(agent_names)
        agents = [_build_agent_summary(name, latest_checks.get(name)) for name in agent_names]

        # RELIABILITY-004 / #307: merge the heartbeat liveness layer in a single
        # batched Redis round-trip (D4). This is the PASSIVE (pull) annotation —
        # it does not change `status`; it just annotates each summary so the
        # UI/MCP can show fast-path liveness. Heartbeat loss is surfaced ACTIVELY
        # (push) by the watch loop via the monitoring_alerts notification path,
        # not through this aggregation. Inside the try/except so a Redis blip
        # degrades, never 500s.
        from services.heartbeat_service import heartbeat_status_bulk
        hb_map = heartbeat_status_bulk(agent_names)
        for agent in agents:
            hb = hb_map.get(agent.name)
            if hb:
                agent.heartbeat_alive = hb["heartbeat_alive"]
                agent.last_heartbeat_age_s = hb["last_heartbeat_age_s"]
                agent.heartbeat_active_executions = hb["heartbeat_active_executions"]
                agent.heartbeat_memory_mb = hb["heartbeat_memory_mb"]
                agent.heartbeat_state = hb["heartbeat_state"]

        agents.sort(key=_status_sort_key)
    except Exception:
        logger.exception("Fleet health aggregation failed for %d agents", len(agent_names))
        return FleetHealthStatus(
            enabled=get_monitoring_service().is_running,
            last_check_at=None,
            summary=FleetHealthSummary(total_agents=len(agent_names), unknown=len(agent_names)),
            agents=[
                AgentHealthSummary(name=n, status="unknown", issues=["Health aggregation failed"])
                for n in agent_names
            ],
        )

    # Include circuit breaker states for admin users
    cb_data = None
    if current_user.role == "admin":
        from services.agent_client import get_all_circuit_states
        all_states = get_all_circuit_states()
        cb_data = {
            name: state for name, state in all_states.items()
            if name in agent_names
        } or None

    return FleetHealthStatus(
        enabled=get_monitoring_service().is_running,
        last_check_at=agents[0].last_check_at if agents else None,
        summary=FleetHealthSummary(
            total_agents=len(agent_names),
            healthy=summary.get("healthy", 0),
            degraded=summary.get("degraded", 0),
            unhealthy=summary.get("unhealthy", 0),
            critical=summary.get("critical", 0),
            unknown=summary.get("unknown", 0)
        ),
        agents=agents,
        circuit_breakers=cb_data,
    )


# ============================================================================
# Agent Health Endpoints
# ============================================================================

@router.get("/agents/{agent_name}", response_model=AgentHealthDetail)
async def get_agent_health(
    agent_name: AuthorizedAgentByName
):
    """
    Get detailed health information for a specific agent.

    Returns all layer health checks and historical metrics.
    """

    # Get latest checks for each layer
    docker_check = db.get_latest_health_check(agent_name, "docker")
    network_check = db.get_latest_health_check(agent_name, "network")
    business_check = db.get_latest_health_check(agent_name, "business")
    aggregate_check = db.get_latest_health_check(agent_name, "aggregate")

    if not aggregate_check:
        # No health data - trigger a check
        return await perform_health_check(agent_name, DEFAULT_CONFIG, store_results=True)

    # Build detailed response
    from db_models import DockerHealthCheck, NetworkHealthCheck, BusinessHealthCheck
    from utils.helpers import utc_now_iso

    docker = None
    if docker_check:
        docker = DockerHealthCheck(
            agent_name=agent_name,
            container_status=docker_check.get("container_status"),
            restart_count=docker_check.get("restart_count", 0),
            oom_killed=docker_check.get("oom_killed", False),
            cpu_percent=docker_check.get("cpu_percent"),
            memory_percent=docker_check.get("memory_percent"),
            memory_mb=docker_check.get("memory_mb"),
            checked_at=docker_check.get("checked_at", utc_now_iso())
        )

    network = None
    if network_check:
        network = NetworkHealthCheck(
            agent_name=agent_name,
            reachable=network_check.get("reachable", False),
            latency_ms=network_check.get("latency_ms"),
            error=network_check.get("error_message"),
            checked_at=network_check.get("checked_at", utc_now_iso())
        )

    business = None
    if business_check:
        business = BusinessHealthCheck(
            agent_name=agent_name,
            status=business_check.get("status", "unknown"),
            runtime_available=business_check.get("runtime_available"),
            claude_available=business_check.get("claude_available"),
            context_percent=business_check.get("context_percent"),
            active_execution_count=business_check.get("active_executions", 0),
            stuck_execution_count=0,  # Not stored directly
            recent_error_rate=business_check.get("error_rate", 0.0),
            checked_at=business_check.get("checked_at", utc_now_iso())
        )

    # Get historical metrics
    uptime = db.calculate_uptime_percent(agent_name, hours=24)
    avg_latency = db.calculate_avg_latency(agent_name, hours=24)

    # Parse issues from error_message
    issues = []
    if aggregate_check.get("error_message"):
        issues = aggregate_check["error_message"].split("; ")

    # #526: unified circuit-breaker block (same shape as
    # GET /api/agents/{name}/circuit-breaker).
    from services.circuit_breaker_view import build_circuit_breaker_block
    circuit_breaker = build_circuit_breaker_block(agent_name)

    return AgentHealthDetail(
        agent_name=agent_name,
        aggregate_status=aggregate_check.get("status", "unknown"),
        last_check_at=aggregate_check.get("checked_at"),
        docker=docker,
        network=network,
        business=business,
        issues=issues,
        recent_alerts=[],  # TODO: Fetch from notifications
        uptime_percent_24h=round(uptime, 2) if uptime else None,
        avg_latency_24h_ms=round(avg_latency, 2) if avg_latency else None,
        circuit_breaker=circuit_breaker,
    )


@router.get("/agents/{agent_name}/history")
async def get_agent_health_history(
    agent_name: AuthorizedAgentByName,
    hours: int = Query(24, ge=1, le=168),  # Max 7 days
    check_type: str = Query("aggregate", regex="^(docker|network|business|aggregate)$"),
    limit: int = Query(100, ge=1, le=1000)
):
    """
    Get health check history for an agent.

    Returns historical health checks for trend analysis.
    """
    history = db.get_agent_health_history(agent_name, check_type, hours, limit)

    return {
        "agent_name": agent_name,
        "check_type": check_type,
        "hours": hours,
        "count": len(history),
        "checks": history
    }


@router.post("/agents/{agent_name}/check", response_model=AgentHealthDetail)
async def trigger_health_check(
    agent_name: AuthorizedAgentByName,
    current_user: User = Depends(require_admin)
):
    """
    Trigger an immediate health check for an agent.

    Admin only. Forces a fresh health check regardless of schedule.

    #631 — this endpoint is the documented manual recovery path out of the
    dormant circuit state. Reset the circuit before running the check so the
    probe actually executes (rather than being short-circuited by the
    dormant guard in perform_health_check).
    """
    try:
        from services.agent_client import reset_circuit
        reset_circuit(agent_name)
    except Exception as exc:
        # Best-effort. If Redis is down the dormant guard fail-opens anyway.
        import logging
        logging.getLogger(__name__).warning(
            "Manual /check could not reset circuit for %s: %s", agent_name, exc
        )

    # Perform health check
    result = await perform_health_check(agent_name, DEFAULT_CONFIG, store_results=True)

    # Broadcast status if changed
    previous_check = db.get_agent_health_history(agent_name, "aggregate", hours=1, limit=2)
    if len(previous_check) > 1:
        previous_status = previous_check[1].get("status", "unknown")
        if previous_status != result.aggregate_status:
            await _broadcast_health_change(
                agent_name,
                previous_status,
                result.aggregate_status,
                result.issues
            )

    return result


# ============================================================================
# Alerts Endpoint
# ============================================================================

@router.get("/alerts")
async def get_active_alerts(
    current_user: User = Depends(require_admin),
    status: str = Query("pending", regex="^(pending|acknowledged|all)$"),
    limit: int = Query(50, ge=1, le=200)
):
    """
    Get active monitoring alerts.

    Admin only. Returns notifications with category='health'.
    """
    # Query notifications with health category
    notifications = db.list_notifications(
        status=None if status == "all" else status,
        category="health",
        limit=limit
    )

    return {
        "count": len(notifications),
        "alerts": notifications
    }


# ============================================================================
# Configuration Endpoints
# ============================================================================

@router.get("/config", response_model=MonitoringConfig)
async def get_monitoring_config(
    current_user: User = Depends(require_admin)
):
    """
    Get current monitoring configuration.

    Admin only.
    """
    return load_persisted_monitoring_config()


@router.put("/config", response_model=MonitoringConfig)
async def update_monitoring_config(
    config: MonitoringConfig,
    current_user: User = Depends(require_admin)
):
    """
    Update monitoring configuration.

    Admin only. Interval changes take effect on the next check cycle;
    a flipped `enabled` flag reconciles the loop immediately (#1121).
    """
    # #1121: `enabled` is the loop's on/off switch, but every MonitoringConfig
    # field has a default — so a body that omits `enabled` would deserialize to
    # `enabled=False` and silently tear down a running loop on a routine
    # interval tweak. Only treat `enabled` as authoritative when the client
    # explicitly sent it; otherwise preserve the persisted run-state.
    if "enabled" not in config.model_fields_set:
        config = config.model_copy(update={"enabled": load_persisted_monitoring_config().enabled})

    # Save to settings
    db.set_setting(_MONITORING_CONFIG_KEY, json.dumps(config.model_dump()))

    # Update running service config, then reconcile its run state to the
    # `enabled` flag so it stays the single source of truth (#1121).
    # Start/stop are done inline (awaited) rather than via background tasks so
    # the runtime state matches the persisted flag before we return — a
    # backgrounded start could otherwise run after a racing disable and leave
    # the loop up against a persisted `enabled=False`. start()/stop() do not
    # block (create_task / cancel-a-sleeping-task).
    service = get_monitoring_service()
    service.config = config
    if config.enabled and not service.is_running:
        await start_monitoring_service(config)
    elif not config.enabled and service.is_running:
        await stop_monitoring_service()

    return config


@router.post("/enable")
async def enable_monitoring(
    current_user: User = Depends(require_admin)
):
    """
    Enable the monitoring service.

    Admin only. Starts the periodic health check loop.
    """
    # #1121: persist enabled=True FIRST so the choice survives restarts even
    # if the service is already running.
    config = _persist_monitoring_enabled(True)

    service = get_monitoring_service()
    if service.is_running:
        service.config = config
        return {"status": "already_running", "message": "Monitoring service is already running"}

    # #1121: start inline (awaited), not via a background task. start() only
    # creates the loop task, so it doesn't block — and doing it synchronously
    # closes the enable→disable race where a backgrounded start would run after
    # a racing disable and leave the loop up against persisted `enabled=False`.
    await start_monitoring_service(config)

    return {"status": "starting", "message": "Monitoring service is starting"}


@router.post("/disable")
async def disable_monitoring(
    current_user: User = Depends(require_admin)
):
    """
    Disable the monitoring service.

    Admin only. Stops the periodic health check loop.
    """
    # #1121: persist enabled=False FIRST so a disabled fleet stays disabled
    # across restarts even if the service wasn't running this process.
    _persist_monitoring_enabled(False)

    service = get_monitoring_service()
    if not service.is_running:
        return {"status": "already_stopped", "message": "Monitoring service is not running"}

    # #1121: stop inline (awaited), mirroring enable — keeps the persisted flag
    # and the runtime state reconciled before returning. stop() cancels a
    # loop task that is normally parked in asyncio.sleep, so it returns promptly.
    await stop_monitoring_service()

    return {"status": "stopping", "message": "Monitoring service is stopping"}


# ============================================================================
# Batch Operations
# ============================================================================

@router.post("/check-all")
async def trigger_fleet_health_check(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_admin)
):
    """
    Trigger health checks for all running agents.

    Admin only. Runs checks in background.
    """
    from services.docker_service import list_all_agents_fast

    agents = list_all_agents_fast()
    # trinity-enterprise#69: ghosts excluded from fleet health (see monitoring_service).
    running_agents = [
        a.name for a in agents
        if a.status == "running" and getattr(a, "ephemeral", False) is not True
    ]

    if not running_agents:
        return {"status": "no_agents", "message": "No running agents to check"}

    # Run in background
    async def run_checks():
        await perform_fleet_health_check(running_agents, DEFAULT_CONFIG, store_results=True)

    background_tasks.add_task(run_checks)

    return {
        "status": "started",
        "message": f"Health checks started for {len(running_agents)} agents",
        "agents": running_agents
    }


@router.get("/cleanup-status")
async def get_cleanup_status(
    current_user: User = Depends(require_admin)
):
    """
    Get cleanup service status and last run report.

    Admin only. Returns the latest cleanup report with counts of
    stale resources found and cleaned.
    """
    from services.cleanup_service import cleanup_service

    report = cleanup_service.last_report
    return {
        "running": cleanup_service._running,
        "interval_seconds": cleanup_service.poll_interval,
        "last_run_at": cleanup_service.last_run_at,
        "last_report": report.to_dict() if report else None,
    }


@router.post("/cleanup-trigger")
async def trigger_cleanup(
    current_user: User = Depends(require_admin)
):
    """
    Trigger an immediate cleanup cycle.

    Admin only. Runs cleanup synchronously and returns the report.
    """
    from services.cleanup_service import cleanup_service

    report = await cleanup_service.run_cleanup()
    return {
        "status": "completed",
        "report": report.to_dict(),
    }


@router.delete("/history")
async def cleanup_health_history(
    days: int = Query(7, ge=1, le=90),
    current_user: User = Depends(require_admin)
):
    """
    Delete old health check records.

    Admin only. Deletes records older than specified days.
    """
    deleted = db.cleanup_old_health_records(days)
    return {
        "status": "success",
        "deleted_records": deleted,
        "retention_days": days
    }
