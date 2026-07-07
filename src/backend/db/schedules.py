"""
Schedule and execution management database operations.

Handles schedule CRUD, execution tracking, and Git configuration.
"""

import json
import logging
import secrets
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

import pytz
from croniter import croniter
from sqlalchemy import select, insert, update, delete, and_, or_, func, text, case
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .query_helpers import latest_per_group
from .tables import (
    agent_schedules,
    schedule_executions,
    agent_git_config,
    agent_ownership,
)
from db_models import Schedule, ScheduleCreate, ScheduleExecution, AgentGitConfig
from models import TaskExecutionStatus
from utils.helpers import iso_cutoff, utc_now_iso, to_utc_iso, parse_iso_timestamp

logger = logging.getLogger(__name__)

# Cap on rows fed into percentile compute and tool-call aggregation
# (#868). Counts and the daily timeline always use the unsampled rowset;
# only the percentile / tool-call pool is bounded. Test-only override is
# via `monkeypatch.setattr(db.schedules, "_PERCENTILE_ROWSET_CAP", N)`.
_PERCENTILE_ROWSET_CAP = 5000

# #1107: user-facing grouping of raw `triggered_by` values for the agent
# Overview "executions by type" chart. Unmapped values fall into "Other"
# (see `_BUCKET_ORDER`) so a new trigger type stays visible instead of
# silently vanishing. Locked by /autoplan taste decision (extend buckets to
# fit real data — `manual` is the dominant real-world value).
_TRIGGER_BUCKETS = {
    "chat": "Chat/Tasks", "manual": "Chat/Tasks", "user": "Chat/Tasks",
    "session": "Chat/Tasks", "self_chat": "Chat/Tasks",
    "mcp": "MCP",
    "telegram": "Channels", "slack": "Channels", "whatsapp": "Channels",
    "public": "Public", "paid": "Public",
    "schedule": "Scheduled", "webhook": "Scheduled",
    "loop": "Loops",  # #1150: first-class bucket so loop bursts don't read as cron load
    "agent": "Agent-to-agent", "fan_out": "Agent-to-agent",
    "self_task": "Agent-to-agent",
    "voip": "Voice", "voice": "Voice",
}
# Stack / legend order; "Other" last so unmapped triggers are visible.
_BUCKET_ORDER = [
    "Chat/Tasks", "MCP", "Channels", "Public",
    "Scheduled", "Loops", "Agent-to-agent", "Voice", "Other",
]
_OTHER_BUCKET = "Other"


def _bucket_for_trigger(trigger: Optional[str]) -> str:
    """Map a raw `triggered_by` value to its user-facing bucket (#1107)."""
    return _TRIGGER_BUCKETS.get(trigger or "", _OTHER_BUCKET)


# #1115: max chars for the per-schedule "command" label derived from a
# schedule's message (the Overview/Schedules-tab scorecard headline).
_SCHEDULE_LABEL_MAX = 80


def _schedule_command_label(message: Optional[str]) -> str:
    """Short headline for a schedule's scorecard, derived from its message.

    Uses the first non-empty line (the command/intent, e.g. ``/do-something``),
    collapsed and truncated. Empty when the message is blank — the frontend
    falls back to the schedule name.
    """
    if not message:
        return ""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return (
                stripped[: _SCHEDULE_LABEL_MAX - 1] + "…"
                if len(stripped) > _SCHEDULE_LABEL_MAX
                else stripped
            )
    return ""


# #73: chunk size for scoped `IN (...)` lookups. SQLite caps host parameters at
# SQLITE_MAX_VARIABLE_NUMBER (999 before SQLite 3.32). Keep a safe margin below
# that so a large accessible-agent set can't blow the limit. Read as a module
# global so tests can monkeypatch it to exercise the multi-chunk path.
_SQLITE_MAX_IN_VARS = 900

# #378: Error-message marker written by cleanup_service._process_stale_slot_reclaims
# when Phase 3 fails an execution. Used to scope the residual-race WARNING log
# below so it doesn't misfire on other legitimate FAILED→SUCCESS transitions
# (e.g. Phase 0 auto-terminate, Phase 1 stale cleanup, startup recovery).


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
            # #913: NULL ⇒ inherit from agent_ownership.execution_timeout_seconds.
            # Do NOT fall through to a constant here — that was the bug.
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row_keys else None,
            allowed_tools=allowed_tools,
            model=row["model"] if "model" in row_keys else None,
            # Retry configuration (RETRY-001)
            max_retries=row["max_retries"] if "max_retries" in row_keys and row["max_retries"] is not None else 0,
            retry_delay_seconds=row["retry_delay_seconds"] if "retry_delay_seconds" in row_keys and row["retry_delay_seconds"] is not None else 60,
            # Validation configuration (VALIDATE-001)
            validation_enabled=bool(row["validation_enabled"]) if "validation_enabled" in row_keys and row["validation_enabled"] is not None else False,
            validation_prompt=row["validation_prompt"] if "validation_prompt" in row_keys else None,
            validation_timeout_seconds=row["validation_timeout_seconds"] if "validation_timeout_seconds" in row_keys and row["validation_timeout_seconds"] is not None else 120,
            # Webhook trigger (WEBHOOK-001 / #647 follow-up)
            webhook_enabled=bool(row["webhook_enabled"]) if "webhook_enabled" in row_keys and row["webhook_enabled"] is not None else False,
            webhook_token=row["webhook_token"] if "webhook_token" in row_keys else None,
            # ent#77: signature-auth fields (graceful default for pre-migration rows)
            webhook_auth_enabled=bool(row["webhook_auth_enabled"]) if "webhook_auth_enabled" in row_keys and row["webhook_auth_enabled"] is not None else False,
            webhook_secret_encrypted=row["webhook_secret_encrypted"] if "webhook_secret_encrypted" in row_keys else None,
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
            # Auto-compact observability (Bundle B)
            compact_metadata=row["compact_metadata"] if "compact_metadata" in row_keys else None,
            # Reader-race auto-retry (#678)
            retry_count=row["retry_count"] if "retry_count" in row_keys and row["retry_count"] is not None else 0,
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

        # #1445: no-orphan invariant enforced at the actual chokepoint.
        # `can_user_access_agent` returns True unconditionally for admins with
        # no existence check, so without this guard an admin could mint a
        # schedule (and a real webhook token) on a never-created agent — whose
        # token then 404s deterministically at `get_schedule_by_webhook_token`
        # (INNER JOIN on agent_ownership, #1423). Refuse creation on an agent
        # with no live ownership row so the invariant holds for every caller
        # (router, MCP, system-manifest deploy, future/internal). The router
        # maps this `None` to 403. Safe for the one direct caller today —
        # system-manifest deploy creates the agent (register_agent_owner)
        # before its schedules.
        if not self._agent_ops.is_agent_live(agent_name):
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

        try:
            # Clamp retry values to valid ranges (RETRY-001)
            max_retries = max(0, min(5, schedule_data.max_retries))
            retry_delay_seconds = max(30, min(600, schedule_data.retry_delay_seconds))

            # Clamp validation timeout to valid range (VALIDATE-001)
            validation_timeout_seconds = max(30, min(600, schedule_data.validation_timeout_seconds))

            with get_engine().begin() as conn:
                conn.execute(
                    insert(agent_schedules).values(
                        id=schedule_id,
                        agent_name=agent_name,
                        name=schedule_data.name,
                        cron_expression=schedule_data.cron_expression,
                        message=schedule_data.message,
                        enabled=1 if schedule_data.enabled else 0,
                        timezone=schedule_data.timezone,
                        description=schedule_data.description,
                        owner_id=user["id"],
                        created_at=now,
                        updated_at=now,
                        next_run_at=next_run_at_iso,
                        timeout_seconds=schedule_data.timeout_seconds,
                        allowed_tools=allowed_tools_json,
                        model=schedule_data.model,
                        max_retries=max_retries,
                        retry_delay_seconds=retry_delay_seconds,
                        validation_enabled=1 if schedule_data.validation_enabled else 0,
                        validation_prompt=schedule_data.validation_prompt,
                        validation_timeout_seconds=validation_timeout_seconds,
                    )
                )

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
        except IntegrityError:
            return None

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        """Get a schedule by ID. Excludes soft-deleted schedules (#834)."""
        stmt = select(agent_schedules).where(
            and_(
                agent_schedules.c.id == schedule_id,
                agent_schedules.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_schedule(row) if row else None

    def list_agent_schedules(self, agent_name: str) -> List[Schedule]:
        """List all schedules for an agent. Excludes soft-deleted (#834)."""
        stmt = (
            select(agent_schedules)
            .where(
                and_(
                    agent_schedules.c.agent_name == agent_name,
                    agent_schedules.c.deleted_at.is_(None),
                )
            )
            .order_by(agent_schedules.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [self._row_to_schedule(row) for row in conn.execute(stmt).mappings()]

    def find_active_schedules_exceeding_timeout(
        self, agent_name: str, ceiling_seconds: int
    ) -> List[Dict]:
        """Active schedules whose ``timeout_seconds > ceiling_seconds`` (#929).

        Returns a thin list of ``{id, name, timeout_seconds}`` dicts for
        the agent-cap-lowering error payload — the caller surfaces them
        so the operator knows which schedules need editing first.
        """
        stmt = (
            select(
                agent_schedules.c.id,
                agent_schedules.c.name,
                agent_schedules.c.timeout_seconds,
            )
            .where(
                and_(
                    agent_schedules.c.agent_name == agent_name,
                    agent_schedules.c.deleted_at.is_(None),
                    agent_schedules.c.timeout_seconds > ceiling_seconds,
                )
            )
            .order_by(agent_schedules.c.timeout_seconds.desc())
        )
        with get_engine().connect() as conn:
            return [
                {"id": row["id"], "name": row["name"], "timeout_seconds": row["timeout_seconds"]}
                for row in conn.execute(stmt).mappings()
            ]

    def list_all_enabled_schedules(self) -> List[Schedule]:
        """List all enabled schedules (for scheduler initialization).

        Two soft-delete filters apply:
        - #834 Phase 1a: skip schedules whose *agent* is soft-deleted
          (`agent_ownership.deleted_at`) — otherwise the scheduler fires
          every enabled schedule for a soft-deleted agent and writes a
          `schedule_executions` failure row per tick until purge.
        - #834 Phase 1b: skip *schedules* that are themselves
          soft-deleted (`agent_schedules.deleted_at`).
        """
        stmt = (
            select(agent_schedules)
            .select_from(
                agent_schedules.join(
                    agent_ownership,
                    agent_ownership.c.agent_name == agent_schedules.c.agent_name,
                )
            )
            .where(
                and_(
                    agent_schedules.c.enabled == 1,
                    agent_schedules.c.deleted_at.is_(None),
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .order_by(agent_schedules.c.agent_name, agent_schedules.c.name)
        )
        with get_engine().connect() as conn:
            return [self._row_to_schedule(row) for row in conn.execute(stmt).mappings()]

    def list_all_disabled_schedules(self) -> List[Schedule]:
        """List all disabled schedules (for resume operations).

        Excludes soft-deleted (#834).
        """
        stmt = (
            select(agent_schedules)
            .where(
                and_(
                    agent_schedules.c.enabled == 0,
                    agent_schedules.c.deleted_at.is_(None),
                )
            )
            .order_by(agent_schedules.c.agent_name, agent_schedules.c.name)
        )
        with get_engine().connect() as conn:
            return [self._row_to_schedule(row) for row in conn.execute(stmt).mappings()]

    def list_all_schedules(self) -> List[Schedule]:
        """List all schedules across all agents (for system agent overview).

        Excludes soft-deleted (#834).
        """
        stmt = (
            select(agent_schedules)
            .where(agent_schedules.c.deleted_at.is_(None))
            .order_by(agent_schedules.c.agent_name, agent_schedules.c.name)
        )
        with get_engine().connect() as conn:
            return [self._row_to_schedule(row) for row in conn.execute(stmt).mappings()]

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

        values: Dict = {}
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
                values[key] = value

        if not values:
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
                values["next_run_at"] = next_run_at.isoformat() if next_run_at else None
            else:
                # Clear next_run_at if schedule is disabled
                values["next_run_at"] = None

        values["updated_at"] = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(**values)
            )

        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str, username: str) -> bool:
        """Soft-delete a schedule (Issue #834 Phase 1b).

        Sets `agent_schedules.deleted_at = NOW`. Executions stay intact —
        they're billing-relevant (subscription_id rollup) and #772's
        retention sweep ages them out independently.

        The scheduler service filters `deleted_at IS NULL` on its
        enabled-schedules poll, so soft-deleted schedules stop firing
        immediately. `cleanup_service.py` hard-purges rows past
        `schedule_soft_delete_retention_days` (default 30).

        Idempotent: re-deleting an already-soft-deleted schedule still
        returns True provided the caller has permission.
        """
        from utils.helpers import utc_now_iso

        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        with get_engine().begin() as conn:
            # Permission check must read the row *including* soft-deleted
            # ones. `get_schedule()` filters `deleted_at IS NULL` (#834
            # Phase 1b), so using it here made a retry on an
            # already-soft-deleted schedule fall through to `return
            # False` → the router turned that into a misleading 403
            # "access denied" for the legitimate owner. Read owner_id
            # directly so re-delete is genuinely idempotent.
            row = conn.execute(
                select(
                    agent_schedules.c.owner_id,
                    agent_schedules.c.deleted_at,
                ).where(agent_schedules.c.id == schedule_id)
            ).mappings().first()
            if not row:
                return False

            if user["role"] != "admin" and row["owner_id"] != user["id"]:
                return False

            if row["deleted_at"] is not None:
                # Already soft-deleted and the caller is authorised —
                # idempotent success (router → 204).
                return True

            result = conn.execute(
                update(agent_schedules)
                .where(
                    and_(
                        agent_schedules.c.id == schedule_id,
                        agent_schedules.c.deleted_at.is_(None),
                    )
                )
                .values(deleted_at=utc_now_iso())
            )
            return result.rowcount > 0

    def purge_schedule(self, schedule_id: str) -> bool:
        """Hard-delete a soft-deleted schedule (#834 Phase 1b).

        Called by the cleanup_service retention sweep. Refuses to purge
        a live (non-soft-deleted) row — callers must soft-delete first.
        Also removes `schedule_executions` rows for the schedule —
        consistent with the previous hard-delete behavior and with
        what cascade_delete does at agent purge time.
        """
        with get_engine().begin() as conn:
            row = conn.execute(
                select(agent_schedules.c.deleted_at).where(
                    agent_schedules.c.id == schedule_id
                )
            ).mappings().first()
            if not row or row["deleted_at"] is None:
                return False

            conn.execute(
                delete(schedule_executions).where(
                    schedule_executions.c.schedule_id == schedule_id
                )
            )
            result = conn.execute(
                delete(agent_schedules).where(agent_schedules.c.id == schedule_id)
            )
            return result.rowcount > 0

    def recover_schedule(self, schedule_id: str) -> bool:
        """Recover a soft-deleted schedule by clearing `deleted_at` (#834).

        Refuses to operate on a row that doesn't exist or is already
        live (`deleted_at IS NULL`). Returns True on successful
        recovery. The schedule reappears on the scheduler's
        firing list on the next poll cycle if it was enabled at the
        time of soft-delete.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(
                    and_(
                        agent_schedules.c.id == schedule_id,
                        agent_schedules.c.deleted_at.isnot(None),
                    )
                )
                .values(deleted_at=None)
            )
            return result.rowcount > 0

    def list_soft_deleted_schedules(
        self, agent_name: Optional[str] = None, limit: int = 200
    ) -> list:
        """List currently-soft-deleted schedules with their `deleted_at`.

        If `agent_name` is given, scopes to that agent's schedules
        (admin endpoint pattern: GET /api/admin/agents/{name}/schedules/
        soft-deleted). With `agent_name=None`, returns soft-deleted
        schedules across the fleet (admin-only).
        """
        conds = [agent_schedules.c.deleted_at.isnot(None)]
        if agent_name is not None:
            conds.append(agent_schedules.c.agent_name == agent_name)
        stmt = (
            select(
                agent_schedules.c.id,
                agent_schedules.c.agent_name,
                agent_schedules.c.name,
                agent_schedules.c.cron_expression,
                agent_schedules.c.message,
                agent_schedules.c.owner_id,
                agent_schedules.c.enabled,
                agent_schedules.c.deleted_at,
            )
            .where(and_(*conds))
            .order_by(agent_schedules.c.deleted_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def find_soft_deleted_schedules_past_retention(
        self, retention_days: int, limit: int = 5000
    ) -> list:
        """List schedule ids whose `deleted_at` is older than `retention_days`.

        Used by the cleanup sweep to find rows ready for hard-purge.
        Bounded by `limit`.
        """
        from utils.helpers import iso_cutoff

        if retention_days <= 0 or limit <= 0:
            return []

        cutoff = iso_cutoff(hours=retention_days * 24)
        stmt = (
            select(agent_schedules.c.id)
            .where(
                and_(
                    agent_schedules.c.deleted_at.isnot(None),
                    agent_schedules.c.deleted_at < cutoff,
                )
            )
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [row["id"] for row in conn.execute(stmt).mappings()]

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

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(
                    enabled=1 if enabled else 0,
                    updated_at=utc_now_iso(),
                    next_run_at=next_run_at_iso,
                )
            )
            return result.rowcount > 0

    def update_schedule_run_times(self, schedule_id: str, last_run_at: datetime = None, next_run_at: datetime = None) -> bool:
        """Update schedule run timestamps.

        Does NOT bump ``updated_at`` — that column signals config changes and
        is watched by the scheduler service's sync loop. Bumping it here caused
        a self-triggering loop that re-registered every schedule once per tick
        (Issue #420).
        """
        if last_run_at is None and next_run_at is None:
            return False

        values: Dict = {}
        if last_run_at:
            values["last_run_at"] = last_run_at.isoformat()
        if next_run_at:
            values["next_run_at"] = next_run_at.isoformat()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(**values)
            )
            return result.rowcount > 0

    def delete_agent_schedules(self, agent_name: str) -> int:
        """Delete all schedules for an agent (when agent is deleted)."""
        with get_engine().begin() as conn:
            # Get schedule IDs first
            schedule_ids = [
                row["id"]
                for row in conn.execute(
                    select(agent_schedules.c.id).where(
                        agent_schedules.c.agent_name == agent_name
                    )
                ).mappings()
            ]

            # Delete executions for all schedules
            for sid in schedule_ids:
                conn.execute(
                    delete(schedule_executions).where(
                        schedule_executions.c.schedule_id == sid
                    )
                )

            # Delete schedules
            conn.execute(
                delete(agent_schedules).where(
                    agent_schedules.c.agent_name == agent_name
                )
            )
            return len(schedule_ids)

    # =========================================================================
    # Webhook Management (WEBHOOK-001, #291)
    # =========================================================================

    def generate_webhook_token(self, schedule_id: str) -> Optional[str]:
        """Generate (or regenerate) a webhook token for a schedule.

        Creates a 32-byte URL-safe random token stored in the DB. Calling
        again replaces the old token, immediately invalidating the old URL.
        """
        token = secrets.token_urlsafe(32)
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                # ent#77: rotating the URL is a credential-rotation event — reset
                # any prior signing secret so a leaked old secret can't sign for
                # the new token. The caller re-enables signing explicitly.
                .values(
                    webhook_token=token,
                    webhook_enabled=1,
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                return None
        return token

    def get_schedule_by_webhook_token(self, token: str) -> Optional[Schedule]:
        """Look up a schedule by its webhook token (O(1) via index).

        Applies the same two soft-delete filters as `list_all_enabled_schedules`
        (#834, #1423): skip a soft-deleted *schedule* (`agent_schedules.deleted_at`)
        AND a schedule whose *agent* is soft-deleted (`agent_ownership.deleted_at`).
        Without the agent join, a webhook could still fire a schedule of a
        soft-deleted (recoverable) agent during the retention window — the exact
        hole the cron path guards against.
        """
        stmt = (
            select(agent_schedules)
            .select_from(
                agent_schedules.join(
                    agent_ownership,
                    agent_ownership.c.agent_name == agent_schedules.c.agent_name,
                )
            )
            .where(
                and_(
                    agent_schedules.c.webhook_token == token,
                    agent_schedules.c.deleted_at.is_(None),
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return self._row_to_schedule(row)

    def set_webhook_enabled(self, schedule_id: str, enabled: bool) -> bool:
        """Enable or disable webhook triggering for a schedule."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(webhook_enabled=1 if enabled else 0, updated_at=now)
            )
            return result.rowcount > 0

    def revoke_webhook_token(self, schedule_id: str) -> bool:
        """Revoke a webhook token, immediately invalidating the URL.

        ent#77: also clears the signing secret + auth flag — a revoked webhook
        must not leave a live secret behind, and a re-minted token should start
        auth-off (the caller re-enables signing explicitly).
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(
                    webhook_token=None,
                    webhook_enabled=0,
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

    # ---- ent#77: webhook signature-auth secret --------------------------------

    @staticmethod
    def _encrypt_webhook_secret(secret: str) -> str:
        from services.credential_encryption import get_credential_encryption_service
        from services.webhook_signature import SECRET_ENVELOPE_KEY
        return get_credential_encryption_service().encrypt({SECRET_ENVELOPE_KEY: secret})

    @staticmethod
    def _decrypt_webhook_secret(encrypted: Optional[str]) -> Optional[str]:
        if not encrypted:
            return None
        try:
            from services.credential_encryption import get_credential_encryption_service
            from services.webhook_signature import SECRET_ENVELOPE_KEY
            return get_credential_encryption_service().decrypt(encrypted).get(SECRET_ENVELOPE_KEY)
        except Exception as e:
            logger.error(f"Failed to decrypt webhook secret: {e}")
            return None

    def set_webhook_secret(self, schedule_id: str) -> Optional[str]:
        """Mint (or rotate) the HMAC signing secret and enable signature auth.

        Returns the PLAINTEXT secret exactly once (the caller surfaces it to the
        user and never persists it in the clear); only the AES-256-GCM envelope
        is stored. Returns None if the schedule row is gone. Requires an existing
        webhook token — signature auth on a schedule with no webhook is a no-op,
        so the router gates on `has_token` first.
        """
        secret = "whsec_" + secrets.token_urlsafe(32)
        encrypted = self._encrypt_webhook_secret(secret)
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(
                    and_(
                        agent_schedules.c.id == schedule_id,
                        agent_schedules.c.webhook_token.isnot(None),
                    )
                )
                .values(
                    webhook_secret_encrypted=encrypted,
                    webhook_auth_enabled=1,
                    updated_at=now,
                )
            )
            if result.rowcount == 0:
                return None
        return secret

    def clear_webhook_secret(self, schedule_id: str) -> bool:
        """Disable signature auth and drop the stored secret (webhook stays live)."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.id == schedule_id)
                .values(
                    webhook_secret_encrypted=None,
                    webhook_auth_enabled=0,
                    updated_at=now,
                )
            )
            return result.rowcount > 0

    def get_webhook_status(self, schedule_id: str) -> Optional[Dict]:
        """Return webhook configuration for a schedule (never the secret)."""
        stmt = select(
            agent_schedules.c.webhook_token,
            agent_schedules.c.webhook_enabled,
            agent_schedules.c.webhook_auth_enabled,
            agent_schedules.c.webhook_secret_encrypted,
        ).where(
            and_(
                agent_schedules.c.id == schedule_id,
                agent_schedules.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return {
            "webhook_token": row["webhook_token"],
            "webhook_enabled": bool(row["webhook_enabled"]),
            "has_token": row["webhook_token"] is not None,
            # ent#77 — surface the auth STATE only, never the secret material
            "auth_enabled": bool(row["webhook_auth_enabled"]),
            "has_secret": row["webhook_secret_encrypted"] is not None,
        }

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
        loop_id: str = None,
        subscription_id: str = None,
    ) -> Optional[ScheduleExecution]:
        """Create a new execution record for a manual/API-triggered task (no schedule).

        Args:
            agent_name: Target agent name
            message: Task message
            triggered_by: Trigger type - "manual", "mcp", "agent", "fan_out", "loop"
            source_user_id: User ID who triggered (for manual/mcp triggers)
            source_user_email: User email (denormalized for queries)
            source_agent_name: Calling agent name (for agent-to-agent)
            source_mcp_key_id: MCP API key ID (for mcp/agent triggers)
            source_mcp_key_name: MCP API key name (denormalized)
            model_used: Model used for this execution (MODEL-001)
            fan_out_id: Parent fan-out operation ID (FANOUT-001)
            loop_id: Parent loop ID (#740) — iterations of a sequential loop
            subscription_id: Subscription active at record time (SUB-004)
        """
        execution_id = self._generate_id()
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(schedule_executions).values(
                    id=execution_id,
                    schedule_id="__manual__",  # Special marker for manual/API-triggered tasks
                    agent_name=agent_name,
                    status=TaskExecutionStatus.RUNNING,
                    started_at=now,
                    message=message,
                    triggered_by=triggered_by,
                    source_user_id=source_user_id,
                    source_user_email=source_user_email,
                    source_agent_name=source_agent_name,
                    source_mcp_key_id=source_mcp_key_id,
                    source_mcp_key_name=source_mcp_key_name,
                    model_used=model_used,
                    fan_out_id=fan_out_id,
                    loop_id=loop_id,
                    subscription_id=subscription_id,
                )
            )

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
                loop_id=loop_id,
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

        with get_engine().begin() as conn:
            conn.execute(
                insert(schedule_executions).values(
                    id=execution_id,
                    schedule_id=schedule_id,
                    agent_name=agent_name,
                    status=TaskExecutionStatus.RUNNING,
                    started_at=now,
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
            )

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

    def mark_execution_dispatched(
        self, execution_id: str, async_dispatch: bool = False
    ) -> bool:
        """Mark an execution as dispatched to the agent.

        Sets claude_session_id to 'dispatched' so the no-session cleanup
        doesn't falsely mark long-running executions as failed.
        Only executions that never reach dispatch (e.g. backend crash before
        agent call) will have NULL claude_session_id and be caught by cleanup.

        #1083 fire-and-forget: when ``async_dispatch`` is True the sentinel is
        ``'dispatched_async'`` instead. This is the **durable async marker** the
        result-callback endpoint gates on (fail-closed): the callback may only
        finalize a RUNNING row carrying ``'dispatched_async'``, so it can never
        terminal-write a sync/interactive execution the backend is mid-await on
        (Codex #3 / decision 2). Both sentinels are non-NULL/non-empty, so the
        no-session sweep (``mark_no_session_executions_failed``) and the #106 /
        E-05 "running row has a session" canary treat them identically.

        Returns:
            True if execution was updated, False if not found.
        """
        sentinel = "dispatched_async" if async_dispatch else "dispatched"
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        schedule_executions.c.claude_session_id.is_(None),
                    )
                )
                .values(claude_session_id=sentinel)
            )
            return result.rowcount > 0

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
            True if the row was moved to QUEUED, False if it is missing or no
            longer RUNNING. The ``status == RUNNING`` precondition makes this a
            CAS-guarded projection write (#1082): a stale or duplicate re-queue
            against an already-terminal row is rejected, so a terminal row can
            never be resurrected into QUEUED (the E-02 phantom-reversal class).
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        # Only a currently-running row (the state set by
                        # create_task_execution before the slot acquire failed)
                        # may spill into the backlog — mirrors the sibling
                        # release_claim_to_queued guard (#1082).
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.QUEUED,
                    queued_at=queued_at,
                    backlog_metadata=backlog_metadata,
                    # reset started_at so drain records a clean run window
                    started_at=queued_at,
                )
            )
            return result.rowcount > 0

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
        oldest_queued = (
            select(schedule_executions.c.id)
            .where(
                and_(
                    schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    schedule_executions.c.agent_name == agent_name,
                )
            )
            .order_by(schedule_executions.c.queued_at.asc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            update(schedule_executions)
            .where(schedule_executions.c.id == oldest_queued)
            .values(
                status=TaskExecutionStatus.RUNNING,
                started_at=now,
                queued_at=None,
            )
            .returning(
                schedule_executions.c.id,
                schedule_executions.c.agent_name,
                schedule_executions.c.message,
                schedule_executions.c.backlog_metadata,
                schedule_executions.c.source_user_id,
                schedule_executions.c.source_user_email,
                schedule_executions.c.source_agent_name,
                schedule_executions.c.source_mcp_key_id,
                schedule_executions.c.source_mcp_key_name,
                schedule_executions.c.subscription_id,
            )
        )
        with get_engine().begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None

    def release_claim_to_queued(self, execution_id: str) -> bool:
        """Release a claimed row back to QUEUED state.

        Used when drain_next() acquired a slot, claimed a row, but then something
        downstream failed (e.g. slot released concurrently, spawn failed) and we
        need to put the row back in the backlog.

        Returns:
            True if the row transitioned back to queued, False otherwise.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.QUEUED,
                    queued_at=schedule_executions.c.started_at,
                )
            )
            return result.rowcount > 0

    def get_queued_count(self, agent_name: str) -> int:
        """Count queued backlog items for an agent."""
        stmt = select(func.count().label("c")).where(
            and_(
                schedule_executions.c.agent_name == agent_name,
                schedule_executions.c.status == TaskExecutionStatus.QUEUED,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return int(row["c"]) if row else 0

    def cancel_queued_execution(self, execution_id: str, reason: str = "cancelled") -> bool:
        """Cancel a single queued execution. No container interaction.

        Returns:
            True if the row was still queued and is now cancelled, False otherwise.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.CANCELLED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount > 0

    def cancel_queued_for_agent(self, agent_name: str, reason: str = "agent_deleted") -> int:
        """Bulk-cancel all queued executions for an agent.

        Used on agent deletion so orphan queued rows don't linger.

        Returns:
            Count of rows moved from QUEUED to CANCELLED.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.agent_name == agent_name,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.CANCELLED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount

    def fail_queued_for_agent(self, agent_name: str, reason: str = "circuit_open") -> int:
        """Bulk-FAIL all queued executions for an agent (#526, RELIABILITY-007).

        Called when the per-agent dispatch circuit breaker trips: the queued
        backlog is doomed (the agent is auth-dead), so fail it out immediately
        instead of letting each row drain into its own failure after the detect
        window.

        Mirrors ``expire_stale_queued`` (status → FAILED) — intentionally NOT
        ``cancel_queued_for_agent`` (which sets CANCELLED). The #526 acceptance
        criteria require these rows close FAILED so they read as failures, not
        as user cancellations.

        Returns:
            Count of rows moved from QUEUED to FAILED.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.agent_name == agent_name,
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=reason,
                )
            )
            return result.rowcount

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
        error_msg = f"Backlog expired: queued longer than {max_age_hours} hours"
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.status == TaskExecutionStatus.QUEUED,
                        schedule_executions.c.queued_at.isnot(None),
                        schedule_executions.c.queued_at < threshold,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    error=error_msg,
                )
            )
            return result.rowcount

    def list_agents_with_queued(self) -> List[str]:
        """Return the list of agent names that currently have queued backlog items.

        Used by the 60s maintenance task to drain orphans after a restart
        (backend crashed between enqueue and drain, or drain callback was lost).
        """
        stmt = (
            select(schedule_executions.c.agent_name)
            .where(schedule_executions.c.status == TaskExecutionStatus.QUEUED)
            .distinct()
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

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
        claude_session_id: str = None,
        compact_metadata: str = None,
        retry_count: Optional[int] = None,
    ) -> bool:
        """Update execution status when completed.

        CAS contract:
        - SUCCESS writes win over RUNNING / QUEUED / PENDING_RETRY / SKIPPED and
          over a phantom-stale FAILED (so a real completion lands even if a
          cleanup path misfired first — see #378). SUCCESS is **blocked** when
          the row is already CANCELLED: a user cancel is authoritative and the
          late-arriving agent reply must not be reported as a deliverable (#671).
        - Non-success terminal writes (FAILED, CANCELLED) are guarded against
          overwriting any already-terminal status (RELIABILITY-005), preventing
          cleanup paths from silently clobbering a real completion.

        Args:
            claude_session_id: Claude Code session ID for --resume support (EXEC-023)
            retry_count: #678 — number of in-line auto-retries used to produce
                this terminal write. None leaves the column unchanged (default
                0 from migration). 1 means the reader-race retry fired once.
        """
        # Terminal states that a non-success write must not overwrite.
        _TERMINAL = (
            TaskExecutionStatus.SUCCESS,
            TaskExecutionStatus.FAILED,
            TaskExecutionStatus.CANCELLED,
            TaskExecutionStatus.SKIPPED,
        )

        with get_engine().begin() as conn:
            row = conn.execute(
                select(schedule_executions.c.started_at).where(
                    schedule_executions.c.id == execution_id
                )
            ).mappings().first()
            if not row:
                return False

            started_at = parse_iso_timestamp(row["started_at"])
            completed_at = parse_iso_timestamp(utc_now_iso())
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            values = {
                "status": status,
                "completed_at": to_utc_iso(completed_at),
                "duration_ms": duration_ms,
                "response": response,
                "error": error,
                "context_used": context_used,
                "context_max": context_max,
                "cost": cost,
                "tool_calls": tool_calls,
                "execution_log": execution_log,
                "claude_session_id": claude_session_id,
                "compact_metadata": compact_metadata,
            }
            # #678: optionally update retry_count alongside the terminal write.
            # Leaving it out of the values dict preserves the prior value when
            # the caller passes None so other update paths (cleanup, scheduler)
            # don't accidentally zero it.
            if retry_count is not None:
                values["retry_count"] = int(retry_count)

            if status == TaskExecutionStatus.SUCCESS:
                # Agent's own completion result wins over everything except a
                # user-issued cancel (#671). A late "I'm done!" from Claude Code
                # after the operator pulled the plug must not flip the row to
                # success — that hides incomplete deliverables and silently
                # advances the schedule's next_run_at.
                where_clause = and_(
                    schedule_executions.c.id == execution_id,
                    schedule_executions.c.status != TaskExecutionStatus.CANCELLED,
                )
            else:
                # Non-success terminal write: block if already terminal so cleanup
                # paths cannot overwrite a real completion (RELIABILITY-005).
                where_clause = and_(
                    schedule_executions.c.id == execution_id,
                    schedule_executions.c.status.notin_(_TERMINAL),
                )

            result = conn.execute(
                update(schedule_executions).where(where_clause).values(**values)
            )
            return result.rowcount > 0

    def get_schedule_executions(self, schedule_id: str, limit: int = 50) -> List[ScheduleExecution]:
        """Get execution history for a schedule."""
        stmt = (
            select(schedule_executions)
            .where(schedule_executions.c.schedule_id == schedule_id)
            .order_by(schedule_executions.c.started_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [
                self._row_to_schedule_execution(row)
                for row in conn.execute(stmt).mappings()
            ]

    def get_latest_execution_per_schedule(self, schedule_ids: List[str]) -> Dict[str, Dict]:
        """Return the most-recent execution per schedule, in one query.

        #1265: replaces the per-schedule ``get_schedule_executions(id, limit=5)``
        fan-out on the /api/ops/schedules dashboard endpoint (one query per
        schedule -> N+1 that grows with total schedule count). Uses the shared
        ``latest_per_group`` window helper (partition by schedule_id, order by
        started_at DESC).

        Projects ONLY the columns the dashboard's ``last_execution`` block needs
        — never the large TEXT blobs (``response``, ``execution_log``,
        ``tool_calls``, ``message``) — so the single bulk query stays light even
        with hundreds of schedules. Returns ``{schedule_id: {id, status,
        started_at, completed_at, duration_ms, error}}``; ``started_at`` /
        ``completed_at`` are normalised to ISO strings (matching the prior
        ``.isoformat()`` API output). Schedules with no executions are absent.
        """
        cols = (
            schedule_executions.c.schedule_id,
            schedule_executions.c.id,
            schedule_executions.c.status,
            schedule_executions.c.started_at,
            schedule_executions.c.completed_at,
            schedule_executions.c.duration_ms,
            schedule_executions.c.error,
        )
        rows = latest_per_group(
            cols,
            schedule_executions.c.schedule_id,   # partition
            schedule_executions.c.started_at,    # order (DESC)
            schedule_executions.c.schedule_id,   # filter IN
            schedule_ids,
        )

        def _iso(v):
            ts = parse_iso_timestamp(v) if v else None
            return ts.isoformat() if ts else None

        return {
            row["schedule_id"]: {
                "id": row["id"],
                "status": row["status"],
                "started_at": _iso(row["started_at"]),
                "completed_at": _iso(row["completed_at"]),
                "duration_ms": row["duration_ms"],
                "error": row["error"],
            }
            for row in rows
        }

    def get_agent_executions(self, agent_name: str, limit: int = 50) -> List[ScheduleExecution]:
        """Get all executions for an agent across all schedules."""
        stmt = (
            select(schedule_executions)
            .where(schedule_executions.c.agent_name == agent_name)
            .order_by(schedule_executions.c.started_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [
                self._row_to_schedule_execution(row)
                for row in conn.execute(stmt).mappings()
            ]

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
        stmt = (
            select(
                schedule_executions.c.id,
                schedule_executions.c.schedule_id,
                schedule_executions.c.agent_name,
                schedule_executions.c.status,
                schedule_executions.c.started_at,
                schedule_executions.c.completed_at,
                schedule_executions.c.duration_ms,
                schedule_executions.c.message,
                schedule_executions.c.triggered_by,
                schedule_executions.c.context_used,
                schedule_executions.c.context_max,
                schedule_executions.c.cost,
                schedule_executions.c.source_user_id,
                schedule_executions.c.source_user_email,
                schedule_executions.c.source_agent_name,
                schedule_executions.c.source_mcp_key_id,
                schedule_executions.c.source_mcp_key_name,
                schedule_executions.c.claude_session_id,
                schedule_executions.c.model_used,
                schedule_executions.c.fan_out_id,
                schedule_executions.c.business_status,
                schedule_executions.c.validation_execution_id,
            )
            .where(schedule_executions.c.agent_name == agent_name)
            .order_by(schedule_executions.c.started_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
        """Get a specific execution by ID."""
        stmt = select(schedule_executions).where(
            schedule_executions.c.id == execution_id
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
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
        with get_engine().connect() as conn:
            row = conn.execute(
                text("""
                SELECT
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    AVG(duration_ms) as avg_duration_ms,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE agent_name = :agent_name
                AND started_at > :cutoff
                """),
                {"agent_name": agent_name, "cutoff": iso_cutoff(hours)},
            ).mappings().first()
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
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE started_at > :cutoff
                GROUP BY agent_name
                """),
                {"cutoff": iso_cutoff(hours)},
            ).mappings().all()

            results = []
            for row in rows:
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
        cutoff_24h = iso_cutoff(24)
        cutoff_7d = iso_cutoff(168)
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    SUM(CASE WHEN started_at > :c24 THEN 1 ELSE 0 END) as task_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'success' THEN 1 ELSE 0 END) as success_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'failed' THEN 1 ELSE 0 END) as failed_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'running' THEN 1 ELSE 0 END) as running_count_24h,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(cost, 0) ELSE 0 END) as total_cost_24h,
                    MAX(CASE WHEN started_at > :c24 THEN started_at ELSE NULL END) as last_execution_at_24h,
                    COUNT(*) as task_count_7d,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count_7d,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count_7d,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count_7d,
                    SUM(COALESCE(cost, 0)) as total_cost_7d,
                    MAX(started_at) as last_execution_at_7d
                FROM schedule_executions
                WHERE started_at > :c7d
                GROUP BY agent_name
                """),
                {"c24": cutoff_24h, "c7d": cutoff_7d},
            ).mappings().all()

            results = []
            for row in rows:
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

    def get_schedule_analytics(
        self,
        schedule_id: str,
        hours: int,
        agent_name: str,
    ) -> Optional[Dict]:
        """Compute per-schedule analytics over a rolling time window.

        Returns the analytics envelope, or `None` when the schedule
        does not exist, is soft-deleted, or belongs to a different
        agent than `agent_name`. The router maps `None` → 404, which
        is the load-bearing tenant-boundary check — `AuthorizedAgent`
        only validates the URL's agent name, not that the user-supplied
        `schedule_id` belongs to it.

        Counts and timeline buckets use the unsampled rowset;
        percentiles and tool-call distribution are computed over the
        newest `_PERCENTILE_ROWSET_CAP` success rows (`sampled=True`
        when the cap was hit). Bucketing is UTC — documented in the
        route's OpenAPI description.
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule or schedule.agent_name != agent_name:
            return None

        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP

        # Converted to SQLAlchemy Core (#300) so it runs on SQLite + PostgreSQL.
        # The SQL is dialect-portable (substr/COALESCE/GROUP BY-alias/LIMIT all
        # valid on both); only the positional binds became named. `.mappings()`
        # preserves the `row["col"]` access the aggregation below relies on.
        with get_engine().connect() as conn:
            agg_rows = conn.execute(
                text(
                    """
                    SELECT
                        substr(started_at, 1, 10) AS day,
                        status,
                        COUNT(*) AS n,
                        SUM(COALESCE(cost, 0)) AS cost_sum
                    FROM schedule_executions
                    WHERE schedule_id = :sid AND started_at > :cutoff
                    GROUP BY day, status
                    """
                ),
                {"sid": schedule_id, "cutoff": cutoff},
            ).mappings().all()

            # `cap + 1` so we can detect sampling without a separate COUNT.
            detail_rows = conn.execute(
                text(
                    """
                    SELECT duration_ms, tool_calls
                    FROM schedule_executions
                    WHERE schedule_id = :sid
                      AND started_at > :cutoff
                      AND status = 'success'
                      AND duration_ms IS NOT NULL
                    ORDER BY started_at DESC
                    LIMIT :lim
                    """
                ),
                {"sid": schedule_id, "cutoff": cutoff, "lim": cap + 1},
            ).mappings().all()

        counts: Dict[str, int] = defaultdict(int)
        cost_by_status: Dict[str, float] = defaultdict(float)
        for row in agg_rows:
            counts[row["status"]] += int(row["n"] or 0)
            cost_by_status[row["status"]] += float(row["cost_sum"] or 0.0)

        total_executions = sum(counts.values())
        success_count = counts.get(TaskExecutionStatus.SUCCESS, 0)
        failed_count = counts.get(TaskExecutionStatus.FAILED, 0)
        cancelled_count = counts.get(TaskExecutionStatus.CANCELLED, 0)
        success_rate = (
            round(success_count / total_executions, 4) if total_executions else 0.0
        )
        cost_total = round(sum(cost_by_status.values()), 4)

        sampled = len(detail_rows) > cap
        sample_size = cap if sampled else len(detail_rows)
        capped_rows = detail_rows[:cap]
        durations = [int(r["duration_ms"]) for r in capped_rows]

        if len(durations) >= 2:
            # `inclusive` matches "x% of observations were ≤ this value"
            # for small N; the default `exclusive` shifts p99 noticeably.
            cuts = statistics.quantiles(durations, n=100, method="inclusive")
            p50, p95, p99 = int(cuts[49]), int(cuts[94]), int(cuts[98])
        elif len(durations) == 1:
            only = int(durations[0])
            p50 = p95 = p99 = only
        else:
            p50 = p95 = p99 = None

        # Top-5 weighted by total wall time (NOT raw count) — avoids
        # `Read`/`Bash` dominating because they're frequent but cheap.
        tool_duration: Dict[str, int] = defaultdict(int)
        tool_call_total = 0
        for row in capped_rows:
            raw = row["tool_calls"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "get_schedule_analytics: malformed tool_calls JSON "
                    "skipped (schedule_id=%s): %s",
                    schedule_id, exc,
                )
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or entry.get("tool")
                if not name:
                    continue
                tool_call_total += 1
                dur = entry.get("duration_ms")
                if isinstance(dur, (int, float)) and dur > 0:
                    tool_duration[name] += int(dur)
                # Entries without a usable duration count toward
                # total_calls but stay out of the top-5 ranking.

        tool_top5 = [
            {"name": name, "total_duration_ms": dur_total}
            for name, dur_total in sorted(
                tool_duration.items(), key=lambda kv: kv[1], reverse=True
            )[:5]
        ]

        timeline_by_day: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"success": 0, "failed": 0, "cost": 0.0}
        )
        for row in agg_rows:
            day = row["day"]
            if not day:
                continue
            n = int(row["n"] or 0)
            cost_sum = float(row["cost_sum"] or 0.0)
            timeline_by_day[day]["cost"] += cost_sum
            if row["status"] == TaskExecutionStatus.SUCCESS:
                timeline_by_day[day]["success"] += n
            elif row["status"] == TaskExecutionStatus.FAILED:
                timeline_by_day[day]["failed"] += n

        # Gap-fill so the chart x-axis stays continuous for zero-days.
        now_utc = datetime.now(timezone.utc).date()
        start_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
        timeline: List[Dict] = []
        day = start_utc
        while day <= now_utc:
            iso = day.isoformat()
            bucket = timeline_by_day.get(iso, {"success": 0, "failed": 0, "cost": 0.0})
            timeline.append({
                "date": iso,
                "success": int(bucket["success"]),
                "failed": int(bucket["failed"]),
                "cost": round(float(bucket["cost"]), 4),
            })
            day = day + timedelta(days=1)

        return {
            "window_hours": hours,
            "total_executions": total_executions,
            "success_count": success_count,
            "failed_count": failed_count,
            "cancelled_count": cancelled_count,
            "success_rate": success_rate,
            "duration_ms": {"p50": p50, "p95": p95, "p99": p99},
            "cost": {"total": cost_total},
            "tool_calls": {"top": tool_top5, "total_calls": tool_call_total},
            "timeline": timeline,
            "sampled": sampled,
            "sample_size": sample_size,
        }

    def get_agent_schedules_summary(self, agent_name: str, hours: int) -> Dict:
        """Per-schedule performance rollups for an agent over a window (#1115).

        ONE row per non-deleted schedule (zero-run schedules included, with
        zeros), so both the Overview "Schedules performance" section and the
        Schedules-tab inline stats render from a single call — no N per-schedule
        round-trips. The #868 deep view stays the drill-in target.

        Per schedule: terminal **success_rate** (success / (success + failed
        [incl. ``error``]); ``None`` when zero terminal runs so the UI shows
        ``—`` not a false 0%), **avg_duration_ms** (NULL-skipping AVG),
        **cost_total**, **context_avg** (NULL-skipping), **tool_call_total**,
        and last-run outcome. Read-only / DB-sourced (renders when stopped).

        Tool-call totals are parsed from the newest ``_PERCENTILE_ROWSET_CAP``
        rows agent-wide (matches the #868 sampling discipline); ``tool_calls_
        sampled`` flags when that cap was hit. Window uses ``iso_cutoff``
        (Invariant #16).
        """
        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP
        FAILED_STATES = (TaskExecutionStatus.FAILED, "error")

        # #300/#1115: ported to SQLAlchemy Core so the summary works on both
        # SQLite and PostgreSQL (the earlier raw-sqlite path NameError'd on the
        # dropped get_db_connection import, and the bare-column-with-MAX last-run
        # trick is SQLite-only — replaced by a portable ROW_NUMBER window below).
        se = schedule_executions.c
        sch = agent_schedules.c
        base_where = and_(se.agent_name == agent_name, se.started_at > cutoff)

        # Schedules (non-deleted) — the authoritative row set so a
        # zero-run schedule still appears.
        q_sched = (
            select(sch.id, sch.name, sch.message, sch.cron_expression, sch.enabled)
            .where(and_(sch.agent_name == agent_name, sch.deleted_at.is_(None)))
            .order_by(sch.created_at.asc())
        )

        # One grouped aggregate for every schedule's executions in window.
        # AVG skips NULLs natively, matching the prior CASE-wrapped AVG.
        q_agg = (
            select(
                se.schedule_id,
                func.count().label("total"),
                func.sum(case((se.status == "success", 1), else_=0)).label("success_count"),
                func.sum(case((se.status.in_(("failed", "error")), 1), else_=0)).label("failed_count"),
                func.sum(case((se.status == "cancelled", 1), else_=0)).label("cancelled_count"),
                func.sum(func.coalesce(se.cost, 0)).label("cost_total"),
                func.avg(se.duration_ms).label("avg_duration_ms"),
                func.avg(se.context_used).label("context_avg"),
            )
            .where(base_where)
            .group_by(se.schedule_id)
        )

        # Last-run outcome per schedule — portable ROW_NUMBER window (replaces
        # SQLite's bare-column-with-MAX, which errors on PostgreSQL) keeping the
        # in-window semantics of the original query.
        _last_rn = func.row_number().over(
            partition_by=se.schedule_id, order_by=se.started_at.desc()
        ).label("rn")
        _last_subq = (
            select(
                se.schedule_id,
                se.started_at.label("last_run_at"),
                se.status.label("last_status"),
                _last_rn,
            )
            .where(base_where)
            .subquery()
        )
        q_last = select(
            _last_subq.c.schedule_id,
            _last_subq.c.last_run_at,
            _last_subq.c.last_status,
        ).where(_last_subq.c.rn == 1)

        # Tool-call totals — bounded JSON parse over newest rows agent-wide
        # (cap + 1 to detect sampling), attributed back per schedule.
        q_tools = (
            select(se.schedule_id, se.tool_calls)
            .where(and_(base_where, se.tool_calls.isnot(None)))
            .order_by(se.started_at.desc())
            .limit(cap + 1)
        )

        with get_engine().connect() as conn:
            schedule_rows = conn.execute(q_sched).mappings().all()
            agg_by_sched = {r["schedule_id"]: r for r in conn.execute(q_agg).mappings().all()}
            last_by_sched = {r["schedule_id"]: r for r in conn.execute(q_last).mappings().all()}
            tool_rows = conn.execute(q_tools).mappings().all()

        tool_calls_sampled = len(tool_rows) > cap
        tool_total_by_sched: Dict[str, int] = defaultdict(int)
        for row in tool_rows[:cap]:
            raw = row["tool_calls"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if isinstance(entry, dict) and (entry.get("name") or entry.get("tool")):
                    tool_total_by_sched[row["schedule_id"]] += 1

        schedules: List[Dict] = []
        for s in schedule_rows:
            sid = s["id"]
            agg = agg_by_sched.get(sid)
            last = last_by_sched.get(sid)

            if agg:
                total = int(agg["total"] or 0)
                success_count = int(agg["success_count"] or 0)
                failed_count = int(agg["failed_count"] or 0)
                cancelled_count = int(agg["cancelled_count"] or 0)
                cost_total = round(float(agg["cost_total"] or 0.0), 4)
                avg_duration_ms = (
                    int(agg["avg_duration_ms"]) if agg["avg_duration_ms"] is not None else None
                )
                context_avg = (
                    int(agg["context_avg"]) if agg["context_avg"] is not None else None
                )
            else:
                total = success_count = failed_count = cancelled_count = 0
                cost_total = 0.0
                avg_duration_ms = context_avg = None

            terminal = success_count + failed_count
            success_rate = round(success_count / terminal, 4) if terminal else None

            schedules.append({
                "schedule_id": sid,
                "name": s["name"],
                "command": _schedule_command_label(s["message"]),
                "cron_expression": s["cron_expression"],
                "enabled": bool(s["enabled"]),
                "total_executions": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "cancelled_count": cancelled_count,
                "success_rate": success_rate,
                "avg_duration_ms": avg_duration_ms,
                "cost_total": cost_total,
                "context_avg": context_avg,
                "tool_call_total": tool_total_by_sched.get(sid, 0),
                "last_run_at": last["last_run_at"] if last else None,
                "last_run_status": last["last_status"] if last else None,
            })

        return {
            "window_hours": hours,
            "schedule_count": len(schedules),
            "tool_calls_sampled": tool_calls_sampled,
            "schedules": schedules,
        }

    def get_agent_analytics(self, agent_name: str, hours: int) -> Dict:
        """Compute agent-scoped execution analytics over a rolling window (#1107).

        Generalises `get_schedule_analytics` to agent scope with a
        `triggered_by` breakdown grouped into user-facing buckets. Powers
        the Agent Detail "Overview" trend charts.

        Data-source discipline (locked by /autoplan engineering review):
          - Counts, per-day type stacks, per-day success-rate, per-day
            duration AVG, and per-day context AVG come from full-set
            aggregate queries.
          - Headline duration `avg` and `context_avg` also come from the
            full set — NEVER from the capped pool, since an average over a
            sampled subset would be silently wrong on high-traffic agents.
          - Only the headline duration `p95` uses the newest
            `_PERCENTILE_ROWSET_CAP` success rows (`sampled=True` when
            capped).
          - `success_rate` is terminal-based: success / (success + failed),
            where failed = status in ('failed', 'error'). Days with zero
            terminal rows report `success_rate=None` so the chart shows a
            gap, not a false 0%.
          - Bucketing is UTC-day; unmapped triggers → "Other".

        Always returns an envelope (zeros / empty when the agent has no
        executions). Access is gated by `AuthorizedAgent` at the router and
        the window is validated there, so there is no None/404 path here.
        """
        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP
        FAILED_STATES = (TaskExecutionStatus.FAILED, "error")

        # #300 rebase: ported to SQLAlchemy Core (this #852 analytics method
        # landed on dev after the Step-C bulk conversion). substr/AVG/CASE/
        # COUNT are dialect-agnostic; the p95 is computed in Python below.
        day_col = func.substr(schedule_executions.c.started_at, 1, 10).label("day")
        base_where = and_(
            schedule_executions.c.agent_name == agent_name,
            schedule_executions.c.started_at > cutoff,
        )
        dur_avg_expr = func.avg(
            case((schedule_executions.c.status == "success", schedule_executions.c.duration_ms))
        ).label("dur_avg")
        ctx_avg_expr = func.avg(schedule_executions.c.context_used).label("ctx_avg")

        # Q1: counts + per-day type stacks (full set).
        q1 = (
            select(
                day_col,
                schedule_executions.c.status,
                schedule_executions.c.triggered_by,
                func.count().label("n"),
            )
            .where(base_where)
            .group_by(day_col, schedule_executions.c.status, schedule_executions.c.triggered_by)
        )

        # Q2: per-day duration AVG (success only) + context AVG
        # (NULL-skipped, all statuses). CASE→NULL means AVG skips
        # non-success durations; AVG(context_used) skips unmeasured rows.
        q2 = select(day_col, dur_avg_expr, ctx_avg_expr).where(base_where).group_by(day_col)

        # Q3: overall duration AVG + context AVG (full set, single row).
        q3 = select(dur_avg_expr, ctx_avg_expr).where(base_where)

        # Q4: capped success-duration pool for the headline p95 only.
        # `cap + 1` so we can detect sampling without a second COUNT.
        q4 = (
            select(schedule_executions.c.duration_ms)
            .where(
                and_(
                    schedule_executions.c.agent_name == agent_name,
                    schedule_executions.c.started_at > cutoff,
                    schedule_executions.c.status == "success",
                    schedule_executions.c.duration_ms.isnot(None),
                )
            )
            .order_by(schedule_executions.c.started_at.desc())
            .limit(cap + 1)
        )

        with get_engine().connect() as conn:
            count_rows = conn.execute(q1).mappings().all()
            daily_metric_rows = conn.execute(q2).mappings().all()
            overall = conn.execute(q3).mappings().first()
            dur_rows = conn.execute(q4).mappings().all()

        # --- counts, per-day stacks, per-bucket window totals ---
        success_count = 0
        failed_count = 0
        total_executions = 0
        bucket_totals: Dict[str, int] = defaultdict(int)
        day_counts: Dict[str, Dict] = defaultdict(
            lambda: {
                "total": 0, "success": 0, "failed": 0,
                "by_type": defaultdict(int),
            }
        )
        for row in count_rows:
            day = row["day"]
            row_status = row["status"]
            n = int(row["n"] or 0)
            bucket = _bucket_for_trigger(row["triggered_by"])
            total_executions += n
            bucket_totals[bucket] += n
            if day:
                d = day_counts[day]
                d["total"] += n
                d["by_type"][bucket] += n
                if row_status == TaskExecutionStatus.SUCCESS:
                    d["success"] += n
                elif row_status in FAILED_STATES:
                    d["failed"] += n
            if row_status == TaskExecutionStatus.SUCCESS:
                success_count += n
            elif row_status in FAILED_STATES:
                failed_count += n

        terminal_total = success_count + failed_count
        success_rate = (
            round(success_count / terminal_total, 4) if terminal_total else 0.0
        )

        # --- headline p95 (capped pool) ---
        sampled = len(dur_rows) > cap
        sample_size = cap if sampled else len(dur_rows)
        durations = [int(r["duration_ms"]) for r in dur_rows[:cap]]
        if len(durations) >= 2:
            cuts = statistics.quantiles(durations, n=100, method="inclusive")
            p95 = int(cuts[94])
        elif len(durations) == 1:
            p95 = int(durations[0])
        else:
            p95 = None

        # --- headline avg duration + context (full set, never sampled) ---
        dur_avg = (
            int(overall["dur_avg"])
            if overall and overall["dur_avg"] is not None else None
        )
        ctx_avg = (
            int(overall["ctx_avg"])
            if overall and overall["ctx_avg"] is not None else None
        )

        # --- per-day duration / context AVG lookup ---
        daily_metrics: Dict[str, Dict] = {}
        for row in daily_metric_rows:
            daily_metrics[row["day"]] = {
                "duration_avg_ms": (
                    int(row["dur_avg"]) if row["dur_avg"] is not None else None
                ),
                "context_avg": (
                    int(row["ctx_avg"]) if row["ctx_avg"] is not None else None
                ),
            }

        # --- gap-filled timeline (continuous UTC-day x-axis) ---
        now_utc = datetime.now(timezone.utc).date()
        start_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
        timeline: List[Dict] = []
        day = start_utc
        while day <= now_utc:
            iso = day.isoformat()
            c = day_counts.get(iso)
            m = daily_metrics.get(iso, {})
            if c:
                day_terminal = c["success"] + c["failed"]
                day_sr = (
                    round(c["success"] / day_terminal, 4)
                    if day_terminal else None
                )
                by_type = {
                    b: c["by_type"][b]
                    for b in _BUCKET_ORDER if c["by_type"].get(b)
                }
                timeline.append({
                    "date": iso,
                    "total": c["total"],
                    "success": c["success"],
                    "failed": c["failed"],
                    "success_rate": day_sr,
                    "duration_avg_ms": m.get("duration_avg_ms"),
                    "context_avg": m.get("context_avg"),
                    "by_type": by_type,
                })
            else:
                timeline.append({
                    "date": iso, "total": 0, "success": 0, "failed": 0,
                    "success_rate": None, "duration_avg_ms": None,
                    "context_avg": None, "by_type": {},
                })
            day = day + timedelta(days=1)

        by_type_totals = [
            {"bucket": b, "total": bucket_totals[b]}
            for b in _BUCKET_ORDER if bucket_totals.get(b)
        ]
        buckets_present = [b for b in _BUCKET_ORDER if bucket_totals.get(b)]

        return {
            "window_hours": hours,
            "total_executions": total_executions,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": success_rate,
            "duration_ms": {"avg": dur_avg, "p95": p95},
            "context_avg": ctx_avg,
            "by_type": by_type_totals,
            "buckets": buckets_present,
            "timeline": timeline,
            "sampled": sampled,
            "sample_size": sample_size,
        }

    def get_all_agents_schedule_counts(self) -> Dict[str, Dict[str, int]]:
        """Get schedule counts (total and enabled) for all agents.

        Returns:
            Dict mapping agent_name to {"total": X, "enabled": Y}
        """
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    COUNT(*) as total,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) as enabled
                FROM agent_schedules
                WHERE deleted_at IS NULL
                GROUP BY agent_name
                """)
            ).mappings().all()

            results = {}
            for row in rows:
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

        try:
            with get_engine().begin() as conn:
                conn.execute(
                    insert(agent_git_config).values(
                        id=config_id,
                        agent_name=agent_name,
                        github_repo=github_repo,
                        working_branch=working_branch,
                        instance_id=instance_id,
                        source_branch=source_branch,
                        source_mode=1 if source_mode else 0,
                        created_at=now,
                        sync_enabled=1,
                        sync_paths=sync_paths_json,
                    )
                )

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
        except IntegrityError:
            # Already exists
            return None

    def get_git_config(self, agent_name: str) -> Optional[AgentGitConfig]:
        """Get git configuration for an agent."""
        stmt = select(agent_git_config).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_git_config(row) if row else None

    def get_git_config_agent_names_for_repo(self, github_repo: str) -> List[str]:
        """Agent names whose git config binds ``github_repo`` (any branch/mode).

        Fork-to-own creation guard (trinity-enterprise#93): source-mode rows
        bypass the partial UNIQUE(github_repo, working_branch) index, so two
        auto-pushing agents could otherwise bind the same destination repo.
        NB: agent delete is a SOFT delete — the git-config row is cascaded only
        at purge (retention default 180d), so a hit may name a soft-deleted
        agent. That block is intentional: admin recovery (#834) would resurrect
        the binding. Comparison is case-insensitive — GitHub repo slugs are.
        """
        stmt = select(agent_git_config.c.agent_name).where(
            func.lower(agent_git_config.c.github_repo) == github_repo.lower()
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).scalars().all()
        return list(rows)

    def update_git_sync(self, agent_name: str, commit_sha: str) -> bool:
        """Update git sync timestamp and commit SHA after successful sync."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(last_sync_at=now, last_commit_sha=commit_sha)
            )
            return result.rowcount > 0

    def set_git_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable or disable git sync for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(sync_enabled=1 if enabled else 0)
            )
            return result.rowcount > 0

    def set_git_auto_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle the 15-min auto-sync heartbeat for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(auto_sync_enabled=1 if enabled else 0)
            )
            return result.rowcount > 0

    def set_freeze_schedules_if_sync_failing(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle scheduler freeze-on-failure opt-in."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(freeze_schedules_if_sync_failing=1 if enabled else 0)
            )
            return result.rowcount > 0

    def get_git_auto_sync_enabled(self, agent_name: str) -> bool:
        """#389: read the auto-sync flag. False if config missing."""
        stmt = select(agent_git_config.c.auto_sync_enabled).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return bool(row[0]) if row else False

    def get_all_git_auto_sync_enabled(
        self, agent_names: Optional[set] = None
    ) -> Dict[str, bool]:
        """#73: bulk read of the auto-sync flag in one query, optionally scoped
        to `agent_names` (mirrors get_all_permission_edges). Removes the N+1 in
        the /sync-health dashboard endpoint. Agents without a config row are
        absent from the result (caller defaults to False).

        `None` = whole fleet; an empty set = empty result (an empty scope must
        NOT fall through to the whole-fleet query, and `IN ()` is invalid SQL).

        Scoped lookups chunk the `IN (...)` list at `_SQLITE_MAX_IN_VARS` so a
        large accessible-agent set can't exceed SQLite's host-parameter cap.
        """
        if agent_names is not None and not agent_names:
            return {}
        with get_engine().connect() as conn:
            if agent_names is None:
                rows = conn.execute(
                    select(
                        agent_git_config.c.agent_name,
                        agent_git_config.c.auto_sync_enabled,
                    )
                ).all()
                return {row[0]: bool(row[1]) for row in rows}

            result: Dict[str, bool] = {}
            names = list(agent_names)
            for start in range(0, len(names), _SQLITE_MAX_IN_VARS):
                chunk = names[start:start + _SQLITE_MAX_IN_VARS]
                rows = conn.execute(
                    select(
                        agent_git_config.c.agent_name,
                        agent_git_config.c.auto_sync_enabled,
                    ).where(agent_git_config.c.agent_name.in_(chunk))
                ).all()
                result.update({row[0]: bool(row[1]) for row in rows})
            return result

    def get_freeze_schedules_if_sync_failing(self, agent_name: str) -> bool:
        """#389: read the freeze-schedules flag. False if config missing."""
        stmt = select(agent_git_config.c.freeze_schedules_if_sync_failing).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return bool(row[0]) if row else False

    def find_duplicate_bindings(self) -> set:
        """#390 S6: return the set of agent_names whose (github_repo, working_branch)
        pair is shared with another row where source_mode = 0.

        Source-mode agents intentionally share branches (e.g. all legacy-mode
        siblings track `main`) and are excluded by the partial filter, mirroring
        the spec's §P5 query verbatim.
        """
        # Row-value tuple IN-subquery — kept as text(); valid on both SQLite
        # and PostgreSQL. No bind params, no sqlite-only constructs.
        query = text("""
            SELECT agent_name FROM agent_git_config
            WHERE source_mode = 0
              AND (github_repo, working_branch) IN (
                  SELECT github_repo, working_branch
                  FROM agent_git_config
                  WHERE source_mode = 0
                  GROUP BY github_repo, working_branch
                  HAVING COUNT(*) > 1
              )
        """)
        with get_engine().connect() as conn:
            rows = conn.execute(query).all()
            return {row[0] for row in rows}

    def delete_git_config(self, agent_name: str) -> bool:
        """Delete git configuration for an agent (when agent is deleted)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_git_config).where(
                    agent_git_config.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0

    def get_running_executions(self) -> list:
        """Get all schedule executions currently in 'running' status.

        Used by startup recovery to detect orphaned executions after a crash.

        Returns:
            List of dicts with id, agent_name, started_at, schedule_id.
        """
        stmt = select(
            schedule_executions.c.id,
            schedule_executions.c.agent_name,
            schedule_executions.c.started_at,
            schedule_executions.c.schedule_id,
        ).where(schedule_executions.c.status == TaskExecutionStatus.RUNNING)
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def mark_stale_executions_failed(
        self,
        timeout_minutes: int = 30,
        agent_timeouts: Optional[Dict[str, int]] = None,
        buffer_seconds: int = 0,
    ) -> int:
        """Mark running executions older than their stale window as failed.

        Uses TaskExecutionStatus.RUNNING / .FAILED for status values.

        #1083 Finding 1: when ``agent_timeouts`` is provided the stale window is
        **per agent** — ``agent_timeout + buffer_seconds`` — instead of a single
        flat ``timeout_minutes``. The old flat 120-min window equalled the MAX
        agent timeout with NO ``SLOT_TTL_BUFFER``, so this no-CAS/no-registry
        sweep failed a legitimately-running max-timeout async turn ~5 min before
        the slot reaper + canary E-01 (which use ``timeout + 300``). Matching the
        reaper's window closes that early-fail. When ``agent_timeouts`` is None
        the prior flat behaviour is reproduced exactly.

        Args:
            timeout_minutes: Flat fallback window (also the default for any agent
                absent from ``agent_timeouts``).
            agent_timeouts: ``{agent_name: execution_timeout_seconds}`` (e.g.
                ``db.get_all_execution_timeouts()``). None → flat behaviour.
            buffer_seconds: Added to each per-agent timeout (pass ``SLOT_TTL_BUFFER``
                to match the slot reaper / E-01 window).

        Returns:
            Number of executions marked as failed.
        """
        now = utc_now_iso()
        completed_at = parse_iso_timestamp(now)
        default_threshold_s = timeout_minutes * 60

        # SQL pre-filter uses the SMALLEST per-agent window so no stale row is
        # missed; Python then applies each row's precise per-agent window. ISO
        # 8601 format matches stored started_at (SQLite's datetime() differs).
        if agent_timeouts:
            min_threshold_s = min(
                [default_threshold_s] + [t + buffer_seconds for t in agent_timeouts.values()]
            )
        else:
            min_threshold_s = default_threshold_s
        prefilter = (
            datetime.now(timezone.utc) - timedelta(seconds=min_threshold_s)
        ).strftime('%Y-%m-%dT%H:%M:%S')

        with get_engine().begin() as conn:
            candidate_rows = conn.execute(
                select(
                    schedule_executions.c.id,
                    schedule_executions.c.started_at,
                    schedule_executions.c.agent_name,
                ).where(
                    and_(
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        schedule_executions.c.started_at < prefilter,
                    )
                )
            ).mappings().all()

            if not candidate_rows:
                return 0

            failed = 0
            for row in candidate_rows:
                started_at = parse_iso_timestamp(row["started_at"])
                age_s = (completed_at - started_at).total_seconds()
                if agent_timeouts is not None:
                    at = agent_timeouts.get(row["agent_name"])
                    effective_s = (at + buffer_seconds) if at is not None else default_threshold_s
                else:
                    effective_s = default_threshold_s
                # Decay-safe boundary: a row exactly AT its window survives; only
                # strictly past it is swept (mirrors the canary E-01 tolerance).
                if age_s <= effective_s:
                    continue
                duration_ms = int(age_s * 1000)
                error_msg = (
                    f"Marked as failed by cleanup: exceeded {int(effective_s)}s "
                    f"stale timeout"
                )
                # RELIABILITY-005: guard the UPDATE so a SUCCESS that arrived
                # between the SELECT and this UPDATE is never overwritten.
                result = conn.execute(
                    update(schedule_executions)
                    .where(
                        and_(
                            schedule_executions.c.id == row["id"],
                            schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        )
                    )
                    .values(
                        status=TaskExecutionStatus.FAILED,
                        completed_at=now,
                        duration_ms=duration_ms,
                        error=error_msg,
                    )
                )
                if result.rowcount:
                    failed += 1

            return failed

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
        error_msg = f"Silent launch failure: no Claude session created within {timeout_seconds} seconds"
        with get_engine().begin() as conn:
            no_session_rows = conn.execute(
                select(
                    schedule_executions.c.id,
                    schedule_executions.c.started_at,
                ).where(
                    and_(
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        or_(
                            schedule_executions.c.claude_session_id.is_(None),
                            schedule_executions.c.claude_session_id == "",
                        ),
                        schedule_executions.c.started_at < threshold,
                    )
                )
            ).mappings().all()

            if not no_session_rows:
                return 0

            completed_at = parse_iso_timestamp(now)
            for row in no_session_rows:
                started_at = parse_iso_timestamp(row["started_at"])
                duration_ms = int((completed_at - started_at).total_seconds() * 1000)
                # RELIABILITY-005: guard the UPDATE so a SUCCESS that arrived
                # between the SELECT and this UPDATE is never overwritten.
                conn.execute(
                    update(schedule_executions)
                    .where(
                        and_(
                            schedule_executions.c.id == row["id"],
                            schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        )
                    )
                    .values(
                        status=TaskExecutionStatus.FAILED,
                        completed_at=now,
                        duration_ms=duration_ms,
                        error=error_msg,
                    )
                )

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
        with get_engine().begin() as conn:
            row = conn.execute(
                select(schedule_executions.c.started_at).where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
            ).mappings().first()
            if not row:
                return False

            completed_at = parse_iso_timestamp(now)
            started_at = parse_iso_timestamp(row["started_at"])
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    duration_ms=duration_ms,
                    error=error,
                )
            )
            return result.rowcount > 0

    def finalize_orphaned_skipped_executions(self) -> int:
        """Finalize skipped executions that are missing completed_at.

        Defensive cleanup for any skipped execution records that were not
        properly terminated at creation time.

        Returns:
            Number of executions finalized.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.status == TaskExecutionStatus.SKIPPED,
                        schedule_executions.c.completed_at.is_(None),
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=func.coalesce(schedule_executions.c.started_at, now),
                    duration_ms=0,
                    error="Finalized by cleanup: skipped execution",
                )
            )
            return result.rowcount

    def prune_execution_logs(self, retention_days: int, chunk_size: int = 500) -> int:
        """Null `execution_log` on terminal executions older than retention_days.

        Issue #772: the JSONL transcript dominates schedule_executions row size
        (~150–190 KB/row). Nulling preserves the row + metadata (cost, duration,
        agent, status) while reclaiming the bulk of the space. Runs in chunks
        to keep the write lock short — each chunk commits before the next, so
        a 3 GB backfill won't block a live system.

        Args:
            retention_days: Null logs older than this. 0 disables the sweep.
            chunk_size: Max rows nulled per commit cycle (default 500).

        Returns:
            Total rows nulled across all chunks.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0

        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        _terminal = ("success", "failed", "cancelled", "skipped")
        engine = get_engine()
        while True:
            # SELECT-then-UPDATE-by-id keeps the WHERE predicate aligned
            # with idx_executions_completed_terminal (#772) and avoids
            # depending on SQLITE_ENABLE_UPDATE_DELETE_LIMIT. Each chunk is
            # its own transaction so the write lock stays short.
            with engine.begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(schedule_executions.c.id)
                        .where(
                            and_(
                                schedule_executions.c.status.in_(_terminal),
                                schedule_executions.c.completed_at.isnot(None),
                                schedule_executions.c.completed_at < cutoff,
                                schedule_executions.c.execution_log.isnot(None),
                            )
                        )
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    update(schedule_executions)
                    .where(schedule_executions.c.id.in_(ids))
                    .values(execution_log=None)
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total

    def prune_execution_rows(self, retention_days: int, chunk_size: int = 500) -> int:
        """Delete terminal schedule_executions rows older than retention_days.

        Issue #772: deeper retention beyond `prune_execution_logs` — fully
        removes ancient rows. Chunked DELETE keeps the write lock short.

        Args:
            retention_days: Delete rows older than this. 0 disables the sweep.
            chunk_size: Max rows deleted per commit cycle (default 500).

        Returns:
            Total rows deleted across all chunks.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0

        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        _terminal = ("success", "failed", "cancelled", "skipped")
        engine = get_engine()
        while True:
            with engine.begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(schedule_executions.c.id)
                        .where(
                            and_(
                                schedule_executions.c.status.in_(_terminal),
                                schedule_executions.c.completed_at.isnot(None),
                                schedule_executions.c.completed_at < cutoff,
                            )
                        )
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(schedule_executions).where(
                        schedule_executions.c.id.in_(ids)
                    )
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total

    def get_running_executions_with_agent_info(self) -> List[Dict]:
        """Get all running executions with effective timeout for watchdog.

        Returns executions joined with schedule and agent ownership data.
        Timeout resolution order:
        1. Schedule's timeout_seconds (for scheduled executions)
        2. Agent's execution_timeout_seconds (for manual/MCP executions)
        3. Fallback default of 3600s (#665)

        Returns:
            List of dicts with id, schedule_id, agent_name, started_at,
            and timeout_seconds.
        """
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT e.id, e.schedule_id, e.agent_name, e.started_at,
                       COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 3600) as timeout_seconds
                FROM schedule_executions e
                LEFT JOIN agent_schedules s ON e.schedule_id = s.id
                LEFT JOIN agent_ownership ao ON e.agent_name = ao.agent_name
                WHERE e.status = :status
                """),
                {"status": TaskExecutionStatus.RUNNING},
            ).mappings().all()
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
        with get_engine().begin() as conn:
            # Get started_at for duration calculation
            row = conn.execute(
                select(schedule_executions.c.started_at).where(
                    schedule_executions.c.id == execution_id
                )
            ).mappings().first()
            if not row:
                return False

            completed_at = parse_iso_timestamp(now)
            started_at = parse_iso_timestamp(row["started_at"])
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            result = conn.execute(
                update(schedule_executions)
                .where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                    )
                )
                .values(
                    status=TaskExecutionStatus.FAILED,
                    completed_at=now,
                    duration_ms=duration_ms,
                    error=error_message,
                )
            )
            return result.rowcount > 0

    def list_git_enabled_agents(self) -> List[AgentGitConfig]:
        """List all agents with git sync enabled."""
        stmt = (
            select(agent_git_config)
            .where(agent_git_config.c.sync_enabled == 1)
            .order_by(agent_git_config.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [
                self._row_to_git_config(row) for row in conn.execute(stmt).mappings()
            ]

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

        with get_engine().begin() as conn:
            conn.execute(
                insert(schedule_executions).values(
                    id=execution_id,
                    schedule_id=schedule_id,
                    agent_name=agent_name,
                    status=TaskExecutionStatus.RUNNING,
                    started_at=now,
                    message=message,
                    triggered_by="validation",  # New trigger type for validation
                    validates_execution_id=validates_execution_id,
                )
            )

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

        values = {"business_status": business_status, "validated_at": now}
        if validation_execution_id:
            values["validation_execution_id"] = validation_execution_id

        with get_engine().begin() as conn:
            result = conn.execute(
                update(schedule_executions)
                .where(schedule_executions.c.id == execution_id)
                .values(**values)
            )
            return result.rowcount > 0

    def get_executions_pending_validation(self, agent_name: str = None) -> List[ScheduleExecution]:
        """Get executions that are pending validation.

        Used for startup recovery to retry failed validation attempts.

        Args:
            agent_name: Optional filter by agent name.

        Returns:
            List of executions with business_status = 'pending_validation'.
        """
        conds = [schedule_executions.c.business_status == "pending_validation"]
        if agent_name:
            conds.append(schedule_executions.c.agent_name == agent_name)
        stmt = (
            select(schedule_executions)
            .where(and_(*conds))
            .order_by(schedule_executions.c.started_at.asc())
        )
        with get_engine().connect() as conn:
            return [
                self._row_to_schedule_execution(row)
                for row in conn.execute(stmt).mappings()
            ]

    def get_validation_execution(self, validates_execution_id: str) -> Optional[ScheduleExecution]:
        """Get the validation execution record for a given original execution.

        Args:
            validates_execution_id: The original execution ID.

        Returns:
            The validation execution record, or None if not found.
        """
        stmt = (
            select(schedule_executions)
            .where(schedule_executions.c.validates_execution_id == validates_execution_id)
            .order_by(schedule_executions.c.started_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_schedule_execution(row) if row else None

    def get_agent_token_stats(self, agent_name: str) -> Dict:
        """Get token usage statistics for an agent: lifetime, 24h, 7d, and 7-day daily breakdown.

        Used for the agent detail token usage display (issue #250).
        """
        from datetime import datetime, timezone, timedelta

        cutoff_24h = iso_cutoff(24)
        cutoff_7d = iso_cutoff(168)

        with get_engine().connect() as conn:
            # Lifetime + 24h + 7d in one pass
            row = conn.execute(
                text("""
                SELECT
                    COUNT(*) as lifetime_executions,
                    SUM(COALESCE(cost, 0)) as lifetime_cost,
                    SUM(COALESCE(context_used, 0)) as lifetime_context_tokens,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(cost, 0) ELSE 0 END) as cost_24h,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(context_used, 0) ELSE 0 END) as context_tokens_24h,
                    SUM(CASE WHEN started_at > :c24 THEN 1 ELSE 0 END) as executions_24h,
                    SUM(CASE WHEN started_at > :c7d THEN COALESCE(cost, 0) ELSE 0 END) as cost_7d,
                    SUM(CASE WHEN started_at > :c7d THEN COALESCE(context_used, 0) ELSE 0 END) as context_tokens_7d,
                    SUM(CASE WHEN started_at > :c7d THEN 1 ELSE 0 END) as executions_7d
                FROM schedule_executions
                WHERE agent_name = :agent_name
                  AND status IN ('success', 'failed')
                """),
                {"c24": cutoff_24h, "c7d": cutoff_7d, "agent_name": agent_name},
            ).mappings().first()

            lifetime_cost = round(row["lifetime_cost"] or 0, 6)
            lifetime_context_tokens = row["lifetime_context_tokens"] or 0
            lifetime_executions = row["lifetime_executions"] or 0
            cost_24h = round(row["cost_24h"] or 0, 6)
            context_tokens_24h = row["context_tokens_24h"] or 0
            executions_24h = row["executions_24h"] or 0
            cost_7d = round(row["cost_7d"] or 0, 6)
            context_tokens_7d = row["context_tokens_7d"] or 0
            executions_7d = row["executions_7d"] or 0

            # Per-day breakdown for last 7 days
            day_rows = conn.execute(
                text("""
                SELECT
                    DATE(started_at) as day,
                    SUM(COALESCE(cost, 0)) as day_cost,
                    SUM(COALESCE(context_used, 0)) as day_context_tokens,
                    COUNT(*) as day_executions
                FROM schedule_executions
                WHERE agent_name = :agent_name
                  AND started_at > :c7d
                  AND status IN ('success', 'failed')
                GROUP BY DATE(started_at)
                ORDER BY day ASC
                """),
                {"agent_name": agent_name, "c7d": cutoff_7d},
            ).mappings().all()

            raw_days = {row["day"]: row for row in day_rows}

            # Build complete 7-day series (fill gaps with zero)
            now_utc = datetime.now(timezone.utc)
            daily_breakdown = []
            for i in range(6, -1, -1):
                d = (now_utc - timedelta(days=i)).strftime("%Y-%m-%d")
                if d in raw_days:
                    r = raw_days[d]
                    daily_breakdown.append({
                        "date": d,
                        "cost": round(r["day_cost"] or 0, 6),
                        "context_tokens": r["day_context_tokens"] or 0,
                        "executions": r["day_executions"] or 0,
                    })
                else:
                    daily_breakdown.append({"date": d, "cost": 0.0, "context_tokens": 0, "executions": 0})

            # Trend: today vs 7d daily average (excluding today to avoid comparison bias)
            avg_daily_cost = cost_7d / 7.0 if cost_7d > 0 else 0.0
            if avg_daily_cost > 0:
                trend_pct = round(((cost_24h - avg_daily_cost) / avg_daily_cost) * 100, 1)
            else:
                trend_pct = 0.0

            return {
                "lifetime_cost": lifetime_cost,
                "lifetime_context_tokens": lifetime_context_tokens,
                "lifetime_executions": lifetime_executions,
                "cost_24h": cost_24h,
                "context_tokens_24h": context_tokens_24h,
                "executions_24h": executions_24h,
                "cost_7d": cost_7d,
                "context_tokens_7d": context_tokens_7d,
                "executions_7d": executions_7d,
                "avg_daily_cost": round(avg_daily_cost, 6),
                "trend_cost_pct": trend_pct,
                "daily_breakdown": daily_breakdown,
            }

    # -------------------------------------------------------------------------
    # Fleet-level execution queries (EXEC-022 / Issue #18)
    # -------------------------------------------------------------------------

    def get_fleet_executions(
        self,
        agent_names: Optional[List[str]],  # None = admin (no agent filter)
        *,
        status: Optional[str] = None,
        triggered_by: Optional[str] = None,
        hours: Optional[int] = 24,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """Cross-fleet execution list for the Unified Executions Dashboard.

        Returns summary rows (no large text fields: response, tool_calls,
        execution_log). The error column is truncated to 200 chars for the
        failed-row one-liner.

        agent_names=None → admin path, no agent_name filter applied.
        agent_names=[]   → non-admin with zero accessible agents, returns [].
        hours=0 or None  → no time-window filter (all-time).
        """
        if agent_names is not None and len(agent_names) == 0:
            return []

        conditions = []
        bind: Dict = {}

        if agent_names is not None:
            keys = [f"an{i}" for i in range(len(agent_names))]
            conditions.append("agent_name IN (%s)" % ",".join(f":{k}" for k in keys))
            bind.update(dict(zip(keys, agent_names)))

        if status:
            conditions.append("status = :status")
            bind["status"] = status

        if triggered_by:
            conditions.append("triggered_by = :triggered_by")
            bind["triggered_by"] = triggered_by

        if hours:
            conditions.append("started_at > :cutoff")
            bind["cutoff"] = iso_cutoff(hours)

        if search:
            conditions.append("message LIKE :search")
            bind["search"] = f"%{search}%"

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        bind["lim"] = limit
        bind["off"] = offset

        # Core (#300): named binds; SELECT body is dialect-portable (the only
        # SQLite-ism, SUBSTR, exists on PostgreSQL too). `.mappings()` keeps the
        # dict-row shape the router expects.
        with get_engine().connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    id, schedule_id, agent_name, status, started_at, completed_at,
                    duration_ms, message, triggered_by,
                    context_used, context_max, cost,
                    CASE WHEN status IN ('failed', 'error')
                         THEN SUBSTR(error, 1, 200)
                         ELSE NULL END AS error_summary,
                    source_user_id, source_user_email, source_agent_name,
                    source_mcp_key_id, source_mcp_key_name,
                    model_used, fan_out_id, business_status, validation_execution_id,
                    queued_at
                FROM schedule_executions
                {where}
                ORDER BY started_at DESC
                LIMIT :lim OFFSET :off
            """), bind).mappings().all()
            return [dict(row) for row in rows]

    def get_fleet_execution_stats(
        self,
        agent_names: Optional[List[str]],  # None = admin (no agent filter)
        hours: int = 24,
    ) -> Dict:
        """Aggregate stats for the fleet executions stat cards.

        Returns total/success/failed/cost within the time window, plus a
        current running + queued count (not time-windowed — a run that started
        before the window is still live).
        """
        if agent_names is not None and len(agent_names) == 0:
            return {
                "total": 0, "success_count": 0, "failed_count": 0,
                "total_cost": 0.0, "success_rate": 0.0,
                "running_count": 0, "queued_count": 0, "hours": hours,
            }

        bind: Dict = {}
        agent_where = ""
        if agent_names is not None:
            keys = [f"an{i}" for i in range(len(agent_names))]
            agent_where = "WHERE agent_name IN (%s)" % ",".join(f":{k}" for k in keys)
            bind.update(dict(zip(keys, agent_names)))

        # Single-pass query: windowed totals via conditional aggregation +
        # live running/queued counts without a time filter, all in one scan.
        # time_cond = "1=1" when hours=0 (all-time) so CASE stays unconditional.
        # ("1=1", not "1": PostgreSQL requires a boolean in CASE WHEN — a bare
        # integer raises; SQLite tolerated it. #300.) The named :cutoff bind is
        # reused across all four CASE expressions.
        if hours:
            time_cond = "started_at > :cutoff"
            bind["cutoff"] = iso_cutoff(hours)
        else:
            time_cond = "1=1"

        with get_engine().connect() as conn:
            row = conn.execute(text(f"""
                SELECT
                    SUM(CASE WHEN {time_cond} THEN 1 ELSE 0 END) AS total,
                    SUM(CASE WHEN {time_cond} AND status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN {time_cond} AND status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN {time_cond} THEN COALESCE(cost, 0) ELSE 0 END) AS total_cost,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count
                FROM schedule_executions
                {agent_where}
            """), bind).mappings().first()
            total = row["total"] or 0
            success_count = row["success_count"] or 0
            failed_count = row["failed_count"] or 0
            total_cost = row["total_cost"] or 0.0
            terminal = success_count + failed_count
            success_rate = round(success_count / terminal * 100, 1) if terminal > 0 else 0.0

            return {
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "total_cost": round(total_cost, 4),
                "success_rate": success_rate,
                "running_count": row["running_count"] or 0,
                "queued_count": row["queued_count"] or 0,
                "hours": hours,
            }
