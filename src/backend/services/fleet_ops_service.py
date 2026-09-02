"""Fleet operations orchestration (#1028).

The five heavyweight `routers/ops.py` handlers — fleet health, the #1860
locked fleet restart, fleet stop, emergency stop, and the cost rollup —
moved here so the router holds routes and auth gates and nothing else
(Invariant #1). Each `<name>_impl` assumes its route gate ALREADY ran
`assert_admin` (with the ops-scope allowance where the fence grants it,
#2389): the gate stays in the router precisely so the #2389 fence-vs-gate
scans keep reading live handler source.
"""
import os
import logging
import concurrent.futures
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
from redis_breaker_util import get_breaker_redis, SingleFlightLock, LeaseState
from utils.helpers import utc_now_iso
from services.platform_audit_service import platform_audit_service, AuditEventType


logger = logging.getLogger(__name__)


_FLEET_RESTART_LOCK_KEY = "ops:fleet_restart"

_FLEET_RESTART_LOCK_TTL_SECONDS = 2100


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
        logger.warning(
            f"Emergency stop: {agent_name} failed: {e}", exc_info=True
        )
        return {
            "agent": agent_name,
            "result": "error",
            # py/stack-trace-exposure (#1917, the PR #1912 pattern).
            "error": (
                f"stop failed ({e.__class__.__name__} — "
                f"details in backend logs)"
            ),
        }







async def get_fleet_health_impl(
    request: Request,
    current_user: User
):
    """
    Get health check for all agents.

    Admin-only. Identifies unhealthy agents based on context usage,
    container errors, and idle time.
    """
    # #2323 — per-route opt-in for the bounded read-only `ops` key.
    # `ADMIN_GATE_SCOPES` keeps ops keys out of admin gates by default, so a
    # NEW ops route is inaccessible until someone adds this — the failure
    # direction we want. Only GET routes carry it; every write below stays bare.
    # auth: the route gate ran assert_admin before delegating (#1028)

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
                # py/stack-trace-exposure (#1917, the PR #1912 pattern): the
                # 50-char truncation bounded the leak, it did not remove it —
                # docker/httpx messages put the host, socket path or URL first.
                logger.warning(
                    f"Fleet health: {agent_name} context probe failed: {e}",
                    exc_info=True,
                )
                warnings.append({
                    "agent": agent_name,
                    "issue": (
                        f"Agent not responding ({e.__class__.__name__} — "
                        f"details in backend logs)"
                    ),
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


async def restart_fleet_impl(
    request: Request,
    current_user: User,
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
    # auth: the route gate ran assert_admin before delegating (#1028)
    # #1860: this endpoint replaces containers, not just restarts them —
    # operator-scale destructiveness re-prices principals (Invariant #8, the
    # #1816 escalation rule). No-op for human/system principals.
    reject_agent_principal(current_user)

    # Single-flight lock (SETNX + TTL, fail-open when Redis is down) via the
    # shared ownership-checked primitive (#1920). `acquire()` returns False only
    # on a real busy holder → 409; a Redis-down / error acquire fails open
    # (returns True, `lock.held` stays False) so the loop proceeds unlocked and
    # the release/refresh below become no-ops.
    lock = SingleFlightLock(
        _FLEET_RESTART_LOCK_KEY, _FLEET_RESTART_LOCK_TTL_SECONDS,
        client=get_breaker_redis(),
    )
    if not lock.acquire():
        raise HTTPException(status_code=409, detail="fleet_restart_in_progress")

    # Pre-initialized: the audit in `finally` reads len(agents) — inside the
    # audit's own try/except, so an unbound name there would not propagate but
    # WOULD silently lose the audit row under a lying "audit write failed" log.
    agents = []
    results = []
    successes = 0
    failures = 0
    skipped = 0
    recreated_count = 0
    recreated_map = {}
    failed_agents = []
    lease_reacquired = False  # lapsed-but-unclaimed lease re-taken (audited)
    refresh_warned = False    # one refresh-failure warning per run, not per agent
    stopped_early = None      # None | "lease_lost_foreign" | "error"
    error_class = None

    try:
        # Inside the try/finally so nothing can ever sit between lock acquire
        # and the release (#1919) — today this helper swallows errors and
        # returns [], but the next statement added here may not be so polite.
        agents = list_all_agents_fast()

        for agent in agents:
            agent_name = agent.name
            status = agent.status

            # Pre-action ownership gate (#1919): refresh our own lease ONLY
            # while the stored token is still ours, immediately before each
            # destructive restart. Foreign token ⇒ our lease lapsed and a
            # concurrent fleet restart now holds the lock — stop (continuing
            # would interleave stop/start with the new holder, and a bare
            # EXPIRE would extend THEIR lease). Absent token ⇒ the lease
            # lapsed with nobody racing us — re-acquire and continue; abort
            # only if the re-acquire loses. Detection latency is one in-flight
            # agent restart: this shrinks the dual-run window, it cannot
            # eliminate it (per-agent start locks are #1817).
            if lock.held:
                state = lock.refresh_if_owned()
                if state is LeaseState.REACQUIRED:
                    lease_reacquired = True
                    logger.warning(
                        "Fleet restart: lock lease lapsed unclaimed and "
                        "was re-acquired after %d of %d agents — if this "
                        "recurs, the TTL no longer covers the slowest "
                        "agent", len(results), len(agents),
                    )
                elif state is LeaseState.LOST:
                    stopped_early = "lease_lost_foreign"
                    logger.warning(
                        "Fleet restart: lock lease lost to a concurrent "
                        "caller after %d of %d agents (token state: %s) "
                        "— stopping this run; already-completed restarts "
                        "stand and are audited",
                        len(results), len(agents),
                        "foreign" if lock.last_current is not None else "absent",
                    )
                    break
                elif state is LeaseState.DEGRADED:
                    # A Redis blip is not lease loss (fail-open) — but say so
                    # once, or a full-run outage is invisible while the lease
                    # quietly expires. (The error is swallowed inside the
                    # helper, so there is no active exception to attach here.)
                    if not refresh_warned:
                        refresh_warned = True
                        logger.warning(
                            "Fleet restart: lease refresh failing open (Redis "
                            "error) — continuing; single-flight is best-effort "
                            "until Redis recovers",
                        )
                # OWNED → fall through and restart this agent.

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
                    # Platform-authored client text (FastAPI returns .detail
                    # to callers by design) — not an exception-internals leak.
                    error_text = f"{e.status_code}: {e.detail}"
                else:
                    # py/stack-trace-exposure (CodeQL, PR #1912): never flow
                    # a raw exception message into the response — docker/OS
                    # error strings can embed internals (the #1885 / git-PAT
                    # stderr class). Class name only; the full message +
                    # traceback go to the backend log line below.
                    error_text = (
                        f"restart failed ({e.__class__.__name__} — "
                        f"details in backend logs)"
                    )
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
                logger.warning(
                    f"Fleet restart: {agent_name} failed: {e}", exc_info=True
                )
    except Exception as e:
        # Abnormal exit — NOT per-agent failures (those are handled in-loop).
        # Mark the audit row so an aborted run is distinguishable from a
        # legitimately empty fleet; class name only (#1912 exposure rule).
        stopped_early = "error"
        error_class = e.__class__.__name__
        raise
    finally:
        # Sync cleanup FIRST, awaited audit LAST: a CancelledError raised at
        # the audit await (backend shutdown mid-loop) is a BaseException the
        # try below doesn't catch — anything after it in this block would be
        # skipped, leaving the lock held for a full TTL after a deploy.
        invalidate_context_stats_cache()
        # Ownership-checked compare-and-delete (#1920). Gates on the stable
        # acquire-time `held`, so it runs even after a detected LOST/DEGRADED
        # refresh: it is foreign-safe by construction (deletes only our own
        # token), and skipping it on a false "absent" (a stale replica read)
        # would abandon OUR live lock for the full TTL. No-op when the acquire
        # failed open.
        lock.release_if_owned()
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
                    # #1919 partial-run honesty: processed vs total makes a
                    # stopped run arithmetically visible; stopped_early names
                    # why (lease_lost_foreign = a concurrent operator took the
                    # lock; error = abnormal exit, class name in "error").
                    "processed": len(results),
                    "stopped_early": stopped_early,
                    "error": error_class,
                    "lease_reacquired": lease_reacquired or None,
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
            "recreated": recreated_count,
            # #1919: a lease-loss stop returns 200 with a PARTIAL fleet —
            # processed vs total + stopped_early let callers tell the
            # difference instead of reading silence as completion.
            "processed": len(results),
            "stopped_early": stopped_early,
        },
        "results": results
    }


async def stop_fleet_impl(
    request: Request,
    current_user: User,
    system_prefix: Optional[str] = Query(None, description="Only stop agents matching this system prefix")
):
    """
    Stop all agents in the fleet.

    Admin-only. Excludes the system agent.
    """
    # auth: the route gate ran assert_admin before delegating (#1028)

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
            logger.warning(
                f"Fleet stop: {agent_name} failed: {e}", exc_info=True
            )
            results.append({
                "agent": agent_name,
                "result": "failed",
                # py/stack-trace-exposure (#1917, the PR #1912 pattern).
                "error": (
                    f"stop failed ({e.__class__.__name__} — "
                    f"details in backend logs)"
                ),
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


async def emergency_stop_impl(
    request: Request,
    current_user: User,
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
    # auth: the route gate ran assert_admin before delegating (#1028)

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


