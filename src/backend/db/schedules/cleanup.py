"""Watchdog finalizers, stale/no-session/orphan cleanup, and business-validation executions."""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from sqlalchemy import select, insert, update, and_, or_, func, text

from ..engine import get_engine
from ..tables import (
    schedule_executions,
)
from db_models import ScheduleExecution
from models import TaskExecutionStatus
from utils.helpers import utc_now_iso, parse_iso_timestamp

class ScheduleCleanupMixin:
    """Watchdog + validation-execution operations."""

    def get_running_executions(self) -> list:
        """Get all schedule executions currently in 'running' status.

        Used by startup recovery to detect orphaned executions after a crash.

        Returns:
            List of dicts with id, agent_name, started_at, schedule_id.
        """
        # #1081 Phase 3: startup orphan-recovery reconciles these rows against
        # the agent registry and FAILs any missing one. A pull-claimed row is
        # `running` with a lease but not yet in the agent registry (its worker
        # hasn't begun the turn), so it would be false-orphaned here. Leased rows
        # (lease_expires_at IS NOT NULL) are owned EXCLUSIVELY by the lease-reaper
        # — exclude them; NULL-lease (non-pull) rows recover as before.
        stmt = select(
            schedule_executions.c.id,
            schedule_executions.c.agent_name,
            schedule_executions.c.started_at,
            schedule_executions.c.schedule_id,
        ).where(
            and_(
                schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                schedule_executions.c.lease_expires_at.is_(None),
            )
        )
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
                        # #1081 Phase 3: leased pull rows (lease_expires_at IS NOT
                        # NULL) are owned EXCLUSIVELY by the lease-reaper — its
                        # lease window, not this generic stale window, decides when
                        # they die. Exclude them so this sweep never FAILs a live
                        # lease. NULL-lease (non-pull) rows still swept as before.
                        schedule_executions.c.lease_expires_at.is_(None),
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
                        # #1081 Phase 3: a pull-claimed row is `running` with a
                        # NULL claude_session_id until its worker begins the turn.
                        # Leased rows (lease_expires_at IS NOT NULL) are owned
                        # EXCLUSIVELY by the lease-reaper — exclude them here so
                        # this no-session sweep never FAILs a legitimate lease
                        # before it expires. NULL-lease (non-pull) rows are
                        # unaffected: this sweep still owns them.
                        schedule_executions.c.lease_expires_at.is_(None),
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
            # #1081 Phase 3: never fail a leased pull row (lease_expires_at IS NOT
            # NULL) — those are owned EXCLUSIVELY by the lease-reaper. The #1083
            # stale-slot reaper reclaims a Redis slot by TTL and calls this to
            # FAIL the matching execution; a pull-claimed row could hold a slot,
            # so gate both the read and the CAS on a NULL lease. NULL-lease
            # (push / async-dispatch) rows are failed exactly as before.
            row = conn.execute(
                select(schedule_executions.c.started_at).where(
                    and_(
                        schedule_executions.c.id == execution_id,
                        schedule_executions.c.status == TaskExecutionStatus.RUNNING,
                        schedule_executions.c.lease_expires_at.is_(None),
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
                        schedule_executions.c.lease_expires_at.is_(None),
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
                  -- #1081 Phase 3: leased pull rows (lease_expires_at IS NOT NULL)
                  -- are owned EXCLUSIVELY by the lease-reaper. The periodic
                  -- watchdog reconciles against the agent registry and would
                  -- false-orphan a claimed-but-not-yet-started lease; exclude it.
                  -- NULL-lease (non-pull) rows are reconciled as before.
                  AND e.lease_expires_at IS NULL
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
