"""Schedule CRUD, listing, soft-delete/recover, and run-time updates."""

import json
import logging
from datetime import datetime
from typing import Optional, List, Dict

import pytz
from croniter import croniter
from sqlalchemy import select, insert, update, delete, and_
from sqlalchemy.exc import IntegrityError

from ..engine import get_engine
from ..tables import (
    agent_schedules,
    schedule_executions,
    agent_ownership,
)
from db_models import Schedule, ScheduleCreate
from utils.helpers import utc_now_iso, parse_iso_timestamp

logger = logging.getLogger("db.schedules")

class ScheduleCrudMixin:
    """Schedule CRUD + lifecycle operations."""

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
