"""
Core scheduler service for Trinity platform.

Manages scheduled task execution for agents using APScheduler.
Uses distributed locks to prevent duplicate executions.
"""

import asyncio
import logging
import json
import time
import zoneinfo
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
from .models import (
    Schedule, ScheduleExecution, ExecutionStatus, SchedulerStatus, ProcessSchedule,
    ExecutionOrigin,
)
from .database import SchedulerDatabase
from .locking import get_lock_manager, LockManager
# Shared SUB-003 auth-class classifier (#1088). The scheduler uses it for
# log-labelling ONLY — the call sites below just pick a logger.error wording.
# Aliased to the historical `_is_auth_failure` name so those call sites stay
# unchanged. Source of truth: src/backend/services/failure_classifier.py,
# mirrored byte-identically at src/scheduler/failure_classifier.py.
from .failure_classifier import is_auth_failure as _is_auth_failure
from .utils import to_utc_iso

logger = logging.getLogger(__name__)


def _describe_exception(e: BaseException) -> str:
    """Never let a blank/whitespace-stringifying exception (e.g. httpx timeouts,
    whose str() is '') be persisted as an empty `error` (#1022). Falls back to
    the type name. .strip() so whitespace-only messages also trip the fallback."""
    return str(e).strip() or f"{type(e).__name__} (no detail)"


def _reminder_outcome_unknown(exc: BaseException) -> bool:
    """True when a reminder-dispatch exception means the outcome is UNKNOWN
    (a dispatch TimeoutException — the backend may already be running the task),
    vs a CLEAN pre-start failure (non-200 like a 503 warmup, or a raw connection
    error — the task never started) (#1296, Codex C2).

    ``_call_backend_execute_task`` wraps an ``httpx.TimeoutException`` in a plain
    ``Exception("… outcome unknown") from e``, so we check both the wrapped cause
    and the message; a bare timeout (direct) is caught too. Everything else — a
    non-200 ``Exception`` or an ``httpx.ConnectError`` propagated raw — is a clean
    pre-start failure that SHOULD retry.
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(getattr(exc, "__cause__", None), httpx.TimeoutException):
        return True
    return "outcome unknown" in str(exc).lower()


# #913: Polling-deadline fallback when the schedule's timeout_seconds is
# NULL (= "inherit per-agent value"). The scheduler does not have the
# per-agent value in this process; the backend enforces the real timeout.
# 7200s matches the upper clamp of PUT /api/agents/{name}/timeout — any
# per-agent value is bounded by this, so the scheduler's poll deadline
# never gives up before the backend itself does.
_POLL_DEADLINE_WHEN_NULL = 7200


# #2391: statuses `_poll_execution_completion` must poll THROUGH rather than
# treat as an outcome. `queued` joined `running` when the scheduler's dispatch
# became able to land on the durable pull queue.
_NON_TERMINAL_POLL_STATES = (ExecutionStatus.RUNNING, ExecutionStatus.QUEUED)


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

        # #1472: schedule ids whose _add_job failed PERMANENTLY (bad cron/timezone).
        # Snapshotted-as-known so the sync loop doesn't retry them every tick; the
        # flag is cleared on config change / disable / delete so a fix re-attempts.
        self._permanent_add_failures: set = set()

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
            added = self._add_job(schedule)
            # #1472: snapshot only when registered OR permanently unregisterable;
            # a transient boot-time add failure stays un-snapshotted so the sync
            # loop retries it instead of orphaning it until the next restart.
            if added or schedule.id in self._permanent_add_failures:
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

        # #1296: recover/arm reminder jobs after restart (own try — a reminder
        # error, or a not-yet-migrated agent_reminders table, must never crash
        # boot). _reconcile_reminders is itself fail-open; this is belt-and-braces.
        try:
            self._reconcile_reminders()
        except Exception as e:
            logger.error(f"Reminder boot recovery failed (continuing): {e}")

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
        now = datetime.now(pytz.utc)
        grace = config.misfire_grace_time
        missed = []

        for schedule in schedules:
            if not schedule.next_run_at:
                continue

            # #1472: compare in aware UTC. A non-UTC schedule stores an aware
            # next_run_at; the old code stripped tzinfo WITHOUT converting, so a
            # Europe/Kiev (+03) schedule was mis-compared by the offset (a real
            # missed window looked future, or vice-versa). Convert to UTC instead.
            # #1474: the DB mapper (parse_scheduler_ts) now returns **naive UTC**
            # — the instant is already correct (convert-then-strip), so the
            # `tzinfo is None` branch below just re-stamps UTC. Both branches stay
            # correct; keep them for datetimes sourced outside the mapper.
            expected = schedule.next_run_at
            if expected.tzinfo is not None:
                expected = expected.astimezone(pytz.utc)
            else:
                expected = expected.replace(tzinfo=pytz.utc)

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
                    last = last.astimezone(pytz.utc)
                else:
                    last = last.replace(tzinfo=pytz.utc)
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

    def _add_job(self, schedule: Schedule) -> bool:
        """Add a schedule as an APScheduler job. Returns True iff registered.

        #1472: on failure, classify **permanent** (bad cron/timezone — this
        config can never register) vs **transient**. A permanent failure is
        recorded in ``self._permanent_add_failures`` and logged ONCE so callers
        can snapshot it (bounded — no 60s retry-storm); a transient failure
        returns False without recording, so the sync loop retries next tick.
        """
        if not self.scheduler:
            return False

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

            # Recovered from any prior failure — clear the permanent flag.
            self._permanent_add_failures.discard(schedule.id)
            logger.info(f"Added schedule job: {job_id} ({schedule.name}) for agent {schedule.agent_name}")
            return True
        except (
            pytz.exceptions.UnknownTimeZoneError,
            ValueError,
            # #1823: a zone pytz knows but this runtime's IANA database lacks —
            # a legacy alias such as `Europe/Kiev` when the image ships no
            # backward-compatibility links. APScheduler's `astimezone()` raises
            # it from inside CronTrigger, and it is a KeyError subclass, so it
            # is NOT a ValueError and fell through to the broad `except
            # Exception` below: the TRANSIENT arm. The row was therefore never
            # snapshotted into _permanent_add_failures, so #1472's bounded-retry
            # machinery was defeated — it retried every 60s forever, logged an
            # ERROR on every sync tick, and froze `next_run_at` into exactly the
            # "Next: Nd ago" symptom #1472 existed to eliminate (canary E-06
            # fires on it each cycle).
            #
            # Named explicitly, NEVER a bare KeyError: this arm SUPPRESSES
            # retries, so widening it would permanently park schedules whose
            # registration failed for a genuinely transient reason.
            zoneinfo.ZoneInfoNotFoundError,
        ) as e:
            # #1472: permanent — a bad timezone or cron that clears the backend's
            # looser croniter check but fails _parse_cron/CronTrigger. Log ONCE and
            # record so the sync loop snapshots it (bounded) instead of retrying
            # every 60s. Cleared when the config changes (see _sync_agent_schedules).
            if schedule.id not in self._permanent_add_failures:
                logger.error(
                    f"Failed to add schedule job {job_id} "
                    f"(permanent — invalid cron/timezone, will not retry until "
                    f"reconfigured): {e}"
                )
                self._permanent_add_failures.add(schedule.id)
            return False
        except Exception as e:
            # #1472: transient (jobstore hiccup, etc.) — do NOT record; the sync
            # loop leaves it un-snapshotted and retries on the next tick.
            logger.error(f"Failed to add schedule job {job_id} (transient, will retry): {e}")
            return False

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

    def _advance_next_run_only(self, schedule) -> None:
        """#1472: advance the ``next_run_at`` projection to the next future cron
        time WITHOUT recording a run (no ``last_run_at``, no execution row).

        Used by the skip paths (autonomy-off) so an enabled schedule never shows
        a stale past "Next". Advances only when the stored projection is missing
        or already in the past — a write-amplification guard, since the
        autonomy-off path is the default state and can fire every tick.
        """
        stored = schedule.next_run_at
        if stored is not None:
            # Compare tz-aware in UTC: a non-UTC schedule stores an aware
            # next_run_at, so stripping tzinfo would mis-compare by the offset
            # (the same class as the _get_missed_schedules bug fixed in #1472).
            # #1474: the DB mapper now returns naive-UTC (instant preserved), so
            # this hits the `else` re-stamp branch — still correct.
            s = stored if stored.tzinfo is not None else stored.replace(tzinfo=pytz.utc)
            if s > datetime.now(pytz.utc):
                return  # projection already future — nothing to repair
        next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
        if next_run:
            self.db.update_schedule_run_times(schedule.id, next_run_at=next_run)

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

        # #1296: reminder reconcile in its OWN try/except, NOT under the cron/
        # process try above (Codex C5) — a cron-sync error must not starve
        # reminder pickup. Latency ≤ one schedule_reload_interval, same as a
        # brand-new cron schedule.
        try:
            self._reconcile_reminders()
        except Exception as e:
            logger.error(f"Reminder reconcile failed: {e}")

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
                added = self._add_job(schedule)
                # #1472: leave a transient add-failure un-snapshotted so the next
                # sync retries it (a permanent failure IS snapshotted → bounded).
                if not (added or schedule_id in self._permanent_add_failures):
                    continue
            self._schedule_snapshot[schedule_id] = current_state[schedule_id]

        # Deleted schedules (in snapshot but not in database)
        deleted_ids = snapshot_ids - current_ids
        for schedule_id in deleted_ids:
            logger.info(f"Sync: Removing deleted schedule {schedule_id}")
            self._remove_job(schedule_id)
            del self._schedule_snapshot[schedule_id]
            self._permanent_add_failures.discard(schedule_id)

        # Updated schedules (still exists but state changed)
        for schedule_id in (snapshot_ids & current_ids):
            old_state = self._schedule_snapshot[schedule_id]
            new_state = current_state[schedule_id]

            if old_state != new_state:
                schedule = schedule_map[schedule_id]
                old_enabled, old_updated = old_state
                new_enabled, new_updated = new_state
                add_ok = True  # non-add transitions (disable) always snapshot

                if old_enabled and not new_enabled:
                    # Schedule was disabled
                    logger.info(f"Sync: Disabling schedule {schedule.name}")
                    self._remove_job(schedule_id)
                    self._permanent_add_failures.discard(schedule_id)
                elif not old_enabled and new_enabled:
                    # Schedule was enabled
                    logger.info(f"Sync: Enabling schedule {schedule.name}")
                    add_ok = self._add_job(schedule) or schedule_id in self._permanent_add_failures
                elif new_enabled and old_updated != new_updated:
                    # Schedule was updated (and still enabled). Config changed, so a
                    # prior permanent failure may now register — clear + re-attempt.
                    logger.info(f"Sync: Updating schedule {schedule.name}")
                    self._remove_job(schedule_id)
                    self._permanent_add_failures.discard(schedule_id)
                    add_ok = self._add_job(schedule) or schedule_id in self._permanent_add_failures

                # #1472: advance the snapshot unless a transient (re-)add failed —
                # then leave it stale so the next sync retries.
                if add_ok:
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

    def _record_skipped_agent_schedule(
        self,
        schedule_id: str,
        skip_reason: str = "Previous execution still running (max_instances reached)",
        event_reason: str = "Previous execution still running",
    ):
        """
        Record a skipped agent schedule execution in the database.

        Creates an execution record with status='skipped' so it appears in
        the execution history and provides an audit trail.

        `skip_reason` / `event_reason` are parameterised (#1808) so the
        sync-freeze gate can reuse this exact audit path; the defaults preserve
        the original max_instances wording for the EVENT_JOB_MAX_INSTANCES
        caller.
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
                skip_reason=skip_reason
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
                    "reason": event_reason
                }))
            else:
                logger.error(f"Failed to create skipped execution record for schedule {schedule_id}")

        except Exception as e:
            logger.error(f"Error recording skipped execution for schedule {schedule_id}: {e}")

    def _record_skipped_process_schedule(
        self,
        schedule_id: str,
        skip_reason: str = "Previous execution still running (max_instances reached)",
        event_reason: str = "Previous execution still running",
    ):
        """
        Record a skipped process schedule execution in the database.

        Creates an execution record with status='skipped' so it appears in
        the execution history and provides an audit trail.

        `skip_reason` / `event_reason` are parameterised (#1994) so the
        lock-denial branch of `_execute_process_schedule` can reuse this exact
        audit path and still name the mechanism that actually suppressed the
        run; the defaults preserve the original max_instances wording for the
        EVENT_JOB_MAX_INSTANCES caller. Mirrors the agent-schedule sibling,
        which gained the same parameters in #1808.
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
                skip_reason=skip_reason
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
                    "reason": event_reason
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
            # #1969: a suppressed tick must leave a record. Suppression is the
            # correct behaviour — what was missing is the evidence it happened.
            # With only the INFO line above, a tick denied by the lock is
            # indistinguishable from a tick that never fired, in the execution
            # history, the UI, and monitoring alike.
            #
            # There are two ways a run gets suppressed and only one was
            # audited. APScheduler's `max_instances=1` refusal writes a
            # `skipped` row via _on_job_max_instances; this branch returned
            # bare. They do not overlap — a max_instances refusal means the job
            # never started, so _execute_schedule is never entered and no lock
            # is attempted; conversely reaching this line means APScheduler
            # already let the job start. Exactly one of the two fires per tick.
            #
            # The gap mattered most for the common case: a MANUAL trigger
            # bypasses APScheduler entirely (_trigger_handler dispatches via
            # asyncio.create_task), so its instance counter stays at zero, the
            # cron job starts normally, and the collision is caught one layer
            # down — here.
            self._record_skipped_agent_schedule(
                schedule_id,
                skip_reason="Previous execution still running (distributed lock held)",
                event_reason="Previous execution still running",
            )
            return

        try:
            await self._execute_schedule_with_lock(schedule_id)
        finally:
            lock.release()

    def _abandon_precreated_execution(self, execution, reason: str) -> None:
        """Fail an execution row created before this run could be aborted (#1968).

        A manual trigger now creates its row synchronously so the caller gets a
        real id back, which means the row can already exist when a gate below
        decides not to run. Leaving it `running` forever is the worse outcome:
        canary E-01 flags exactly that, and the UI would show a task that never
        ends. Fail it with the reason instead. Best-effort — an abort path must
        not raise.
        """
        if execution is None:
            return
        try:
            # CAS on RUNNING: this row was created moments ago by the trigger
            # and every abandon gate runs BEFORE dispatch, so nothing else can
            # have written it — but "safe by argument" is what #1082 exists to
            # retire, and the precondition is one clause.
            abandoned = self.db.update_execution_status(
                execution.id,
                ExecutionStatus.FAILED,
                error=reason,
                expected_status=ExecutionStatus.RUNNING,
            )
            if abandoned:
                logger.info(f"Abandoned pre-created execution {execution.id}: {reason}")
            else:
                logger.info(
                    f"Pre-created execution {execution.id} already left RUNNING — "
                    f"not abandoning ({reason})"
                )
        except Exception as exc:
            logger.error(
                f"Could not abandon pre-created execution {execution.id}: {exc}"
            )

    async def _execute_schedule_with_lock(
        self,
        schedule_id: str,
        triggered_by: str = "schedule",
        origin: Optional[ExecutionOrigin] = None,
        execution=None,
    ):
        """Execute schedule after acquiring lock.

        Args:
            schedule_id: ID of the schedule to execute
            triggered_by: Source of trigger - "schedule" (cron) or "manual"
            origin: Who initiated the run (#1970). None for a cron tick — there
                is no caller, and the execution row records that honestly as
                NULLs rather than inventing an owner.
            execution: A row already created by the caller (#1968). The manual
                trigger endpoint creates it synchronously so it can return a
                real `execution_id`; passing it here is what stops this method
                creating a SECOND row for the same run. None on the cron path,
                which still creates its own.
        """
        schedule = self.db.get_schedule(schedule_id)
        if not schedule:
            logger.error(f"Schedule {schedule_id} not found")
            # Deleted between the trigger response and this task starting.
            self._abandon_precreated_execution(
                execution, "Schedule was deleted before the run started"
            )
            return

        if not schedule.enabled and triggered_by == "schedule":
            logger.info(f"Schedule {schedule_id} is disabled, skipping")
            self._abandon_precreated_execution(
                execution, "Schedule was disabled before the run started"
            )
            return

        # Check if agent has autonomy enabled (only for cron-triggered, not manual)
        if triggered_by == "schedule" and not self.db.get_autonomy_enabled(schedule.agent_name):
            logger.info(f"Schedule {schedule_id} skipped: agent {schedule.agent_name} autonomy is disabled")
            # #1472: advance next_run_at ONLY so an enabled schedule on an
            # autonomy-off agent never displays a receding "Next: Nd ago". No
            # execution row (autonomy-off is a static config gate, not a per-fire
            # event — a skipped row per tick would flood schedule_executions for
            # the default-OFF fleet) and no last_run_at (nothing ran; setting it
            # would suppress the _get_missed_schedules catch-up).
            self._advance_next_run_only(schedule)
            self._abandon_precreated_execution(
                execution, "Agent autonomy was disabled before the run started"
            )
            return

        # #1808: git-sync freeze gate. The owner opted in via
        # `freeze_schedules_if_sync_failing` (#389) so the agent stops doing
        # autonomous work while its repo is broken — until now the flag was
        # stored, reported back as enabled, and never enforced, and
        # docs/user-docs/faq/troubleshooting.md told users it worked.
        #
        # Cron only: a manual trigger is an operator explicitly asking, exactly
        # like the autonomy gate above.
        #
        # Unlike the autonomy branch this DOES record a skipped execution row.
        # Autonomy-off is a static config gate on a default-OFF fleet, so a row
        # per tick would flood the table; a sync freeze needs BOTH an opt-in
        # toggle AND 3+ consecutive real sync failures, so the row count is
        # bounded and it is exactly the signal an operator needs to see. Uses
        # the same audit path as the max_instances skip.
        if triggered_by == "schedule" and self.db.should_freeze_schedules(schedule.agent_name):
            logger.warning(
                f"Schedule {schedule_id} skipped: agent {schedule.agent_name} git sync is "
                f"failing and freeze_schedules_if_sync_failing is enabled"
            )
            self._record_skipped_agent_schedule(
                schedule_id,
                skip_reason=(
                    "Git sync is failing and freeze_schedules_if_sync_failing is "
                    "enabled for this agent"
                ),
                event_reason="Git sync failing (schedules frozen)",
            )
            # Same projection advance as the autonomy branch (#1472) so the
            # schedule never renders a receding "Next: Nd ago" while frozen.
            self._advance_next_run_only(schedule)
            self._abandon_precreated_execution(
                execution, "Git sync started failing before the run started"
            )
            return

        # Agent-owned pre-check gate (#454): may skip the firing entirely or
        # override the message. Manual triggers always fire.
        should_fire, effective_message = await self._apply_pre_check_gate(schedule, triggered_by)
        if not should_fire:
            # Unreachable for a pre-created row: the gate short-circuits to
            # (True, schedule.message) for anything but triggered_by="schedule",
            # and only manual/webhook triggers pre-create. Abandon anyway — a
            # future gate change must not silently strand a running row.
            self._abandon_precreated_execution(
                execution, "Agent pre-check declined the run"
            )
            return

        logger.info(f"Executing schedule: {schedule.name} for agent {schedule.agent_name} (triggered_by={triggered_by})")

        # #1968: the manual path already created the row, synchronously, so its
        # id could be returned to the caller. Creating another here would give
        # one trigger two executions — the caller's id would name a row that
        # never runs while a second one did the work.
        if execution is None:
            origin = origin or ExecutionOrigin()
            execution = self.db.create_execution(
                schedule_id=schedule.id,
                agent_name=schedule.agent_name,
                message=effective_message,
                triggered_by=triggered_by,
                model_used=schedule.model,
                source_user_id=origin.user_id,
                source_user_email=origin.user_email,
                source_agent_name=origin.agent_name,
                source_mcp_key_id=origin.mcp_key_id,
                source_mcp_key_name=origin.mcp_key_name
            )

            if not execution:
                logger.error(f"Failed to create execution record for schedule {schedule_id}")
                return

        # #1472: advance the run-time projection ONCE, at fire time, regardless of
        # the eventual outcome — the single "this window was consumed" write. It
        # replaces the per-branch advances in _dispatch_and_record_outcome
        # (success + dispatched) and covers the failure and dispatch-exception
        # paths, which previously left next_run_at stale. Placed after the
        # create_execution guard so a genuine INSERT failure doesn't advance.
        _fire_now = datetime.utcnow()
        _fire_next = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
        self.db.update_schedule_run_times(schedule.id, last_run_at=_fire_now, next_run_at=_fire_next)

        # Broadcast execution started
        await self._publish_event({
            "type": "schedule_execution_started",
            "agent": schedule.agent_name,
            "schedule_id": schedule.id,
            "execution_id": execution.id,
            "schedule_name": schedule.name,
            "triggered_by": triggered_by
        })

        await self._dispatch_and_record_outcome(schedule, execution, effective_message, triggered_by)

    async def _apply_pre_check_gate(self, schedule, triggered_by: str) -> tuple[bool, str]:
        """Agent-owned pre-check hook (#454). Cron-triggered only — manual
        triggers from the UI are an explicit operator decision and always fire.

        Returns ``(should_fire, effective_message)``. On a ``fire=false``
        decision this records a skipped execution, advances the schedule run
        times, and publishes the skipped event, then returns
        ``(False, ...)`` — the caller aborts. A string ``message`` override is
        threaded back as ``effective_message``. Fail-open: a ``None`` decision
        (no hook, or the call errored) falls through to normal firing.
        """
        effective_message = schedule.message
        if triggered_by != "schedule":
            return True, effective_message

        decision = await self._run_pre_check(schedule.agent_name)
        if decision is None:
            return True, effective_message

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
            return False, effective_message

        override = decision.get("message")
        if override and isinstance(override, str):
            effective_message = override
            logger.info(
                f"Schedule {schedule.name} pre-check overrode message "
                f"({len(override)} chars)"
            )
        return True, effective_message

    async def _dispatch_and_record_outcome(self, schedule, execution, effective_message: str, triggered_by: str):
        """Dispatch the schedule to the backend's TaskExecutionService (same path
        as manual/public tasks: slots, activity tracking, sanitization, capacity
        meter) and record the outcome.

        Handles the three result modes — fire-and-forget ``dispatched`` (#132,
        advance run times + return, the background poll emits completion),
        ``success``, and failure — plus the exception path's SCHED-ASYNC-001
        anti-overwrite guard (don't clobber an execution the backend already
        finalized).
        """
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
                # #1472: run-time projection already advanced at fire time in
                # _execute_schedule_with_lock; the background poll finalizes.
                logger.info(
                    f"Schedule {schedule.name} dispatched (fire-and-forget), "
                    f"execution_id={execution.id}, background poll running"
                )
                return  # Background task handles completion events

            if status == ExecutionStatus.SUCCESS:
                # #1472: run-time projection already advanced at fire time.
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
                # TaskExecutionService already updated the execution record with
                # failure status. The run-time projection was advanced at fire
                # time (#1472); here we only classify auth errors for logging.
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
            # #1022: never persist a blank error — the cron path's only handler
            # is generic, so a blank-stringifying dispatch timeout lands here.
            error_msg = _describe_exception(e)
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
                    # #1022: configurable (agent-side hook is 60s; default 70s headroom)
                    timeout=config.pre_check_timeout,
                )
        except Exception as e:
            # #1022: show the timeout type instead of the empty parens a blank
            # httpx exception would log.
            logger.warning(
                f"[pre-check] backend call for {agent_name} failed "
                f"({_describe_exception(e)}) — fail-open"
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
        1. POST with async_mode=True and the configured dispatch deadline
           (config.dispatch_timeout, default 30s — dispatch only)
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

        # Step 1: Dispatch with the configured deadline (#1022 — was a 30s
        # literal). The async endpoint normally returns ~instantly; reaching
        # this ceiling means the backend did not RESPOND in time, NOT that the
        # task was rejected (the bg task may already be running — outcome
        # unknown; see the D4 follow-up).
        dispatch_timeout = config.dispatch_timeout

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{config.backend_url}/api/internal/execute-task",
                    headers=headers,
                    json=payload,
                    timeout=dispatch_timeout,
                )
            except httpx.TimeoutException as e:
                # Outcome UNKNOWN: the backend may have accepted+started the task
                # before the timeout (orphan; see D4 follow-up). Name the
                # threshold + subtype so the persisted `error` is never blank
                # (httpx timeouts str() to '' — the #1022 silent-failure root).
                raise Exception(
                    f"dispatch to /api/internal/execute-task timed out after "
                    f"{dispatch_timeout}s ({type(e).__name__}) — outcome unknown"
                ) from e

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

            # #2391: `queued` is NOT a terminal. A pull-pilot agent's scheduled
            # row is handed to the durable queue and sits there until a worker
            # claims it back to `running`; treating that as "completed" would
            # publish a bogus schedule_execution_completed(status=queued),
            # classify it as a failure (anything != success is), and hand it to
            # `_maybe_schedule_retry` — a duplicate run of work that is queued
            # and about to run. Poll through it like `running`.
            if execution.status not in _NON_TERMINAL_POLL_STATES:
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
                logger.info(
                    f"Execution {execution_id} still {execution.status} "
                    f"({elapsed}s elapsed, poll #{poll_count})"
                )

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
            if current and current.status not in _NON_TERMINAL_POLL_STATES:
                logger.info(
                    f"Execution {execution_id} already finalized as '{current.status}' "
                    "— background poll error is benign"
                )
            else:
                # #2391: name the state we actually saw. A row still `queued` is
                # waiting for a pull worker, and its owner is the lease reaper /
                # backlog maintenance, not `cleanup_service`'s stale-running sweep.
                logger.warning(
                    f"Execution {execution_id} may be stuck in "
                    f"'{current.status if current else 'unknown'}' state — "
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

        # #1970: a retry inherits the original run's attribution. The retry has
        # no caller of its own, but it is not anonymous — it exists only because
        # someone (or a cron tick) started the original, and a chain of retries
        # that drops the initiator makes the very first attempt the only
        # attributable one.
        #
        # Fail-open around the READ, not merely around a missing row: this is a
        # brand-new DB call on the retry path, and letting it raise would trade
        # "the retry is unattributed" for "the retry never runs". A row already
        # purged by retention lands in the same place — no origin, still fires.
        origin = ExecutionOrigin()
        try:
            original = self.db.get_execution(original_execution_id)
            if original:
                origin = ExecutionOrigin(
                    user_id=original.source_user_id,
                    user_email=original.source_user_email,
                    agent_name=original.source_agent_name,
                    mcp_key_id=original.source_mcp_key_id,
                    mcp_key_name=original.source_mcp_key_name,
                )
        except Exception as exc:
            logger.warning(
                f"Could not read the origin of {original_execution_id} for its "
                f"retry — the retry proceeds unattributed: {exc}"
            )

        # Create a new execution record for the retry
        retry_execution = self.db.create_execution(
            schedule_id=schedule_id,
            agent_name=agent_name,
            message=message,
            triggered_by="retry",
            model_used=model,
            attempt_number=next_attempt_number,
            retry_of_execution_id=original_execution_id,
            source_user_id=origin.user_id,
            source_user_email=origin.user_email,
            source_agent_name=origin.agent_name,
            source_mcp_key_id=origin.mcp_key_id,
            source_mcp_key_name=origin.mcp_key_name
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
                error=_describe_exception(e)[:2000]  # #1022: never blank
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
    # Agent Self-Reminders (#1296)
    #
    # A near-clone of the RETRY-001 one-shot machinery: a durable DB row is the
    # source of truth, an APScheduler DateTrigger is armed per pending reminder,
    # and the reconcile (boot + 60s sync-loop + reload) re-arms/reclaims from the
    # DB. On fire the scheduler CREATES the execution row itself (real id) and
    # dispatches a normal execution of the same agent through the standard
    # capacity/slot path (triggered_by="reminder") — no bespoke dispatch.
    # =========================================================================

    def _schedule_reminder_job(self, reminder, run_at: datetime):
        """Arm a one-shot DateTrigger for a reminder (mirrors _schedule_retry_job).

        Deterministic job id + replace_existing ⇒ re-arming is idempotent at the
        APScheduler level, so the reconcile can safely re-arm without dup jobs.
        """
        from apscheduler.triggers.date import DateTrigger

        self.scheduler.add_job(
            self._execute_reminder,
            trigger=DateTrigger(run_date=run_at),
            id=f"reminder_{reminder.id}",
            kwargs={
                "reminder_id": reminder.id,
                "agent_name": reminder.agent_name,
                "message": reminder.message,
                "model": reminder.model,
                "timeout_seconds": reminder.timeout_seconds,
                "allowed_tools": reminder.allowed_tools,
            },
            replace_existing=True,
        )

    def _reminder_after_failed_attempt(self, reminder_id: str, attempts: int, error: str):
        """A clean pre-start failure or a stale-firing reclaim: retry (bounded)
        by releasing firing→pending for the reconcile to re-arm, or — once
        fire_attempts hits the cap — mark the reminder terminal `failed`."""
        if attempts >= config.max_reminder_fire_attempts:
            self.db.mark_reminder_failed(reminder_id, (error or "")[:2000])
            logger.error(
                f"Reminder {reminder_id} failed permanently after {attempts} "
                f"attempts: {error}"
            )
        else:
            self.db.release_reminder_to_pending(reminder_id, (error or "")[:2000])
            logger.warning(
                f"Reminder {reminder_id} attempt {attempts} failed "
                f"({error}) — released to pending for retry"
            )

    async def _execute_reminder(
        self,
        reminder_id: str,
        agent_name: str,
        message: str,
        model: Optional[str],
        timeout_seconds: Optional[int],
        allowed_tools: Optional[list],
    ):
        """Fire handler for a reminder (at-least-once, bounded, observable, #1296).

        Called by APScheduler when the DateTrigger fires.
        """
        # 1. Single-fire claim (CAS pending→firing, committed, increments
        #    fire_attempts). False ⇒ another fire won / cancelled / already
        #    firing → skip. Also cancel-safe (a cancelled reminder is no longer
        #    pending).
        if not self.db.claim_reminder_firing(reminder_id):
            logger.info(
                f"Reminder {reminder_id} not claimable (cancelled/fired/already "
                "firing) — skipping"
            )
            return

        # Re-read for the current fire_attempts (drives the bounded-retry cap).
        reminder = self.db.get_reminder_by_id(reminder_id)
        attempts = reminder.fire_attempts if reminder else 1

        # #1970: a reminder fires with no live caller, but it is not anonymous —
        # #1296 already persisted who set it. Carry that provenance onto the
        # execution row so "who caused this run?" is answerable from the DB.
        origin = (
            ExecutionOrigin(
                user_id=reminder.owner_id,
                user_email=reminder.created_by_email,
                agent_name=reminder.source_agent_name or agent_name,
                mcp_key_id=reminder.source_mcp_key_id,
            )
            if reminder
            else ExecutionOrigin(agent_name=agent_name)
        )

        execution_id = None
        try:
            # 2. Create the execution row up-front (REAL id, __manual__ marker —
            #    PG-safe, no FK; triggered_by="reminder"; status RUNNING). The
            #    real id auto-stamps Idempotency-Key: sched:{id} and lets
            #    _poll_and_finalize own the execution's terminal.
            execution = self.db.create_execution(
                schedule_id="__manual__",
                agent_name=agent_name,
                message=message,
                triggered_by="reminder",
                model_used=model,
                source_user_id=origin.user_id,
                source_user_email=origin.user_email,
                source_agent_name=origin.agent_name,
                source_mcp_key_id=origin.mcp_key_id,
                source_mcp_key_name=origin.mcp_key_name,
            )
            if not execution:
                raise Exception("failed to create reminder execution row")
            execution_id = execution.id
            self.db.set_reminder_execution(reminder_id, execution_id)

            # 3. Dispatch through the standard backend path (capacity/slot admit).
            await self._call_backend_execute_task(
                agent_name=agent_name,
                message=message,
                triggered_by="reminder",
                timeout_seconds=timeout_seconds,
                model=model,
                allowed_tools=allowed_tools,
                execution_id=execution_id,
            )
        except Exception as e:
            error_msg = _describe_exception(e)
            if execution_id is not None and _reminder_outcome_unknown(e):
                # Dispatch outcome UNKNOWN (timeout) — the backend may already be
                # running the task. Assume dispatched: mark fired and let
                # _poll_and_finalize finalize the execution row. Do NOT force it
                # FAILED (would clobber a running task) and do NOT retry (a fresh
                # execution_id ⇒ fresh key ⇒ not deduped ⇒ double-execute).
                logger.warning(
                    f"Reminder {reminder_id} dispatch outcome unknown "
                    f"({error_msg}) — assuming dispatched (firing→fired)"
                )
                self.db.mark_reminder_fired(reminder_id)
                return
            # Clean pre-start failure (non-200 503-warmup/5xx, connection error)
            # OR a row-creation failure — the task never started. Mark the
            # attempt's execution row FAILED (status-guarded, mirrors
            # service.py:989) then bounded-retry (this is what makes AC #3 hold —
            # a reminder due during a backend restart retries instead of being
            # permanently consumed).
            logger.error(
                f"Reminder {reminder_id} dispatch failed cleanly ({error_msg})"
            )
            if execution_id is not None:
                current = self.db.get_execution(execution_id)
                if current and current.status == ExecutionStatus.RUNNING:
                    self.db.update_execution_status(
                        execution_id=execution_id,
                        status=ExecutionStatus.FAILED,
                        error=error_msg[:2000],
                    )
            self._reminder_after_failed_attempt(reminder_id, attempts, error_msg)
            return

        # 4. Dispatch accepted (200/dispatched) → delivered.
        self.db.mark_reminder_fired(reminder_id)
        logger.info(
            f"Reminder {reminder_id} fired for agent {agent_name} "
            f"(execution_id={execution_id})"
        )

    def _reconcile_reminders(self):
        """Steady-state pickup + boot recovery + stale-`firing` reclaim (#1296).

        FAIL-OPEN (eng D + Codex C5): the WHOLE body is wrapped so a not-yet-
        migrated ``agent_reminders`` table (a new scheduler image before the
        backend applies 0028) or any error is a logged no-op, never a boot
        crash-loop nor a starve of the other syncs. Sync (no dispatch happens
        here — only arming + reclaim), so boot (sync ``initialize``) can call it
        directly. Idempotent: it only arms MISSING ``reminder_`` jobs and never
        blanket-removes them.
        """
        try:
            reminders = self.db.get_active_reminders()
        except Exception as e:
            logger.warning(
                f"Reminder reconcile skipped (table absent or read error): {e}"
            )
            return

        # #1806: reminders on autonomy-disabled agents are filtered out of
        # get_active_reminders, so they are never armed and produce no job and
        # no skip line — the reconcile is the only place that can say they
        # exist. Log a COUNT (not a row per reminder) once per pass, and only
        # when non-zero, so a paused fleet doesn't spam the log. Best-effort:
        # observability must never break the reconcile.
        try:
            held = self.db.count_held_reminders()
            if held:
                logger.info(
                    f"{held} reminder(s) held: their agent's autonomy is disabled. "
                    f"They will fire past-due when autonomy is re-enabled."
                )
        except Exception as e:
            logger.debug(f"Held-reminder count unavailable: {e}")

        now = datetime.utcnow()
        # A firing row older than this (with no live job) is a crash-mid-fire
        # orphan: the dispatch window + poll buffer + a margin.
        stale_after = timedelta(
            seconds=config.dispatch_timeout + config.poll_deadline_buffer + 60
        )

        for reminder in reminders:
            try:
                if reminder.status == "pending":
                    if self.scheduler.get_job(f"reminder_{reminder.id}") is None:
                        run_at = reminder.fire_at
                        if run_at < now:
                            # Past-due (created while the scheduler was down, or
                            # autonomy was just re-enabled) — fire shortly
                            # (mirror _recover_pending_retries).
                            run_at = now + timedelta(seconds=5)
                        self._schedule_reminder_job(reminder, run_at)
                elif reminder.status == "firing":
                    # Reclaim a stale firing row (crash mid-fire, no live job).
                    if reminder.firing_at is not None and (now - reminder.firing_at) < stale_after:
                        continue
                    if self.scheduler.get_job(f"reminder_{reminder.id}") is not None:
                        continue
                    # FAIL any orphan RUNNING execution row (status-guarded).
                    if reminder.execution_id:
                        current = self.db.get_execution(reminder.execution_id)
                        if current and current.status == ExecutionStatus.RUNNING:
                            self.db.update_execution_status(
                                execution_id=reminder.execution_id,
                                status=ExecutionStatus.FAILED,
                                error="reminder fire reclaimed (stale firing / crash mid-fire)",
                            )
                    self._reminder_after_failed_attempt(
                        reminder.id, reminder.fire_attempts, "stale firing reclaimed"
                    )
            except Exception as e:
                logger.error(
                    f"Reminder reconcile: failed to process reminder {reminder.id}: {e}"
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
            """, (business_status, to_utc_iso(datetime.utcnow()), execution_id))
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

        # #1296: the full-reload path removes only schedule_/process_schedule_
        # jobs (Codex C6), so rebuild/reclaim reminder jobs too. The reconcile is
        # idempotent — it only arms MISSING reminder_ jobs and never blanket-
        # removes them, so this doesn't disturb already-armed reminders.
        try:
            self._reconcile_reminders()
        except Exception as e:
            logger.error(f"Reminder reconcile during reload failed: {e}")

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
            # #1994: the sibling of #1969, on the process-schedule path.
            # Suppression is correct — two concurrent runs of one schedule is
            # what the lock exists to prevent. What was missing is the evidence
            # it happened: with only the INFO line above, a denied tick is
            # indistinguishable from a tick that never fired, in the execution
            # history, the UI, and monitoring alike.
            #
            # The two suppression paths do not overlap, which is what makes
            # calling the same helper from both safe (no #91-style duplicate
            # skipped+success pairing). APScheduler's `max_instances=1` refusal
            # fires EVENT_JOB_MAX_INSTANCES -> _on_job_max_instances -> a skipped
            # row, and the job never starts, so this function is never entered.
            # Reaching this line means APScheduler already let the job start,
            # so no max-instances event fires. Exactly one of the two per tick.
            self._record_skipped_process_schedule(
                schedule_id,
                skip_reason="Previous execution still running (distributed lock held)",
                event_reason="Previous execution still running",
            )
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
            # #1022 defense-in-depth: process-schedule timeouts already have a
            # dedicated non-empty handler above; this only sees other blank
            # exceptions, normalized here for consistency.
            error_msg = _describe_exception(e)
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
