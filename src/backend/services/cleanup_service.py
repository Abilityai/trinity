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
from services.capacity_manager import get_capacity_manager
from utils.helpers import utc_now, utc_now_iso, parse_iso_timestamp
from utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)

# Configuration
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes
EXECUTION_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
ACTIVITY_STALE_TIMEOUT_MINUTES = 120  # SCHED-ASYNC-001: increased from 30 to support long-running tasks
NO_SESSION_TIMEOUT_SECONDS = 60  # Issue #106: fast-fail executions that never got a Claude session
WATCHDOG_HTTP_TIMEOUT = 15.0  # Timeout for agent HTTP calls during reconciliation (#869: increased from 5s to handle agents under load)
WATCHDOG_MIN_AGE_SECONDS = 60  # Don't orphan-recover executions younger than this (dispatch window)
STARTUP_RECOVERY_GRACE_SECONDS = 15  # #748: skip startup orphan-recovery for rows
                                     # whose started_at is within this window — they
                                     # may be from an in-flight /internal/execute-task
                                     # call that races the backend startup.
# #749: grace window for the Redis-side orphan-slot sweep. A slot whose
# ZSET score (unix seconds, recorded at ZADD time) is within this many
# seconds of "now" may belong to a concurrent /internal/execute-task
# handler that has done its ZADD but not yet inserted the SQL row.
# Symmetric with the SQL-side window in `recover_orphaned_executions`.
SLOT_RECOVERY_GRACE_SECONDS = 15
# Terminal SQL statuses that mean "the row is done; any matching Redis
# slot is a leak." Mirrors the values in models.TaskExecutionStatus.
_TERMINAL_EXECUTION_STATUSES = {"success", "failed", "cancelled", "skipped"}
# #749: members starting with this prefix are drain sentinels used by
# the capacity manager — not real executions — and must be skipped by
# the orphan-slot sweep. Matches the canary S-01 filter
# (`canary/invariants/s01_slot_row_bijection.py:DRAIN_PREFIX`).
_DRAIN_SENTINEL_PREFIX = "drain-"
ERROR_FETCH_TIMEOUT = 2.0  # Issue #286: short timeout for fetching error context from agent
MAX_ERROR_MESSAGE_LENGTH = 2000  # Issue #286: truncate combined error messages
# Issue #772: per-cycle row budget for retention sweeps. Caps the work each
# 5-min tick performs so the first post-deploy backfill (potentially GB of
# execution_log on production instances) is spread over hours, not held in a
# single multi-minute write lock. At 5000 rows/cycle the worst observed prod
# backlog (~9000 terminal rows past 30 days) drains in 2 cycles; for the
# 90-day row-delete sweep on the same fleet that's roughly one cycle.
RETENTION_CHUNK_SIZE_PER_CYCLE = 5000

# WebSocket manager (injected from main.py)
_ws_manager = None


def set_cleanup_ws_manager(manager):
    """Set the WebSocket manager for watchdog event broadcasting."""
    global _ws_manager
    _ws_manager = manager


def _extract_agent_known_ids(payload: Dict) -> set:
    """Set of execution IDs the agent considers 'known': currently-running
    plus the recently-completed window (#921).

    Single source of truth for parsing the `/api/executions/running`
    response so the periodic watchdog (`_reconcile_orphaned_executions`)
    and the startup recovery (`recover_orphaned_executions`) can't drift
    out of sync. Defensive against malformed entries and missing fields:
    older agent images that haven't shipped the buffer return only the
    `executions` field — the union degrades silently to pre-#921 behaviour.
    """
    ids = {
        eid for ex in (payload.get("executions") or [])
        if (eid := ex.get("execution_id"))
    }
    ids.update(payload.get("recently_completed_ids") or [])
    return ids


def _read_retention_settings() -> tuple[int, int, int]:
    """Read retention windows from ops settings (#772).

    Returns:
        (execution_log_retention_days, execution_row_retention_days,
         health_check_retention_days). 0 means the sweep is disabled.
        Invalid (non-integer or negative) values are coerced to 0 so a
        malformed setting can't accidentally enable an unbounded prune.
    """
    from services.settings_service import OPS_SETTINGS_DEFAULTS

    def _read(key: str) -> int:
        raw = db.get_setting_value(key, OPS_SETTINGS_DEFAULTS.get(key, "0"))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(value, 0)

    return (
        _read("execution_log_retention_days"),
        _read("execution_row_retention_days"),
        _read("health_check_retention_days"),
    )


def _wal_checkpoint_truncate() -> None:
    """Run PRAGMA wal_checkpoint(TRUNCATE) to return freed pages to the OS.

    Called after retention sweeps reclaim measurable space. TRUNCATE mode is
    safe under concurrent readers — it only blocks if another writer holds
    the lock, in which case the checkpoint returns busy and we move on.
    """
    from db.connection import get_db_connection

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # PRAGMA result is (busy, log_pages, checkpointed) — log at debug
        # only; non-zero busy is normal under contention.
        try:
            row = cursor.fetchone()
            if row is not None:
                logger.debug(
                    "[Cleanup] wal_checkpoint(TRUNCATE) "
                    f"busy={row[0]} log_pages={row[1]} checkpointed={row[2]}"
                )
        except Exception:
            pass


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
    shared_files_purged: int = 0  # C4 / FILES-001: expired or old-revoked file shares
    # Issue #772: retention sweeps
    execution_logs_pruned: int = 0
    execution_rows_pruned: int = 0
    health_checks_pruned: int = 0
    # Issue #834 Phase 1a: soft-deleted agents purged past their retention window
    soft_deleted_agents_purged: int = 0
    # Issue #834 Phase 1b: soft-deleted schedules purged past their retention window
    soft_deleted_schedules_purged: int = 0
    # RELIABILITY-006 / #525: idempotency keys purged past their 24h TTL
    idempotency_keys_purged: int = 0

    @property
    def total(self) -> int:
        return (self.orphaned_executions + self.auto_terminated +
                self.stale_executions + self.no_session_executions +
                self.orphaned_skipped + self.stale_activities + self.stale_slots +
                self.stale_slot_executions + self.shared_files_purged +
                self.execution_logs_pruned + self.execution_rows_pruned +
                self.health_checks_pruned + self.soft_deleted_agents_purged +
                self.soft_deleted_schedules_purged + self.idempotency_keys_purged)

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
            "shared_files_purged": self.shared_files_purged,
            "execution_logs_pruned": self.execution_logs_pruned,
            "execution_rows_pruned": self.execution_rows_pruned,
            "health_checks_pruned": self.health_checks_pruned,
            "soft_deleted_agents_purged": self.soft_deleted_agents_purged,
            "soft_deleted_schedules_purged": self.soft_deleted_schedules_purged,
            "idempotency_keys_purged": self.idempotency_keys_purged,
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
        # Cumulative counters for the #306 soak dashboard. Monotonic, reset on
        # process restart. Zero orphan recoveries over 2 weeks is the gate.
        self.cumulative_orphaned: int = 0
        self.cumulative_auto_terminated: int = 0
        # #476: Cycle counter gates hourly maintenance (rate-limit-event prune)
        # inside the 5-min cleanup loop. First cycle runs maintenance
        # immediately; then every 12th cycle (60 min at 5-min interval).
        self._cycle_count: int = 0

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
            self.cumulative_orphaned += orphaned
            self.cumulative_auto_terminated += terminated
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

        # 3. Cleanup stale Redis slots and fail corresponding execution records
        #    (#219, #226, #378 — see _process_stale_slot_reclaims docstring).
        try:
            capacity = get_capacity_manager()

            # #226: Query per-agent timeouts from DB so slot cleanup uses the
            # correct TTL instead of a fixed 20-min default.
            agent_timeouts = db.get_all_execution_timeouts()

            reclaimed = await capacity.reclaim_stale(
                agent_timeouts=agent_timeouts
            )
            report.stale_slots = sum(len(ids) for ids in reclaimed.values())

            await self._process_stale_slot_reclaims(
                reclaimed, confirmed_running_ids, report
            )
        except Exception as e:
            logger.error(f"[Cleanup] Error cleaning stale slots: {e}")

        # 4. Hourly maintenance: prune rate-limit events older than 24h (#476).
        # Runs every 12th cycle (60 min at 5-min interval) plus the first
        # cycle after startup so we don't wait an hour on boot.
        if self._cycle_count % 12 == 0:
            try:
                pruned = db.cleanup_old_rate_limit_events()
                if pruned > 0:
                    logger.info(f"[Cleanup] Pruned {pruned} rate-limit events (>24h old)")
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning rate-limit events: {e}")

        # 4b. Purge expired / old-revoked shared files (C4 / FILES-001).
        # Every cycle — the set is usually small and both DB row + disk
        # unlink are cheap. Grace period for revoked rows keeps them
        # queryable for a day post-revoke (incident diagnosis).
        try:
            from pathlib import Path
            stored_filenames = db.delete_expired_and_revoked_shared_files(
                revoke_grace_hours=24
            )
            if stored_filenames:
                storage_root = Path("/data/agent-files")
                unlinked = 0
                for sf in stored_filenames:
                    try:
                        p = storage_root / sf
                        if p.exists():
                            p.unlink()
                            unlinked += 1
                    except Exception as e:
                        logger.warning(f"[Cleanup] failed to unlink {sf}: {e}")
                report.shared_files_purged = len(stored_filenames)
                logger.info(
                    f"[Cleanup] Purged {len(stored_filenames)} shared-file "
                    f"rows ({unlinked} files unlinked from /data/agent-files/)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error purging shared files: {e}")

        # 4c. Issue #772: retention pruning for execution_log, execution rows,
        # and agent_health_checks. All three obey the configurable retention
        # window from ops settings; "0" disables the corresponding sweep.
        # Per-cycle budget caps each sweep so the first post-deploy backfill
        # spans multiple cycles instead of holding the write lock end-to-end.
        try:
            log_days, row_days, hc_days = _read_retention_settings()
        except Exception as e:
            logger.error(f"[Cleanup] Error reading retention settings: {e}")
            log_days = row_days = hc_days = 0

        if log_days > 0:
            try:
                pruned = db.prune_execution_logs(
                    retention_days=log_days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.execution_logs_pruned = pruned
                if pruned > 0:
                    logger.info(
                        f"[Cleanup] Nulled execution_log on {pruned} executions "
                        f"older than {log_days} days (#772)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning execution_log: {e}")

        if row_days > 0:
            try:
                pruned = db.prune_execution_rows(
                    retention_days=row_days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.execution_rows_pruned = pruned
                if pruned > 0:
                    logger.info(
                        f"[Cleanup] Deleted {pruned} schedule_executions rows "
                        f"older than {row_days} days (#772)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning execution rows: {e}")

        if hc_days > 0:
            try:
                pruned = db.cleanup_old_health_records(
                    days=hc_days,
                    chunk_size=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                report.health_checks_pruned = pruned
                if pruned > 0:
                    logger.info(
                        f"[Cleanup] Deleted {pruned} agent_health_checks rows "
                        f"older than {hc_days} days (#772)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning health checks: {e}")

        # 4c-bis. Issue #834 Phase 1a: hard-purge soft-deleted agents past
        # their retention window. `purge_agent_ownership` runs the #816
        # cascade_delete primitive so every per-agent child row goes with
        # the parent in a single transaction. Bounded by the same
        # 5000-row/cycle cap as the other sweeps so a backlog after a
        # long-disabled retention setting drains gradually.
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS

            raw_sd_days = db.get_setting_value(
                "agent_soft_delete_retention_days",
                OPS_SETTINGS_DEFAULTS.get("agent_soft_delete_retention_days", "180"),
            )
            try:
                sd_days = max(int(raw_sd_days), 0)
            except (TypeError, ValueError):
                sd_days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading agent retention setting: {e}")
            sd_days = 0

        if sd_days > 0:
            try:
                names = db.find_soft_deleted_agents_past_retention(
                    retention_days=sd_days,
                    limit=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                purged = 0
                for name in names:
                    try:
                        if db.purge_agent_ownership(name):
                            purged += 1
                    except Exception as e:
                        logger.warning(
                            f"[Cleanup] Failed to purge soft-deleted agent {name}: {e}"
                        )
                report.soft_deleted_agents_purged = purged
                if purged > 0:
                    logger.info(
                        f"[Cleanup] Hard-purged {purged} soft-deleted agent(s) "
                        f"past {sd_days}-day retention (#834)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning soft-deleted agents: {e}")

        # 4c-ter. Issue #834 Phase 1b: hard-purge soft-deleted schedules
        # past their retention window. Unlike the agent purge, this does
        # not chain into cascade_delete — schedules don't have child
        # rows registered with #816 (schedule_executions are KEEP-policy
        # via subscription_id rollups, and the per-schedule cleanup of
        # executions belongs to #772's separate sweep).
        try:
            from services.settings_service import OPS_SETTINGS_DEFAULTS

            raw_schedule_days = db.get_setting_value(
                "schedule_soft_delete_retention_days",
                OPS_SETTINGS_DEFAULTS.get("schedule_soft_delete_retention_days", "30"),
            )
            try:
                schedule_days = max(int(raw_schedule_days), 0)
            except (TypeError, ValueError):
                schedule_days = 0
        except Exception as e:
            logger.error(f"[Cleanup] Error reading schedule retention setting: {e}")
            schedule_days = 0

        if schedule_days > 0:
            try:
                ids = db.find_soft_deleted_schedules_past_retention(
                    retention_days=schedule_days,
                    limit=RETENTION_CHUNK_SIZE_PER_CYCLE,
                )
                purged = 0
                for sid in ids:
                    try:
                        if db.purge_schedule(sid):
                            purged += 1
                    except Exception as e:
                        logger.warning(
                            f"[Cleanup] Failed to purge soft-deleted schedule {sid}: {e}"
                        )
                report.soft_deleted_schedules_purged = purged
                if purged > 0:
                    logger.info(
                        f"[Cleanup] Hard-purged {purged} soft-deleted schedule(s) "
                        f"past {schedule_days}-day retention (#834 Phase 1b)"
                    )
            except Exception as e:
                logger.error(f"[Cleanup] Error pruning soft-deleted schedules: {e}")

        # 4c-quater. RELIABILITY-006 (#525): purge idempotency keys past their
        # 24h TTL. Fixed window (not an ops setting) — the contract guarantees
        # dedup for 24h, no longer. Cheap point-delete on the created_at index.
        try:
            purged = db.idempotency_purge_expired(ttl_hours=24)
            report.idempotency_keys_purged = purged
            if purged > 0:
                logger.info(
                    f"[Cleanup] Purged {purged} idempotency key(s) past 24h TTL (#525)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Error purging idempotency keys: {e}")

        # 4d. Issue #772: after a retention sweep reclaims meaningful space,
        # truncate the WAL so the OS sees the free pages. Checkpoint is cheap
        # and safe to run per-cycle when there's work to do; full VACUUM is
        # gated to a daily off-peak job (see start()).
        retention_total = (report.execution_logs_pruned
                           + report.execution_rows_pruned
                           + report.health_checks_pruned
                           + report.soft_deleted_agents_purged
                           + report.soft_deleted_schedules_purged
                           + report.idempotency_keys_purged)
        if retention_total > 0:
            try:
                _wal_checkpoint_truncate()
            except Exception as e:
                logger.warning(f"[Cleanup] WAL checkpoint failed: {e}")

        self._cycle_count += 1

        self.last_run_at = utc_now_iso()
        self.last_report = report

        if report.total > 0:
            logger.info(f"[Cleanup] Cycle complete: {report.to_dict()}")

        return report

    async def _process_stale_slot_reclaims(
        self,
        reclaimed: Dict[str, List[str]],
        confirmed_running_ids: set,
        report: CleanupReport,
    ) -> None:
        """Fail execution records whose slots were reclaimed, with just-in-time
        re-verify to prevent phantom failures (#378).

        The bug: cleanup service's Phase 3 sometimes marked executions FAILED
        with "Stale execution — slot TTL expired" even though the task was
        still running (agent had just dropped it from its registry after
        completion, so Phase 0's batch query missed it). The SUCCESS response
        then arrived after Phase 3 already wrote FAILED — user saw the flip.

        The fix:
        - Do a just-in-time re-verify with each agent RIGHT BEFORE writing
          FAILED, closing the window between Phase 0 and Phase 3.
        - Parallel fan-out via asyncio.gather (mirrors Phase 0 pattern at
          _reconcile_orphaned_executions).
        - On agent unreachable (#497): force-fail via the race-guarded
          `fail_stale_slot_execution`. The slot was reclaimed by TTL, so
          the execution is by construction older than `timeout + buffer`.
          Waiting for the 120-min Phase 1 stale cleanup was leaving DB
          rows as zombie `running` for up to 2 hours under sustained
          partial-outage conditions. The race guard preserves any
          SUCCESS that arrived between slot reclaim and this write.
        """
        if not reclaimed:
            return

        agent_names = list(reclaimed.keys())
        async with httpx.AsyncClient(timeout=WATCHDOG_HTTP_TIMEOUT) as client:
            results = await asyncio.gather(
                *(self._get_agent_running_ids(client, name) for name in agent_names),
                return_exceptions=True,
            )
            per_agent_running: Dict[str, Optional[set]] = {}
            for name, result in zip(agent_names, results):
                if isinstance(result, BaseException):
                    logger.warning(
                        f"[Cleanup] Phase 3 re-verify failed for '{name}': {result}"
                    )
                    per_agent_running[name] = None
                else:
                    # result is Optional[set] here after the BaseException branch
                    per_agent_running[name] = result

            for agent_name, execution_ids in reclaimed.items():
                running_ids = per_agent_running.get(agent_name)

                for execution_id in execution_ids:
                    # #226: Phase 0 already confirmed this exec as running —
                    # trust it to save an HTTP call.
                    if execution_id in confirmed_running_ids:
                        logger.info(
                            f"[Cleanup] Skipping {execution_id} for '{agent_name}' "
                            f"— watchdog confirmed still running"
                        )
                        continue

                    # Just-in-time re-verify interpretation
                    if running_ids is None:
                        # Agent unreachable during re-verify (#497).
                        #
                        # The slot was already reclaimed by TTL — by
                        # construction the execution is older than
                        # `timeout_seconds + buffer`, which is Phase 1's
                        # criterion at a much shorter window. Force-fail
                        # via the race-guarded writer instead of waiting
                        # the full 120-min Phase 1 stale-cleanup deadline.
                        #
                        # Race safety: `fail_stale_slot_execution` has a
                        # `WHERE status='running'` guard, so a SUCCESS
                        # that landed between the slot reclaim and this
                        # write is preserved.
                        #
                        # Documented residual risk: if the agent later
                        # recovers and writes SUCCESS via
                        # `update_execution_status`, that path overwrites
                        # FAILED per #378's design. The execution must
                        # have run past its configured `timeout + buffer`
                        # for the slot to be reclaimed in the first
                        # place, so a "late SUCCESS" here represents a
                        # deliverable that exceeded its budget.
                        try:
                            updated = db.fail_stale_slot_execution(
                                execution_id=execution_id,
                                error=(
                                    f"Stale execution — agent '{agent_name}' "
                                    f"unresponsive during cleanup re-verify, "
                                    f"slot TTL expired (#497)"
                                ),
                            )
                            if updated:
                                report.stale_slot_executions += 1
                                logger.info(
                                    f"[Cleanup] Failed execution {execution_id} for "
                                    f"agent '{agent_name}' "
                                    f"(slot reclaimed, agent unreachable during re-verify)"
                                )
                            else:
                                # Race-guard refused — a real terminal write
                                # arrived first. Expected and benign.
                                logger.debug(
                                    f"[Cleanup] fail_stale_slot_execution declined "
                                    f"for {execution_id} on '{agent_name}' "
                                    f"(race-guard — already terminal)"
                                )
                        except Exception as e:
                            logger.error(
                                f"[Cleanup] Error failing {execution_id} after slot "
                                f"reclaim (unreachable branch): {e}"
                            )
                        continue

                    if execution_id in running_ids:
                        # #378: agent says this exec is still running — the
                        # slot TTL fired prematurely relative to the task.
                        # Skip; the task's own SUCCESS/FAILED write will
                        # land correctly later.
                        logger.info(
                            f"[Cleanup] Skipping {execution_id} for '{agent_name}' "
                            f"— re-verification shows still running (#378)"
                        )
                        continue

                    # Re-verify confirmed inactive → safe to fail.
                    try:
                        # Issue #61: best-effort terminate before marking failed.
                        try:
                            await self._terminate_on_agent(
                                client, agent_name, execution_id
                            )
                        except Exception as term_err:
                            logger.debug(
                                f"[Cleanup] Could not terminate {execution_id}: {term_err}"
                            )

                        updated = db.fail_stale_slot_execution(
                            execution_id=execution_id,
                            error=f"Stale execution — slot TTL expired for agent '{agent_name}', cleaned by cleanup service",
                        )
                        if updated:
                            report.stale_slot_executions += 1
                            logger.info(
                                f"[Cleanup] Failed execution {execution_id} for agent "
                                f"'{agent_name}' (slot reclaimed)"
                            )
                    except Exception as e:
                        logger.error(
                            f"[Cleanup] Error failing {execution_id} after slot reclaim: {e}"
                        )

    async def _reconcile_orphaned_executions(self) -> tuple[int, int, set]:
        """Reconcile DB execution state against agent process registries.

        For each execution marked 'running' in the DB:
        1. Check if the agent's process registry still has it (including
           the #921 recently-completed window — see `_get_agent_running_ids`)
        2. If not found: mark failed, release resources
        3. If found but exceeded timeout: terminate, mark failed, release resources

        Issue #921: the race between the agent's `finally: unregister()`
        and the backend's `update_execution_status(SUCCESS)` is closed at
        the source — agents include recently-completed IDs in their
        `/api/executions/running` response. The watchdog therefore needs
        no two-cycle confirmation: a single observation of "missing from
        agent + DB still running" is a true orphan.

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

                            # Orphan: missing from agent's running + recently-
                            # completed sets. The agent-side window in
                            # `process_registry.list_recently_completed_ids`
                            # already absorbed the success-write race (#921),
                            # so this is a true orphan.
                            recovery_attempts += 1
                            error_msg = (
                                "Execution completed on agent but status not reported "
                                "— recovered by watchdog"
                            )
                            recovered = await self._recover_execution(
                                execution_id, agent_name, error_msg, "orphan_recovered", client
                            )
                            if recovered:
                                orphaned_count += 1
                        else:
                            # Execution is on agent — check timeout.
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
                                execution_id, agent_name, error_msg, "auto_terminated", client
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
                # #921: union of currently-running + recently-completed via
                # the shared helper — same parsing as `recover_orphaned_executions`.
                return _extract_agent_known_ids(response.json())
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

    async def _get_execution_error(
        self, client: httpx.AsyncClient, agent_name: str, execution_id: str
    ) -> Optional[str]:
        """Fetch the last error from an agent's execution log buffer.

        Issue #286: Preserves original error context when cleanup recovers
        stale executions. Queries the agent's /api/executions/{id}/last-error
        endpoint to retrieve error details before they're lost.

        Args:
            client: Shared httpx client for the reconciliation cycle.
            agent_name: The agent to query.
            execution_id: The execution to get error for.

        Returns:
            Error message string if found, None otherwise.
        """
        try:
            response = await client.get(
                f"http://agent-{agent_name}:8000/api/executions/{execution_id}/last-error",
                timeout=ERROR_FETCH_TIMEOUT,
            )
            if response.status_code == 200:
                data = response.json()
                error_type = data.get("error_type")
                error_message = data.get("error_message")

                if error_type or error_message:
                    # Sanitize to remove any credential patterns
                    parts = []
                    if error_type:
                        parts.append(f"[{error_type}]")
                    if error_message:
                        sanitized = sanitize_text(error_message)
                        parts.append(sanitized)
                    return " ".join(parts) if parts else None

            return None
        except (httpx.ConnectError, httpx.TimeoutException):
            logger.debug(
                f"[Watchdog] Could not fetch error context for {execution_id} "
                f"from agent '{agent_name}' (unreachable)"
            )
            return None
        except Exception as e:
            logger.debug(
                f"[Watchdog] Error fetching error context for {execution_id}: {e}"
            )
            return None

    async def _recover_execution(
        self,
        execution_id: str,
        agent_name: str,
        error_msg: str,
        action: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> bool:
        """Mark execution as failed and release all associated resources.

        Shared helper for both orphan recovery and auto-terminate paths (DRY).

        Issue #286: Now attempts to fetch original error context from the agent
        before marking the execution as failed, preserving diagnostic info.

        Args:
            execution_id: The execution to recover.
            agent_name: The agent the execution belongs to.
            error_msg: Descriptive error message (cleanup reason).
            action: Event action type ("orphan_recovered" or "auto_terminated").
            client: Optional httpx client for fetching error context from agent.

        Returns:
            True if recovery succeeded, False if execution already transitioned.
        """
        # Issue #286: Try to fetch original error context from agent before marking failed
        original_error = None
        if client:
            original_error = await self._get_execution_error(client, agent_name, execution_id)

        # Combine original error with cleanup reason
        if original_error:
            combined_error = f"{original_error}. Cleanup: {error_msg}"
        else:
            combined_error = error_msg

        # Truncate to prevent DB bloat
        if len(combined_error) > MAX_ERROR_MESSAGE_LENGTH:
            combined_error = combined_error[:MAX_ERROR_MESSAGE_LENGTH - 3] + "..."

        updated = db.mark_execution_failed_by_watchdog(execution_id, combined_error)
        if not updated:
            # Race condition: execution completed normally between check and update
            return False

        # Release capacity (idempotent — no error if already released).
        # CAPACITY-CONSOLIDATE (#428): single CapacityManager.release_if_matches
        # replaces the prior slot_service.release_slot + queue.force_release_if_matches
        # pair. The match check preserves the TOCTOU-safety the original Lua
        # script provided.
        try:
            capacity = get_capacity_manager()
            await capacity.release_if_matches(agent_name, execution_id)
        except Exception as e:
            logger.warning(f"[Watchdog] Error releasing capacity for {execution_id}: {e}")

        # Broadcast WebSocket event with combined error (includes original context)
        await self._broadcast_watchdog_event(action, agent_name, execution_id, combined_error)

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
        # One-shot startup hook for #740: any non-terminal agent_loops left
        # over from a prior process get marked `interrupted`. Loops do not
        # auto-resume. Runs once on boot, not every cycle.
        try:
            interrupted = db.mark_orphan_loops_interrupted()
            if interrupted > 0:
                logger.info(
                    f"[Cleanup] Startup: marked {interrupted} orphan agent_loops as interrupted (#740)"
                )
        except Exception as e:
            logger.error(f"[Cleanup] Loop orphan sweep error: {e}")

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


# ---------------------------------------------------------------------------
# Startup-recovery readiness flag (#748)
# ---------------------------------------------------------------------------
# Set False at module load; flipped True at the end of
# recover_orphaned_executions(). The /internal/execute-task router returns
# 503 while False so the scheduler retries instead of racing recovery with
# a slot ZADD on a row that's about to be flipped to FAILED.

_startup_recovery_complete: bool = False


def is_startup_recovery_complete() -> bool:
    """Return True once startup orphan-recovery has finished (#748).

    The internal task-execution route uses this as a gate: while False, the
    backend is still in the window where startup recovery may flip in-flight
    rows to FAILED. Returning 503 lets the scheduler retry once the gate
    opens, instead of leaking a Redis capacity slot on the doomed row.
    """
    return _startup_recovery_complete


def mark_startup_recovery_complete() -> None:
    """Set the warming-up gate to admit /internal/execute-task calls (#748)."""
    global _startup_recovery_complete
    _startup_recovery_complete = True


def reset_startup_recovery_flag_for_tests() -> None:
    """Test-only helper: revert the gate to its pre-recovery state."""
    global _startup_recovery_complete
    _startup_recovery_complete = False


async def recover_orphaned_executions() -> Dict:
    """Recover orphaned task executions on backend startup.

    Two passes:

      1. SQL→Redis (legacy): for each 'running' schedule execution row,
         check the agent's container and process registry — if missing,
         mark the row failed and release any capacity slot.
      2. Redis→SQL (#749): scan ``agent:slots:*`` for members whose SQL
         row is terminal or missing and ZREM them. Necessary because a
         backend kill between slot ZADD and the finally-block ZREM leaves
         the slot leaked, and the SQL→Redis pass cannot see Redis-only
         orphans.

    #748: rows whose ``started_at`` is younger than
    ``STARTUP_RECOVERY_GRACE_SECONDS`` are *skipped* — they may be from a
    ``/internal/execute-task`` call that the scheduler queued while the
    backend was still booting and that is about to ZADD a capacity slot.
    Failing such a row would race the handler and leave a permanently
    leaked Redis slot. The skipped row is handled either by the regular
    watchdog cycle (which uses the same grace window) or by the now-late
    handler completing normally.

    Returns:
        Dict with recovered, still_running, skipped_grace, errors, and
        redis_slots_reclaimed counts.
    """
    from services.agent_client import AgentClientError, get_agent_client
    from services.docker_service import get_agent_container

    running = db.get_running_executions()
    if not running:
        # #749: still run the Redis-side sweep — orphan slots can exist
        # even when SQL has zero running rows (that is in fact the
        # textbook symptom of the kill-between-ZADD-and-ZREM bug).
        redis_reclaimed = await _reconcile_orphaned_slots()
        return {
            "recovered": 0,
            "still_running": 0,
            "skipped_grace": 0,
            "errors": 0,
            "redis_slots_reclaimed": sum(redis_reclaimed.values()),
        }

    capacity = get_capacity_manager()

    # Group by agent to minimize container/HTTP checks
    by_agent: Dict[str, list] = {}
    for execution in running:
        by_agent.setdefault(execution["agent_name"], []).append(execution)

    recovered = 0
    still_running = 0
    skipped_grace = 0
    errors = 0

    for agent_name, executions in by_agent.items():
        # Check if container is running
        container = get_agent_container(agent_name)
        if not container or container.status != "running":
            # Container down — all executions for this agent are orphaned
            for execution in executions:
                if _within_startup_grace(execution):
                    skipped_grace += 1
                    continue
                if await _recover_execution(execution, agent_name, capacity):
                    recovered += 1
                else:
                    errors += 1
            continue

        # Container is up — check agent's process registry
        registry_ids: set = set()
        try:
            client = get_agent_client(agent_name)
            resp = await client.get("/api/executions/running", timeout=5.0)
            if resp.status_code == 200:
                # #921: same union as the periodic watchdog — includes
                # recently-completed IDs so a backend restart that races
                # an in-flight completion doesn't false-orphan it.
                registry_ids = _extract_agent_known_ids(resp.json())
        except AgentClientError as e:
            logger.warning(f"[Recovery] Could not reach agent {agent_name} registry: {e}")

        for execution in executions:
            if execution["id"] in registry_ids:
                still_running += 1
            elif _within_startup_grace(execution):
                skipped_grace += 1
            else:
                if await _recover_execution(execution, agent_name, capacity):
                    recovered += 1
                else:
                    errors += 1

    logger.info(
        f"[Recovery] Task execution recovery complete: "
        f"recovered={recovered}, still_running={still_running}, "
        f"skipped_grace={skipped_grace}, errors={errors}"
    )

    # #749: complete the asymmetric pair. The SQL→Redis pass above flips
    # SQL rows to FAILED when Redis lost their slot; the Redis→SQL pass
    # below ZREMs slots whose SQL row is terminal or missing (e.g. backend
    # killed between ZADD and ZREM). Without this pass the leaked slot
    # persists until 1200s TTL or the next acquire on this agent.
    redis_reclaimed = await _reconcile_orphaned_slots()
    redis_reclaimed_total = sum(redis_reclaimed.values())

    return {
        "recovered": recovered,
        "still_running": still_running,
        "skipped_grace": skipped_grace,
        "errors": errors,
        "redis_slots_reclaimed": redis_reclaimed_total,
    }


def _within_startup_grace(execution: Dict) -> bool:
    """Return True if the execution's started_at is within the startup grace window.

    Mirrors the WATCHDOG_MIN_AGE_SECONDS pattern at the regular-cycle path
    (cleanup_service.py:609). Rows are skipped instead of failed during
    startup recovery so an in-flight ``/internal/execute-task`` call cannot
    race the recovery flip and leak a slot (#748).
    """
    raw = execution.get("started_at")
    if not raw:
        # No timestamp — be conservative and allow recovery to proceed.
        return False
    try:
        age_seconds = (utc_now() - parse_iso_timestamp(raw)).total_seconds()
    except Exception:
        return False
    return age_seconds < STARTUP_RECOVERY_GRACE_SECONDS


async def _recover_execution(execution: Dict, agent_name: str, capacity) -> bool:
    """Mark a single execution as orphaned and release its capacity. Returns True on success."""
    try:
        # Use the guarded writer so a real completion that arrived during restart
        # is not overwritten (RELIABILITY-005).
        db.mark_execution_failed_by_watchdog(
            execution_id=execution["id"],
            error_message="Execution orphaned — recovered on backend restart",
        )
        await capacity.release(agent_name, execution["id"])
        return True
    except Exception as e:
        logger.error(f"[Recovery] Error recovering execution {execution['id']}: {e}")
        return False


async def _reconcile_orphaned_slots() -> Dict[str, int]:
    """Sweep Redis for slot members that have no live SQL execution (#749).

    SQL→Redis recovery (the existing path in `recover_orphaned_executions`)
    flips SQL rows to FAILED when Redis lost their slot. It is asymmetric —
    it doesn't catch the inverse leak, where Redis still holds a slot for an
    execution whose SQL row is either terminal (someone wrote SUCCESS /
    FAILED already) or missing entirely. That happens whenever the backend
    is killed between `capacity.acquire()` (ZADD) and the `finally`-block
    `capacity.release()` (ZREM): the in-flight handler dies, the slot
    stays. The canary S-01 invariant (Issue #411) detects exactly this
    shape; rows #26/#27/#28 in `canary_violations` reproduced it three
    cycles in a row during the same incident as #748.

    For each Redis slot member we:

      - skip drain sentinels (members starting with ``drain-``) — they
        are not executions;
      - skip members whose ZSET score is within
        ``SLOT_RECOVERY_GRACE_SECONDS`` of "now" — they may belong to a
        concurrent /internal/execute-task handler that has done its ZADD
        but not yet committed the SQL row (mirrors the SQL-side grace
        window in #748);
      - look up the execution by id in SQL: if the row is missing or its
        status is terminal (success/failed/cancelled/skipped), the slot
        is orphaned → ZREM the member and DELETE its metadata key.

    Returns a dict ``{agent_name: int}`` counting reclaimed slots per
    agent. Never raises — Redis-unreachable errors are logged and the
    call returns whatever was reclaimed before the failure.
    """
    import time

    from services.slot_service import get_slot_service

    try:
        slot_service = get_slot_service()
    except Exception as e:
        logger.error(f"[Recovery] Slot service unavailable; skipping Redis sweep: {e}")
        return {}

    redis_client = slot_service.redis
    prefix = slot_service.slots_prefix
    grace_cutoff = time.time() - SLOT_RECOVERY_GRACE_SECONDS

    reclaimed: Dict[str, int] = {}

    # SCAN the agent:slots:* keyspace (matches canary/snapshot.py:325 and
    # slot_service.cleanup_stale_slots — same SCAN pattern, count=200 to
    # keep network round-trips low under fleet scale).
    cursor = 0
    try:
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=f"{prefix}*", count=200)
            for key in keys:
                # decode_responses=True → key is str.
                agent_name = key[len(prefix):]
                try:
                    members_with_scores = redis_client.zrange(
                        key, 0, -1, withscores=True
                    )
                except Exception as exc:
                    logger.warning(
                        f"[Recovery] ZRANGE failed for {key}: {exc}"
                    )
                    continue

                for execution_id, score in members_with_scores:
                    if execution_id.startswith(_DRAIN_SENTINEL_PREFIX):
                        continue
                    if float(score) >= grace_cutoff:
                        # In the grace window — may be an in-flight ZADD
                        # whose SQL row hasn't been written yet.
                        continue

                    row = db.get_execution(execution_id)
                    if row is not None and row.status not in _TERMINAL_EXECUTION_STATUSES:
                        # Still active — leave the slot alone.
                        continue

                    # Orphan: SQL row missing OR terminal. Reclaim the slot.
                    try:
                        removed = redis_client.zrem(key, execution_id)
                        if removed:
                            metadata_key = slot_service._metadata_key(
                                agent_name, execution_id
                            )
                            redis_client.delete(metadata_key)
                            reclaimed[agent_name] = reclaimed.get(agent_name, 0) + 1
                            logger.info(
                                f"[Recovery] Reclaimed orphan slot: agent='{agent_name}' "
                                f"execution_id='{execution_id}' "
                                f"sql_status={'<missing>' if row is None else row.status}"
                            )
                    except Exception as exc:
                        logger.warning(
                            f"[Recovery] ZREM failed for {key}/{execution_id}: {exc}"
                        )

            if cursor == 0:
                break
    except Exception as e:
        # SCAN itself blew up — return partial results.
        logger.error(f"[Recovery] Redis SCAN failed during orphan-slot sweep: {e}")

    total = sum(reclaimed.values())
    if total:
        logger.info(
            f"[Recovery] Orphan-slot sweep reclaimed {total} slot(s) across "
            f"{len(reclaimed)} agent(s)"
        )
    return reclaimed
