"""
Core scheduler service for Trinity platform.

Manages scheduled task execution for agents using APScheduler.
Uses distributed locks to prevent duplicate executions.
"""

import asyncio
import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobExecutionEvent
from croniter import croniter
import pytz
import redis

import httpx

from .config import config
from .models import Schedule, ScheduleExecution, ExecutionStatus, SchedulerStatus, ProcessSchedule
from .database import SchedulerDatabase
from .locking import get_lock_manager, LockManager

logger = logging.getLogger(__name__)


# Substrings indicating an auth-class subscription failure. Mirrors
# `src/backend/services/subscription_auto_switch.py::AUTH_INDICATORS` —
# the scheduler runs in a separate container and cannot import from
# backend.services, so this list is duplicated by necessity. Keep the
# two in sync when editing either side.
_AUTH_INDICATORS = [
    "credit balance",
    "unauthorized",
    "authentication",
    "credentials",
    "forbidden",
    "401",
    "403",
    "oauth",
    "token expired",
    "not authenticated",
]

# #904: see `subscription_auto_switch.NON_AUTH_KILL_MARKERS` — same list,
# duplicated here because the scheduler runs in a separate container and
# can't import from backend.services. Keep in sync.
_NON_AUTH_KILL_MARKERS = [
    "sigkill",
    "sigterm",
    "sigint",
    "exit code -9",
    "exit code -15",
    "exit code -2",
    "exit code 137",
    "exit code 143",
    "exit code 130",
    "terminated by",
    "killed by",
    "out of memory",
    "oom",
    "memory cgroup",
]


def _is_auth_failure(error_msg: str) -> bool:
    """Return True if `error_msg` matches any AUTH_INDICATORS substring AND
    does not contain an unambiguous signal-kill / OOM / timeout marker
    (#904)."""
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    if any(marker in error_lower for marker in _NON_AUTH_KILL_MARKERS):
        return False
    return any(ind in error_lower for ind in _AUTH_INDICATORS)


# #913: Polling-deadline fallback when the schedule's timeout_seconds is
# NULL (= "inherit per-agent value"). The scheduler does not have the
# per-agent value in this process; the backend enforces the real timeout.
# 7200s matches the upper clamp of PUT /api/agents/{name}/timeout — any
# per-agent value is bounded by this, so the scheduler's poll deadline
# never gives up before the backend itself does.
_POLL_DEADLINE_WHEN_NULL = 7200


class SchedulerService:
    """
    Manages scheduled task execution for agents.

    Key features:
    - APScheduler with AsyncIO for non-blocking execution
    - Distributed locking to prevent duplicate executions
    - Event publishing for WebSocket compatibility
    - Periodic sync to detect new/updated/deleted schedules
    """

    def __init__(
        self,
        database: SchedulerDatabase = None,
        lock_manager: LockManager = None,
        redis_url: str = None
    ):
        """
        Initialize the scheduler service.

        Args:
            database: Database access instance
            lock_manager: Distributed lock manager
            redis_url: Redis URL for event publishing
        """
        self.db = database or SchedulerDatabase()
        self.lock_manager = lock_manager or get_lock_manager()
        self.redis_url = redis_url or config.redis_url

        self.scheduler: Optional[AsyncIOScheduler] = None
        self._redis: Optional[redis.Redis] = None
        self._initialized = False
        self._start_time: Optional[datetime] = None
        self._instance_id: str = f"scheduler-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        # Schedule state snapshots for sync detection
        # Maps schedule_id -> (enabled, updated_at_iso)
        self._schedule_snapshot: Dict[str, tuple] = {}
        self._process_schedule_snapshot: Dict[str, tuple] = {}

        # Issue #132: Track active background polling tasks for graceful shutdown
        self._active_poll_tasks: set = set()

    @property
    def redis(self) -> redis.Redis:
        """Get or create Redis connection for events."""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def initialize(self):
        """Initialize the scheduler and load all enabled schedules."""
        if self._initialized:
            logger.warning("Scheduler already initialized")
            return

        # Ensure process schedules table exists
        self.db.ensure_process_schedules_table()

        # Create scheduler with memory job store
        jobstores = {
            'default': MemoryJobStore()
        }

        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            timezone=pytz.UTC
        )

        # Load all enabled agent schedules from database
        schedules = self.db.list_all_enabled_schedules()

        # Detect missed schedules BEFORE _add_job overwrites next_run_at
        # (Issue #145). _add_job recalculates next_run_at via croniter,
        # which advances past the missed window, so we must snapshot first.
        self._missed_schedules = self._get_missed_schedules(schedules)

        for schedule in schedules:
            self._add_job(schedule)
            # Capture snapshot for sync detection
            self._schedule_snapshot[schedule.id] = (
                schedule.enabled,
                schedule.updated_at.isoformat() if schedule.updated_at else None
            )

        # Load all enabled process schedules from database
        process_schedules = self.db.list_all_enabled_process_schedules()
        for process_schedule in process_schedules:
            self._add_process_job(process_schedule)
            # Capture snapshot for sync detection
            self._process_schedule_snapshot[process_schedule.id] = (
                process_schedule.enabled,
                process_schedule.updated_at.isoformat() if process_schedule.updated_at else None
            )

        # Add listener for skipped executions (max_instances reached)
        # This records when a scheduled job is dropped because previous execution is still running
        self.scheduler.add_listener(
            self._on_job_max_instances,
            EVENT_JOB_MAX_INSTANCES
        )

        # Start the scheduler
        self.scheduler.start()
        self._initialized = True
        self._start_time = datetime.utcnow()

        logger.info(f"Scheduler initialized with {len(schedules)} agent schedules, {len(process_schedules)} process schedules")
        logger.info(f"Instance ID: {self._instance_id}")
        logger.info(f"Schedule sync interval: {config.schedule_reload_interval}s")
        logger.info(f"Misfire grace time: {config.misfire_grace_time}s")

        # RETRY-001: Recover any pending retries from before restart
        self._recover_pending_retries()

    def _get_missed_schedules(self, schedules: List[Schedule]) -> List[Schedule]:
        """
        Detect schedules that missed their last expected run (Issue #145).

        A schedule is considered missed when:
        - It has a next_run_at that is in the past
        - The miss is within the misfire_grace_time window
        - It hasn't already been executed at that time (last_run_at < next_run_at)

        This covers the case where the scheduler container was down during
        a trigger window and APScheduler's in-memory job store lost the job.
        """
        now = datetime.utcnow()
        grace = config.misfire_grace_time
        missed = []

        for schedule in schedules:
            if not schedule.next_run_at:
                continue

            # Normalize next_run_at to naive UTC for comparison
            expected = schedule.next_run_at
            if expected.tzinfo is not None:
                expected = expected.replace(tzinfo=None)

            # Skip if the next run is in the future — not missed
            if expected > now:
                continue

            # How long ago was it missed?
            missed_seconds = (now - expected).total_seconds()
            if missed_seconds > grace:
                logger.info(
                    f"Schedule {schedule.name} ({schedule.agent_name}) missed by "
                    f"{missed_seconds:.0f}s — exceeds grace time {grace}s, skipping catch-up"
                )
                continue

            # Check it wasn't already executed at the expected time
            if schedule.last_run_at:
                last = schedule.last_run_at
                if last.tzinfo is not None:
                    last = last.replace(tzinfo=None)
                if last >= expected:
                    continue  # Already ran

            logger.warning(
                f"Schedule {schedule.name} ({schedule.agent_name}) missed by "
                f"{missed_seconds:.0f}s — queuing catch-up execution"
            )
            missed.append(schedule)

        return missed

    def shutdown(self):
        """Shutdown the scheduler gracefully."""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self._initialized = False
            logger.info("Scheduler shutdown")

        # Issue #132: Cancel active poll tasks on shutdown
        if self._active_poll_tasks:
            logger.info(f"Cancelling {len(self._active_poll_tasks)} active poll tasks")
            for task in self._active_poll_tasks:
                task.cancel()
            self._active_poll_tasks.clear()

        if self._redis:
            self._redis.close()
            self._redis = None

        self.lock_manager.close()

    async def fire_missed_schedules(self):
        """
        Execute schedules missed while the container was down (Issue #145).

        Must be called after initialize(). The missed list was captured during
        initialize() BEFORE _add_job overwrote next_run_at with the next
        future occurrence.
        """
        missed = getattr(self, '_missed_schedules', [])
        logger.info(f"Startup catch-up: {len(missed)} missed schedule(s) to recover")
        for schedule in missed:
            logger.info(f"Startup catch-up: firing {schedule.name} ({schedule.agent_name})")
            asyncio.ensure_future(self._execute_schedule(schedule.id))
        self._missed_schedules = []

    async def run_forever(self):
        """Run the scheduler until interrupted."""
        self.initialize()
        await self.fire_missed_schedules()

        sync_interval = config.schedule_reload_interval
        heartbeat_interval = 30
        last_sync = datetime.utcnow()

        try:
            # Keep the service running with periodic heartbeat and sync
            while True:
                self.lock_manager.set_heartbeat(self._instance_id)

                # Check if it's time to sync schedules
                now = datetime.utcnow()
                if (now - last_sync).total_seconds() >= sync_interval:
                    await self._sync_schedules()
                    last_sync = now

                await asyncio.sleep(heartbeat_interval)
        except asyncio.CancelledError:
            logger.info("Scheduler received cancel signal")
        finally:
            self.shutdown()

    # =========================================================================
    # Job Management
    # =========================================================================

    def _get_job_id(self, schedule_id: str) -> str:
        """Generate a unique job ID for APScheduler."""
        return f"schedule_{schedule_id}"

    def _parse_cron(self, cron_expression: str) -> Dict:
        """
        Parse a cron expression into APScheduler CronTrigger kwargs.

        Format: minute hour day month day_of_week
        Examples:
          - "0 9 * * *" = Every day at 9:00 AM
          - "*/15 * * * *" = Every 15 minutes
          - "0 0 * * 0" = Every Sunday at midnight

        Note: the raw day_of_week value uses Unix cron numbering (0=Sun,
        1=Mon, …).  Callers that build an APScheduler CronTrigger must
        first run the value through _cron_dow_to_apscheduler() to avoid
        a one-day shift (see Issue #220).
        """
        parts = cron_expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expression}. Expected 5 parts.")

        return {
            'minute': parts[0],
            'hour': parts[1],
            'day': parts[2],
            'month': parts[3],
            'day_of_week': parts[4]
        }

    @staticmethod
    def _cron_dow_to_apscheduler(dow: str) -> str:
        """
        Translate a Unix cron day-of-week field to APScheduler named-day format.

        Unix cron:   0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat, 7=Sun
        APScheduler: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

        Passing raw cron numeric values straight to CronTrigger shifts every
        schedule one day forward (e.g. cron '1'=Monday fires on Tuesday).
        Converting to APScheduler's named abbreviations (mon, tue, …) removes
        the ambiguity because the names mean the same thing in both systems.

        Handles: wildcard (*), single value (1), comma list (1,3,5),
        range (1-5).  Step expressions (*/2) are returned unchanged — they
        are uncommon for day-of-week and can be addressed separately.
        """
        _NAMES = {
            0: 'sun', 1: 'mon', 2: 'tue', 3: 'wed',
            4: 'thu', 5: 'fri', 6: 'sat', 7: 'sun',
        }

        def _token(t: str) -> str:
            try:
                return _NAMES[int(t)]
            except (ValueError, KeyError):
                return t  # already a named day or unrecognised — leave as-is

        if dow == '*' or '/' in dow:
            return dow
        if ',' in dow:
            return ','.join(_token(t) for t in dow.split(','))
        if '-' in dow:
            lo, hi = dow.split('-', 1)
            return f'{_token(lo)}-{_token(hi)}'
        return _token(dow)

    def _add_job(self, schedule: Schedule):
        """Add a schedule as an APScheduler job."""
        if not self.scheduler:
            return

        job_id = self._get_job_id(schedule.id)

        try:
            # Parse cron expression
            cron_kwargs = self._parse_cron(schedule.cron_expression)

            # Build APScheduler trigger kwargs, translating day_of_week from
            # Unix cron convention (0=Sun, 1=Mon) to APScheduler named days
            # to prevent a one-day shift on weekday-based schedules (Issue #220).
            trigger_kwargs = dict(cron_kwargs)
            trigger_kwargs['day_of_week'] = self._cron_dow_to_apscheduler(
                cron_kwargs['day_of_week']
            )

            # Create timezone-aware trigger
            timezone = pytz.timezone(schedule.timezone) if schedule.timezone else pytz.UTC
            trigger = CronTrigger(timezone=timezone, **trigger_kwargs)

            # Add the job
            self.scheduler.add_job(
                self._execute_schedule,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                replace_existing=True,
                name=f"{schedule.agent_name}:{schedule.name}",
                misfire_grace_time=config.misfire_grace_time,
                coalesce=True,
                max_instances=1,
            )

            # Calculate and store next run time
            next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
            if next_run:
                self.db.update_schedule_run_times(schedule.id, next_run_at=next_run)

            logger.info(f"Added schedule job: {job_id} ({schedule.name}) for agent {schedule.agent_name}")
        except Exception as e:
            logger.error(f"Failed to add schedule job {job_id}: {e}")

    def _remove_job(self, schedule_id: str):
        """Remove a schedule job from APScheduler."""
        if not self.scheduler:
            return

        job_id = self._get_job_id(schedule_id)
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed schedule job: {job_id}")
        except Exception as e:
            logger.warning(f"Failed to remove schedule job {job_id}: {e}")

    def _get_next_run_time(self, cron_expression: str, timezone: str = "UTC") -> Optional[datetime]:
        """Calculate the next run time for a cron expression."""
        try:
            tz = pytz.timezone(timezone) if timezone else pytz.UTC
            now = datetime.now(tz)
            cron = croniter(cron_expression, now)
            next_time = cron.get_next(datetime)
            return next_time
        except Exception as e:
            logger.error(f"Failed to calculate next run time: {e}")
            return None

    # =========================================================================
    # Schedule Sync (detect new/updated/deleted schedules)
    # =========================================================================

    async def _sync_schedules(self):
        """
        Sync in-memory APScheduler jobs with database schedules.

        This is called periodically to detect:
        - New schedules created since last sync
        - Deleted schedules
        - Updated schedules (cron, timezone, enabled status changed)

        This allows new schedules to work without restarting the scheduler container.
        """
        try:
            await self._sync_agent_schedules()
            await self._sync_process_schedules()
        except Exception as e:
            logger.error(f"Schedule sync failed: {e}")

    async def _sync_agent_schedules(self):
        """Sync agent schedules with database."""
        # Get all schedules from database (enabled and disabled)
        all_schedules = self.db.list_all_schedules()

        # Build current state map
        current_state: Dict[str, tuple] = {}
        schedule_map: Dict[str, Schedule] = {}
        for schedule in all_schedules:
            current_state[schedule.id] = (
                schedule.enabled,
                schedule.updated_at.isoformat() if schedule.updated_at else None
            )
            schedule_map[schedule.id] = schedule

        # Detect changes
        snapshot_ids = set(self._schedule_snapshot.keys())
        current_ids = set(current_state.keys())

        # New schedules (in database but not in snapshot)
        new_ids = current_ids - snapshot_ids
        for schedule_id in new_ids:
            schedule = schedule_map[schedule_id]
            if schedule.enabled:
                logger.info(f"Sync: Adding new schedule {schedule.name} for agent {schedule.agent_name}")
                self._add_job(schedule)
            self._schedule_snapshot[schedule_id] = current_state[schedule_id]

        # Deleted schedules (in snapshot but not in database)
        deleted_ids = snapshot_ids - current_ids
        for schedule_id in deleted_ids:
            logger.info(f"Sync: Removing deleted schedule {schedule_id}")
            self._remove_job(schedule_id)
            del self._schedule_snapshot[schedule_id]

        # Updated schedules (still exists but state changed)
        for schedule_id in (snapshot_ids & current_ids):
            old_state = self._schedule_snapshot[schedule_id]
            new_state = current_state[schedule_id]

            if old_state != new_state:
                schedule = schedule_map[schedule_id]
                old_enabled, old_updated = old_state
                new_enabled, new_updated = new_state

                if old_enabled and not new_enabled:
                    # Schedule was disabled
                    logger.info(f"Sync: Disabling schedule {schedule.name}")
                    self._remove_job(schedule_id)
                elif not old_enabled and new_enabled:
                    # Schedule was enabled
                    logger.info(f"Sync: Enabling schedule {schedule.name}")
                    self._add_job(schedule)
                elif new_enabled and old_updated != new_updated:
                    # Schedule was updated (and still enabled)
                    logger.info(f"Sync: Updating schedule {schedule.name}")
                    self._remove_job(schedule_id)
                    self._add_job(schedule)

                self._schedule_snapshot[schedule_id] = new_state

        if new_ids or deleted_ids:
            logger.info(f"Sync complete: {len(new_ids)} added, {len(deleted_ids)} removed")

    async def _sync_process_schedules(self):
        """Sync process schedules with database."""
        # Get all process schedules from database
        all_schedules = self.db.list_all_process_schedules()

        # Build current state map
        current_state: Dict[str, tuple] = {}
        schedule_map: Dict[str, ProcessSchedule] = {}
        for schedule in all_schedules:
            current_state[schedule.id] = (
                schedule.enabled,
                schedule.updated_at.isoformat() if schedule.updated_at else None
            )
            schedule_map[schedule.id] = schedule

        # Detect changes
        snapshot_ids = set(self._process_schedule_snapshot.keys())
        current_ids = set(current_state.keys())

        # New schedules
        new_ids = current_ids - snapshot_ids
        for schedule_id in new_ids:
            schedule = schedule_map[schedule_id]
            if schedule.enabled:
                logger.info(f"Sync: Adding new process schedule {schedule.process_name}/{schedule.trigger_id}")
                self._add_process_job(schedule)
            self._process_schedule_snapshot[schedule_id] = current_state[schedule_id]

        # Deleted schedules
        deleted_ids = snapshot_ids - current_ids
        for schedule_id in deleted_ids:
            logger.info(f"Sync: Removing deleted process schedule {schedule_id}")
            self._remove_process_job(schedule_id)
            del self._process_schedule_snapshot[schedule_id]

        # Updated schedules
        for schedule_id in (snapshot_ids & current_ids):
            old_state = self._process_schedule_snapshot[schedule_id]
            new_state = current_state[schedule_id]

            if old_state != new_state:
                schedule = schedule_map[schedule_id]
                old_enabled, old_updated = old_state
                new_enabled, new_updated = new_state

                if old_enabled and not new_enabled:
                    logger.info(f"Sync: Disabling process schedule {schedule.process_name}/{schedule.trigger_id}")
                    self._remove_process_job(schedule_id)
                elif not old_enabled and new_enabled:
                    logger.info(f"Sync: Enabling process schedule {schedule.process_name}/{schedule.trigger_id}")
                    self._add_process_job(schedule)
                elif new_enabled and old_updated != new_updated:
                    logger.info(f"Sync: Updating process schedule {schedule.process_name}/{schedule.trigger_id}")
                    self._remove_process_job(schedule_id)
                    self._add_process_job(schedule)

                self._process_schedule_snapshot[schedule_id] = new_state

    # =========================================================================
    # Skipped Execution Tracking (Issue #46)
    # =========================================================================

    def _on_job_max_instances(self, event: JobExecutionEvent):
        """
        Handle EVENT_JOB_MAX_INSTANCES - triggered when a job is skipped because
        the previous execution is still running (max_instances=1 reached).

        This ensures we have an audit trail for skipped scheduled executions
        instead of silently dropping them with only a log warning.

        Args:
            event: APScheduler JobExecutionEvent with job_id
        """
        job_id = event.job_id
        logger.warning(f"Job {job_id} skipped: previous execution still running (max_instances reached)")

        # Extract schedule_id from job_id (format: "schedule_{schedule_id}" or "process_schedule_{schedule_id}")
        if job_id.startswith("schedule_"):
            schedule_id = job_id[len("schedule_"):]
            self._record_skipped_agent_schedule(schedule_id)
        elif job_id.startswith("process_schedule_"):
            schedule_id = job_id[len("process_schedule_"):]
            self._record_skipped_process_schedule(schedule_id)
        else:
            logger.warning(f"Unknown job_id format for skipped job: {job_id}")

    def _record_skipped_agent_schedule(self, schedule_id: str):
        """
        Record a skipped agent schedule execution in the database.

        Creates an execution record with status='skipped' so it appears in
        the execution history and provides an audit trail.
        """
        try:
            schedule = self.db.get_schedule(schedule_id)
            if not schedule:
                logger.error(f"Cannot record skipped execution: schedule {schedule_id} not found")
                return

            # Create execution record with 'skipped' status
            execution = self.db.create_skipped_execution(
                schedule_id=schedule.id,
                agent_name=schedule.agent_name,
                message=schedule.message,
                triggered_by="schedule",
                skip_reason="Previous execution still running (max_instances reached)"
            )

            if execution:
                logger.info(f"Recorded skipped execution {execution.id} for schedule {schedule.name}")

                # Publish event for WebSocket notification
                asyncio.create_task(self._publish_event({
                    "type": "schedule_execution_skipped",
                    "agent": schedule.agent_name,
                    "schedule_id": schedule.id,
                    "execution_id": execution.id,
                    "schedule_name": schedule.name,
                    "reason": "Previous execution still running"
                }))
            else:
                logger.error(f"Failed to create skipped execution record for schedule {schedule_id}")

        except Exception as e:
            logger.error(f"Error recording skipped execution for schedule {schedule_id}: {e}")

    def _record_skipped_process_schedule(self, schedule_id: str):
        """
        Record a skipped process schedule execution in the database.

        Creates an execution record with status='skipped' so it appears in
        the execution history and provides an audit trail.
        """
        try:
            schedule = self.db.get_process_schedule(schedule_id)
            if not schedule:
                logger.error(f"Cannot record skipped execution: process schedule {schedule_id} not found")
                return

            # Create execution record with 'skipped' status
            execution = self.db.create_skipped_process_schedule_execution(
                schedule_id=schedule.id,
                process_id=schedule.process_id,
                process_name=schedule.process_name,
                triggered_by="schedule",
                skip_reason="Previous execution still running (max_instances reached)"
            )

            if execution:
                logger.info(f"Recorded skipped process execution {execution.id} for {schedule.process_name}/{schedule.trigger_id}")

                # Publish event for WebSocket notification
                asyncio.create_task(self._publish_event({
                    "type": "process_schedule_execution_skipped",
                    "process_id": schedule.process_id,
                    "process_name": schedule.process_name,
                    "schedule_id": schedule.id,
                    "trigger_id": schedule.trigger_id,
                    "execution_id": execution.id,
                    "reason": "Previous execution still running"
                }))
            else:
                logger.error(f"Failed to create skipped execution record for process schedule {schedule_id}")

        except Exception as e:
            logger.error(f"Error recording skipped execution for process schedule {schedule_id}: {e}")

    # =========================================================================
    # Schedule Execution
    # =========================================================================

    async def _execute_schedule(self, schedule_id: str):
        """
        Execute a scheduled task.

        This is called by APScheduler when a schedule is due.
        Uses distributed locking to prevent duplicate executions.
        """
        # Try to acquire lock - if failed, another instance is executing
        lock = self.lock_manager.try_acquire_schedule_lock(schedule_id)
        if not lock:
            logger.info(f"Schedule {schedule_id} already being executed by another instance")
            return

        try:
            await self._execute_schedule_with_lock(schedule_id)
        finally:
            lock.release()

    async def _execute_schedule_with_lock(self, schedule_id: str, triggered_by: str = "schedule"):
        """Execute schedule after acquiring lock.

        Args:
            schedule_id: ID of the schedule to execute
            triggered_by: Source of trigger - "schedule" (cron) or "manual"
        """
        schedule = self.db.get_schedule(schedule_id)
        if not schedule:
            logger.error(f"Schedule {schedule_id} not found")
            return

        if not schedule.enabled and triggered_by == "schedule":
            logger.info(f"Schedule {schedule_id} is disabled, skipping")
            return

        # Check if agent has autonomy enabled (only for cron-triggered, not manual)
        if triggered_by == "schedule" and not self.db.get_autonomy_enabled(schedule.agent_name):
            logger.info(f"Schedule {schedule_id} skipped: agent {schedule.agent_name} autonomy is disabled")
            return

        # Agent-owned pre-check hook (#454). Only for cron-triggered invocations:
        # manual triggers from the UI represent an explicit operator decision and
        # must always fire. Fail-open: any None return means the agent has no
        # pre-check or the call errored — fall through to normal firing.
        effective_message = schedule.message
        if triggered_by == "schedule":
            decision = await self._run_pre_check(schedule.agent_name)
            if decision is not None:
                if not decision.get("fire", True):
                    reason = decision.get("reason") or "pre-check returned fire=false"
                    skipped = self.db.create_skipped_execution(
                        schedule_id=schedule.id,
                        agent_name=schedule.agent_name,
                        message=schedule.message,
                        triggered_by=triggered_by,
                        skip_reason=f"pre-check: {reason}",
                    )
                    logger.info(
                        f"Schedule {schedule.name} skipped by pre-check: {reason}"
                    )
                    now = datetime.utcnow()
                    next_run = self._get_next_run_time(
                        schedule.cron_expression, schedule.timezone
                    )
                    self.db.update_schedule_run_times(
                        schedule.id, last_run_at=now, next_run_at=next_run
                    )
                    await self._publish_event({
                        "type": "schedule_execution_skipped",
                        "agent": schedule.agent_name,
                        "schedule_id": schedule.id,
                        "execution_id": skipped.id if skipped else None,
                        "schedule_name": schedule.name,
                        "reason": reason,
                    })
                    return
                override = decision.get("message")
                if override and isinstance(override, str):
                    effective_message = override
                    logger.info(
                        f"Schedule {schedule.name} pre-check overrode message "
                        f"({len(override)} chars)"
                    )

        logger.info(f"Executing schedule: {schedule.name} for agent {schedule.agent_name} (triggered_by={triggered_by})")

        # Create execution record
        execution = self.db.create_execution(
            schedule_id=schedule.id,
            agent_name=schedule.agent_name,
            message=effective_message,
            triggered_by=triggered_by,
            model_used=schedule.model
        )

        if not execution:
            logger.error(f"Failed to create execution record for schedule {schedule_id}")
            return

        # Broadcast execution started
        await self._publish_event({
            "type": "schedule_execution_started",
            "agent": schedule.agent_name,
            "schedule_id": schedule.id,
            "execution_id": execution.id,
            "schedule_name": schedule.name,
            "triggered_by": triggered_by
        })

        # Delegate to backend's TaskExecutionService via internal API.
        # This ensures scheduled tasks go through the same code path as manual
        # and public tasks: slot acquisition, activity tracking, credential
        # sanitization, and dashboard capacity meter visibility.
        try:
            result = await self._call_backend_execute_task(
                agent_name=schedule.agent_name,
                message=effective_message,
                triggered_by=triggered_by,
                model=schedule.model,
                timeout_seconds=schedule.timeout_seconds,
                allowed_tools=schedule.allowed_tools,
                execution_id=execution.id,
            )

            status = result.get("status", ExecutionStatus.FAILED)
            error_msg = result.get("error")

            # Issue #132: Handle fire-and-forget dispatch mode.
            # When status == "dispatched", the backend accepted the task and a
            # background poll task is running. Update last_run_at immediately
            # (to ensure missed schedule detection works on restart) and return.
            # The background task will handle completion events.
            if status == "dispatched":
                now = datetime.utcnow()
                next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
                self.db.update_schedule_run_times(schedule.id, last_run_at=now, next_run_at=next_run)
                logger.info(
                    f"Schedule {schedule.name} dispatched (fire-and-forget), "
                    f"execution_id={execution.id}, background poll running"
                )
                return  # Background task handles completion events

            if status == ExecutionStatus.SUCCESS:
                # Update schedule last run time
                now = datetime.utcnow()
                next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
                self.db.update_schedule_run_times(schedule.id, last_run_at=now, next_run_at=next_run)

                logger.info(f"Schedule {schedule.name} executed successfully")

                # Broadcast execution completed
                await self._publish_event({
                    "type": "schedule_execution_completed",
                    "agent": schedule.agent_name,
                    "schedule_id": schedule.id,
                    "execution_id": execution.id,
                    "status": "success"
                })
            else:
                # TaskExecutionService already updated the execution record
                # with failure status, but we still need to detect auth errors
                # for logging and update schedule run times
                if error_msg:
                    if _is_auth_failure(error_msg):
                        logger.error(
                            f"Schedule {schedule.name} execution failed due to authentication error: {error_msg}"
                        )
                    else:
                        logger.error(f"Schedule {schedule.name} execution failed: {error_msg}")
                else:
                    logger.error(f"Schedule {schedule.name} execution failed (no error detail)")

                await self._publish_event({
                    "type": "schedule_execution_completed",
                    "agent": schedule.agent_name,
                    "schedule_id": schedule.id,
                    "execution_id": execution.id,
                    "status": "failed",
                    "error": error_msg
                })

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Schedule {schedule.name} execution failed: {error_msg}")

            # SCHED-ASYNC-001: Check current status before overwriting.
            # The backend's TaskExecutionService may have already finalized
            # the execution (e.g., marked it as 'success') before the scheduler
            # caught a connection error or polling timeout.
            current = self.db.get_execution(execution.id)
            if current and current.status != ExecutionStatus.RUNNING:
                # Backend already finalized — don't overwrite with 'failed'
                logger.info(
                    f"Schedule {schedule.name} execution {execution.id} already finalized "
                    f"as '{current.status}' — not overwriting with 'failed'"
                )
                actual_status = current.status
            else:
                # Genuinely failed — mark as failed
                self.db.update_execution_status(
                    execution_id=execution.id,
                    status=ExecutionStatus.FAILED,
                    error=error_msg
                )
                actual_status = ExecutionStatus.FAILED

            await self._publish_event({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": actual_status,
                "error": error_msg if actual_status == ExecutionStatus.FAILED else None
            })

    async def _run_pre_check(self, agent_name: str) -> Optional[dict]:
        """Run the agent's optional pre-check hook (#454, SCHED-COND-001).

        Calls the backend's internal endpoint, which `docker exec`s the
        executable ``~/.trinity/pre-check`` file inside the agent
        container (same primitive as the persistent-state allowlist in
        ``services/git_service.py``). The hook is language-agnostic —
        Trinity execs the path directly, so the interpreter is chosen
        by the file's shebang. The scheduler never opens its own HTTP
        edge to agents — backend remains the orchestrator.

        Contract translation (backend → caller):
          - ``hook_present == False``               → return ``None`` (fire as usual)
          - ``exit_code != 0``                       → return ``None`` (fail-open + log)
          - ``exit_code == 0`` & empty stdout        → return ``{"fire": False, "reason": ...}``
          - ``exit_code == 0`` & non-empty stdout    → return ``{"fire": True, "message": stdout}``

        Returning ``None`` means "no decision, fire the schedule as today."
        Fail-open is structural — a broken hook or unreachable backend must
        never silently suppress scheduled work.
        """
        headers = {}
        if config.internal_api_secret:
            headers["X-Internal-Secret"] = config.internal_api_secret

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{config.backend_url}/api/internal/agents/{agent_name}/pre-check",
                    headers=headers,
                    timeout=70.0,  # agent-side timeout is 60s, give us headroom
                )
        except Exception as e:
            logger.warning(
                f"[pre-check] backend call for {agent_name} failed ({e}) — fail-open"
            )
            return None

        if response.status_code == 404:
            logger.warning(
                f"[pre-check] backend says agent {agent_name} not found — fail-open"
            )
            return None
        if response.status_code != 200:
            logger.warning(
                f"[pre-check] backend returned {response.status_code} for {agent_name} — fail-open"
            )
            return None

        try:
            data = response.json()
        except Exception as e:
            logger.warning(
                f"[pre-check] malformed backend response for {agent_name} ({e}) — fail-open"
            )
            return None

        if not data.get("hook_present"):
            return None  # template has no hook — backward compat

        exit_code = data.get("exit_code", 1)
        if exit_code != 0:
            stderr = (data.get("stderr") or data.get("stdout") or "")[:500]
            logger.warning(
                f"[pre-check] hook for {agent_name} exited {exit_code}: "
                f"{stderr!r} — fail-open"
            )
            return None

        stdout = (data.get("stdout") or "").strip()
        if not stdout:
            return {"fire": False, "reason": "pre-check returned empty stdout"}
        return {"fire": True, "message": stdout}

    async def _call_backend_execute_task(
        self,
        agent_name: str,
        message: str,
        triggered_by: str,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        allowed_tools: Optional[list] = None,
        execution_id: Optional[str] = None,
    ) -> dict:
        """
        Execute a task via the backend's internal TaskExecutionService endpoint.

        Uses async fire-and-forget dispatch with DB polling (SCHED-ASYNC-001):
        1. POST with async_mode=True and 30s timeout (dispatch only)
        2. If backend accepts, poll DB every poll_interval seconds until done
        3. Backward compatible: if backend returns sync result, use it directly

        Returns:
            dict with execution_id, status, response, cost, context_used, etc.

        Raises:
            Exception on HTTP errors, dispatch failures, or polling timeout.
        """
        headers = {}
        if config.internal_api_secret:
            headers["X-Internal-Secret"] = config.internal_api_secret

        # RELIABILITY-006 (#525): deterministic idempotency key per fire. The
        # execution_id is created once per schedule fire and reused across an
        # HTTP-level resend of this same dispatch, so a transient backend 5xx +
        # resend resolves to the same key and the backend short-circuits the
        # duplicate instead of starting a second execution. Intentional #271
        # retries create a fresh execution_id → fresh key → not suppressed.
        if execution_id:
            headers["Idempotency-Key"] = f"sched:{execution_id}"

        payload = {
            "agent_name": agent_name,
            "message": message,
            "triggered_by": triggered_by,
            "timeout_seconds": timeout_seconds,
            "async_mode": True,
        }
        if model:
            payload["model"] = model
        if allowed_tools:
            payload["allowed_tools"] = allowed_tools
        if execution_id:
            payload["execution_id"] = execution_id

        # Step 1: Dispatch with short timeout (30s max for the HTTP round-trip)
        dispatch_timeout = 30.0

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{config.backend_url}/api/internal/execute-task",
                headers=headers,
                json=payload,
                timeout=dispatch_timeout,
            )

            if response.status_code != 200:
                error_text = response.text[:500] if response.text else f"HTTP {response.status_code}"
                raise Exception(f"Backend execute-task returned {response.status_code}: {error_text}")

            result = response.json()

        # Step 2: Check if backend accepted async mode
        if result.get("status") == "accepted" and result.get("async_mode"):
            # Issue #132: Fire-and-forget — spawn background task for polling,
            # return immediately so APScheduler job function doesn't block.
            # This prevents max_instances=1 from skipping subsequent triggers.
            logger.info(
                f"Backend accepted async execution for {agent_name}, "
                f"execution_id={execution_id}, spawning background poll task"
            )
            task = asyncio.create_task(
                self._poll_and_finalize(
                    execution_id=execution_id,
                    timeout_seconds=timeout_seconds,
                    agent_name=agent_name,
                )
            )
            # Track task for graceful shutdown
            self._active_poll_tasks.add(task)
            task.add_done_callback(self._active_poll_tasks.discard)

            # Return immediately with "dispatched" status
            return {
                "status": "dispatched",
                "async_mode": True,
                "execution_id": execution_id,
            }

        # Backward compatibility: backend returned a sync result (old backend
        # without async_mode support). Use the result directly.
        return result

    async def _poll_execution_completion(
        self,
        execution_id: str,
        timeout_seconds: Optional[int],
    ) -> dict:
        """
        Poll the DB for execution completion (SCHED-ASYNC-001).

        Polls every config.poll_interval seconds until the execution status
        is no longer 'running', or the deadline is exceeded.

        Returns:
            dict with status, response, error, cost, etc. from the execution record.

        Raises:
            Exception if polling deadline exceeded.
        """
        # #913: schedule.timeout_seconds may be None (= inherit per-agent
        # value). The backend enforces the real timeout; the scheduler just
        # needs a poll budget that's at least as long as any per-agent
        # timeout can be. _POLL_DEADLINE_WHEN_NULL is the agent-config upper
        # clamp, so we never give up before the backend itself does.
        effective_timeout = float(timeout_seconds if timeout_seconds is not None else _POLL_DEADLINE_WHEN_NULL)
        # Deadline = task timeout + grace buffer for slot acquisition and cleanup
        deadline = time.monotonic() + effective_timeout + config.poll_deadline_buffer
        poll_count = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(config.poll_interval)
            poll_count += 1

            execution = self.db.get_execution(execution_id)
            if not execution:
                logger.warning(f"Execution {execution_id} not found in DB during polling (poll #{poll_count})")
                continue

            if execution.status != ExecutionStatus.RUNNING:
                logger.info(
                    f"Execution {execution_id} completed: status={execution.status} "
                    f"(polled {poll_count} times)"
                )
                return {
                    "execution_id": execution.id,
                    "status": execution.status,
                    "response": execution.response,
                    "error": execution.error,
                    "cost": execution.cost,
                    "context_used": execution.context_used,
                    "context_max": execution.context_max,
                }

            if poll_count % 6 == 0:  # Log every ~60s at default 10s interval
                elapsed = int(time.monotonic() - (deadline - effective_timeout - 60))
                logger.info(f"Execution {execution_id} still running ({elapsed}s elapsed, poll #{poll_count})")

        raise Exception(
            f"Polling deadline exceeded for execution {execution_id} "
            f"(timeout_seconds={timeout_seconds}, polls={poll_count})"
        )

    async def _poll_and_finalize(
        self,
        execution_id: str,
        timeout_seconds: Optional[int],
        agent_name: str,
    ):
        """
        Background task that polls for execution completion and publishes events.

        Issue #132: This runs as a fire-and-forget background task so the
        APScheduler job function can return immediately, preventing
        max_instances=1 from skipping subsequent triggers.

        Args:
            execution_id: The execution ID to poll
            timeout_seconds: Task timeout for polling deadline. None ⇒ use
                _POLL_DEADLINE_WHEN_NULL (#913 — schedule inherits per-agent
                value, which the scheduler does not have locally).
            agent_name: Agent name for logging and events
        """
        try:
            result = await self._poll_execution_completion(
                execution_id=execution_id,
                timeout_seconds=timeout_seconds,
            )

            status = result.get("status", ExecutionStatus.FAILED)
            error_msg = result.get("error")

            # Look up schedule_id from execution record for WebSocket event
            execution = self.db.get_execution(execution_id)
            schedule_id = execution.schedule_id if execution else None

            if status == ExecutionStatus.SUCCESS:
                logger.info(f"Background poll: execution {execution_id} succeeded")
                await self._publish_event({
                    "type": "schedule_execution_completed",
                    "agent": agent_name,
                    "schedule_id": schedule_id,
                    "execution_id": execution_id,
                    "status": "success"
                })

                # VALIDATE-001: Check if validation is enabled and trigger it
                await self._maybe_trigger_validation(
                    execution=execution,
                    agent_name=agent_name,
                )
            else:
                # Log auth failures specially for diagnostics
                if error_msg:
                    if _is_auth_failure(error_msg):
                        logger.error(
                            f"Background poll: execution {execution_id} failed due to auth error: {error_msg}"
                        )
                    else:
                        logger.error(f"Background poll: execution {execution_id} failed: {error_msg}")
                else:
                    logger.error(f"Background poll: execution {execution_id} failed (no error detail)")

                await self._publish_event({
                    "type": "schedule_execution_completed",
                    "agent": agent_name,
                    "schedule_id": schedule_id,
                    "execution_id": execution_id,
                    "status": "failed",
                    "error": error_msg
                })

                # RETRY-001: Check if retry is eligible and schedule it
                await self._maybe_schedule_retry(
                    execution=execution,
                    error_msg=error_msg
                )

        except asyncio.CancelledError:
            logger.info(f"Background poll for execution {execution_id} cancelled (shutdown)")
            raise
        except Exception as e:
            # Log but don't propagate - this is a background task
            logger.error(f"Background poll for execution {execution_id} failed: {e}")
            # Check if backend already finalized the execution
            current = self.db.get_execution(execution_id)
            if current and current.status != ExecutionStatus.RUNNING:
                logger.info(
                    f"Execution {execution_id} already finalized as '{current.status}' "
                    "— background poll error is benign"
                )
            else:
                # Execution stuck in running state - cleanup service will recover
                logger.warning(
                    f"Execution {execution_id} may be stuck in 'running' state — "
                    "cleanup service will recover within 5 minutes"
                )

    # =========================================================================
    # Retry Management (RETRY-001)
    # =========================================================================

    async def _maybe_schedule_retry(
        self,
        execution: ScheduleExecution,
        error_msg: str = None
    ):
        """Check if a failed execution is eligible for retry and schedule it.

        Called after _poll_and_finalize detects a failure.
        """
        if not execution:
            return

        # Don't retry skipped or cancelled executions
        if execution.status in (ExecutionStatus.SKIPPED, ExecutionStatus.CANCELLED):
            logger.debug(f"Execution {execution.id} status '{execution.status}' is not retriable")
            return

        # Get the schedule to check retry configuration
        schedule = self.db.get_schedule(execution.schedule_id)
        if not schedule:
            logger.debug(f"Schedule {execution.schedule_id} not found, skipping retry")
            return

        # Check if retries are enabled
        max_retries = schedule.max_retries
        if max_retries <= 0:
            logger.debug(f"Schedule {schedule.id} has max_retries=0, skipping retry")
            return

        # Check if we've exceeded retry limit
        current_attempt = execution.attempt_number or 1
        if current_attempt > max_retries:
            logger.info(
                f"Execution {execution.id} attempt {current_attempt} exceeds max_retries={max_retries}, "
                "no more retries"
            )
            return

        # Calculate retry delay
        retry_delay = schedule.retry_delay_seconds

        # Check for rate limit (429) in error - use 2x delay, capped at 300s
        if error_msg:
            error_lower = error_msg.lower()
            if "429" in error_lower or "rate limit" in error_lower or "too many requests" in error_lower:
                retry_delay = min(300, retry_delay * 2)
                logger.info(f"Rate limit detected, using 2x delay: {retry_delay}s")

        # Schedule the retry
        retry_at = datetime.utcnow() + timedelta(seconds=retry_delay)

        logger.info(
            f"Scheduling retry for execution {execution.id} "
            f"(attempt {current_attempt + 1}/{max_retries + 1}) at {retry_at.isoformat()}"
        )

        # Persist retry intent to DB (survives restart)
        self.db.schedule_retry(execution.id, retry_at)

        # Schedule APScheduler date trigger
        self._schedule_retry_job(
            execution=execution,
            schedule=schedule,
            retry_at=retry_at,
            next_attempt_number=current_attempt + 1
        )

    def _schedule_retry_job(
        self,
        execution: ScheduleExecution,
        schedule: Schedule,
        retry_at: datetime,
        next_attempt_number: int
    ):
        """Schedule an APScheduler date trigger for retry."""
        from apscheduler.triggers.date import DateTrigger

        # Find the original execution ID for linking
        original_id = self.db.get_original_execution_id(execution.id)

        job_id = f"retry_{execution.id}"

        # Remove any existing retry job for this execution
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        self.scheduler.add_job(
            self._execute_retry,
            trigger=DateTrigger(run_date=retry_at),
            id=job_id,
            kwargs={
                "original_execution_id": original_id,
                "failed_execution_id": execution.id,
                "schedule_id": schedule.id,
                "agent_name": schedule.agent_name,
                "message": schedule.message,
                "timeout_seconds": schedule.timeout_seconds,
                "model": schedule.model,
                "allowed_tools": schedule.allowed_tools,
                "next_attempt_number": next_attempt_number,
            },
            replace_existing=True
        )

    async def _execute_retry(
        self,
        original_execution_id: str,
        failed_execution_id: str,
        schedule_id: str,
        agent_name: str,
        message: str,
        timeout_seconds: Optional[int],
        model: str,
        allowed_tools: list,
        next_attempt_number: int
    ):
        """Execute a scheduled retry.

        Called by APScheduler when the date trigger fires.
        """
        logger.info(
            f"Executing retry for schedule {schedule_id}, agent {agent_name}, "
            f"attempt {next_attempt_number}"
        )

        # Clear the retry_scheduled_at from the failed execution
        self.db.clear_retry_scheduled(failed_execution_id)

        # Check if agent still exists (in case it was deleted while retry was pending)
        schedule = self.db.get_schedule(schedule_id)
        if not schedule or not schedule.enabled:
            logger.warning(
                f"Schedule {schedule_id} no longer exists or is disabled, "
                "skipping retry execution"
            )
            return

        # Create a new execution record for the retry
        retry_execution = self.db.create_execution(
            schedule_id=schedule_id,
            agent_name=agent_name,
            message=message,
            triggered_by="retry",
            model_used=model,
            attempt_number=next_attempt_number,
            retry_of_execution_id=original_execution_id
        )

        if not retry_execution:
            logger.error(f"Failed to create retry execution record for schedule {schedule_id}")
            return

        # Execute the task via the backend
        try:
            await self._call_backend_execute_task(
                agent_name=agent_name,
                message=message,
                triggered_by="retry",
                timeout_seconds=timeout_seconds,
                model=model,
                allowed_tools=allowed_tools,
                execution_id=retry_execution.id
            )
        except Exception as e:
            logger.error(f"Retry execution failed for {retry_execution.id}: {e}")
            self.db.update_execution_status(
                execution_id=retry_execution.id,
                status=ExecutionStatus.FAILED,
                error=str(e)[:2000]
            )

    def _recover_pending_retries(self):
        """Recover pending retries after scheduler restart.

        Called during initialization to reschedule any retries that were
        pending when the scheduler shut down.
        """
        pending = self.db.get_pending_retries()
        if not pending:
            return

        logger.info(f"Recovering {len(pending)} pending retries after restart")

        now = datetime.utcnow()
        for execution in pending:
            schedule = self.db.get_schedule(execution.schedule_id)
            if not schedule or not schedule.enabled:
                # Schedule deleted or disabled, clear the retry
                logger.info(
                    f"Schedule {execution.schedule_id} no longer valid, "
                    f"clearing retry for execution {execution.id}"
                )
                self.db.clear_retry_scheduled(execution.id)
                continue

            retry_at = execution.retry_scheduled_at
            if retry_at < now:
                # Retry time has passed, execute immediately
                retry_at = now + timedelta(seconds=5)
                logger.info(
                    f"Retry for execution {execution.id} was scheduled for "
                    f"{execution.retry_scheduled_at}, executing in 5s"
                )

            self._schedule_retry_job(
                execution=execution,
                schedule=schedule,
                retry_at=retry_at,
                next_attempt_number=(execution.attempt_number or 1) + 1
            )

    # =========================================================================
    # Validation Management (VALIDATE-001)
    # =========================================================================

    async def _maybe_trigger_validation(
        self,
        execution: ScheduleExecution,
        agent_name: str,
    ):
        """Check if a successful execution should be validated and trigger it.

        Called after _poll_and_finalize detects a success.

        Args:
            execution: The completed execution record.
            agent_name: The agent name for logging.
        """
        if not execution:
            return

        # Skip validation for validation executions (prevent infinite loop)
        if execution.triggered_by == "validation":
            logger.debug(f"Execution {execution.id} is a validation execution, skipping validation")
            return

        # Skip if execution already has a validation (duplicate prevention)
        if execution.validation_execution_id:
            logger.debug(f"Execution {execution.id} already has validation {execution.validation_execution_id}")
            return

        # Get the schedule to check validation configuration
        schedule = self.db.get_schedule(execution.schedule_id)
        if not schedule:
            logger.debug(f"Schedule {execution.schedule_id} not found, skipping validation")
            return

        # Check if validation is enabled
        if not schedule.validation_enabled:
            logger.debug(f"Schedule {schedule.id} has validation_enabled=False, skipping validation")
            # Mark as skipped for visibility
            self._update_business_status(execution.id, "skipped")
            return

        logger.info(
            f"Triggering validation for execution {execution.id} "
            f"(schedule {schedule.id}, agent {agent_name})"
        )

        # Trigger validation via backend API
        try:
            await self._call_backend_trigger_validation(
                execution_id=execution.id,
                agent_name=agent_name,
                schedule_id=schedule.id,
                original_message=execution.message,
                execution_response=execution.response or "",
                custom_prompt=schedule.validation_prompt,
                timeout_seconds=schedule.validation_timeout_seconds,
            )
        except Exception as e:
            logger.error(f"Failed to trigger validation for execution {execution.id}: {e}")
            # Mark as failed validation if trigger fails
            self._update_business_status(execution.id, "failed_validation")

    async def _call_backend_trigger_validation(
        self,
        execution_id: str,
        agent_name: str,
        schedule_id: str,
        original_message: str,
        execution_response: str,
        custom_prompt: str = None,
        timeout_seconds: int = 120,
    ) -> dict:
        """Call the backend API to trigger validation.

        Args:
            execution_id: The execution to validate.
            agent_name: The agent to run validation on.
            schedule_id: The schedule ID.
            original_message: The original task message.
            execution_response: The response from the original execution.
            custom_prompt: Optional custom auditor prompt.
            timeout_seconds: Timeout for validation task.

        Returns:
            dict with validation result.
        """
        url = f"{config.backend_url}/api/internal/validate-execution"

        payload = {
            "execution_id": execution_id,
            "agent_name": agent_name,
            "schedule_id": schedule_id,
            "original_message": original_message,
            "execution_response": execution_response,
            "custom_prompt": custom_prompt,
            "timeout_seconds": timeout_seconds,
        }

        headers = {"X-Internal-Secret": config.internal_api_secret}

        async with httpx.AsyncClient(timeout=float(timeout_seconds) + 30) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code >= 400:
                logger.error(
                    f"Backend validation trigger failed: {response.status_code} - {response.text}"
                )
                response.raise_for_status()

            return response.json()

    def _update_business_status(self, execution_id: str, business_status: str):
        """Update the business status of an execution in the database.

        Args:
            execution_id: The execution to update.
            business_status: The new business status.
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE schedule_executions
                SET business_status = ?, validated_at = ?
                WHERE id = ?
            """, (business_status, datetime.utcnow().isoformat(), execution_id))
            conn.commit()

    # =========================================================================
    # Schedule Management (for runtime updates)
    # =========================================================================

    def add_schedule(self, schedule: Schedule):
        """Add a new schedule to the scheduler."""
        if schedule.enabled:
            self._add_job(schedule)

    def remove_schedule(self, schedule_id: str):
        """Remove a schedule from the scheduler."""
        self._remove_job(schedule_id)

    def update_schedule(self, schedule: Schedule):
        """Update an existing schedule in the scheduler."""
        self._remove_job(schedule.id)
        if schedule.enabled:
            self._add_job(schedule)

    def reload_schedules(self):
        """Reload all schedules from the database."""
        if not self.scheduler:
            return

        # Remove all existing jobs
        for job in self.scheduler.get_jobs():
            if job.id.startswith("schedule_") or job.id.startswith("process_schedule_"):
                self.scheduler.remove_job(job.id)

        # Reload agent schedules from database
        schedules = self.db.list_all_enabled_schedules()
        for schedule in schedules:
            self._add_job(schedule)

        # Reload process schedules from database
        process_schedules = self.db.list_all_enabled_process_schedules()
        for process_schedule in process_schedules:
            self._add_process_job(process_schedule)

        logger.info(f"Reloaded {len(schedules)} agent schedules, {len(process_schedules)} process schedules")

    # =========================================================================
    # Process Schedule Management
    # =========================================================================

    def _get_process_job_id(self, schedule_id: str) -> str:
        """Generate a unique job ID for process schedules."""
        return f"process_schedule_{schedule_id}"

    def _add_process_job(self, schedule: ProcessSchedule):
        """Add a process schedule as an APScheduler job."""
        if not self.scheduler:
            return

        job_id = self._get_process_job_id(schedule.id)

        try:
            # Parse cron expression
            cron_kwargs = self._parse_cron(schedule.cron_expression)

            # Build APScheduler trigger kwargs, translating day_of_week from
            # Unix cron convention (0=Sun, 1=Mon) to APScheduler named days
            # to prevent a one-day shift on weekday-based schedules (Issue #220).
            trigger_kwargs = dict(cron_kwargs)
            trigger_kwargs['day_of_week'] = self._cron_dow_to_apscheduler(
                cron_kwargs['day_of_week']
            )

            # Create timezone-aware trigger
            timezone = pytz.timezone(schedule.timezone) if schedule.timezone else pytz.UTC
            trigger = CronTrigger(timezone=timezone, **trigger_kwargs)

            # Add the job
            self.scheduler.add_job(
                self._execute_process_schedule,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                replace_existing=True,
                name=f"process:{schedule.process_name}:{schedule.trigger_id}",
                misfire_grace_time=config.misfire_grace_time,
                coalesce=True,
                max_instances=1,
            )

            # Calculate and store next run time
            next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
            if next_run:
                self.db.update_process_schedule_run_times(schedule.id, next_run_at=next_run)

            logger.info(f"Added process schedule job: {job_id} ({schedule.process_name}/{schedule.trigger_id})")
        except Exception as e:
            logger.error(f"Failed to add process schedule job {job_id}: {e}")

    def _remove_process_job(self, schedule_id: str):
        """Remove a process schedule job from APScheduler."""
        if not self.scheduler:
            return

        job_id = self._get_process_job_id(schedule_id)
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed process schedule job: {job_id}")
        except Exception as e:
            logger.warning(f"Failed to remove process schedule job {job_id}: {e}")

    async def _execute_process_schedule(self, schedule_id: str):
        """
        Execute a scheduled process.

        This is called by APScheduler when a process schedule is due.
        Uses distributed locking to prevent duplicate executions.
        """
        # Try to acquire lock - if failed, another instance is executing
        lock = self.lock_manager.try_acquire_schedule_lock(f"process_{schedule_id}")
        if not lock:
            logger.info(f"Process schedule {schedule_id} already being executed by another instance")
            return

        try:
            await self._execute_process_schedule_with_lock(schedule_id)
        finally:
            lock.release()

    async def _execute_process_schedule_with_lock(self, schedule_id: str):
        """Execute process schedule after acquiring lock."""
        schedule = self.db.get_process_schedule(schedule_id)
        if not schedule:
            logger.error(f"Process schedule {schedule_id} not found")
            return

        if not schedule.enabled:
            logger.info(f"Process schedule {schedule_id} is disabled, skipping")
            return

        logger.info(f"Executing process schedule: {schedule.process_name}/{schedule.trigger_id}")

        # Create execution record
        execution = self.db.create_process_schedule_execution(
            schedule_id=schedule.id,
            process_id=schedule.process_id,
            process_name=schedule.process_name,
            triggered_by="schedule"
        )

        if not execution:
            logger.error(f"Failed to create execution record for process schedule {schedule_id}")
            return

        # Broadcast execution started
        await self._publish_event({
            "type": "process_schedule_execution_started",
            "process_id": schedule.process_id,
            "process_name": schedule.process_name,
            "schedule_id": schedule.id,
            "trigger_id": schedule.trigger_id,
            "execution_id": execution.id
        })

        try:
            # Call backend API to start process execution
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{config.backend_url}/api/processes/{schedule.process_id}/execute",
                    json={
                        "triggered_by": "schedule",
                        "input_data": {
                            "trigger": {
                                "type": "schedule",
                                "id": schedule.trigger_id,
                                "schedule_id": schedule.id,
                            }
                        }
                    },
                    timeout=60.0
                )

                if response.status_code == 200 or response.status_code == 201:
                    result = response.json()
                    process_execution_id = result.get("id")

                    # Update execution status
                    self.db.update_process_schedule_execution(
                        execution_id=execution.id,
                        status=ExecutionStatus.SUCCESS,
                        process_execution_id=process_execution_id
                    )

                    # Update schedule last run time
                    now = datetime.utcnow()
                    next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
                    self.db.update_process_schedule_run_times(schedule.id, last_run_at=now, next_run_at=next_run)

                    logger.info(f"Process schedule {schedule.process_name}/{schedule.trigger_id} executed successfully, execution_id={process_execution_id}")

                    # Broadcast execution completed
                    await self._publish_event({
                        "type": "process_schedule_execution_completed",
                        "process_id": schedule.process_id,
                        "process_name": schedule.process_name,
                        "schedule_id": schedule.id,
                        "execution_id": execution.id,
                        "process_execution_id": process_execution_id,
                        "status": "success"
                    })

                else:
                    error_msg = f"Backend returned {response.status_code}: {response.text[:200]}"
                    logger.error(f"Process schedule {schedule.process_name} execution failed: {error_msg}")

                    self.db.update_process_schedule_execution(
                        execution_id=execution.id,
                        status=ExecutionStatus.FAILED,
                        error=error_msg
                    )

                    await self._publish_event({
                        "type": "process_schedule_execution_completed",
                        "process_id": schedule.process_id,
                        "process_name": schedule.process_name,
                        "schedule_id": schedule.id,
                        "execution_id": execution.id,
                        "status": "failed",
                        "error": error_msg
                    })

        except httpx.TimeoutException:
            error_msg = "Backend request timed out"
            logger.error(f"Process schedule {schedule.process_name} execution failed: {error_msg}")

            self.db.update_process_schedule_execution(
                execution_id=execution.id,
                status=ExecutionStatus.FAILED,
                error=error_msg
            )

            await self._publish_event({
                "type": "process_schedule_execution_completed",
                "process_id": schedule.process_id,
                "process_name": schedule.process_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "failed",
                "error": error_msg
            })

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Process schedule {schedule.process_name} execution failed: {error_msg}")

            self.db.update_process_schedule_execution(
                execution_id=execution.id,
                status=ExecutionStatus.FAILED,
                error=error_msg
            )

            await self._publish_event({
                "type": "process_schedule_execution_completed",
                "process_id": schedule.process_id,
                "process_name": schedule.process_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "failed",
                "error": error_msg
            })

    def add_process_schedule(self, schedule: ProcessSchedule):
        """Add a new process schedule to the scheduler."""
        if schedule.enabled:
            self._add_process_job(schedule)

    def remove_process_schedule(self, schedule_id: str):
        """Remove a process schedule from the scheduler."""
        self._remove_process_job(schedule_id)

    def update_process_schedule(self, schedule: ProcessSchedule):
        """Update an existing process schedule in the scheduler."""
        self._remove_process_job(schedule.id)
        if schedule.enabled:
            self._add_process_job(schedule)

    # =========================================================================
    # Event Publishing
    # =========================================================================

    async def _publish_event(self, event: dict):
        """
        Publish an event to Redis for WebSocket compatibility.

        Events are published to a channel that the backend can subscribe to.
        """
        if not config.publish_events:
            return

        try:
            event_json = json.dumps(event)
            self.redis.publish("scheduler:events", event_json)
            logger.debug(f"Published event: {event['type']}")
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")

    # =========================================================================
    # Status & Health
    # =========================================================================

    def get_status(self) -> SchedulerStatus:
        """Get current scheduler status."""
        if not self.scheduler:
            return SchedulerStatus(
                running=False,
                jobs_count=0,
                last_check=datetime.utcnow(),
                uptime_seconds=0
            )

        jobs = self.scheduler.get_jobs()
        uptime = (datetime.utcnow() - self._start_time).total_seconds() if self._start_time else 0

        return SchedulerStatus(
            running=self.scheduler.running,
            jobs_count=len(jobs),
            last_check=datetime.utcnow(),
            uptime_seconds=uptime,
            jobs=[
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None
                }
                for job in jobs
            ]
        )

    def is_healthy(self) -> bool:
        """Check if the scheduler is healthy."""
        if not self._initialized:
            return False
        if not self.scheduler or not self.scheduler.running:
            return False
        return True
