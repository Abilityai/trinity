"""Execution-row create/update lifecycle, dispatch marker, and execution getters."""

from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, insert, update, and_, func, or_

from ..engine import get_engine
from ..query_helpers import latest_per_group
from ..tables import (
    schedule_executions,
)
from db_models import ScheduleExecution
from models import TaskExecutionStatus
from utils.helpers import utc_now_iso, to_utc_iso, parse_iso_timestamp
from ._common import _norm_ts

# #378: Error-message marker written by cleanup_service._process_stale_slot_reclaims
# when Phase 3 fails an execution. Used to scope the residual-race WARNING log
# below so it doesn't misfire on other legitimate FAILED→SUCCESS transitions
# (e.g. Phase 0 auto-terminate, Phase 1 stale cleanup, startup recovery).


class ScheduleExecutionsMixin:
    """Execution lifecycle writers + getters (incl. #1082 CAS update_execution_status)."""

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
            # Turn-integrity flags (#2467)
            turn_integrity=row["turn_integrity"] if "turn_integrity" in row_keys else None,
            # Reader-race auto-retry (#678)
            retry_count=row["retry_count"] if "retry_count" in row_keys and row["retry_count"] is not None else 0,
            # Lease-reaper re-delivery counter (#1081 Phase 3, #429/#1402)
            redelivery_count=row["redelivery_count"]
                if "redelivery_count" in row_keys and row["redelivery_count"] is not None else 0,
            # Channel delivery target (ent#117)
            source_channel=row["source_channel"] if "source_channel" in row_keys else None,
            source_channel_chat_id=row["source_channel_chat_id"] if "source_channel_chat_id" in row_keys else None,
            source_channel_thread=row["source_channel_thread"] if "source_channel_thread" in row_keys else None,
            # Binding-agent for channel report-back (ent#265)
            source_channel_agent=row["source_channel_agent"] if "source_channel_agent" in row_keys else None,
            source_channel_client=row["source_channel_client"] if "source_channel_client" in row_keys else None,
        )

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
        source_channel: str = None,
        source_channel_chat_id: str = None,
        source_channel_thread: str = None,
        source_channel_agent: str = None,
        source_channel_client: str = None,
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
            source_channel_agent: Binding-agent for channel report-back (ent#265).
            source_channel_client: WHICH client the context belongs to — the
                portal recipient check (ent#457 review). NULL on every
                pre-existing row; the resolver fails closed on NULL.
                Set ONLY when channel context is inherited from a parent
                execution; None for direct rows (reporter falls back to the
                executing agent).
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
                    source_channel=source_channel,
                    source_channel_chat_id=source_channel_chat_id,
                    source_channel_thread=source_channel_thread,
                    source_channel_agent=source_channel_agent,
                    source_channel_client=source_channel_client,
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
                source_channel=source_channel,
                source_channel_chat_id=source_channel_chat_id,
                source_channel_thread=source_channel_thread,
                source_channel_agent=source_channel_agent,
                source_channel_client=source_channel_client,
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

    def restamp_execution_dispatch(self, execution_id: str) -> bool:
        """#2433: re-anchor a RUNNING row's clock at the moment the backend
        actually handed the call to the agent, after a park in the backend
        agent-call queue.

        ``started_at`` is written at admission, but every age check reads it
        as "the run began": the registry-blind Phase-1 stale sweep
        (``mark_stale_executions_failed``), canary E-01, the watchdog's 60s
        dispatch grace, and ``duration_ms`` at the terminal write. A long park
        therefore spent the run's own budget — a row parked 20 min and then
        run for 50 min against a 3600s timeout was bulk-FAILed **mid-run**,
        its slot TTL-reclaimed, and its ``duration_ms`` recorded the park. The
        backlog spill already defines ``started_at`` as *left the queue*
        (``queue.py``: "reset started_at so drain records a clean run window");
        the limiter is a second, hidden queue, so the same rule applies.

        The admission instant is preserved in ``queued_at`` (only when it was
        NULL — a drained backlog row keeps its own), so a parked row carries the
        same ``queued_at``/``started_at`` shape as a drained one and the wait is
        visible in the row rather than erased. CAS: RUNNING + NULL lease only
        (pull rows are owned by the lease reaper); a row that went terminal
        during the park is left alone.

        **Why re-stamping the row is what reaches ``duration_ms``:** it is
        computed in ``update_execution_status`` from THIS column, not from the
        in-coroutine ``start_time`` in ``task_execution_service`` — that one is
        taken before the capacity acquire and still spans the park, which is
        why the sibling ``execution_time_ms`` continues to measure park + run
        and is not a bug. Two clocks, two meanings: ``duration_ms`` answers
        "how long did the agent run", ``execution_time_ms`` answers "how long
        did the caller wait". (#2435 review verified this end-to-end.)

        Returns:
            True if the row was re-stamped.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
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
                    started_at=now,
                    # SET expressions read the pre-update row on both dialects,
                    # so this captures the ADMISSION started_at, not `now`.
                    queued_at=func.coalesce(
                        schedule_executions.c.queued_at,
                        schedule_executions.c.started_at,
                    ),
                )
            )
            return result.rowcount > 0

    def resume_session_belongs_to_user(
        self, agent_name: str, claude_session_id: str, user_id: int
    ) -> bool:
        """Does an execution on ``agent_name`` carry ``claude_session_id`` and belong to ``user_id``?

        The authorization primitive behind EXEC-023 "Continue as Chat" (#1672).
        ``resume_session_id`` becomes ``claude --resume <id>`` inside the container,
        replaying that Claude conversation. Execution rows are **agent**-scoped
        (``accessible_agent_names``), not user-scoped, and the row's
        ``claude_session_id`` is returned in the execution payload — so on a *shared*
        agent one operator can read another's session id and resume their private
        conversation (IDOR). The caller gates on this: no owning row → 404 (uniform
        with the Session tab's per-user 404 in ``routers/sessions.py``, which
        deliberately does not leak session-id existence).

        Matches a session id a turn actually ran under (Claude ``str(uuid4())`` or a
        Codex ``thread_id``) — the ``'dispatched'`` / ``'dispatched_async'`` dispatch
        sentinels are also stored in this column but are never a resumable session, so
        the caller rejects them before ever reaching here.
        """
        stmt = (
            select(schedule_executions.c.id)
            .where(
                and_(
                    schedule_executions.c.agent_name == agent_name,
                    schedule_executions.c.claude_session_id == claude_session_id,
                    schedule_executions.c.source_user_id == user_id,
                )
            )
            .limit(1)
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

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
        claim_token: Optional[str] = None,
        turn_integrity: Optional[str] = None,
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
            claim_token: #1081 Phase 1 (**DARK**) — when provided, the CAS WHERE
                additionally requires ``claim_token == <token>`` so ONLY the
                pull-worker holding the matching lease token can finalize the
                row. A stale/duplicate/wrong-token result finds no row to write
                (rowcount 0) and cannot clobber a terminal. None (every existing
                caller) adds no precondition — dark by default.
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
            # started_at and completed_at are written by different processes
            # (backend --workers 2, and the standalone scheduler container), so
            # clock skew can make this subtraction negative. Clamp at the write
            # rather than at every reader — get_agent_analytics consumes
            # duration_ms unguarded (#1832).
            duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))

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
            # #2467: same conditional shape — an unconditional None here would
            # NULL the column on the documented FAILED→SUCCESS resurrect CAS
            # and on every terminal writer that doesn't derive it.
            if turn_integrity is not None:
                values["turn_integrity"] = turn_integrity

            if status == TaskExecutionStatus.SUCCESS:
                # Agent's own completion result wins over everything except a
                # user-issued cancel (#671). A late "I'm done!" from Claude Code
                # after the operator pulled the plug must not flip the row to
                # success — that hides incomplete deliverables and silently
                # advances the schedule's next_run_at.
                conds = [
                    schedule_executions.c.id == execution_id,
                    schedule_executions.c.status != TaskExecutionStatus.CANCELLED,
                ]
            else:
                # Non-success terminal write: block if already terminal so cleanup
                # paths cannot overwrite a real completion (RELIABILITY-005).
                conds = [
                    schedule_executions.c.id == execution_id,
                    schedule_executions.c.status.notin_(_TERMINAL),
                ]

            # #1081 Phase 1 (DARK): pull-worker lease-token gate. When a
            # claim_token is supplied the CAS ALSO requires it to match the row's
            # stamped token, so only the worker holding the current lease can
            # finalize. Folds into the SAME atomic UPDATE as the status
            # precondition — no separate read-then-write. Dark: every existing
            # caller passes None and this clause never engages.
            if claim_token is not None:
                conds.append(schedule_executions.c.claim_token == claim_token)

            where_clause = and_(*conds)

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

    def get_agent_executions_summary(
        self,
        agent_name: str,
        limit: int = 50,
        *,
        exclude_triggers: Optional[frozenset] = None,
    ) -> List[Dict]:
        """Get execution summaries for list view - excludes large text fields.

        `exclude_triggers` (#2423 review) filters BEFORE the LIMIT, which is the
        whole point of it living here rather than in the caller. A caller that
        fetches N rows and drops some in Python is bounded by how many of the
        newest N survive, so a RUN of excluded rows starves the list — and for
        the client Workspace page the excluded trigger is `loop`, whose
        documented ceiling (`models.MAX_RUNS_LIMIT`) is 100 consecutive rows
        from ONE loop. No over-fetch multiplier outruns that: the multiplier is
        a constant a reviewer picks, the run length is a product limit.

        NULL is deliberately NOT excluded. `triggered_by` is NOT NULL in the
        schema, but SQL `NOT IN` evaluates to NULL for a NULL left side and the
        row would vanish — an unclassified row is not a hidden one, and dropping
        it silently is the failure this parameter exists to prevent.

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
                schedule_executions.c.turn_integrity,
            )
            .where(schedule_executions.c.agent_name == agent_name)
            .order_by(schedule_executions.c.started_at.desc())
            .limit(limit)
        )
        if exclude_triggers:
            stmt = stmt.where(or_(
                schedule_executions.c.triggered_by.is_(None),
                schedule_executions.c.triggered_by.notin_(sorted(exclude_triggers)),
            ))
        with get_engine().connect() as conn:
            rows = []
            for row in conn.execute(stmt).mappings():
                d = dict(row)
                # #1474: normalize naive scheduler-written timestamps to UTC 'Z'
                # so the ExecutionSummary response never serializes naive (the
                # reported TasksPanel relative-time shift).
                d["started_at"] = _norm_ts(d.get("started_at"))
                d["completed_at"] = _norm_ts(d.get("completed_at"))
                rows.append(d)
            return rows

    def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
        """Get a specific execution by ID."""
        stmt = select(schedule_executions).where(
            schedule_executions.c.id == execution_id
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_schedule_execution(row) if row else None

    # ent#525 — the in-flight rows a Workspace chat is waiting on.
    _CHAT_INFLIGHT_STATUSES = ("running", "queued")

    def get_running_for_chat(self, chat_id: str) -> List[Dict]:
        """Every RUNNING / QUEUED execution bound to one chat, newest first.

        The Work tab's agent filter cannot find a delegated child: the child's
        ``agent_name`` is the DELEGATE, not the participant (A asks B → the row
        says B). What the child does carry is the chat it was started from —
        ``source_channel_chat_id`` is copied from the parent at creation
        (ent#265 D0, #2386) — so the card under a message finds its steps by
        the chat, not by the agent.

        Only in-flight statuses, by design: the selector has no index of its
        own, and ``idx_executions_status`` makes ``status IN (running, queued)``
        the driving predicate — a handful of rows on any install — so this
        needs no migration. History stays on the agent-keyed read.

        Summary columns only (never ``response`` / ``execution_log``).
        """
        if not chat_id:
            return []
        stmt = (
            select(
                schedule_executions.c.id,
                schedule_executions.c.agent_name,
                schedule_executions.c.status,
                schedule_executions.c.started_at,
                schedule_executions.c.completed_at,
                schedule_executions.c.duration_ms,
                schedule_executions.c.message,
                schedule_executions.c.triggered_by,
                schedule_executions.c.source_user_email,
                schedule_executions.c.source_agent_name,
                schedule_executions.c.source_channel,
                schedule_executions.c.source_channel_chat_id,
                schedule_executions.c.loop_id,
            )
            .where(and_(
                schedule_executions.c.status.in_(self._CHAT_INFLIGHT_STATUSES),
                schedule_executions.c.source_channel_chat_id == chat_id,
            ))
            .order_by(schedule_executions.c.started_at.desc())
            .limit(50)
        )
        with get_engine().connect() as conn:
            rows = []
            for row in conn.execute(stmt).mappings():
                d = dict(row)
                d["started_at"] = _norm_ts(d.get("started_at"))
                d["completed_at"] = _norm_ts(d.get("completed_at"))
                rows.append(d)
            return rows
