"""
Schedule and execution management database operations.

Handles schedule CRUD, execution tracking, and Git configuration.
"""

import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

import pytz
from croniter import croniter

from .connection import get_db_connection
from db_models import Schedule, ScheduleCreate, ScheduleExecution, AgentGitConfig
from models import TaskExecutionStatus
from utils.helpers import utc_now_iso, to_utc_iso, parse_iso_timestamp

logger = logging.getLogger(__name__)

# #378: Error-message marker written by cleanup_service._process_stale_slot_reclaims
# when Phase 3 fails an execution. Used to scope the residual-race WARNING log
# below so it doesn't misfire on other legitimate FAILED→SUCCESS transitions
# (e.g. Phase 0 auto-terminate, Phase 1 stale cleanup, startup recovery).
_STALE_SLOT_ERROR_PATTERN = "Stale execution — slot TTL expired"


class ScheduleOperations:
    """Schedule and execution database operations."""

    def __init__(self, user_ops, agent_ops):
        """Initialize with references to user and agent operations."""
        self._user_ops = user_ops
        self._agent_ops = agent_ops

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)

    @staticmethod
    def _calculate_next_run_at(cron_expression: str, timezone: str = "UTC") -> Optional[datetime]:
        """Calculate the next run time for a cron expression.

        This is calculated in the database layer to ensure next_run_at is always
        set when schedules are created or updated, independent of the scheduler service.

        Args:
            cron_expression: Cron expression (5-field format)
            timezone: Timezone for schedule (default: UTC)

        Returns:
            Next run time as datetime, or None if calculation fails
        """
        try:
            tz = pytz.timezone(timezone) if timezone else pytz.UTC
            now = datetime.now(tz)
            cron = croniter(cron_expression, now)
            next_time = cron.get_next(datetime)
            return next_time
        except Exception as e:
            logger.warning(f"Failed to calculate next_run_at for cron '{cron_expression}': {e}")
            return None

    @staticmethod
    def _row_to_schedule(row) -> Schedule:
        """Convert a schedule row to a Schedule model."""
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
            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            created_at=parse_iso_timestamp(row["created_at"]),
            updated_at=parse_iso_timestamp(row["updated_at"]),
            last_run_at=parse_iso_timestamp(row["last_run_at"]) if row["last_run_at"] else None,
            next_run_at=parse_iso_timestamp(row["next_run_at"]) if row["next_run_at"] else None,
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row_keys and row["timeout_seconds"] else 900,
            allowed_tools=allowed_tools,
            model=row["model"] if "model" in row_keys else None,
            # Retry configuration (RETRY-001)
            max_retries=row["max_retries"] if "max_retries" in row_keys and row["max_retries"] is not None else 1,
            retry_delay_seconds=row["retry_delay_seconds"] if "retry_delay_seconds" in row_keys and row["retry_delay_seconds"] is not None else 60,
            # Validation configuration (VALIDATE-001)
            validation_enabled=bool(row["validation_enabled"]) if "validation_enabled" in row_keys and row["validation_enabled"] is not None else False,
            validation_prompt=row["validation_prompt"] if "validation_prompt" in row_keys else None,
            validation_timeout_seconds=row["validation_timeout_seconds"] if "validation_timeout_seconds" in row_keys and row["validation_timeout_seconds"] is not None else 120
        )

    @staticmethod
    def _row_to_schedule_execution(row) -> ScheduleExecution:
        """Convert a schedule_executions row to a ScheduleExecution model."""
        row_keys = row.keys()
        return ScheduleExecution(
            id=row["id"],
            schedule_id=row["schedule_id"],
            agent_name=row["agent_name"],
            status=row["status"],
            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            started_at=parse_iso_timestamp(row["started_at"]),
            completed_at=parse_iso_timestamp(row["completed_at"]) if row["completed_at"] else None,
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
            # Session resume support (EXEC-023)
            claude_session_id=row["claude_session_id"] if "claude_session_id" in row_keys else None,
            # Model selection (MODEL-001)
            model_used=row["model_used"] if "model_used" in row_keys else None,
            # Fan-out linkage (FANOUT-001)
            fan_out_id=row["fan_out_id"] if "fan_out_id" in row_keys else None,
            # Subscription usage tracking (SUB-004)
            subscription_id=row["subscription_id"] if "subscription_id" in row_keys else None,
            # Persistent backlog (BACKLOG-001)
            queued_at=parse_iso_timestamp(row["queued_at"])
                if "queued_at" in row_keys and row["queued_at"] else None,
            backlog_metadata=row["backlog_metadata"] if "backlog_metadata" in row_keys else None,
            # Retry tracking (RETRY-001)
            attempt_number=row["attempt_number"] if "attempt_number" in row_keys and row["attempt_number"] else 1,
            retry_of_execution_id=row["retry_of_execution_id"] if "retry_of_execution_id" in row_keys else None,
            retry_scheduled_at=parse_iso_timestamp(row["retry_scheduled_at"])
                if "retry_scheduled_at" in row_keys and row["retry_scheduled_at"] else None,
            # Validation tracking (VALIDATE-001)
            business_status=row["business_status"] if "business_status" in row_keys else None,
            validated_at=parse_iso_timestamp(row["validated_at"])
                if "validated_at" in row_keys and row["validated_at"] else None,
            validation_execution_id=row["validation_execution_id"] if "validation_execution_id" in row_keys else None,
            validates_execution_id=row["validates_execution_id"] if "validates_execution_id" in row_keys else None,
        )

    @staticmethod
    def _row_to_git_config(row) -> AgentGitConfig:
        """Convert an agent_git_config row to an AgentGitConfig model."""
        row_keys = row.keys() if hasattr(row, 'keys') else []
        return AgentGitConfig(
            id=row["id"],
            agent_name=row["agent_name"],
            github_repo=row["github_repo"],
            working_branch=row["working_branch"],
            instance_id=row["instance_id"],
            source_branch=row["source_branch"] if "source_branch" in row_keys else "main",
            source_mode=bool(row["source_mode"]) if "source_mode" in row_keys else False,
            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            created_at=parse_iso_timestamp(row["created_at"]),
            last_sync_at=parse_iso_timestamp(row["last_sync_at"]) if row["last_sync_at"] else None,
            last_commit_sha=row["last_commit_sha"],
            sync_enabled=bool(row["sync_enabled"]),
            sync_paths=row["sync_paths"],
            # #389 sync health fields — absent on DBs predating the migration.
            auto_sync_enabled=bool(row["auto_sync_enabled"])
                if "auto_sync_enabled" in row_keys else False,
            freeze_schedules_if_sync_failing=bool(row["freeze_schedules_if_sync_failing"])
                if "freeze_schedules_if_sync_failing" in row_keys else False,
        )

    # =========================================================================
    # Schedule Management
    # =========================================================================

    def create_schedule(self, agent_name: str, username: str, schedule_data: ScheduleCreate) -> Optional[Schedule]:
        """Create a new schedule for an agent."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        # Check user has access to this agent
        if not self._agent_ops.can_user_access_agent(username, agent_name):
            return None

        schedule_id = self._generate_id()
        now = utc_now_iso()

        # Calculate next_run_at if schedule is enabled
        next_run_at = None
        next_run_at_iso = None
        if schedule_data.enabled:
            next_run_at = self._calculate_next_run_at(
                schedule_data.cron_expression,
                schedule_data.timezone or "UTC"
            )
            if next_run_at:
                next_run_at_iso = next_run_at.isoformat()

        # Serialize allowed_tools to JSON if provided
        allowed_tools_json = None
        if schedule_data.allowed_tools is not None:
            allowed_tools_json = json.dumps(schedule_data.allowed_tools)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                # Clamp retry values to valid ranges (RETRY-001)
                max_retries = max(0, min(5, schedule_data.max_retries))
                retry_delay_seconds = max(30, min(600, schedule_data.retry_delay_seconds))

                # Clamp validation timeout to valid range (VALIDATE-001)
                validation_timeout_seconds = max(30, min(600, schedule_data.validation_timeout_seconds))

                cursor.execute("""
                    INSERT INTO agent_schedules (
                        id, agent_name, name, cron_expression, message, enabled,
                        timezone, description, owner_id, created_at, updated_at, next_run_at,
                        timeout_seconds, allowed_tools, model, max_retries, retry_delay_seconds,
                        validation_enabled, validation_prompt, validation_timeout_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    schedule_id,
                    agent_name,
                    schedule_data.name,
                    schedule_data.cron_expression,
                    schedule_data.message,
                    1 if schedule_data.enabled else 0,
                    schedule_data.timezone,
                    schedule_data.description,
                    user["id"],
                    now,
                    now,
                    next_run_at_iso,
                    schedule_data.timeout_seconds,
                    allowed_tools_json,
                    schedule_data.model,
                    max_retries,
                    retry_delay_seconds,
                    1 if schedule_data.validation_enabled else 0,
                    schedule_data.validation_prompt,
                    validation_timeout_seconds
                ))
                conn.commit()

                return Schedule(
                    id=schedule_id,
                    agent_name=agent_name,
                    name=schedule_data.name,
                    cron_expression=schedule_data.cron_expression,
                    message=schedule_data.message,
                    enabled=schedule_data.enabled,
                    timezone=schedule_data.timezone,
                    description=schedule_data.description,
                    owner_id=user["id"],
                    created_at=datetime.fromisoformat(now),
                    updated_at=datetime.fromisoformat(now),
                    next_run_at=next_run_at,
                    timeout_seconds=schedule_data.timeout_seconds,
                    allowed_tools=schedule_data.allowed_tools,
                    model=schedule_data.model,
                    max_retries=max_retries,
                    retry_delay_seconds=retry_delay_seconds,
                    validation_enabled=schedule_data.validation_enabled,
                    validation_prompt=schedule_data.validation_prompt,
                    validation_timeout_seconds=validation_timeout_seconds
                )
            except sqlite3.IntegrityError:
                return None

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Get a schedule by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM agent_schedules WHERE id = ?", (schedule_id,))
            row = cursor.fetchone()
            return self._row_to_schedule(row) if row else None

    def list_agent_schedules(self, agent_name: str) -> List[Schedule]:
        """List all schedules for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_schedules WHERE agent_name = ?
                ORDER BY created_at DESC
            """, (agent_name,))
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def list_all_enabled_schedules(self) -> List[Schedule]:
        """List all enabled schedules (for scheduler initialization)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_schedules WHERE enabled = 1
                ORDER BY agent_name, name
            """)
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def list_all_disabled_schedules(self) -> List[Schedule]:
        """List all disabled schedules (for resume operations)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_schedules WHERE enabled = 0
                ORDER BY agent_name, name
            """)
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def list_all_schedules(self) -> List[Schedule]:
        """List all schedules across all agents (for system agent overview)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_schedules
                ORDER BY agent_name, name
            """)
            return [self._row_to_schedule(row) for row in cursor.fetchall()]

    def update_schedule(self, schedule_id: str, username: str, updates: Dict) -> Optional[Schedule]:
        """Update a schedule."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return None

        # Check permission (owner or admin)
        if user["role"] != "admin" and schedule.owner_id != user["id"]:
            return None

        with get_db_connection() as conn:
            cursor = conn.cursor()

            set_clauses = []
            params = []
            allowed_fields = [
                "name", "cron_expression", "message", "enabled", "timezone",
                "description", "timeout_seconds", "allowed_tools", "model",
                "max_retries", "retry_delay_seconds",  # RETRY-001
                "validation_enabled", "validation_prompt", "validation_timeout_seconds"  # VALIDATE-001
            ]

            for key, value in updates.items():
                if key in allowed_fields:
                    if key == "enabled":
                        value = 1 if value else 0
                    elif key == "allowed_tools":
                        # Serialize allowed_tools to JSON
                        value = json.dumps(value) if value is not None else None
                    elif key == "max_retries":
                        # Clamp to valid range (RETRY-001)
                        value = max(0, min(5, int(value)))
                    elif key == "retry_delay_seconds":
                        # Clamp to valid range (RETRY-001)
                        value = max(30, min(600, int(value)))
                    elif key == "validation_enabled":
                        # Convert to integer for SQLite (VALIDATE-001)
                        value = 1 if value else 0
                    elif key == "validation_timeout_seconds":
                        # Clamp to valid range (VALIDATE-001)
                        value = max(30, min(600, int(value)))
                    set_clauses.append(f"{key} = ?")
                    params.append(value)

            if not set_clauses:
                return schedule

            # Check if we need to recalculate next_run_at
            # Recalculate if cron_expression, timezone, or enabled status changed
            needs_next_run_recalc = (
                "cron_expression" in updates or
                "timezone" in updates or
                "enabled" in updates
            )

            if needs_next_run_recalc:
                # Determine final values after update
                new_cron = updates.get("cron_expression", schedule.cron_expression)
                new_timezone = updates.get("timezone", schedule.timezone) or "UTC"
                new_enabled = updates.get("enabled", schedule.enabled)

                if new_enabled:
                    next_run_at = self._calculate_next_run_at(new_cron, new_timezone)
                    set_clauses.append("next_run_at = ?")
                    params.append(next_run_at.isoformat() if next_run_at else None)
                else:
                    # Clear next_run_at if schedule is disabled
                    set_clauses.append("next_run_at = ?")
                    params.append(None)

            set_clauses.append("updated_at = ?")
            params.append(utc_now_iso())
            params.append(schedule_id)

            cursor.execute(f"""
                UPDATE agent_schedules SET {", ".join(set_clauses)} WHERE id = ?
            """, params)
            conn.commit()

            return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str, username: str) -> bool:
        """Delete a schedule and its executions."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False

        # Check permission (owner or admin)
        if user["role"] != "admin" and schedule.owner_id != user["id"]:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Delete executions first
            cursor.execute("DELETE FROM schedule_executions WHERE schedule_id = ?", (schedule_id,))
            # Delete schedule
            cursor.execute("DELETE FROM agent_schedules WHERE id = ?", (schedule_id,))
            conn.commit()
            return cursor.rowcount > 0

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable a schedule.

        When enabling, calculates and stores next_run_at.
        When disabling, clears next_run_at.
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule:
            return False

        next_run_at_iso = None
        if enabled:
            next_run_at = self._calculate_next_run_at(
                schedule.cron_expression,
                schedule.timezone or "UTC"
            )
            if next_run_at:
                next_run_at_iso = next_run_at.isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_schedules SET enabled = ?, updated_at = ?, next_run_at = ? WHERE id = ?
            """, (1 if enabled else 0, utc_now_iso(), next_run_at_iso, schedule_id))
            conn.commit()
            return cursor.rowcount > 0

    def update_schedule_run_times(self, schedule_id: str, last_run_at: datetime = None, next_run_at: datetime = None) -> bool:
        """Update schedule run timestamps.

        Does NOT bump ``updated_at`` — that column signals config changes and
        is watched by the scheduler service's sync loop. Bumping it here caused
        a self-triggering loop that re-registered every schedule once per tick
        (Issue #420).
        """
        if last_run_at is None and next_run_at is None:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            updates = []
            params = []

            if last_run_at:
                updates.append("last_run_at = ?")
                params.append(last_run_at.isoformat())
            if next_run_at:
                updates.append("next_run_at = ?")
                params.append(next_run_at.isoformat())

            params.append(schedule_id)
            cursor.execute(f"""
                UPDATE agent_schedules SET {", ".join(updates)} WHERE id = ?
            """, params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_agent_schedules(self, agent_name: str) -> int:
        """Delete all schedules for an agent (when agent is deleted)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Get schedule IDs first
            cursor.execute("SELECT id FROM agent_schedules WHERE agent_name = ?", (agent_name,))
            schedule_ids = [row["id"] for row in cursor.fetchall()]

            # Delete executions for all schedules
            for sid in schedule_ids:
                cursor.execute("DELETE FROM schedule_executions WHERE schedule_id = ?", (sid,))

            # Delete schedules
            cursor.execute("DELETE FROM agent_schedules WHERE agent_name = ?", (agent_name,))
            conn.commit()
            return len(schedule_ids)

    # =========================================================================
    # Schedule Execution Management
    # =========================================================================

    def create_task_execution(
        self,
        agent_name: str,
        message: str,
        triggered_by: str = "manual",
        source_user_id: int = None,
        source_user_email: str = None,
        source_agent_name: str = None,
        source_mcp_key_id: str = None,
        source_mcp_key_name: str = None,
        model_used: str = None,
        fan_out_id: str = None,
        subscription_id: str = None,
    ) -> Optional[ScheduleExecution]:
        """Create a new execution record for a manual/API-triggered task (no schedule).

        Args:
            agent_name: Target agent name
            message: Task message
            triggered_by: Trigger type - "manual", "mcp", "agent", "fan_out"
            source_user_id: User ID who triggered (for manual/mcp triggers)
            source_user_email: User email (denormalized for queries)
            source_agent_name: Calling agent name (for agent-to-agent)
            source_mcp_key_id: MCP API key ID (for mcp/agent triggers)
            source_mcp_key_name: MCP API key name (denormalized)
            model_used: Model used for this execution (MODEL-001)
            fan_out_id: Parent fan-out operation ID (FANOUT-001)
            subscription_id: Subscription active at record time (SUB-004)
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schedule_executions (
                    id, schedule_id, agent_name, status, started_at, message, triggered_by,
                    source_user_id, source_user_email, source_agent_name,
                    source_mcp_key_id, source_mcp_key_name, model_used, fan_out_id,
                    subscription_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                "__manual__",  # Special marker for manual/API-triggered tasks
                agent_name,
                TaskExecutionStatus.RUNNING,
                now,
                message,
                triggered_by,
                source_user_id,
                source_user_email,
                source_agent_name,
                source_mcp_key_id,
                source_mcp_key_name,
                model_used,
                fan_out_id,
                subscription_id,
            ))
            conn.commit()

            return ScheduleExecution(
                id=execution_id,
                schedule_id="__manual__",
                agent_name=agent_name,
                status=TaskExecutionStatus.RUNNING,
                started_at=datetime.fromisoformat(now),
                message=message,
                triggered_by=triggered_by,
                source_user_id=source_user_id,
                source_user_email=source_user_email,
                source_agent_name=source_agent_name,
                source_mcp_key_id=source_mcp_key_id,
                source_mcp_key_name=source_mcp_key_name,
                model_used=model_used,
                fan_out_id=fan_out_id,
                subscription_id=subscription_id,
            )

    def create_schedule_execution(
        self,
        schedule_id: str,
        agent_name: str,
        message: str,
        triggered_by: str = "schedule",
        source_user_id: int = None,
        source_user_email: str = None,
        source_agent_name: str = None,
        source_mcp_key_id: str = None,
        source_mcp_key_name: str = None,
        model_used: str = None,
        subscription_id: str = None,
    ) -> Optional[ScheduleExecution]:
        """Create a new execution record for a scheduled task.

        Note: For schedule-triggered executions, source fields are typically NULL
        since the schedule itself is the trigger (schedule owner is tracked via schedule.owner_id).
        For manual schedule triggers, source fields may be populated.
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schedule_executions (
                    id, schedule_id, agent_name, status, started_at, message, triggered_by,
                    source_user_id, source_user_email, source_agent_name,
                    source_mcp_key_id, source_mcp_key_name, model_used, subscription_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                agent_name,
                TaskExecutionStatus.RUNNING,
                now,
                message,
                triggered_by,
                source_user_id,
                source_user_email,
                source_agent_name,
                source_mcp_key_id,
                source_mcp_key_name,
                model_used,
                subscription_id,
            ))
            conn.commit()

            return ScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                agent_name=agent_name,
                status=TaskExecutionStatus.RUNNING,
                started_at=datetime.fromisoformat(now),
                message=message,
                triggered_by=triggered_by,
                source_user_id=source_user_id,
                source_user_email=source_user_email,
                source_agent_name=source_agent_name,
                source_mcp_key_id=source_mcp_key_id,
                source_mcp_key_name=source_mcp_key_name,
                model_used=model_used,
                subscription_id=subscription_id,
            )

    def mark_execution_dispatched(self, execution_id: str) -> bool:
        """Mark an execution as dispatched to the agent.

        Sets claude_session_id to 'dispatched' so the no-session cleanup
        doesn't falsely mark long-running executions as failed.
        Only executions that never reach dispatch (e.g. backend crash before
        agent call) will have NULL claude_session_id and be caught by cleanup.

        Returns:
            True if execution was updated, False if not found.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE schedule_executions
                SET claude_session_id = 'dispatched'
                WHERE id = ? AND status = ? AND claude_session_id IS NULL
            """, (execution_id, TaskExecutionStatus.RUNNING))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Persistent Backlog (BACKLOG-001)
    # =========================================================================

    def update_execution_to_queued(
        self, execution_id: str, backlog_metadata: str, queued_at: str
    ) -> bool:
        """Transition an execution row to QUEUED state and attach its backlog metadata.

        Called by BacklogService.enqueue(). The row is already created by
        create_task_execution in RUNNING state, so we flip it back to queued and
        stamp queued_at for FIFO ordering.

        Args:
            execution_id: Execution row to transition.
            backlog_metadata: JSON string capturing the full request context.
            queued_at: ISO timestamp (used as the FIFO ordering key).

        Returns:
            True if the row was updated, False if not found.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    queued_at = ?,
                    backlog_metadata = ?,
                    started_at = ?
                WHERE id = ?
                """,
                (
                    TaskExecutionStatus.QUEUED,
                    queued_at,
                    backlog_metadata,
                    queued_at,  # reset started_at so drain records a clean run window
                    execution_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def claim_next_queued(self, agent_name: str) -> Optional[Dict]:
        """Atomically claim the oldest QUEUED execution for an agent.

        Uses a single SQL UPDATE with a subquery that selects the oldest row
        by queued_at, filtered WHERE status='queued'. RETURNING gives us the
        full row so the caller can reconstruct the request. This is race-safe
        under concurrent drain callbacks — only one caller wins the update.

        Returns:
            Dict of the claimed row (id, agent_name, message, backlog_metadata, ...)
            or None if the backlog is empty for this agent.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    started_at = ?,
                    queued_at = NULL
                WHERE id = (
                    SELECT id FROM schedule_executions
                    WHERE status = ? AND agent_name = ?
                    ORDER BY queued_at ASC
                    LIMIT 1
                )
                RETURNING id, agent_name, message, backlog_metadata,
                          source_user_id, source_user_email, source_agent_name,
                          source_mcp_key_id, source_mcp_key_name, subscription_id
                """,
                (TaskExecutionStatus.RUNNING, now, TaskExecutionStatus.QUEUED, agent_name),
            )
            row = cursor.fetchone()
            conn.commit()
            return dict(row) if row else None

    def release_claim_to_queued(self, execution_id: str) -> bool:
        """Release a claimed row back to QUEUED state.

        Used when drain_next() acquired a slot, claimed a row, but then something
        downstream failed (e.g. slot released concurrently, spawn failed) and we
        need to put the row back in the backlog.

        Returns:
            True if the row transitioned back to queued, False otherwise.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    queued_at = started_at
                WHERE id = ? AND status = ?
                """,
                (TaskExecutionStatus.QUEUED, execution_id, TaskExecutionStatus.RUNNING),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_queued_count(self, agent_name: str) -> int:
        """Count queued backlog items for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) as c FROM schedule_executions
                WHERE agent_name = ? AND status = ?
                """,
                (agent_name, TaskExecutionStatus.QUEUED),
            )
            row = cursor.fetchone()
            return int(row["c"]) if row else 0

    def cancel_queued_execution(self, execution_id: str, reason: str = "cancelled") -> bool:
        """Cancel a single queued execution. No container interaction.

        Returns:
            True if the row was still queued and is now cancelled, False otherwise.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = ?,
                    error = ?
                WHERE id = ? AND status = ?
                """,
                (
                    TaskExecutionStatus.CANCELLED,
                    now,
                    reason,
                    execution_id,
                    TaskExecutionStatus.QUEUED,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def cancel_queued_for_agent(self, agent_name: str, reason: str = "agent_deleted") -> int:
        """Bulk-cancel all queued executions for an agent.

        Used on agent deletion so orphan queued rows don't linger.

        Returns:
            Count of rows moved from QUEUED to CANCELLED.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = ?,
                    error = ?
                WHERE agent_name = ? AND status = ?
                """,
                (
                    TaskExecutionStatus.CANCELLED,
                    now,
                    reason,
                    agent_name,
                    TaskExecutionStatus.QUEUED,
                ),
            )
            conn.commit()
            return cursor.rowcount

    def expire_stale_queued(self, max_age_hours: float = 24) -> int:
        """Mark queued executions older than max_age_hours as FAILED.

        Runs from the 60s maintenance task. Uses ISO-8601 string comparison on
        queued_at, matching how stale running executions are handled elsewhere.

        Returns:
            Count of queued rows expired.
        """
        now = utc_now_iso()
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=float(max_age_hours))
        ).strftime('%Y-%m-%dT%H:%M:%S')
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = ?,
                    error = 'Backlog expired: queued longer than ' || ? || ' hours'
                WHERE status = ? AND queued_at IS NOT NULL AND queued_at < ?
                """,
                (
                    TaskExecutionStatus.FAILED,
                    now,
                    str(max_age_hours),
                    TaskExecutionStatus.QUEUED,
                    threshold,
                ),
            )
            conn.commit()
            return cursor.rowcount

    def list_agents_with_queued(self) -> List[str]:
        """Return the list of agent names that currently have queued backlog items.

        Used by the 60s maintenance task to drain orphans after a restart
        (backend crashed between enqueue and drain, or drain callback was lost).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT agent_name FROM schedule_executions
                WHERE status = ?
                """,
                (TaskExecutionStatus.QUEUED,),
            )
            return [row["agent_name"] for row in cursor.fetchall()]

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
        claude_session_id: str = None
    ) -> bool:
        """Update execution status when completed.

        Args:
            claude_session_id: Claude Code session ID for --resume support (EXEC-023)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get started_at for duration calculation and current status/error
            # for #378 residual-race observability (see log below).
            cursor.execute(
                "SELECT started_at, status, error FROM schedule_executions WHERE id = ?",
                (execution_id,),
            )
            row = cursor.fetchone()
            if not row:
                return False

            # #378: warn when SUCCESS overwrites a Phase-3 phantom-stale FAILED.
            # This lets us observe residual races in production without
            # changing update semantics (agent's response still wins). Scoped
            # to the stale-slot error pattern so other legitimate FAILED→SUCCESS
            # transitions (Phase 0/1 recovery, startup recovery) don't misfire.
            current_status = row["status"] if "status" in row.keys() else None
            current_error = row["error"] if "error" in row.keys() else None
            if (
                status == TaskExecutionStatus.SUCCESS
                and current_status == TaskExecutionStatus.FAILED
                and current_error
                and _STALE_SLOT_ERROR_PATTERN in current_error
            ):
                logger.warning(
                    f"[DB] SUCCESS overwrote Phase-3 stale-slot FAILED for execution "
                    f"{execution_id} — residual race condition (#378). Prior error: "
                    f"{current_error[:200]}"
                )

            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            started_at = parse_iso_timestamp(row["started_at"])
            completed_at = parse_iso_timestamp(utc_now_iso())
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?, completed_at = ?, duration_ms = ?, response = ?, error = ?,
                    context_used = ?, context_max = ?, cost = ?, tool_calls = ?, execution_log = ?,
                    claude_session_id = ?
                WHERE id = ?
            """, (
                status,
                to_utc_iso(completed_at),  # Use UTC with 'Z' suffix
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

    def get_schedule_executions(self, schedule_id: str, limit: int = 50) -> List[ScheduleExecution]:
        """Get execution history for a schedule."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM schedule_executions
                WHERE schedule_id = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (schedule_id, limit))
            return [self._row_to_schedule_execution(row) for row in cursor.fetchall()]

    def get_agent_executions(self, agent_name: str, limit: int = 50) -> List[ScheduleExecution]:
        """Get all executions for an agent across all schedules."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM schedule_executions
                WHERE agent_name = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (agent_name, limit))
            return [self._row_to_schedule_execution(row) for row in cursor.fetchall()]

    def get_agent_executions_summary(self, agent_name: str, limit: int = 50) -> List[Dict]:
        """Get execution summaries for list view - excludes large text fields.

        Returns only the columns needed for the Tasks list UI, excluding:
        - response (can be large)
        - error (can be large)
        - tool_calls (JSON array, can be large)
        - execution_log (100KB+ per execution)

        This provides 50-100x data reduction vs SELECT * for list views.
        Use get_execution() for full details on a single execution.

        PERF-001: Task List Performance Optimization
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    id, schedule_id, agent_name, status, started_at, completed_at,
                    duration_ms, message, triggered_by, context_used, context_max, cost,
                    source_user_id, source_user_email, source_agent_name,
                    source_mcp_key_id, source_mcp_key_name, claude_session_id, model_used,
                    fan_out_id, business_status, validation_execution_id
                FROM schedule_executions
                WHERE agent_name = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, (agent_name, limit))
            return [dict(row) for row in cursor.fetchall()]

    def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
        """Get a specific execution by ID."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM schedule_executions WHERE id = ?", (execution_id,))
            row = cursor.fetchone()
            return self._row_to_schedule_execution(row) if row else None

    def get_agent_execution_stats(self, agent_name: str, hours: int = 24) -> Dict:
        """Get execution statistics for a single agent.

        Used for platform metrics injection in dashboard (DASH-001).

        Args:
            agent_name: Name of the agent
            hours: Time window in hours (default: 24)

        Returns:
            Dict with execution stats: task_count, success_count, failed_count,
            running_count, success_rate, total_cost, avg_duration_ms, last_execution_at
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    AVG(duration_ms) as avg_duration_ms,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE agent_name = ?
                AND started_at > datetime('now', ? || ' hours')
            """, (agent_name, f"-{hours}"))

            row = cursor.fetchone()
            if not row or row["task_count"] == 0:
                return {
                    "task_count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "running_count": 0,
                    "success_rate": 0,
                    "total_cost": 0,
                    "avg_duration_ms": None,
                    "last_execution_at": None
                }

            task_count = row["task_count"]
            success_count = row["success_count"] or 0
            success_rate = round((success_count / task_count * 100), 1) if task_count > 0 else 0

            return {
                "task_count": task_count,
                "success_count": success_count,
                "failed_count": row["failed_count"] or 0,
                "running_count": row["running_count"] or 0,
                "success_rate": success_rate,
                "total_cost": round(row["total_cost"] or 0, 4),
                "avg_duration_ms": int(row["avg_duration_ms"]) if row["avg_duration_ms"] else None,
                "last_execution_at": row["last_execution_at"]
            }

    def get_all_agents_execution_stats(self, hours: int = 24) -> List[Dict]:
        """Get execution statistics for all agents.

        Returns aggregated stats per agent for the specified time window.

        Args:
            hours: Time window in hours (default: 24)

        Returns:
            List of dicts with agent execution stats
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    agent_name,
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE started_at > datetime('now', ? || ' hours')
                GROUP BY agent_name
            """, (f"-{hours}",))

            results = []
            for row in cursor.fetchall():
                task_count = row["task_count"]
                success_count = row["success_count"]
                success_rate = round((success_count / task_count * 100), 1) if task_count > 0 else 0

                results.append({
                    "name": row["agent_name"],
                    "task_count_24h": task_count,
                    "success_count": success_count,
                    "failed_count": row["failed_count"],
                    "running_count": row["running_count"],
                    "success_rate": success_rate,
                    "total_cost": round(row["total_cost"], 4) if row["total_cost"] else 0,
                    "last_execution_at": row["last_execution_at"]
                })

            return results

    def get_all_agents_execution_stats_dual(self) -> List[Dict]:
        """Get execution statistics for all agents with both 24h and 7d windows.

        Single SQL query using CASE WHEN to compute both time windows efficiently.

        Returns:
            List of dicts with agent execution stats for both 24h and 7d windows.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    agent_name,
                    SUM(CASE WHEN started_at > datetime('now', '-24 hours') THEN 1 ELSE 0 END) as task_count_24h,
                    SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'success' THEN 1 ELSE 0 END) as success_count_24h,
                    SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'failed' THEN 1 ELSE 0 END) as failed_count_24h,
                    SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'running' THEN 1 ELSE 0 END) as running_count_24h,
                    SUM(CASE WHEN started_at > datetime('now', '-24 hours') THEN COALESCE(cost, 0) ELSE 0 END) as total_cost_24h,
                    MAX(CASE WHEN started_at > datetime('now', '-24 hours') THEN started_at ELSE NULL END) as last_execution_at_24h,
                    COUNT(*) as task_count_7d,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count_7d,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count_7d,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count_7d,
                    SUM(COALESCE(cost, 0)) as total_cost_7d,
                    MAX(started_at) as last_execution_at_7d
                FROM schedule_executions
                WHERE started_at > datetime('now', '-168 hours')
                GROUP BY agent_name
            """)

            results = []
            for row in cursor.fetchall():
                task_count_24h = row["task_count_24h"] or 0
                success_count_24h = row["success_count_24h"] or 0
                success_rate_24h = round((success_count_24h / task_count_24h * 100), 1) if task_count_24h > 0 else 0

                task_count_7d = row["task_count_7d"] or 0
                success_count_7d = row["success_count_7d"] or 0
                success_rate_7d = round((success_count_7d / task_count_7d * 100), 1) if task_count_7d > 0 else 0

                results.append({
                    "name": row["agent_name"],
                    "task_count_24h": task_count_24h,
                    "success_count": success_count_24h,
                    "failed_count": row["failed_count_24h"] or 0,
                    "running_count": row["running_count_24h"] or 0,
                    "success_rate": success_rate_24h,
                    "total_cost": round(row["total_cost_24h"] or 0, 4),
                    "last_execution_at": row["last_execution_at_24h"],
                    "task_count_7d": task_count_7d,
                    "success_count_7d": success_count_7d,
                    "failed_count_7d": row["failed_count_7d"] or 0,
                    "running_count_7d": row["running_count_7d"] or 0,
                    "success_rate_7d": success_rate_7d,
                    "total_cost_7d": round(row["total_cost_7d"] or 0, 4),
                    "last_execution_at_7d": row["last_execution_at_7d"]
                })

            return results

    def get_all_agents_schedule_counts(self) -> Dict[str, Dict[str, int]]:
        """Get schedule counts (total and enabled) for all agents.

        Returns:
            Dict mapping agent_name to {"total": X, "enabled": Y}
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    agent_name,
                    COUNT(*) as total,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) as enabled
                FROM agent_schedules
                GROUP BY agent_name
            """)

            results = {}
            for row in cursor.fetchall():
                results[row["agent_name"]] = {
                    "total": row["total"],
                    "enabled": row["enabled"]
                }

            return results

    # =========================================================================
    # Git Configuration Management (Phase 7: GitHub Bidirectional Sync)
    # =========================================================================

    def create_git_config(
        self,
        agent_name: str,
        github_repo: str,
        working_branch: str,
        instance_id: str,
        sync_paths: List[str] = None,
        source_branch: str = "main",
        source_mode: bool = False
    ) -> Optional[AgentGitConfig]:
        """Create git configuration for an agent.

        Args:
            agent_name: Name of the agent
            github_repo: GitHub repository (e.g., "owner/repo")
            working_branch: Branch for Trinity to work on (legacy mode) or same as source_branch
            instance_id: Unique instance identifier
            sync_paths: Paths to sync (default: memory/, outputs/, etc.)
            source_branch: Branch to pull updates from (default: "main")
            source_mode: If True, track source_branch directly without creating a working branch
        """
        config_id = self._generate_id()
        now = utc_now_iso()
        sync_paths_json = json.dumps(sync_paths) if sync_paths else json.dumps(["memory/", "outputs/", "CLAUDE.md", ".claude/"])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO agent_git_config (
                        id, agent_name, github_repo, working_branch, instance_id,
                        source_branch, source_mode, created_at, sync_enabled, sync_paths
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (config_id, agent_name, github_repo, working_branch, instance_id,
                      source_branch, 1 if source_mode else 0, now, sync_paths_json))
                conn.commit()

                return AgentGitConfig(
                    id=config_id,
                    agent_name=agent_name,
                    github_repo=github_repo,
                    working_branch=working_branch,
                    instance_id=instance_id,
                    source_branch=source_branch,
                    source_mode=source_mode,
                    created_at=datetime.fromisoformat(now),
                    sync_enabled=True,
                    sync_paths=sync_paths_json
                )
            except sqlite3.IntegrityError:
                # Already exists
                return None

    def get_git_config(self, agent_name: str) -> Optional[AgentGitConfig]:
        """Get git configuration for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM agent_git_config WHERE agent_name = ?", (agent_name,))
            row = cursor.fetchone()
            return self._row_to_git_config(row) if row else None

    def update_git_sync(self, agent_name: str, commit_sha: str) -> bool:
        """Update git sync timestamp and commit SHA after successful sync."""
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_git_config
                SET last_sync_at = ?, last_commit_sha = ?
                WHERE agent_name = ?
            """, (now, commit_sha, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def set_git_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable or disable git sync for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_git_config SET sync_enabled = ? WHERE agent_name = ?
            """, (1 if enabled else 0, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def set_git_auto_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle the 15-min auto-sync heartbeat for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_git_config SET auto_sync_enabled = ? WHERE agent_name = ?",
                (1 if enabled else 0, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_freeze_schedules_if_sync_failing(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle scheduler freeze-on-failure opt-in."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_git_config "
                "SET freeze_schedules_if_sync_failing = ? WHERE agent_name = ?",
                (1 if enabled else 0, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_git_auto_sync_enabled(self, agent_name: str) -> bool:
        """#389: read the auto-sync flag. False if config missing."""
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT auto_sync_enabled FROM agent_git_config WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
            return bool(row[0]) if row else False

    def get_freeze_schedules_if_sync_failing(self, agent_name: str) -> bool:
        """#389: read the freeze-schedules flag. False if config missing."""
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT freeze_schedules_if_sync_failing "
                "FROM agent_git_config WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
            return bool(row[0]) if row else False

    def delete_git_config(self, agent_name: str) -> bool:
        """Delete git configuration for an agent (when agent is deleted)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM agent_git_config WHERE agent_name = ?", (agent_name,))
            conn.commit()
            return cursor.rowcount > 0

    def get_running_executions(self) -> list:
        """Get all schedule executions currently in 'running' status.

        Used by startup recovery to detect orphaned executions after a crash.

        Returns:
            List of dicts with id, agent_name, started_at, schedule_id.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, agent_name, started_at, schedule_id
                FROM schedule_executions
                WHERE status = ?
            """, (TaskExecutionStatus.RUNNING,))
            return [dict(row) for row in cursor.fetchall()]

    def mark_stale_executions_failed(self, timeout_minutes: int = 30) -> int:
        """Mark running executions older than timeout as failed.

        Uses TaskExecutionStatus.RUNNING / .FAILED for status values.

        Args:
            timeout_minutes: Executions running longer than this are considered stale.

        Returns:
            Number of executions marked as failed.
        """
        now = utc_now_iso()
        # Compute threshold in ISO 8601 format to match stored started_at
        # (SQLite's datetime() returns space-separated format which breaks string comparison)
        threshold = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).strftime('%Y-%m-%dT%H:%M:%S')
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Find stale executions for duration calculation
            cursor.execute("""
                SELECT id, started_at FROM schedule_executions
                WHERE status = ?
                AND started_at < ?
            """, (TaskExecutionStatus.RUNNING, threshold))
            stale_rows = cursor.fetchall()

            if not stale_rows:
                return 0

            completed_at = parse_iso_timestamp(now)
            for row in stale_rows:
                started_at = parse_iso_timestamp(row["started_at"])
                duration_ms = int((completed_at - started_at).total_seconds() * 1000)
                # SQL literal matches TaskExecutionStatus.FAILED
                cursor.execute("""
                    UPDATE schedule_executions
                    SET status = ?,
                        completed_at = ?,
                        duration_ms = ?,
                        error = 'Marked as failed by cleanup: exceeded ' || ? || '-minute timeout'
                    WHERE id = ?
                """, (TaskExecutionStatus.FAILED, now, duration_ms, str(timeout_minutes), row["id"]))

            conn.commit()
            return len(stale_rows)

    def mark_no_session_executions_failed(self, timeout_seconds: int = 60) -> int:
        """Mark running executions with no claude_session_id as failed.

        Executions that are 'running' but never received a claude_session_id
        are silent launch failures — the backend failed to start a Claude session.
        These should be cleaned up quickly rather than waiting the full stale timeout.

        Args:
            timeout_seconds: Executions running longer than this without a session
                are considered failed launches.

        Returns:
            Number of executions marked as failed.
        """
        now = utc_now_iso()
        # Compute threshold in ISO 8601 format to match stored started_at
        # (SQLite's datetime() returns space-separated format which breaks string comparison)
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).strftime('%Y-%m-%dT%H:%M:%S')
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, started_at FROM schedule_executions
                WHERE status = ?
                AND (claude_session_id IS NULL OR claude_session_id = '')
                AND started_at < ?
            """, (TaskExecutionStatus.RUNNING, threshold))
            no_session_rows = cursor.fetchall()

            if not no_session_rows:
                return 0

            completed_at = parse_iso_timestamp(now)
            for row in no_session_rows:
                started_at = parse_iso_timestamp(row["started_at"])
                duration_ms = int((completed_at - started_at).total_seconds() * 1000)
                cursor.execute("""
                    UPDATE schedule_executions
                    SET status = ?,
                        completed_at = ?,
                        duration_ms = ?,
                        error = 'Silent launch failure: no Claude session created within ' || ? || ' seconds'
                    WHERE id = ?
                """, (TaskExecutionStatus.FAILED, now, duration_ms, str(timeout_seconds), row["id"]))

            conn.commit()
            return len(no_session_rows)

    def fail_stale_slot_execution(self, execution_id: str, error: str) -> bool:
        """Mark a single execution as failed if it is still running.

        Used by the cleanup service when a stale Redis slot is reclaimed.
        The WHERE status='running' guard prevents overwriting executions
        that have already completed or failed via another path.

        Args:
            execution_id: The execution to fail.
            error: Error message describing why the execution was failed.

        Returns:
            True if the execution was updated, False if it was not found
            or was no longer in 'running' status.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT started_at FROM schedule_executions WHERE id = ? AND status = ?",
                (execution_id, TaskExecutionStatus.RUNNING),
            )
            row = cursor.fetchone()
            if not row:
                return False

            completed_at = parse_iso_timestamp(now)
            started_at = parse_iso_timestamp(row["started_at"])
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = ?,
                    duration_ms = ?,
                    error = ?
                WHERE id = ? AND status = ?
            """, (TaskExecutionStatus.FAILED, now, duration_ms, error,
                  execution_id, TaskExecutionStatus.RUNNING))

            conn.commit()
            return cursor.rowcount > 0

    def finalize_orphaned_skipped_executions(self) -> int:
        """Finalize skipped executions that are missing completed_at.

        Defensive cleanup for any skipped execution records that were not
        properly terminated at creation time.

        Returns:
            Number of executions finalized.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = COALESCE(started_at, ?),
                    duration_ms = 0,
                    error = 'Finalized by cleanup: skipped execution'
                WHERE status = ?
                AND completed_at IS NULL
            """, (TaskExecutionStatus.FAILED, now, TaskExecutionStatus.SKIPPED))

            conn.commit()
            return cursor.rowcount

    def get_running_executions_with_agent_info(self) -> List[Dict]:
        """Get all running executions with effective timeout for watchdog.

        Returns executions joined with schedule and agent ownership data.
        Timeout resolution order:
        1. Schedule's timeout_seconds (for scheduled executions)
        2. Agent's execution_timeout_seconds (for manual/MCP executions)
        3. Fallback default of 900s

        Returns:
            List of dicts with id, schedule_id, agent_name, started_at,
            and timeout_seconds.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.id, e.schedule_id, e.agent_name, e.started_at,
                       COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900) as timeout_seconds
                FROM schedule_executions e
                LEFT JOIN agent_schedules s ON e.schedule_id = s.id
                LEFT JOIN agent_ownership ao ON e.agent_name = ao.agent_name
                WHERE e.status = ?
            """, (TaskExecutionStatus.RUNNING,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def mark_execution_failed_by_watchdog(self, execution_id: str, error_message: str) -> bool:
        """Mark a running execution as failed by the watchdog.

        Uses a conditional update (WHERE status='running') to prevent overwriting
        a normal completion that happened between the watchdog check and this update.

        Args:
            execution_id: The execution to mark as failed.
            error_message: Descriptive error message for the failure.

        Returns:
            True if the execution was updated (was still running),
            False if it had already transitioned to another status.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get started_at for duration calculation
            cursor.execute(
                "SELECT started_at FROM schedule_executions WHERE id = ?",
                (execution_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False

            completed_at = parse_iso_timestamp(now)
            started_at = parse_iso_timestamp(row["started_at"])
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            cursor.execute("""
                UPDATE schedule_executions
                SET status = ?,
                    completed_at = ?,
                    duration_ms = ?,
                    error = ?
                WHERE id = ? AND status = ?
            """, (
                TaskExecutionStatus.FAILED,
                now,
                duration_ms,
                error_message,
                execution_id,
                TaskExecutionStatus.RUNNING,
            ))

            conn.commit()
            return cursor.rowcount > 0

    def list_git_enabled_agents(self) -> List[AgentGitConfig]:
        """List all agents with git sync enabled."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM agent_git_config WHERE sync_enabled = 1
                ORDER BY agent_name
            """)
            return [self._row_to_git_config(row) for row in cursor.fetchall()]

    # =========================================================================
    # Business Validation (VALIDATE-001)
    # =========================================================================

    def create_validation_execution(
        self,
        validates_execution_id: str,
        agent_name: str,
        schedule_id: str,
        message: str,
        timeout_seconds: int = 120,
    ) -> Optional[ScheduleExecution]:
        """Create a validation execution record linked to the original execution.

        Args:
            validates_execution_id: The execution being validated.
            agent_name: The agent running validation.
            schedule_id: The schedule that triggered the original execution.
            message: The validation prompt message.
            timeout_seconds: Timeout for validation task.

        Returns:
            The created validation execution record.
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO schedule_executions (
                    id, schedule_id, agent_name, status, started_at, message, triggered_by,
                    validates_execution_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                execution_id,
                schedule_id,
                agent_name,
                TaskExecutionStatus.RUNNING,
                now,
                message,
                "validation",  # New trigger type for validation
                validates_execution_id,
            ))
            conn.commit()

            return ScheduleExecution(
                id=execution_id,
                schedule_id=schedule_id,
                agent_name=agent_name,
                status=TaskExecutionStatus.RUNNING,
                started_at=datetime.fromisoformat(now),
                message=message,
                triggered_by="validation",
                validates_execution_id=validates_execution_id,
            )

    def update_business_status(
        self,
        execution_id: str,
        business_status: str,
        validation_execution_id: Optional[str] = None,
    ) -> bool:
        """Update the business validation status of an execution.

        Args:
            execution_id: The execution to update.
            business_status: The new business status (pending_validation, validated, failed_validation, skipped).
            validation_execution_id: Optional FK to the validation execution record.

        Returns:
            True if the row was updated.
        """
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()

            if validation_execution_id:
                cursor.execute("""
                    UPDATE schedule_executions
                    SET business_status = ?, validated_at = ?, validation_execution_id = ?
                    WHERE id = ?
                """, (business_status, now, validation_execution_id, execution_id))
            else:
                cursor.execute("""
                    UPDATE schedule_executions
                    SET business_status = ?, validated_at = ?
                    WHERE id = ?
                """, (business_status, now, execution_id))

            conn.commit()
            return cursor.rowcount > 0

    def get_executions_pending_validation(self, agent_name: str = None) -> List[ScheduleExecution]:
        """Get executions that are pending validation.

        Used for startup recovery to retry failed validation attempts.

        Args:
            agent_name: Optional filter by agent name.

        Returns:
            List of executions with business_status = 'pending_validation'.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            if agent_name:
                cursor.execute("""
                    SELECT * FROM schedule_executions
                    WHERE business_status = 'pending_validation' AND agent_name = ?
                    ORDER BY started_at ASC
                """, (agent_name,))
            else:
                cursor.execute("""
                    SELECT * FROM schedule_executions
                    WHERE business_status = 'pending_validation'
                    ORDER BY started_at ASC
                """)

            return [self._row_to_schedule_execution(row) for row in cursor.fetchall()]

    def get_validation_execution(self, validates_execution_id: str) -> Optional[ScheduleExecution]:
        """Get the validation execution record for a given original execution.

        Args:
            validates_execution_id: The original execution ID.

        Returns:
            The validation execution record, or None if not found.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM schedule_executions
                WHERE validates_execution_id = ?
                ORDER BY started_at DESC
                LIMIT 1
            """, (validates_execution_id,))
            row = cursor.fetchone()
            return self._row_to_schedule_execution(row) if row else None
