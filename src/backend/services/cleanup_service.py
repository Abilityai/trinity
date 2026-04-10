"""
Cleanup Service for Trinity platform.

Background service that periodically cleans up stale resources:
- Reconciles DB execution state against agent process registries (Issue #129)
- Auto-terminates executions exceeding their schedule timeout (Issue #129)
- Marks stale executions (running > threshold) as failed
- Marks stale activities (started > threshold) as failed
- Cleans up stale Redis slots

Runs every 5 minutes with a one-shot startup sweep.
"""

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Dict, List

import httpx

from database import db
from models import TaskExecutionStatus
from services.slot_service import get_slot_service
from services.execution_queue import get_execution_queue
from utils.helpers import utc_now, utc_now_iso, parse_iso_timestamp

logger = logging.getLogger(__name__)

# Configuration
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes
EXECUTION_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
ACTIVITY_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
NO_SESSION_TIMEOUT_SECONDS = 60  # Issue #106: fast-fail executions that never got a Claude session
WATCHDOG_HTTP_TIMEOUT = 5.0  # Timeout for agent HTTP calls during reconciliation
WATCHDOG_MIN_AGE_SECONDS = 60  # Don't orphan-recover executions younger than this (dispatch window)

# WebSocket manager (injected from main.py)
_ws_manager = None


def set_cleanup_ws_manager(manager):
    """Set the WebSocket manager for watchdog event broadcasting."""
    global _ws_manager
    _ws_manager = manager


@dataclass
class CleanupReport:
    """Results from a single cleanup cycle."""
    orphaned_executions: int = 0
    auto_terminated: int = 0
    stale_executions: int = 0
    no_session_executions: int = 0
    orphaned_skipped: int = 0
    stale_activities: int = 0
    stale_slots: int = 0
    stale_slot_executions: int = 0  # Issue #219: executions failed when their slot was reclaimed

    @property
    def total(self) -> int:
        return (self.orphaned_executions + self.auto_terminated +
                self.stale_executions + self.no_session_executions +
                self.orphaned_skipped + self.stale_activities + self.stale_slots +
                self.stale_slot_executions)

    def to_dict(self) -> Dict:
        return {
            "orphaned_executions": self.orphaned_executions,
            "auto_terminated": self.auto_terminated,
            "stale_executions": self.stale_executions,
            "no_session_executions": self.no_session_executions,
            "orphaned_skipped": self.orphaned_skipped,
            "stale_activities": self.stale_activities,
            "stale_slots": self.stale_slots,
            "stale_slot_executions": self.stale_slot_executions,
            "total": self.total,
        }


class CleanupService:
    """Background service that cleans up stale resources."""

    def __init__(self, poll_interval: int = CLEANUP_INTERVAL_SECONDS):
        self.poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self.last_run_at: Optional[str] = None
        self.last_report: Optional[CleanupReport] = None

    def start(self):
        """Start the background cleanup loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"Cleanup service started (interval={self.poll_interval}s)")

    def stop(self):
        """Stop the background cleanup loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Cleanup service stopped")

    async def run_cleanup(self) -> CleanupReport:
        """Run a single cleanup cycle. Called by loop and on startup."""
        if self._lock.locked():
            logger.debug("[Cleanup] Cycle already in progress, skipping")
            return self.last_report or CleanupReport()
        async with self._lock:
            return await self._run_cleanup_inner()

    async def _run_cleanup_inner(self) -> CleanupReport:
        """Inner cleanup logic, called under lock."""
        report = CleanupReport()

        # 0. Watchdog: reconcile DB vs agent process registries (Issue #129)
        # Runs FIRST so it can release resources before stale cleanup marks
        # executions failed without resource cleanup.
        # Also returns confirmed_running_ids (#226) to prevent slot cleanup
        # from falsely failing executions the watchdog verified as alive.
        confirmed_running_ids: set = set()
        try:
            orphaned, terminated, confirmed_running_ids = (
                await self._reconcile_orphaned_executions()
            )
            report.orphaned_executions = orphaned
            report.auto_terminated = terminated
            if orphaned > 0:
                logger.info(f"[Watchdog] Recovered {orphaned} orphaned executions")
            if terminated > 0:
                logger.info(f"[Watchdog] Auto-terminated {terminated} timed-out executions")
        except Exception as e:
            logger.error(f"[Watchdog] Reconciliation error: {e}")

        # 1. Mark stale executions as failed (safety net for agent-unreachable cases)
        try:
            count = db.mark_stale_executions_failed(EXECUTION_STALE_TIMEOUT_MINUTES)
            report.stale_executions = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} stale executions as failed")
        except Exception as e:
            logger.error(f"[Cleanup] Error marking stale executions: {e}")

        # 1b. Fast-fail running executions with no Claude session (Issue #106)
        try:
            count = db.mark_no_session_executions_failed(NO_SESSION_TIMEOUT_SECONDS)
            report.no_session_executions = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} no-session executions as failed")
        except Exception as e:
            logger.error(f"[Cleanup] Error marking no-session executions: {e}")

        # 1c. Finalize orphaned skipped executions (Issue #106)
        try:
            count = db.finalize_orphaned_skipped_executions()
            report.orphaned_skipped = count
            if count > 0:
                logger.info(f"[Cleanup] Finalized {count} orphaned skipped executions")
        except Exception as e:
            logger.error(f"[Cleanup] Error finalizing orphaned skipped executions: {e}")

        # 2. Mark stale activities as failed
        try:
            count = db.mark_stale_activities_failed(ACTIVITY_STALE_TIMEOUT_MINUTES)
            report.stale_activities = count
            if count > 0:
                logger.info(f"[Cleanup] Marked {count} stale activities as failed")
        except Exception as e:
            logger.error(f"[Cleanup] Error marking stale activities: {e}")

        # 3. Cleanup stale Redis slots and fail corresponding execution records (#219, #226)
        try:
            slot_service = get_slot_service()

            # #226: Query per-agent timeouts from DB so slot cleanup uses the
            # correct TTL instead of a fixed 20-min default.
            agent_timeouts = db.get_all_execution_timeouts()

            reclaimed = await slot_service.cleanup_stale_slots(
                agent_timeouts=agent_timeouts
            )
            report.stale_slots = sum(len(ids) for ids in reclaimed.values())

            # Fail execution records whose slots were reclaimed,
            # but skip IDs the watchdog confirmed as still running (#226).
            for agent_name, execution_ids in reclaimed.items():
                for execution_id in execution_ids:
                    if execution_id in confirmed_running_ids:
                        logger.info(
                            f"[Cleanup] Skipping execution {execution_id} for agent "
                            f"'{agent_name}' — watchdog confirmed still running"
                        )
                        continue
                    try:
                        updated = db.fail_stale_slot_execution(
                            execution_id=execution_id,
                            error=f"Stale execution — slot TTL expired for agent '{agent_name}', cleaned by cleanup service",
                        )
                        if updated:
                            report.stale_slot_executions += 1
                            logger.info(
                                f"[Cleanup] Failed execution {execution_id} for agent '{agent_name}' (slot reclaimed)"
                            )
                    except Exception as e:
                        logger.error(
                            f"[Cleanup] Error failing execution {execution_id} after slot reclaim: {e}"
                        )
        except Exception as e:
            logger.error(f"[Cleanup] Error cleaning stale slots: {e}")

        self.last_run_at = utc_now_iso()
        self.last_report = report

        if report.total > 0:
            logger.info(f"[Cleanup] Cycle complete: {report.to_dict()}")

        return report

    async def _reconcile_orphaned_executions(self) -> tuple[int, int, set]:
        """Reconcile DB execution state against agent process registries.

        For each execution marked 'running' in the DB:
        1. Check if the agent's process registry still has it
        2. If not found (orphaned): mark failed, release resources
        3. If found but exceeded timeout: terminate, mark failed, release resources

        Returns:
            Tuple of (orphaned_count, auto_terminated_count, confirmed_running_ids)
            where confirmed_running_ids is the set of execution IDs verified as still
            running on their agents (used by slot cleanup to avoid false failures, #226).
        """
        running_executions = db.get_running_executions_with_agent_info()
        if not running_executions:
            return (0, 0, set())

        # Group by agent for batch HTTP calls (one call per agent)
        agents: Dict[str, List[Dict]] = defaultdict(list)
        for ex in running_executions:
            agents[ex["agent_name"]].append(ex)

        # Parallel fan-out: query all agents concurrently with a shared client
        async with httpx.AsyncClient(timeout=WATCHDOG_HTTP_TIMEOUT) as client:
            agent_names = list(agents.keys())
            results = await asyncio.gather(
                *(self._get_agent_running_ids(client, name) for name in agent_names),
                return_exceptions=True,
            )
            agent_running: Dict[str, Optional[set]] = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, Exception):
                    logger.warning(f"[Watchdog] Error querying agent '{name}': {result}")
                    agent_running[name] = None
                else:
                    agent_running[name] = result

            orphaned_count = 0
            terminated_count = 0
            recovery_attempts = 0
            recovery_failures = 0
            confirmed_running: set = set()  # #226: track IDs verified as still running

            for agent_name, executions in agents.items():
                agent_running_ids = agent_running.get(agent_name)
                if agent_running_ids is None:
                    # Agent unreachable — skip entirely, retry next cycle
                    continue

                for ex in executions:
                    try:
                        execution_id = ex["id"]
                        is_on_agent = execution_id in agent_running_ids

                        # Compute age for both orphan grace period and timeout checks
                        started_at = parse_iso_timestamp(ex["started_at"])
                        age_seconds = (utc_now() - started_at).total_seconds()

                        if not is_on_agent:
                            # Skip very recent executions that may still be dispatching
                            if age_seconds < WATCHDOG_MIN_AGE_SECONDS:
                                logger.debug(
                                    f"[Watchdog] Skipping {execution_id} — only "
                                    f"{int(age_seconds)}s old, may still be dispatching"
                                )
                                continue

                            # Orphan: execution not found on agent
                            recovery_attempts += 1
                            error_msg = (
                                "Execution completed on agent but status not reported "
                                "— recovered by watchdog"
                            )
                            recovered = await self._recover_execution(
                                execution_id, agent_name, error_msg, "orphan_recovered"
                            )
                            if recovered:
                                orphaned_count += 1
                        else:
                            # Execution is on agent — check timeout
                            timeout_seconds = ex.get("timeout_seconds") or 900

                            if age_seconds <= timeout_seconds:
                                # Still running within timeout — mark as confirmed (#226)
                                confirmed_running.add(execution_id)
                                continue

                            # Auto-terminate: exceeded schedule timeout
                            recovery_attempts += 1
                            age_minutes = int(age_seconds / 60)
                            terminated = await self._terminate_on_agent(
                                client, agent_name, execution_id
                            )
                            if not terminated:
                                # Process may still be running — skip DB/resource
                                # cleanup, let the 120-min stale cleanup handle it
                                logger.warning(
                                    f"[Watchdog] Terminate failed for {execution_id} "
                                    f"on '{agent_name}' — deferring to stale cleanup"
                                )
                                continue
                            error_msg = (
                                f"Execution auto-terminated after {age_minutes} minutes "
                                f"by watchdog (exceeded timeout of {timeout_seconds}s)"
                            )
                            recovered = await self._recover_execution(
                                execution_id, agent_name, error_msg, "auto_terminated"
                            )
                            if recovered:
                                terminated_count += 1

                    except Exception as e:
                        recovery_failures += 1
                        logger.error(
                            f"[Watchdog] Error recovering execution {ex.get('id', '?')} "
                            f"on agent '{agent_name}': {e}"
                        )

        # Systemic failure detection: warn if majority of recoveries failed
        if recovery_attempts > 0 and recovery_failures > recovery_attempts / 2:
            logger.warning(
                f"[Watchdog] Systemic failure: {recovery_failures}/{recovery_attempts} "
                f"recovery attempts failed in this cycle"
            )

        return (orphaned_count, terminated_count, confirmed_running)

    async def _get_agent_running_ids(
        self, client: httpx.AsyncClient, agent_name: str
    ) -> Optional[set]:
        """Get the set of execution IDs currently running on an agent.

        Args:
            client: Shared httpx client for the reconciliation cycle.
            agent_name: The agent to query.

        Returns:
            Set of execution IDs, or None if agent is unreachable.
        """
        try:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/executions/running"
            )
            if response.status_code == 200:
                data = response.json()
                executions = data.get("executions", [])
                return {eid for ex in executions if (eid := ex.get("execution_id"))}
            else:
                logger.warning(
                    f"[Watchdog] Agent '{agent_name}' returned {response.status_code} "
                    f"from /api/executions/running"
                )
                return None
        except (httpx.ConnectError, httpx.TimeoutException):
            logger.debug(f"[Watchdog] Agent '{agent_name}' unreachable, skipping")
            return None
        except Exception as e:
            logger.warning(f"[Watchdog] Error checking agent '{agent_name}': {e}")
            return None

    async def _recover_execution(
        self,
        execution_id: str,
        agent_name: str,
        error_msg: str,
        action: str,
    ) -> bool:
        """Mark execution as failed and release all associated resources.

        Shared helper for both orphan recovery and auto-terminate paths (DRY).

        Args:
            execution_id: The execution to recover.
            agent_name: The agent the execution belongs to.
            error_msg: Descriptive error message.
            action: Event action type ("orphan_recovered" or "auto_terminated").

        Returns:
            True if recovery succeeded, False if execution already transitioned.
        """
        updated = db.mark_execution_failed_by_watchdog(execution_id, error_msg)
        if not updated:
            # Race condition: execution completed normally between check and update
            return False

        # Release capacity slot (idempotent — no error if already released)
        try:
            slot_service = get_slot_service()
            await slot_service.release_slot(agent_name, execution_id)
        except Exception as e:
            logger.warning(f"[Watchdog] Error releasing slot for {execution_id}: {e}")

        # Atomically release execution queue only if THIS execution holds the slot.
        # Uses Lua script to prevent TOCTOU race where a new execution could start
        # between checking and releasing.
        try:
            queue = get_execution_queue()
            await queue.force_release_if_matches(agent_name, execution_id)
        except Exception as e:
            logger.warning(f"[Watchdog] Error releasing queue for agent '{agent_name}': {e}")

        # Broadcast WebSocket event
        await self._broadcast_watchdog_event(action, agent_name, execution_id, error_msg)

        logger.info(
            f"[Watchdog] {action}: execution {execution_id} on agent '{agent_name}'"
        )
        return True

    async def _terminate_on_agent(
        self, client: httpx.AsyncClient, agent_name: str, execution_id: str
    ) -> bool:
        """Terminate an execution on an agent.

        Calls POST /api/executions/{id}/terminate on the agent.
        Returns True if the agent confirmed termination (HTTP 2xx),
        False otherwise. Callers should only proceed with DB/resource
        cleanup if termination succeeded.
        """
        try:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/executions/{execution_id}/terminate"
            )
            if response.status_code < 300:
                return True
            logger.warning(
                f"[Watchdog] Terminate returned {response.status_code} for "
                f"{execution_id} on '{agent_name}'"
            )
            return False
        except Exception as e:
            logger.warning(
                f"[Watchdog] Failed to terminate execution {execution_id} "
                f"on agent '{agent_name}': {e}"
            )
            return False

    async def _broadcast_watchdog_event(
        self,
        action: str,
        agent_name: str,
        execution_id: str,
        reason: str,
    ) -> None:
        """Broadcast a watchdog recovery event via WebSocket."""
        if _ws_manager is None:
            logger.debug("[Watchdog] WebSocket manager not set — recovery event not broadcast")
            return

        event = json.dumps({
            "type": "watchdog_recovery",
            "agent_name": agent_name,
            "execution_id": execution_id,
            "action": action,
            "reason": reason,
            "timestamp": utc_now_iso(),
        })
        try:
            await _ws_manager.broadcast(event)
        except Exception as e:
            logger.debug(f"[Watchdog] WebSocket broadcast error: {e}")

    async def _cleanup_loop(self):
        """Main cleanup loop."""
        # Run initial cleanup on startup
        try:
            startup_report = await self.run_cleanup()
            if startup_report.total > 0:
                logger.info(f"[Cleanup] Startup sweep: {startup_report.to_dict()}")
            else:
                logger.info("[Cleanup] Startup sweep: no stale resources found")
        except Exception as e:
            logger.error(f"[Cleanup] Startup sweep error: {e}")

        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

            try:
                await self.run_cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cleanup] Cycle error: {e}")


# Global service instance
cleanup_service = CleanupService()


async def recover_orphaned_executions() -> Dict:
    """Recover orphaned task executions on backend startup.

    Checks each 'running' schedule execution against the agent's container
    and process registry. Executions not found on the agent are marked failed
    and their capacity slots released.

    Returns:
        Dict with recovered, still_running, and errors counts.
    """
    from services.agent_client import AgentClientError, get_agent_client
    from services.docker_service import get_agent_container

    running = db.get_running_executions()
    if not running:
        return {"recovered": 0, "still_running": 0, "errors": 0}

    slot_service = get_slot_service()

    # Group by agent to minimize container/HTTP checks
    by_agent: Dict[str, list] = {}
    for execution in running:
        by_agent.setdefault(execution["agent_name"], []).append(execution)

    recovered = 0
    still_running = 0
    errors = 0

    for agent_name, executions in by_agent.items():
        # Check if container is running
        container = get_agent_container(agent_name)
        if not container or container.status != "running":
            # Container down — all executions for this agent are orphaned
            for execution in executions:
                if await _recover_execution(execution, agent_name, slot_service):
                    recovered += 1
                else:
                    errors += 1
            continue

        # Container is up — check agent's process registry
        registry_ids: set[str] = set()
        try:
            client = get_agent_client(agent_name)
            resp = await client.get("/api/executions/running", timeout=5.0)
            if resp.status_code == 200:
                registry_ids = {
                    e["execution_id"] for e in resp.json().get("executions", [])
                }
        except AgentClientError as e:
            logger.warning(f"[Recovery] Could not reach agent {agent_name} registry: {e}")

        for execution in executions:
            if execution["id"] in registry_ids:
                still_running += 1
            else:
                if await _recover_execution(execution, agent_name, slot_service):
                    recovered += 1
                else:
                    errors += 1

    logger.info(
        f"[Recovery] Task execution recovery complete: "
        f"recovered={recovered}, still_running={still_running}, errors={errors}"
    )
    return {"recovered": recovered, "still_running": still_running, "errors": errors}


async def _recover_execution(execution: Dict, agent_name: str, slot_service) -> bool:
    """Mark a single execution as orphaned and release its slot. Returns True on success."""
    try:
        db.update_execution_status(
            execution_id=execution["id"],
            status=TaskExecutionStatus.FAILED,
            error="Execution orphaned — recovered on backend restart",
        )
        await slot_service.release_slot(agent_name, execution["id"])
        return True
    except Exception as e:
        logger.error(f"[Recovery] Error recovering execution {execution['id']}: {e}")
        return False
