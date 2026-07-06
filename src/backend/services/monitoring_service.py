"""
Agent Monitoring Service (MON-001)

Performs multi-layer health checks on agent infrastructure:
- Layer 1: Docker (container status, resources, restarts, OOM)
- Layer 2: Network (HTTP reachability, latency)
- Layer 3: Business (runtime availability, context usage, error rates)

Health checks run as background tasks and store results in the database.
Alerts are sent via the notification system when status changes.
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

import docker
import httpx

from database import db
from redis_breaker_util import get_breaker_redis
from services.agent_auth import agent_httpx_client
from db_models import (
    AgentHealthStatus,
    DockerHealthCheck,
    NetworkHealthCheck,
    BusinessHealthCheck,
    AgentHealthDetail,
    AgentHealthSummary,
    FleetHealthSummary,
    FleetHealthStatus,
    MonitoringConfig,
)
from utils.helpers import utc_now_iso

# Exception classification tuples are pure constants — no Redis / circular
# risk — so they're safe at module-top. Only `CircuitState` (which touches
# Redis) stays lazy below. Importing the tuples at module-top also keeps
# them stable under test patches that replace `services.agent_client` with
# a MagicMock, which would otherwise turn the names into mocks that fail
# `except <not-an-exception>` at runtime.
#
# The ImportError fallback supports fixtures that stub services.agent_client
# with just CircuitState (e.g. test_monitoring_dormant_skip.py) and don't
# bother to populate these tuples. The literals here MUST track
# agent_client.py — keep both lists in sync.
try:
    from services.agent_client import (
        CIRCUIT_FAILURE_EXCEPTIONS,
        TRANSIENT_TRANSPORT_EXCEPTIONS,
    )
except ImportError:
    CIRCUIT_FAILURE_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)
    TRANSIENT_TRANSPORT_EXCEPTIONS = (
        httpx.TimeoutException,
        httpx.WriteError,
        httpx.ReadError,
        httpx.RemoteProtocolError,
    )

logger = logging.getLogger(__name__)


# =========================================================================
# Configuration
# =========================================================================

DEFAULT_CONFIG = MonitoringConfig()


def _is_circuit_dormant(agent_name: str) -> bool:
    """Return True when the agent's circuit breaker is parked dormant (#631).

    Imported lazily so a circular import (services.agent_client →
    services.monitoring_service) can never form even if reachability ever
    flips.
    """
    try:
        from services.agent_client import CircuitState
        return CircuitState(agent_name).state == "dormant"
    except Exception:
        # Treat any failure to read circuit state as 'not dormant' — the
        # historical behaviour (probe + write) is the safe fallback when we
        # can't tell. Worst case is we flood DB on a Redis blip; that's
        # already what pre-#631 code did.
        return False


def _dormant_health_detail(agent_name: str) -> "AgentHealthDetail":
    """Synthesise an AgentHealthDetail for a dormant agent without DB I/O."""
    return AgentHealthDetail(
        agent_name=agent_name,
        aggregate_status="unhealthy",
        last_check_at=utc_now_iso(),
        docker=None,
        network=None,
        business=None,
        issues=[
            "Agent circuit dormant — too many consecutive failed probes. "
            "Awaiting container restart or manual /api/monitoring/agents/{name}/check."
        ],
        recent_alerts=[],
        uptime_percent_24h=None,
        avg_latency_24h_ms=None,
    )

# Initialize Docker client
try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"Warning: Monitoring service could not connect to Docker: {e}")
    docker_client = None


# =========================================================================
# Health Check Functions
# =========================================================================

def check_docker_health(agent_name: str) -> DockerHealthCheck:
    """
    Perform Docker layer health check.

    Checks:
    - Container status (running/stopped/etc)
    - Exit code
    - Restart count
    - OOM killed status
    - CPU and memory usage
    """
    now = utc_now_iso()

    if not docker_client:
        return DockerHealthCheck(
            agent_name=agent_name,
            container_status="unknown",
            checked_at=now
        )

    try:
        container = docker_client.containers.get(f"agent-{agent_name}")
    except docker.errors.NotFound:
        return DockerHealthCheck(
            agent_name=agent_name,
            container_status="not_found",
            checked_at=now
        )
    except Exception as e:
        return DockerHealthCheck(
            agent_name=agent_name,
            container_status="error",
            checked_at=now
        )

    # Get container state from attrs
    state = container.attrs.get("State", {})

    # Get resource stats (this can be slow, ~1-2s)
    cpu_percent = None
    memory_percent = None
    memory_mb = None

    try:
        # Use stream=False for one-shot stats
        stats = container.stats(stream=False)

        # Calculate CPU percentage
        cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                    stats["precpu_stats"]["cpu_usage"]["total_usage"]
        system_delta = stats["cpu_stats"]["system_cpu_usage"] - \
                       stats["precpu_stats"]["system_cpu_usage"]
        num_cpus = stats["cpu_stats"].get("online_cpus", 1)

        if system_delta > 0 and cpu_delta > 0:
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100

        # Calculate memory percentage
        mem_usage = stats["memory_stats"].get("usage", 0)
        mem_limit = stats["memory_stats"].get("limit", 1)
        memory_percent = (mem_usage / mem_limit) * 100 if mem_limit > 0 else 0
        memory_mb = mem_usage / (1024 * 1024)
    except Exception:
        pass  # Stats unavailable for stopped containers

    return DockerHealthCheck(
        agent_name=agent_name,
        container_status=container.status,
        exit_code=state.get("ExitCode"),
        restart_count=container.attrs.get("RestartCount", 0),
        oom_killed=state.get("OOMKilled", False),
        cpu_percent=round(cpu_percent, 2) if cpu_percent is not None else None,
        memory_percent=round(memory_percent, 2) if memory_percent is not None else None,
        memory_mb=round(memory_mb, 2) if memory_mb is not None else None,
        checked_at=now
    )


async def _agent_has_active_executions(agent_name: str) -> bool:
    """True when the agent currently holds ≥1 admitted execution slot (#1463).

    Read through the CapacityManager facade (Invariant #428) — a pure
    backend/Redis lookup that never touches the (possibly unresponsive) agent.
    Used to distinguish a genuinely wedged agent from one merely busy with a
    long, still-progressing run when its /health probe times out.

    Fail-open to False (treat as NOT busy → preserve the pre-#1463 liveness
    contract): if the slot lookup errors, or Redis is down, we fall back to
    counting the timeout as a failure — but the circuit breaker is itself
    fail-open on Redis-down, so a stray False here can't wrongly trip it.
    """
    try:
        from services.capacity_manager import get_capacity_manager
        status = await get_capacity_manager().get_status(agent_name)
        return bool(status.is_busy)
    except Exception as e:
        logger.debug(
            "_agent_has_active_executions(%s) slot lookup failed, treating as idle: %s",
            agent_name, e,
        )
        return False


async def check_network_health(
    agent_name: str,
    timeout: float = 10.0
) -> NetworkHealthCheck:
    """
    Perform Network layer health check.

    Checks:
    - HTTP reachability to agent's /health endpoint
    - Response time (latency)
    - HTTP status code

    #631 — feeds the result into the per-agent circuit breaker so a dead
    agent's repeated unreachability eventually trips dormant and stops the
    health-check write flood.

    #474 — failure classification largely mirrors AgentClient._request().
    CIRCUIT_FAILURE_EXCEPTIONS (ConnectError, ConnectTimeout) count toward
    the circuit; *Timeout / PoolTimeout do not. Any HTTP response (200..599)
    records success — symmetric with _request() so stale failure counters
    clear as soon as the agent proves reachable. A wedged-but-listening
    agent (e.g. /health 5xx) is still flagged via aggregate_health()'s
    status_code >= 500 check, not via the circuit.

    #474 Layer 2 — /health-specific divergence from _request(): on this
    endpoint, BrokenPipeError / ConnectionResetError (raw OSError —
    uncaught in _request() per #798) and httpx.ReadError / WriteError /
    RemoteProtocolError (transient and circuit-neutral in _request())
    are reclassified. Raw pipe errors are client-side cancellations and
    stay circuit-neutral; mid-stream httpx transport errors on this
    small, fast endpoint are taken as agent liveness signals and DO
    record_failure(). See the explicit handlers below.
    """
    now = utc_now_iso()
    url = f"http://agent-{agent_name}:8000/health"

    # `CircuitState` stays lazy — it touches Redis on construction and
    # could plausibly grow a backwards dependency on this module later.
    # The exception tuples it shares with us are imported at module-top
    # (constants, no circular risk).
    try:
        from services.agent_client import CircuitState
        circuit = CircuitState(agent_name)
    except Exception:
        circuit = None

    start = time.monotonic()
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            response = await client.get(url)
            latency_ms = (time.monotonic() - start) * 1000

            # Any httpx.Response (status_code in 100..599) proves TCP/HTTP
            # reachability, so the circuit records success unconditionally —
            # symmetric with AgentClient._request() at line ~642. 5xx is
            # NOT a circuit signal; aggregate_health() below flags it as
            # UNHEALTHY using the explicit status_code >= 500 check.
            if circuit is not None:
                circuit.record_success()

            return NetworkHealthCheck(
                agent_name=agent_name,
                reachable=True,
                status_code=response.status_code,
                latency_ms=round(latency_ms, 2),
                checked_at=now
            )

    except asyncio.CancelledError:
        # Don't swallow cancellation during shutdown.
        raise

    except CIRCUIT_FAILURE_EXCEPTIONS as e:
        # Real unreachability — TCP refused or connect handshake timed out.
        # User-facing string is stable ("Connection refused") so dashboards
        # don't change on httpx version bumps and exception messages don't
        # leak into UI; full classname+message stays in logs for triage.
        logger.debug(
            "check_network_health(%s) circuit failure %s: %s",
            agent_name, type(e).__name__, e,
        )
        if circuit is not None:
            circuit.record_failure()
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error="Connection refused",
            checked_at=now
        )

    except (BrokenPipeError, ConnectionResetError) as e:
        # Client-side transport drop — agent isn't sick, the connection died
        # mid-flight (likely an upstream MCP-sync client cancellation that
        # cascaded into the pooled keepalive socket). Do NOT record_failure
        # — the agent's health hasn't been observed. (#474 Layer 2.)
        #
        # Layered ABOVE TRANSIENT_TRANSPORT_EXCEPTIONS because the shared
        # tuple from agent_client.py does not include the raw OSError
        # subclasses — #798 leaves those uncaught in _request() so they
        # surface as client bugs, but on a /health probe we'd rather
        # observe the drop and report `reachable=False` than raise.
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error=f"Connection dropped: {type(e).__name__}",
            checked_at=now
        )

    except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as e:
        # On a /health probe these ARE liveness signals: if the agent
        # partially writes then drops (event-loop wedge, OOM mid-write,
        # segfault), the agent IS unhealthy. Distinct from a client-side
        # BrokenPipeError above. (#474 Phase 3 Eng finding #3.)
        #
        # /health-specific OVERRIDE of TRANSIENT_TRANSPORT_EXCEPTIONS
        # below: in AgentClient._request() these same exceptions on
        # arbitrary /api/* paths stay circuit-neutral (#798) because a
        # mid-stream drop there could be benign (client cancel, busy
        # agent, network blip). On the dedicated /health endpoint the
        # response is supposed to be small and immediate, so a partial
        # response is genuinely "agent crashed mid-write" — circuit
        # signal applies. Must be layered ABOVE TRANSIENT_TRANSPORT_EXCEPTIONS
        # so Python's first-match wins.
        if circuit is not None:
            circuit.record_failure()
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error=f"HTTP transport error on /health: {type(e).__name__}",
            checked_at=now
        )

    except httpx.TimeoutException as e:
        # /health is supposed to be a small, fast endpoint that responds
        # immediately. A timeout here is a liveness signal — the agent is
        # wedged (event-loop blocked, GIL contention, runaway compute) —
        # so record_failure() applies even though in AgentClient._request()
        # the same exception is circuit-neutral.
        #
        # /health-specific OVERRIDE of TRANSIENT_TRANSPORT_EXCEPTIONS
        # below (TimeoutException is the tuple's parent class). Layered
        # ABOVE so the timeout subset gets the liveness contract instead
        # of the transient one. Note: ConnectTimeout is intercepted by
        # CIRCUIT_FAILURE_EXCEPTIONS above (first-match wins) and never
        # reaches here.
        #
        # #1463 — EXCEPT when the agent has an execution in flight: a
        # long CPU-bound run (Claude Code subprocess streaming, repo
        # synthesis) starves the agent-server event loop enough that
        # /health can't answer within the probe timeout, while the
        # execution itself completes SUCCESS. That's "busy", not "wedged":
        # counting it opened the breaker on every long run and fast-failed
        # every other trigger until the ~1h dormant probe recovered. The
        # backend already knows about in-flight work (slot service), so
        # gate the liveness verdict on it — stay circuit-neutral while
        # busy, but still report reachable=False for the aggregate rollup.
        busy = await _agent_has_active_executions(agent_name)
        logger.debug(
            "check_network_health(%s) timeout %s (busy=%s): %s",
            agent_name, type(e).__name__, busy, e,
        )
        if circuit is not None and not busy:
            circuit.record_failure()
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error="HTTP timeout (agent busy)" if busy else "HTTP timeout",
            checked_at=now
        )

    except TRANSIENT_TRANSPORT_EXCEPTIONS as e:
        # Garbled HTTP framing or other transient transport hiccup. NOT a
        # circuit signal. The ReadError/WriteError/RemoteProtocolError and
        # TimeoutException entries of this tuple are no longer reachable
        # from here — the /health-specific handlers above intercept them
        # with the opposite contract. Kept as a defensive default for any
        # future member added to the shared tuple.
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error=f"{type(e).__name__}: {e}"[:200],
            checked_at=now
        )

    except Exception as e:
        # Unexpected — log with traceback. Unknown errors are almost always
        # our bug, not evidence of agent unhealth, so DO NOT trip the
        # circuit here.
        logger.exception(
            "check_network_health(%s) unexpected %s",
            agent_name, type(e).__name__,
        )
        return NetworkHealthCheck(
            agent_name=agent_name,
            reachable=False,
            error=f"{type(e).__name__}: {e}"[:200],
            checked_at=now
        )


async def check_business_health(
    agent_name: str,
    timeout: float = 10.0
) -> BusinessHealthCheck:
    """
    Perform Business logic health check.

    Checks:
    - Runtime availability (from /health response)
    - Claude availability
    - Context window usage (from /api/chat/session)
    - Active executions
    - Recent error rate
    """
    now = utc_now_iso()

    runtime_available = None
    claude_available = None
    context_percent = None
    active_execution_count = 0
    stuck_execution_count = 0
    recent_error_rate = 0.0
    consecutive_failures = None  # #1020: None when agent image predates the field
    last_task_at = None
    clone_status = None  # #1439: None when agent image predates the field

    # Check /health endpoint for runtime status
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            health_response = await client.get(f"http://agent-{agent_name}:8000/health")
            if health_response.status_code == 200:
                health_data = health_response.json()
                runtime_available = health_data.get("runtime_available", True)
                claude_available = health_data.get("claude_available", True)
                # #1020: richer /health signal — graceful when keys are absent
                # (older agent images). Used for fleet-health + the dispatch
                # circuit breaker (#526).
                consecutive_failures = health_data.get("consecutive_failures")
                last_task_at = health_data.get("last_task_at")
                # #1439: coarse identity-clone status ("ok"|"failed"); absent on
                # older images → None → treated as healthy by aggregation.
                clone_status = health_data.get("clone_status")
    except Exception:
        pass  # Will be marked as degraded/unhealthy by aggregation

    # Check /api/chat/session for context usage
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            session_response = await client.get(f"http://agent-{agent_name}:8000/api/chat/session")
            if session_response.status_code == 200:
                session_data = session_response.json()
                context_used = session_data.get("context_used", 0)
                context_max = session_data.get("context_max", 200000)
                if context_max > 0:
                    context_percent = (context_used / context_max) * 100
    except Exception:
        pass

    # Check /api/executions/running for active executions
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            exec_response = await client.get(f"http://agent-{agent_name}:8000/api/executions/running")
            if exec_response.status_code == 200:
                exec_data = exec_response.json()
                executions = exec_data.get("executions", [])
                active_execution_count = len(executions)

                # Check for stuck executions (running > 30 min)
                stuck_threshold = datetime.utcnow() - timedelta(minutes=30)
                for ex in executions:
                    started_at = ex.get("started_at", "")
                    try:
                        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        if started.replace(tzinfo=None) < stuck_threshold:
                            stuck_execution_count += 1
                    except Exception:
                        pass
    except Exception:
        pass

    # Calculate recent error rate from activities (last 5 min)
    # This would require querying the activities table, simplified here
    # TODO: Implement actual error rate calculation from agent_activities

    # Determine status based on checks
    status = "healthy"
    issues = []
    if runtime_available is False or claude_available is False:
        status = "unhealthy"
    elif clone_status == "failed":
        # #1439: identity clone failed — running but no workspace identity.
        status = "unhealthy"
    elif context_percent and context_percent > 95:
        status = "degraded"
    elif stuck_execution_count > 0:
        status = "degraded"

    return BusinessHealthCheck(
        agent_name=agent_name,
        status=status,
        runtime_available=runtime_available,
        claude_available=claude_available,
        context_percent=round(context_percent, 2) if context_percent is not None else None,
        active_execution_count=active_execution_count,
        stuck_execution_count=stuck_execution_count,
        recent_error_rate=recent_error_rate,
        credential_status=None,
        clone_status=clone_status,  # #1439
        consecutive_failures=consecutive_failures,  # #1020
        last_task_at=last_task_at,  # #1020
        checked_at=now
    )


def aggregate_health(
    docker: DockerHealthCheck,
    network: NetworkHealthCheck,
    business: BusinessHealthCheck,
    config: MonitoringConfig = DEFAULT_CONFIG
) -> Tuple[AgentHealthStatus, List[str]]:
    """
    Aggregate health checks into a single status.

    Priority: Docker > Network > Business

    Returns:
        Tuple of (status, list of issues)
    """
    issues = []

    # Critical: Docker layer failures
    if docker.container_status == "not_found":
        issues.append("Container not found")
        return AgentHealthStatus.CRITICAL, issues

    if docker.container_status not in ("running", "unknown"):
        issues.append(f"Container status: {docker.container_status}")
        return AgentHealthStatus.CRITICAL, issues

    if docker.oom_killed:
        issues.append("Container killed by OOM")
        return AgentHealthStatus.CRITICAL, issues

    # Unhealthy: Network or runtime failures
    if not network.reachable:
        issues.append(f"Network unreachable: {network.error or 'unknown'}")
        return AgentHealthStatus.UNHEALTHY, issues

    # /health returned but with a server-error status — agent is reachable
    # but its HTTP layer is broken. Mark UNHEALTHY (#474 eng review). Without
    # this, the new "any HTTP response = reachable" rule would silently rate
    # a wedged-but-listening agent as HEALTHY.
    if network.status_code is not None and network.status_code >= 500:
        issues.append(f"Network /health returned {network.status_code}")
        return AgentHealthStatus.UNHEALTHY, issues

    if business.runtime_available is False:
        issues.append("Runtime not available")
        return AgentHealthStatus.UNHEALTHY, issues

    # #1439: identity clone failed — the agent is running but has no workspace
    # identity (no repo, no CLAUDE.md / skills). Surface it instead of reporting
    # a healthy-but-empty agent. The issue string is a fixed, server-controlled
    # constant (never the agent-supplied repo/branch/error), so it can't forge
    # extra '; '-joined rows or inject into the operator UI.
    if business.clone_status == "failed":
        issues.append("Agent identity clone failed")
        return AgentHealthStatus.UNHEALTHY, issues

    # Degraded: Performance issues
    if docker.cpu_percent is not None and docker.cpu_percent > config.cpu_critical_percent:
        issues.append(f"High CPU usage ({docker.cpu_percent:.1f}%)")
        return AgentHealthStatus.DEGRADED, issues

    if docker.memory_percent is not None and docker.memory_percent > config.memory_critical_percent:
        issues.append(f"High memory usage ({docker.memory_percent:.1f}%)")
        return AgentHealthStatus.DEGRADED, issues

    if network.latency_ms is not None and network.latency_ms > config.latency_critical_ms:
        issues.append(f"High latency ({network.latency_ms:.0f}ms)")
        return AgentHealthStatus.DEGRADED, issues

    if business.context_percent is not None and business.context_percent > config.context_critical_percent:
        issues.append(f"Context window at {business.context_percent:.1f}%")
        return AgentHealthStatus.DEGRADED, issues

    if business.recent_error_rate > config.error_rate_critical:
        issues.append(f"High error rate ({business.recent_error_rate * 100:.1f}%)")
        return AgentHealthStatus.DEGRADED, issues

    if business.stuck_execution_count > 0:
        issues.append(f"{business.stuck_execution_count} stuck execution(s)")
        return AgentHealthStatus.DEGRADED, issues

    # Warning-level issues (still healthy but with warnings)
    if docker.cpu_percent is not None and docker.cpu_percent > config.cpu_warning_percent:
        issues.append(f"Elevated CPU usage ({docker.cpu_percent:.1f}%)")

    if docker.memory_percent is not None and docker.memory_percent > config.memory_warning_percent:
        issues.append(f"Elevated memory usage ({docker.memory_percent:.1f}%)")

    if docker.restart_count and docker.restart_count > 3:
        issues.append(f"High restart count ({docker.restart_count})")

    return AgentHealthStatus.HEALTHY, issues


# =========================================================================
# Composite Health Check
# =========================================================================

async def perform_health_check(
    agent_name: str,
    config: MonitoringConfig = DEFAULT_CONFIG,
    store_results: bool = True
) -> AgentHealthDetail:
    """
    Perform comprehensive health check for an agent.

    Runs all three health check layers and aggregates results.
    Optionally stores results in the database.

    #631 — short-circuit when the agent's circuit breaker is dormant. Once we
    enter dormant we already know the HTTP server is hard-down (~40min of
    failed probes). Continuing to run network + business probes wastes CPU
    and — more importantly — every cycle wrote 4 health_check rows; with two
    uvicorn workers polling concurrently this was the SQLite-contention
    source flagged in the bug report. Bail fast with a synthetic detail and
    no DB writes; recovery resumes when the circuit exits dormant
    (container restart, manual /api/monitoring/agents/{name}/check, or
    autonomy re-enabled).

    Returns:
        AgentHealthDetail with all check results
    """
    if _is_circuit_dormant(agent_name):
        return _dormant_health_detail(agent_name)

    # Run Docker check in thread pool (blocking)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        docker_check = await loop.run_in_executor(
            executor,
            check_docker_health,
            agent_name
        )

    # Run network and business checks concurrently
    network_check, business_check = await asyncio.gather(
        check_network_health(agent_name, config.http_timeout),
        check_business_health(agent_name, config.http_timeout)
    )

    # Aggregate results
    status, issues = aggregate_health(docker_check, network_check, business_check, config)
    now = utc_now_iso()

    # Store results if requested
    if store_results:
        # Store individual layer checks
        db.create_health_check(
            agent_name=agent_name,
            check_type="docker",
            status="healthy" if docker_check.container_status == "running" else "unhealthy",
            docker_metrics={
                "container_status": docker_check.container_status,
                "exit_code": docker_check.exit_code,
                "restart_count": docker_check.restart_count,
                "oom_killed": docker_check.oom_killed,
                "cpu_percent": docker_check.cpu_percent,
                "memory_percent": docker_check.memory_percent,
                "memory_mb": docker_check.memory_mb,
            }
        )

        db.create_health_check(
            agent_name=agent_name,
            check_type="network",
            status="healthy" if network_check.reachable else "unhealthy",
            network_metrics={
                "reachable": network_check.reachable,
                "latency_ms": network_check.latency_ms,
            },
            error_message=network_check.error
        )

        db.create_health_check(
            agent_name=agent_name,
            check_type="business",
            status=business_check.status,
            business_metrics={
                "runtime_available": business_check.runtime_available,
                "claude_available": business_check.claude_available,
                "context_percent": business_check.context_percent,
                "active_executions": business_check.active_execution_count,
                "error_rate": business_check.recent_error_rate,
                "credential_status": business_check.credential_status,
            }
        )

        # Store aggregate result
        db.create_health_check(
            agent_name=agent_name,
            check_type="aggregate",
            status=status.value,
            docker_metrics={
                "container_status": docker_check.container_status,
                "cpu_percent": docker_check.cpu_percent,
                "memory_percent": docker_check.memory_percent,
            },
            network_metrics={
                "reachable": network_check.reachable,
                "latency_ms": network_check.latency_ms,
            },
            business_metrics={
                "runtime_available": business_check.runtime_available,
                "context_percent": business_check.context_percent,
            },
            error_message="; ".join(issues) if issues else None
        )

        # Check for status change and send alerts
        try:
            from services.monitoring_alerts import get_alert_service
            alert_service = get_alert_service()

            # Get previous status from recent history
            history = db.get_agent_health_history(agent_name, "aggregate", hours=1, limit=2)
            if len(history) > 1:
                previous_status = history[1].get("status", "unknown")
                if previous_status != status.value:
                    await alert_service.evaluate_and_alert(
                        agent_name=agent_name,
                        previous_status=previous_status,
                        current_status=status.value,
                        issues=issues,
                        details={
                            "docker_status": docker_check.container_status,
                            "cpu_percent": docker_check.cpu_percent,
                            "memory_percent": docker_check.memory_percent,
                            "network_reachable": network_check.reachable,
                            "latency_ms": network_check.latency_ms,
                        }
                    )

            # Check for specific alert conditions
            if docker_check.oom_killed:
                await alert_service.alert_container_stopped(
                    agent_name, docker_check.exit_code, oom_killed=True
                )
            elif docker_check.container_status not in ("running", "unknown", "not_found"):
                await alert_service.alert_container_stopped(
                    agent_name, docker_check.exit_code
                )

            if docker_check.restart_count and docker_check.restart_count > 3:
                await alert_service.alert_high_restart_count(
                    agent_name, docker_check.restart_count
                )

            if docker_check.cpu_percent and docker_check.cpu_percent > config.cpu_critical_percent:
                await alert_service.alert_resource_critical(
                    agent_name, "cpu", docker_check.cpu_percent
                )

            if docker_check.memory_percent and docker_check.memory_percent > config.memory_critical_percent:
                await alert_service.alert_resource_critical(
                    agent_name, "memory", docker_check.memory_percent
                )

        except Exception as e:
            print(f"Failed to send monitoring alert: {e}")

    # Get historical metrics
    uptime = db.calculate_uptime_percent(agent_name, hours=24)
    avg_latency = db.calculate_avg_latency(agent_name, hours=24)

    return AgentHealthDetail(
        agent_name=agent_name,
        aggregate_status=status.value,
        last_check_at=now,
        docker=docker_check,
        network=network_check,
        business=business_check,
        issues=issues,
        recent_alerts=[],  # TODO: Fetch from notifications
        uptime_percent_24h=round(uptime, 2) if uptime else None,
        avg_latency_24h_ms=round(avg_latency, 2) if avg_latency else None
    )


async def perform_fleet_health_check(
    agent_names: List[str],
    config: MonitoringConfig = DEFAULT_CONFIG,
    store_results: bool = True
) -> FleetHealthStatus:
    """
    Perform health checks for multiple agents in parallel.

    Returns:
        FleetHealthStatus with summary and individual agent statuses
    """
    now = utc_now_iso()

    # Run health checks in parallel with concurrency limit
    semaphore = asyncio.Semaphore(10)  # Max 10 concurrent checks

    async def check_with_limit(name: str) -> AgentHealthDetail:
        async with semaphore:
            return await perform_health_check(name, config, store_results)

    results = await asyncio.gather(
        *[check_with_limit(name) for name in agent_names],
        return_exceptions=True
    )

    # Process results
    agents = []
    summary = FleetHealthSummary(total_agents=len(agent_names))

    for i, result in enumerate(results):
        agent_name = agent_names[i]

        if isinstance(result, Exception):
            # Handle check failure
            agents.append(AgentHealthSummary(
                name=agent_name,
                status="unknown",
                issues=[f"Check failed: {str(result)[:100]}"]
            ))
            summary.unknown += 1
        else:
            # Convert detail to summary
            agents.append(AgentHealthSummary(
                name=result.agent_name,
                status=result.aggregate_status,
                docker_status=result.docker.container_status if result.docker else None,
                network_reachable=result.network.reachable if result.network else None,
                runtime_available=result.business.runtime_available if result.business else None,
                last_check_at=result.last_check_at,
                issues=result.issues
            ))

            # Update summary counts
            status = result.aggregate_status
            if status == "healthy":
                summary.healthy += 1
            elif status == "degraded":
                summary.degraded += 1
            elif status == "unhealthy":
                summary.unhealthy += 1
            elif status == "critical":
                summary.critical += 1
            else:
                summary.unknown += 1

    return FleetHealthStatus(
        enabled=True,
        last_check_at=now,
        summary=summary,
        agents=agents
    )


# =========================================================================
# Background Task Management
# =========================================================================

# #1464: single Redis key holding the current fleet-health leader's worker id.
# One leader across all uvicorn workers (and, if it ever moves there, the
# scheduler process) — whoever holds it runs the probe; the rest idle.
_LEADER_KEY = "monitoring:leader"


class MonitoringService:
    """
    Background service for periodic health checks.

    Usage:
        service = MonitoringService(config)
        await service.start()
        ...
        await service.stop()
    """

    def __init__(self, config: MonitoringConfig = DEFAULT_CONFIG):
        self.config = config
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # #1464: unique per worker process so the cross-worker leader lock only
        # ever refreshes/releases ITS OWN lease. pid is a readable prefix for
        # log triage; the uuid suffix disambiguates a recycled pid.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        """Start the monitoring background task."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        print("Monitoring service started")

    async def stop(self):
        """Stop the monitoring background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # #1464: best-effort release so a graceful shutdown hands leadership off
        # immediately instead of leaving a sibling worker idle until the TTL
        # expires. Never raises.
        self._release_leadership()
        print("Monitoring service stopped")

    def _leader_ttl(self) -> int:
        """Lease TTL — 3× the cycle interval so one or two missed refreshes
        don't drop leadership, but a dead holder's lock expires within ~3
        cycles and a sibling takes over."""
        return max(1, self.config.docker_check_interval) * 3

    def _try_acquire_leadership(self) -> bool:
        """#1464 — cross-worker leader election for the fleet-health loop.

        The FastAPI lifespan starts this loop in EVERY uvicorn worker (prod runs
        `--workers 2`). Without a guard, N workers each probe the whole fleet
        per interval — duplicate `agent_health_checks` rows and, worse, an N×
        `record_failure()` feed into the per-agent circuit breaker (#631),
        effectively dividing the open threshold + dormant budget by N (amplifies
        #1463). Mirrors the single-process + Redis-lock design the scheduler
        already uses.

        Every worker still RUNS the loop and re-evaluates leadership each cycle
        (not once at startup), so if the current leader dies its lease expires
        and a sibling transparently takes over — no restart needed.

        Returns True iff this worker holds the lease for this cycle. Fail-open:
        if Redis is unreachable, act as leader (single-worker dev keeps probing;
        in a Redis-down prod the breaker is itself fail-open, so a doubled feed
        is inert).
        """
        r = get_breaker_redis()
        if r is None:
            return True  # fail-open: no Redis → behave as the sole worker

        ttl = self._leader_ttl()
        try:
            # Acquire only if free (atomic). `nx` guarantees a single winner
            # across workers even under a simultaneous race.
            if r.set(_LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            # Already held — refresh the TTL only if the lease is OURS. We never
            # touch another worker's key, so we can't steal or clobber it.
            if r.get(_LEADER_KEY) == self._worker_id:
                r.expire(_LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:
            # Fail-open on a transient Redis error — a stray extra probe is far
            # cheaper than silently stopping fleet monitoring.
            logger.warning("monitoring leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        """Delete the lease iff we hold it (best-effort, never raises)."""
        try:
            r = get_breaker_redis()
            if r is not None and r.get(_LEADER_KEY) == self._worker_id:
                r.delete(_LEADER_KEY)
        except Exception:
            pass

    async def _run_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                # #1464: only the lease-holding worker performs the fleet probe.
                # Non-leaders idle this cycle so checks aren't duplicated N×.
                leader = self._try_acquire_leadership()
                if leader and not self._is_leader:
                    logger.info(
                        "Monitoring loop acquired leadership (worker %s)", self._worker_id
                    )
                elif not leader and self._is_leader:
                    logger.info(
                        "Monitoring loop yielded leadership (worker %s)", self._worker_id
                    )
                self._is_leader = leader

                if leader:
                    await self._run_check_cycle()
            except Exception as e:
                print(f"Monitoring check cycle failed: {e}")

            # Wait for next cycle. #1121: clamp to >=1s as a belt-and-suspenders
            # guard against a non-positive interval slipping past the
            # MonitoringConfig validator (e.g. a directly-mutated config),
            # which would otherwise spin the loop into a tight flood.
            await asyncio.sleep(max(1, self.config.docker_check_interval))

    async def _run_check_cycle(self):
        """Run one cycle of health checks for every Trinity agent.

        #675: this previously filtered to ``status == "running"`` only.
        A stopped / exited / crashed agent was excluded from every
        cycle, so its ``agent_health_checks`` row never refreshed —
        production showed agents stale for *months* (last refresh
        2026-02-23..04-28) while the one running agent stayed fresh.
        That is precisely backwards: a down agent is the case
        monitoring most needs to surface. `perform_health_check`
        already produces a correct "docker exited / unreachable →
        unhealthy" record for a stopped agent (and the #631 dormant
        short-circuit bounds cost for hard-down agents), so we now
        check the whole fleet.

        Note: `perform_fleet_health_check` is already per-agent
        isolated via `asyncio.gather(..., return_exceptions=True)` +
        per-result handling — issue hypothesis #1 (one agent's failure
        short-circuits the fleet) did not hold; the bug was this
        filter, not missing isolation.
        """
        from services.docker_service import list_all_agents_fast

        # `list_all_agents_fast()` already passes `all=True`, so this
        # includes stopped/exited/dead/created containers (normalised
        # to status="stopped"). Check all of them.
        agents = list_all_agents_fast()
        all_agent_names = [a.name for a in agents]

        if not all_agent_names:
            return

        running_count = sum(1 for a in agents if a.status == "running")
        stopped_count = len(all_agent_names) - running_count
        cycle_start = time.monotonic()

        # Perform health checks
        fleet = await perform_fleet_health_check(
            all_agent_names,
            self.config,
            store_results=True
        )

        # #675 ask: one structured line per pass so a stalled / partial
        # fleet check is observable next time instead of being inferred
        # months later from stale rollups. Includes the agent count,
        # running/stopped split, the aggregate-status breakdown, and
        # wall-clock duration.
        s = fleet.summary
        logger.info(
            "fleet_health_check pass complete: "
            "agents=%d (running=%d stopped=%d) "
            "healthy=%d degraded=%d unhealthy=%d critical=%d unknown=%d "
            "duration_ms=%d",
            len(all_agent_names), running_count, stopped_count,
            s.healthy, s.degraded, s.unhealthy, s.critical, s.unknown,
            int((time.monotonic() - cycle_start) * 1000),
        )

        # Cleanup old records periodically (every hour)
        # This is a simple implementation; could be more sophisticated
        import random
        if random.random() < 0.03:  # ~1 in 33 cycles (every ~15 min at 30s interval)
            db.cleanup_old_health_records(days=7)


# Global service instance
_monitoring_service: Optional[MonitoringService] = None


def get_monitoring_service() -> MonitoringService:
    """Get or create the global monitoring service instance."""
    global _monitoring_service
    if _monitoring_service is None:
        _monitoring_service = MonitoringService()
    return _monitoring_service


async def start_monitoring_service(config: MonitoringConfig = None):
    """Start the global monitoring service."""
    service = get_monitoring_service()
    if config:
        service.config = config
    await service.start()


async def stop_monitoring_service():
    """Stop the global monitoring service."""
    service = get_monitoring_service()
    await service.stop()
