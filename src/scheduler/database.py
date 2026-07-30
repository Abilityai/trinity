"""
Database access layer for the scheduler service.

Provides read-only access to schedules and write access to executions.
Uses the same SQLite database as the main backend.
"""

import json
import os
import secrets
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List

from .config import config
from .models import Schedule, ScheduleExecution, ExecutionStatus, ProcessSchedule, ProcessScheduleExecution, Reminder
from .utils import utc_now_iso, to_utc_iso, parse_scheduler_ts

logger = logging.getLogger(__name__)

# #389/#1808: consecutive failed syncs before a freeze-enabled agent stops
# firing. Mirrors the threshold in the backend's
# routers/internal.py::internal_agent_sync_health — keep the two in step.
SYNC_FAILURE_FREEZE_THRESHOLD = 3


def _scheduler_pg_url() -> Optional[str]:
    """Return the PostgreSQL ``DATABASE_URL`` if configured, else None (#300).

    The scheduler shares the backend's database. When the backend runs on
    PostgreSQL, ``DATABASE_URL`` is a ``postgresql://`` URL and the scheduler
    MUST read from PostgreSQL too — otherwise it reads a stale SQLite file and
    silently never fires a schedule. When ``DATABASE_URL`` is unset (or
    ``sqlite://``), the scheduler keeps using the shared SQLite file at
    ``database_path`` exactly as before.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if url and not url.startswith("sqlite"):
        return url
    return None


# IntegrityError across both backends (psycopg2 only importable in PG deploys).
_INTEGRITY_ERRORS: tuple = (sqlite3.IntegrityError,)
try:  # pragma: no cover - depends on deployment
    import psycopg2 as _psycopg2
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError, _psycopg2.IntegrityError)
except Exception:
    pass


class _PgCursor:
    """Adapt a psycopg2 cursor to the sqlite3 cursor API the scheduler uses:
    qmark (``?``) params, Python-bool→int coercion for INTEGER columns, and
    dict-style row access (the psycopg2 RealDictCursor returns mappings, so the
    existing ``row["col"]`` access works unchanged). The scheduler's SQL is
    plain ANSI with no ``%``/``LIKE``/sqlite-only constructs, so the ``?``→``%s``
    rewrite is safe.
    """

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        pg_params = (
            tuple(int(p) if isinstance(p, bool) else p for p in params)
            if params else params
        )
        self._cur.execute(sql.replace("?", "%s"), pg_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        self._cur.close()


class _PgConn:
    """Minimal connection shim exposing the sqlite3 conn API used by the scheduler."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class SchedulerDatabase:
    """
    Database access for the scheduler service.

    Thread-safe SQLite access with connection pooling.
    """

    def __init__(self, database_path: str = None):
        """Initialize database with path."""
        self.database_path = database_path or config.database_path
        if _scheduler_pg_url():
            logger.info("Scheduler database: PostgreSQL (via DATABASE_URL)")
        else:
            logger.info(f"Scheduler database initialized: {self.database_path}")

    @contextmanager
    def get_connection(self):
        """Get a database connection with dict-style row access.

        PostgreSQL when ``DATABASE_URL`` points at it (#300), else the shared
        SQLite file. Both yield connections whose cursors accept ``?`` params
        and return ``row["col"]``-accessible rows, so all query methods below
        are backend-agnostic and unchanged.
        """
        pg_url = _scheduler_pg_url()
        if pg_url:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            conn = _PgConn(psycopg2.connect(pg_url, cursor_factory=RealDictCursor))
        else:
            conn = sqlite3.connect(self.database_path)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> Schedule:
        """Convert a database row to a Schedule model."""
        row_keys = row.keys() if hasattr(row, 'keys') else []

        # Parse allowed_tools from JSON if present
        allowed_tools = None
        if "allowed_tools" in row_keys and row["allowed_tools"]:
            try:
                allowed_tools = json.loads(row["allowed_tools"])
            except (json.JSONDecodeError, TypeError):
                allowed_tools = None

        return Schedule(
            id=row["id"],
            agent_name=row["agent_name"],
            name=row["name"],
            cron_expression=row["cron_expression"],
            message=row["message"],
            enabled=bool(row["enabled"]),
            timezone=row["timezone"],
            description=row["description"],
            owner_id=row["owner_id"],
            created_at=parse_scheduler_ts(row["created_at"]),
            updated_at=parse_scheduler_ts(row["updated_at"]),
            last_run_at=parse_scheduler_ts(row["last_run_at"]) if row["last_run_at"] else None,
            next_run_at=parse_scheduler_ts(row["next_run_at"]) if row["next_run_at"] else None,
            # #913: NULL ⇒ inherit from agent's execution_timeout_seconds. The
            # backend's TaskExecutionService applies that fallback when it
            # sees None. Substituting 900 here was what made
            # PUT /api/agents/{name}/timeout silently ineffective for cron.
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row_keys else None,
            allowed_tools=allowed_tools,
            model=row["model"] if "model" in row_keys else None,
            # Retry configuration (RETRY-001)
            max_retries=row["max_retries"] if "max_retries" in row_keys and row["max_retries"] is not None else 0,
            retry_delay_seconds=row["retry_delay_seconds"] if "retry_delay_seconds" in row_keys and row["retry_delay_seconds"] is not None else 60,
            # Validation configuration (VALIDATE-001)
            validation_enabled=bool(row["validation_enabled"]) if "validation_enabled" in row_keys and row["validation_enabled"] is not None else False,
            validation_prompt=row["validation_prompt"] if "validation_prompt" in row_keys else None,
            validation_timeout_seconds=row["validation_timeout_seconds"] if "validation_timeout_seconds" in row_keys and row["validation_timeout_seconds"] is not None else 120
        )

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> ScheduleExecution:
        """Convert a database row to a ScheduleExecution model."""
        row_keys = row.keys()
        return ScheduleExecution(
            id=row["id"],
            schedule_id=row["schedule_id"],
            agent_name=row["agent_name"],
            status=row["status"],
            started_at=parse_scheduler_ts(row["started_at"]),
            completed_at=parse_scheduler_ts(row["completed_at"]) if row["completed_at"] else None,
            duration_ms=row["duration_ms"],
            message=row["message"],
            response=row["response"],
            error=row["error"],
            triggered_by=row["triggered_by"],
            context_used=row["context_used"] if "context_used" in row_keys else None,
            context_max=row["context_max"] if "context_max" in row_keys else None,
            cost=row["cost"] if "cost" in row_keys else None,
            tool_calls=row["tool_calls"] if "tool_calls" in row_keys else None,
            execution_log=row["execution_log"] if "execution_log" in row_keys else None,
            # Origin tracking fields (AUDIT-001)
            source_user_id=row["source_user_id"] if "source_user_id" in row_keys else None,
            source_user_email=row["source_user_email"] if "source_user_email" in row_keys else None,
            source_agent_name=row["source_agent_name"] if "source_agent_name" in row_keys else None,
            source_mcp_key_id=row["source_mcp_key_id"] if "source_mcp_key_id" in row_keys else None,
            source_mcp_key_name=row["source_mcp_key_name"] if "source_mcp_key_name" in row_keys else None,
            # Retry tracking (RETRY-001)
            attempt_number=row["attempt_number"] if "attempt_number" in row_keys and row["attempt_number"] else 1,
            retry_of_execution_id=row["retry_of_execution_id"] if "retry_of_execution_id" in row_keys else None,
            retry_scheduled_at=parse_scheduler_ts(row["retry_scheduled_at"])
                if "retry_scheduled_at" in row_keys and row["retry_scheduled_at"] else None,
            # Validation tracking (VALIDATE-001)
            business_status=row["business_status"] if "business_status" in row_keys else None,
            validated_at=parse_scheduler_ts(row["validated_at"])
                if "validated_at" in row_keys and row["validated_at"] else None,
            validation_execution_id=row["validation_execution_id"] if "validation_execution_id" in row_keys else None,
            validates_execution_id=row["validates_execution_id"] if "validates_execution_id" in row_keys else None
        )

    # =========================================================================
    # Schedule Read Operations
    # =========================================================================

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Get a schedule by ID. Excludes soft-deleted (#834 Phase 1b)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_schedules WHERE id = ? AND deleted_at IS NULL",
                (schedule_id,),
            )
            row = cursor.fetchone()
            return self._row_to_schedule(row) if row else None

    def list_all_enabled_schedules(self) -> List[Schedule]:
        """List all enabled schedules.

        This is the primary cron-firing list. Two soft-delete filters
        apply:
        - #834 Phase 1a: skip schedules whose *agent* is soft-deleted
          (`agent_ownership.deleted_at`) — otherwise the scheduler fires
          every enabled schedule for a soft-deleted agent and writes a
          `schedule_executions` failure row per tick until purge.
        - #834 Phase 1b: skip *schedules* that are themselves
          soft-deleted (`agent_schedules.deleted_at`).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.* FROM agent_schedules s
                JOIN agent_ownership ao ON ao.agent_name = s.agent_name
                WHERE s.enabled = 1
                  AND s.deleted_at IS NULL
                  AND ao.deleted_at IS NULL
                ORDER BY s.agent_name, s.name
            """)
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def list_all_schedules(self) -> List[Schedule]:
        """List all schedules (enabled and disabled) for sync detection.

        Excludes schedules that are themselves soft-deleted (#834 Phase 1b).

        A schedule whose *parent agent* is soft-deleted (`agent_ownership.
        deleted_at`) is reported as **disabled** here (#1427): soft-deleting an
        agent leaves the child schedule row untouched (enabled=1, deleted_at
        NULL, same updated_at), so the sync's transition diff saw no change and
        never removed the already-registered APScheduler job — it kept firing
        into a nonexistent container (ConnectError every cron tick). Folding the
        agent's soft-delete into the reported `enabled` makes the existing
        enabled→disabled transition remove the job, and — because agent recovery
        clears `agent_ownership.deleted_at` — the disabled→enabled transition
        re-adds it. Symmetric with #834 recovery; no schedule-row mutation.
        `list_all_enabled_schedules` already applies this join at startup; this
        closes the same gap on the live-sync path.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, ao.deleted_at AS _agent_deleted_at
                FROM agent_schedules s
                LEFT JOIN agent_ownership ao ON ao.agent_name = s.agent_name
                WHERE s.deleted_at IS NULL
                ORDER BY s.agent_name, s.name
            """)
            schedules = []
            for row in cursor.fetchall():
                schedule = self._row_to_schedule(row)
                if row["_agent_deleted_at"] is not None:
                    schedule.enabled = False
                schedules.append(schedule)
            return schedules

    def list_agent_schedules(self, agent_name: str) -> List[Schedule]:
        """List all schedules for a specific agent.

        Excludes soft-deleted (#834 Phase 1b).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_schedules
                WHERE agent_name = ? AND deleted_at IS NULL
                ORDER BY name
            """, (agent_name,))
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def get_autonomy_enabled(self, agent_name: str) -> bool:
        """Check if autonomy is enabled for an agent."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT autonomy_enabled FROM agent_ownership WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()
            return bool(row["autonomy_enabled"]) if row else False

    # =========================================================================
    # Schedule Update Operations
    # =========================================================================

    def update_schedule_run_times(
        self,
        schedule_id: str,
        last_run_at: datetime = None,
        next_run_at: datetime = None
    ) -> bool:
        """Update schedule run timestamps.

        Does NOT bump ``updated_at`` — that column signals config changes and
        is watched by the sync loop in ``SchedulerService._sync_agent_schedules``.
        Bumping it on every run would cause a self-triggering sync loop that
        re-registers every schedule once per tick (Issue #420).
        """
        if last_run_at is None and next_run_at is None:
            return False

        with self.get_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []

            if last_run_at:
                updates.append("last_run_at = ?")
                params.append(to_utc_iso(last_run_at))
            if next_run_at:
                updates.append("next_run_at = ?")
                params.append(to_utc_iso(next_run_at))

            params.append(schedule_id)
            cursor.execute(f"""
                UPDATE agent_schedules SET {", ".join(updates)} WHERE id = ?
            """, params)
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Execution Operations
    # =========================================================================

    def create_execution(
        self,
        schedule_id: str,
        agent_name: str,
        message: str,
        triggered_by: str = "schedule",
        model_used: str = None,
        attempt_number: int = 1,
        retry_of_execution_id: str = None
    ) -> Optional[ScheduleExecution]:
        """Create a new execution record.

        Args:
            schedule_id: Schedule ID
            agent_name: Agent name
            message: Task message
            triggered_by: Trigger source ("schedule", "manual", "retry")
            model_used: Model override
            attempt_number: Which attempt this is (1 = first try, RETRY-001)
            retry_of_execution_id: Original execution ID for retries (RETRY-001)
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schedule_executions (
                    id, schedule_id, agent_name, status, started_at, message, triggered_by,
                    model_used, attempt_number, retry_of_execution_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                agent_name,
                ExecutionStatus.RUNNING,
                now,
                message,
                triggered_by,
                model_used,
                attempt_number,
                retry_of_execution_id
            ))
            conn.commit()

            return ScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                agent_name=agent_name,
                status=ExecutionStatus.RUNNING,
                started_at=parse_scheduler_ts(now),
                message=message,
                triggered_by=triggered_by,
                attempt_number=attempt_number,
                retry_of_execution_id=retry_of_execution_id
            )

    def should_freeze_schedules(self, agent_name: str) -> bool:
        """Is this agent's cron firing frozen by a failing git sync? (#389/#1808)

        True only when the owner opted in (`freeze_schedules_if_sync_failing`)
        AND sync is actually failing. Mirrors the predicate the backend already
        exposes at `/api/internal/agents/{name}/sync-health-status`; read
        directly here because the scheduler talks to the same database for
        every other gate (autonomy, enabled, reminders) and an extra HTTP hop
        would add a failure mode to the fire path for no benefit.

        FAIL-OPEN: any error (missing table on an older DB, read failure)
        returns False and the schedule fires. A freeze-on-error would silently
        stop the whole fleet the moment this query broke — the opposite of the
        bug this closes.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT gc.freeze_schedules_if_sync_failing,
                           ss.last_sync_status,
                           ss.consecutive_failures
                    FROM agent_git_config gc
                    LEFT JOIN agent_sync_state ss ON ss.agent_name = gc.agent_name
                    WHERE gc.agent_name = ?
                    """,
                    (agent_name,),
                )
                row = cursor.fetchone()
                if not row:
                    return False
                # Named access only: the PG path (#300) yields RealDictCursor
                # mapping rows with no positional indexing — row[0] would
                # KeyError and silently fail-open on every PostgreSQL deploy.
                freeze_enabled = bool(row["freeze_schedules_if_sync_failing"])
                if not freeze_enabled:
                    return False
                failing = (
                    row["last_sync_status"] == "failed"
                    and (row["consecutive_failures"] or 0) >= SYNC_FAILURE_FREEZE_THRESHOLD
                )
                return bool(failing)
        except Exception as e:
            logger.warning(
                f"Sync-freeze check failed for {agent_name} ({e}); firing anyway"
            )
            return False

    def create_skipped_execution(
        self,
        schedule_id: str,
        agent_name: str,
        message: str,
        triggered_by: str = "schedule",
        skip_reason: str = None
    ) -> Optional[ScheduleExecution]:
        """
        Create an execution record for a skipped scheduled task.

        This is called when APScheduler fires EVENT_JOB_MAX_INSTANCES,
        indicating that a scheduled job was dropped because the previous
        execution is still running.

        Args:
            schedule_id: ID of the schedule that was skipped
            agent_name: Name of the agent
            message: The scheduled message that would have been sent
            triggered_by: Source of the trigger (always "schedule" for skipped)
            skip_reason: Human-readable reason for the skip

        Returns:
            ScheduleExecution with status='skipped', or None on failure
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schedule_executions (
                    id, schedule_id, agent_name, status, started_at, completed_at,
                    duration_ms, message, triggered_by, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                agent_name,
                ExecutionStatus.SKIPPED,
                now,
                now,  # completed_at = started_at for skipped
                0,    # duration_ms = 0 for skipped
                message,
                triggered_by,
                skip_reason
            ))
            conn.commit()

            return ScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                agent_name=agent_name,
                status=ExecutionStatus.SKIPPED,
                started_at=parse_scheduler_ts(now),
                completed_at=parse_scheduler_ts(now),
                duration_ms=0,
                message=message,
                triggered_by=triggered_by,
                error=skip_reason
            )

    def update_execution_status(
        self,
        execution_id: str,
        status: str,
        response: str = None,
        error: str = None,
        context_used: int = None,
        context_max: int = None,
        cost: float = None,
        tool_calls: str = None,
        execution_log: str = None,
        claude_session_id: str = None  # Claude Code session ID for --resume (EXEC-023)
    ) -> bool:
        """Update execution status when completed.

        Args:
            claude_session_id: Claude Code session ID for --resume support (EXEC-023)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get started_at for duration calculation
            cursor.execute("SELECT started_at FROM schedule_executions WHERE id = ?", (execution_id,))
            row = cursor.fetchone()
            if not row:
                return False

            started_at = parse_scheduler_ts(row["started_at"])
            completed_at = datetime.utcnow()
            # Clock skew between the writer of started_at and this process can
            # make the subtraction negative; clamp so a poisoned duration never
            # reaches the analytics chart (#1832).
            duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))

            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?, completed_at = ?, duration_ms = ?, response = ?, error = ?,
                    context_used = ?, context_max = ?, cost = ?, tool_calls = ?, execution_log = ?,
                    claude_session_id = ?
                WHERE id = ?
            """, (
                status,
                to_utc_iso(completed_at),
                duration_ms,
                response,
                error,
                context_used,
                context_max,
                cost,
                tool_calls,
                execution_log,
                claude_session_id,
                execution_id
            ))
            conn.commit()
            return cursor.rowcount > 0

    def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
        """Get an execution by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM schedule_executions WHERE id = ?", (execution_id,))
            row = cursor.fetchone()
            return self._row_to_execution(row) if row else None

    def get_recent_executions(self, limit: int = 50) -> List[ScheduleExecution]:
        """Get recent executions across all schedules."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM schedule_executions
                ORDER BY started_at DESC
                LIMIT ?
            """, (limit,))
            return [self._row_to_execution(row) for row in cursor.fetchall()]

    # =========================================================================
    # Retry Operations (RETRY-001)
    # =========================================================================

    def schedule_retry(
        self,
        original_execution_id: str,
        retry_scheduled_at: datetime
    ) -> bool:
        """Mark an execution as pending retry.

        Updates the execution status to 'pending_retry' and records when the retry
        is scheduled to fire. This persists the retry intent so it survives scheduler restart.

        Args:
            original_execution_id: The failed execution ID
            retry_scheduled_at: When the retry is scheduled to fire

        Returns:
            True if update succeeded
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?, retry_scheduled_at = ?
                WHERE id = ?
            """, (
                ExecutionStatus.PENDING_RETRY,
                to_utc_iso(retry_scheduled_at),
                original_execution_id
            ))
            conn.commit()
            return cursor.rowcount > 0

    def get_pending_retries(self) -> List[ScheduleExecution]:
        """Get all executions with pending retries (for startup recovery).

        Returns executions where:
        - status = 'pending_retry'
        - retry_scheduled_at is not null

        The caller should reschedule APScheduler date triggers for these.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM schedule_executions
                WHERE status = ? AND retry_scheduled_at IS NOT NULL
                ORDER BY retry_scheduled_at ASC
            """, (ExecutionStatus.PENDING_RETRY,))
            return [self._row_to_execution(row) for row in cursor.fetchall()]

    def clear_retry_scheduled(self, execution_id: str) -> bool:
        """Clear the retry_scheduled_at after retry fires.

        Called when the retry actually executes. The new execution record
        will have its own status tracking.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE schedule_executions
                SET retry_scheduled_at = NULL
                WHERE id = ?
            """, (execution_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_original_execution_id(self, execution_id: str) -> Optional[str]:
        """Traverse retry chain to find the original execution ID.

        For grouping retry attempts in the UI.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            current_id = execution_id

            # Follow the chain (max 5 hops to prevent infinite loops)
            for _ in range(5):
                cursor.execute(
                    "SELECT retry_of_execution_id FROM schedule_executions WHERE id = ?",
                    (current_id,)
                )
                row = cursor.fetchone()
                if not row or not row["retry_of_execution_id"]:
                    return current_id
                current_id = row["retry_of_execution_id"]

            return current_id

    # =========================================================================
    # Reminder Operations (#1296)
    #
    # ALL mutating methods commit (eng C): mirror schedule_retry (committed),
    # NOT get_pending_retries (a no-commit SELECT). Without commit, sqlite3's
    # implicit txn + psycopg2 both roll back on conn.close(), leaving the row
    # unchanged while rowcount was 1 pre-close → a double-fire latent for 24h.
    # =========================================================================

    @staticmethod
    def _row_to_reminder(row) -> Reminder:
        """Hydrate a Reminder from a row; parse timestamps to NAIVE UTC via
        parse_scheduler_ts (#1472/#1474 — a Z-suffixed fire_at compared against
        datetime.utcnow() must not raise)."""
        allowed = row["allowed_tools"]
        try:
            allowed_tools = json.loads(allowed) if allowed else None
        except (TypeError, ValueError):
            allowed_tools = None
        firing_at = row["firing_at"]
        return Reminder(
            id=row["id"],
            agent_name=row["agent_name"],
            message=row["message"],
            fire_at=parse_scheduler_ts(row["fire_at"]),
            status=row["status"],
            fire_attempts=row["fire_attempts"] or 0,
            firing_at=parse_scheduler_ts(firing_at) if firing_at else None,
            model=row["model"],
            timeout_seconds=row["timeout_seconds"],
            allowed_tools=allowed_tools,
            execution_id=row["execution_id"],
            error=row["error"],
        )

    def get_active_reminders(self) -> List[Reminder]:
        """Reminders eligible for arming/reclaim: ``pending`` ∪ ``firing``, for a
        LIVE, autonomy-ENABLED agent.

        Reads BOTH states so the reconcile can arm pending reminders AND reclaim
        stale ``firing`` ones. Excludes soft-deleted agents (``deleted_at`` —
        mirrors ``list_all_enabled_schedules``, else a reminder fires into a
        nonexistent container) and autonomy-disabled agents (their pending
        reminders are held and resume, past-due-fire, when autonomy is
        re-enabled). Order-by is lexicographic on the ISO-Z TEXT column
        (ordering-only; correctness rides the consistent ``to_utc_iso`` write
        format, so Invariant #16 doesn't bite).
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT r.* FROM agent_reminders r
                JOIN agent_ownership ao ON ao.agent_name = r.agent_name
                WHERE r.status IN ('pending', 'firing')
                  AND ao.deleted_at IS NULL
                  AND ao.autonomy_enabled = 1
                ORDER BY r.fire_at ASC
            """)
            return [self._row_to_reminder(row) for row in cursor.fetchall()]

    def count_held_reminders(self) -> int:
        """Live reminders excluded from arming solely because autonomy is off (#1806).

        The complement of ``get_active_reminders``'s autonomy predicate, with the
        same live-status and not-deleted conditions. Exists purely for
        observability: a held reminder is never armed, so there is no job and
        therefore no skip for the scheduler to log. Without this count an
        operator debugging "my reminder never fired" has nothing to go on.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM agent_reminders r
                JOIN agent_ownership ao ON ao.agent_name = r.agent_name
                WHERE r.status IN ('pending', 'firing')
                  AND ao.deleted_at IS NULL
                  AND ao.autonomy_enabled = 0
            """)
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_reminder_by_id(self, reminder_id: str) -> Optional[Reminder]:
        """Fetch a single reminder by id (to re-read fire_attempts after a CAS)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_reminders WHERE id = ?", (reminder_id,)
            )
            row = cursor.fetchone()
            return self._row_to_reminder(row) if row else None

    def claim_reminder_firing(self, reminder_id: str) -> bool:
        """The single-fire CAS: ``pending → firing`` + increment fire_attempts.

        Commit; return ``rowcount > 0``. The mutated predicate ``status='pending'``
        is in the outer WHERE, so a losing concurrent updater's CAS is a no-op
        (learnings #1081 #70) — exactly one claimer wins.
        """
        now = utc_now_iso()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_reminders
                SET status = 'firing', firing_at = ?, fire_attempts = fire_attempts + 1
                WHERE id = ? AND status = 'pending'
            """, (now, reminder_id))
            conn.commit()
            return cursor.rowcount > 0

    def mark_reminder_fired(self, reminder_id: str) -> bool:
        """``firing → fired`` (terminal delivered). Commit."""
        now = utc_now_iso()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_reminders SET status = 'fired', fired_at = ?
                WHERE id = ? AND status = 'firing'
            """, (now, reminder_id))
            conn.commit()
            return cursor.rowcount > 0

    def release_reminder_to_pending(self, reminder_id: str, error: str = None) -> bool:
        """``firing → pending`` (transient-failure release; the reconcile re-arms). Commit."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_reminders SET status = 'pending', firing_at = NULL, error = ?
                WHERE id = ? AND status = 'firing'
            """, (error, reminder_id))
            conn.commit()
            return cursor.rowcount > 0

    def mark_reminder_failed(self, reminder_id: str, error: str = None) -> bool:
        """``firing → failed`` (bounded-attempts terminal). Commit."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_reminders SET status = 'failed', firing_at = NULL, error = ?
                WHERE id = ? AND status = 'firing'
            """, (error, reminder_id))
            conn.commit()
            return cursor.rowcount > 0

    def set_reminder_execution(self, reminder_id: str, execution_id: str) -> bool:
        """Link the latest fire attempt's execution row onto the reminder. Commit."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_reminders SET execution_id = ? WHERE id = ?",
                (execution_id, reminder_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Process Schedule Operations
    # =========================================================================

    def ensure_process_schedules_table(self) -> None:
        """Create process_schedules table if it doesn't exist."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS process_schedules (
                    id TEXT PRIMARY KEY,
                    process_id TEXT NOT NULL,
                    process_name TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    cron_expression TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    timezone TEXT DEFAULT 'UTC',
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    UNIQUE(process_id, trigger_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS process_schedule_executions (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    process_id TEXT NOT NULL,
                    process_name TEXT NOT NULL,
                    execution_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    duration_ms INTEGER,
                    triggered_by TEXT NOT NULL,
                    error TEXT,
                    FOREIGN KEY (schedule_id) REFERENCES process_schedules(id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_process_schedules_process
                ON process_schedules(process_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_process_schedules_enabled
                ON process_schedules(enabled)
            """)
            conn.commit()
            logger.info("Process schedules tables ensured")

    @staticmethod
    def _row_to_process_schedule(row: sqlite3.Row) -> ProcessSchedule:
        """Convert a database row to a ProcessSchedule model."""
        return ProcessSchedule(
            id=row["id"],
            process_id=row["process_id"],
            process_name=row["process_name"],
            trigger_id=row["trigger_id"],
            cron_expression=row["cron_expression"],
            enabled=bool(row["enabled"]),
            timezone=row["timezone"],
            description=row["description"],
            created_at=parse_scheduler_ts(row["created_at"]),
            updated_at=parse_scheduler_ts(row["updated_at"]),
            last_run_at=parse_scheduler_ts(row["last_run_at"]) if row["last_run_at"] else None,
            next_run_at=parse_scheduler_ts(row["next_run_at"]) if row["next_run_at"] else None
        )

    @staticmethod
    def _row_to_process_schedule_execution(row: sqlite3.Row) -> ProcessScheduleExecution:
        """Convert a database row to a ProcessScheduleExecution model."""
        return ProcessScheduleExecution(
            id=row["id"],
            schedule_id=row["schedule_id"],
            process_id=row["process_id"],
            process_name=row["process_name"],
            execution_id=row["execution_id"],
            status=row["status"],
            started_at=parse_scheduler_ts(row["started_at"]),
            completed_at=parse_scheduler_ts(row["completed_at"]) if row["completed_at"] else None,
            duration_ms=row["duration_ms"],
            triggered_by=row["triggered_by"],
            error=row["error"]
        )

    def get_process_schedule(self, schedule_id: str) -> Optional[ProcessSchedule]:
        """Get a process schedule by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM process_schedules WHERE id = ?", (schedule_id,))
            row = cursor.fetchone()
            return self._row_to_process_schedule(row) if row else None

    def get_process_schedule_by_trigger(self, process_id: str, trigger_id: str) -> Optional[ProcessSchedule]:
        """Get a process schedule by process ID and trigger ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM process_schedules WHERE process_id = ? AND trigger_id = ?",
                (process_id, trigger_id)
            )
            row = cursor.fetchone()
            return self._row_to_process_schedule(row) if row else None

    def list_all_enabled_process_schedules(self) -> List[ProcessSchedule]:
        """List all enabled process schedules."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM process_schedules WHERE enabled = 1
                ORDER BY process_name, trigger_id
            """)
            return [self._row_to_process_schedule(row) for row in cursor.fetchall()]

    def list_all_process_schedules(self) -> List[ProcessSchedule]:
        """List all process schedules (enabled and disabled) for sync detection."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM process_schedules
                ORDER BY process_name, trigger_id
            """)
            return [self._row_to_process_schedule(row) for row in cursor.fetchall()]

    def list_process_schedules(self, process_id: str) -> List[ProcessSchedule]:
        """List all schedules for a specific process."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM process_schedules WHERE process_id = ?
                ORDER BY trigger_id
            """, (process_id,))
            return [self._row_to_process_schedule(row) for row in cursor.fetchall()]

    def create_process_schedule(
        self,
        process_id: str,
        process_name: str,
        trigger_id: str,
        cron_expression: str,
        timezone: str = "UTC",
        description: str = "",
        enabled: bool = True
    ) -> Optional[ProcessSchedule]:
        """Create a new process schedule."""
        schedule_id = self._generate_id()
        now = utc_now_iso()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO process_schedules (
                        id, process_id, process_name, trigger_id, cron_expression,
                        enabled, timezone, description, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    schedule_id,
                    process_id,
                    process_name,
                    trigger_id,
                    cron_expression,
                    1 if enabled else 0,
                    timezone,
                    description,
                    now,
                    now
                ))
                conn.commit()

                return ProcessSchedule(
                    id=schedule_id,
                    process_id=process_id,
                    process_name=process_name,
                    trigger_id=trigger_id,
                    cron_expression=cron_expression,
                    enabled=enabled,
                    timezone=timezone,
                    description=description,
                    created_at=parse_scheduler_ts(now),
                    updated_at=parse_scheduler_ts(now)
                )
            except _INTEGRITY_ERRORS:
                logger.warning(f"Process schedule already exists: {process_id}/{trigger_id}")
                return None

    def update_process_schedule_run_times(
        self,
        schedule_id: str,
        last_run_at: datetime = None,
        next_run_at: datetime = None
    ) -> bool:
        """Update process schedule run timestamps.

        Does NOT bump ``updated_at`` — same rationale as
        :meth:`update_schedule_run_times` (Issue #420).
        """
        if last_run_at is None and next_run_at is None:
            return False

        with self.get_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []

            if last_run_at:
                updates.append("last_run_at = ?")
                params.append(to_utc_iso(last_run_at))
            if next_run_at:
                updates.append("next_run_at = ?")
                params.append(to_utc_iso(next_run_at))

            params.append(schedule_id)
            cursor.execute(f"""
                UPDATE process_schedules SET {", ".join(updates)} WHERE id = ?
            """, params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_process_schedule(self, schedule_id: str) -> bool:
        """Delete a process schedule."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM process_schedules WHERE id = ?", (schedule_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_process_schedules_for_process(self, process_id: str) -> int:
        """Delete all schedules for a process. Returns count deleted."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM process_schedules WHERE process_id = ?", (process_id,))
            conn.commit()
            return cursor.rowcount

    def create_process_schedule_execution(
        self,
        schedule_id: str,
        process_id: str,
        process_name: str,
        triggered_by: str = "schedule"
    ) -> Optional[ProcessScheduleExecution]:
        """Create a new process schedule execution record."""
        execution_id = self._generate_id()
        now = utc_now_iso()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO process_schedule_executions (
                    id, schedule_id, process_id, process_name, status, started_at, triggered_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                process_id,
                process_name,
                ExecutionStatus.RUNNING,
                now,
                triggered_by
            ))
            conn.commit()

            return ProcessScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                process_id=process_id,
                process_name=process_name,
                execution_id=None,
                status=ExecutionStatus.RUNNING,
                started_at=parse_scheduler_ts(now),
                triggered_by=triggered_by
            )

    def create_skipped_process_schedule_execution(
        self,
        schedule_id: str,
        process_id: str,
        process_name: str,
        triggered_by: str = "schedule",
        skip_reason: str = None
    ) -> Optional[ProcessScheduleExecution]:
        """
        Create an execution record for a skipped process schedule.

        This is called when APScheduler fires EVENT_JOB_MAX_INSTANCES,
        indicating that a scheduled process was dropped because the previous
        execution is still running.

        Args:
            schedule_id: ID of the process schedule that was skipped
            process_id: ID of the process definition
            process_name: Name of the process
            triggered_by: Source of the trigger (always "schedule" for skipped)
            skip_reason: Human-readable reason for the skip

        Returns:
            ProcessScheduleExecution with status='skipped', or None on failure
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO process_schedule_executions (
                    id, schedule_id, process_id, process_name, status,
                    started_at, completed_at, duration_ms, triggered_by, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                process_id,
                process_name,
                ExecutionStatus.SKIPPED,
                now,
                now,  # completed_at = started_at for skipped
                0,    # duration_ms = 0 for skipped
                triggered_by,
                skip_reason
            ))
            conn.commit()

            return ProcessScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                process_id=process_id,
                process_name=process_name,
                execution_id=None,
                status=ExecutionStatus.SKIPPED,
                started_at=parse_scheduler_ts(now),
                completed_at=parse_scheduler_ts(now),
                duration_ms=0,
                triggered_by=triggered_by,
                error=skip_reason
            )

    def update_process_schedule_execution(
        self,
        execution_id: str,
        status: str,
        process_execution_id: str = None,
        error: str = None
    ) -> bool:
        """Update process schedule execution status."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get started_at for duration calculation
            cursor.execute(
                "SELECT started_at FROM process_schedule_executions WHERE id = ?",
                (execution_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            started_at = parse_scheduler_ts(row["started_at"])
            completed_at = datetime.utcnow()
            # Clock skew between the writer of started_at and this process can
            # make the subtraction negative; clamp so a poisoned duration never
            # reaches the analytics chart (#1832).
            duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))

            cursor.execute("""
                UPDATE process_schedule_executions
                SET status = ?, completed_at = ?, duration_ms = ?, execution_id = ?, error = ?
                WHERE id = ?
            """, (
                status,
                to_utc_iso(completed_at),
                duration_ms,
                process_execution_id,
                error,
                execution_id
            ))
            conn.commit()
            return cursor.rowcount > 0
