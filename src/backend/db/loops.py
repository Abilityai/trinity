"""
Loop operations for sequential bounded task execution (#740).

`agent_loops` is the parent row: configuration, status, terminal-reason.
`agent_loop_runs` is one row per iteration with the per-run summary
(execution_id joins back to `schedule_executions`).
"""

import json
import secrets
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import select, insert, update, func, and_

from .engine import get_engine
from .tables import agent_loops, agent_loop_runs
from utils.helpers import utc_now_iso


# Terminal statuses for restart-recovery and stop_loop logic.
# `completed_with_errors` (#1167): continue-mode loop that ran to max_runs with
# at least one tolerated failed iteration.
TERMINAL_STATUSES = {
    "completed", "completed_with_errors", "stopped", "failed", "interrupted",
}


def _parse_instant(value: Optional[str]) -> datetime:
    """Parse a stored ISO timestamp to an aware UTC instant for sorting (ent#99).

    Tolerates both the `...Z` form this module writes and an explicit offset, so
    the ordering is by real instant rather than by wall-clock text. An
    unparseable value sorts first — it is due by definition (the SQL filter
    already matched it) and must not be starved by a formatting fault.
    """
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _loop_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "agent_name": row["agent_name"],
        "message_template": row["message_template"],
        "max_runs": row["max_runs"],
        "stop_signal": row["stop_signal"],
        "delay_seconds": row["delay_seconds"],
        "timeout_per_run": row["timeout_per_run"],
        "max_duration_seconds": row["max_duration_seconds"],
        "max_cost_usd": row["max_cost_usd"],
        "no_progress_threshold": row["no_progress_threshold"],
        "on_failure": row["on_failure"],
        "max_consecutive_failures": row["max_consecutive_failures"],
        "model": row["model"],
        "allowed_tools": json.loads(row["allowed_tools"]) if row["allowed_tools"] else None,
        "status": row["status"],
        "runs_completed": row["runs_completed"],
        "failed_runs": row["failed_runs"],
        "stop_reason": row["stop_reason"],
        "last_response": row["last_response"],
        "error": row["error"],
        "started_by_user_id": row["started_by_user_id"],
        "started_by_user_email": row["started_by_user_email"],
        "source_agent_name": row["source_agent_name"],
        "source_mcp_key_id": row["source_mcp_key_id"],
        "source_mcp_key_name": row["source_mcp_key_name"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        # #2523
        "next_run_at": row["next_run_at"],
        "stop_requested_at": row["stop_requested_at"],
    }


def _run_row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "loop_id": row["loop_id"],
        "run_number": row["run_number"],
        "execution_id": row["execution_id"],
        "status": row["status"],
        "response": row["response"],
        "error": row["error"],
        "cost": row["cost"],
        "duration_ms": row["duration_ms"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


class LoopOperations:
    """Database operations for agent_loops + agent_loop_runs."""

    # ---- Loop CRUD ---------------------------------------------------------

    def create_loop(
        self,
        agent_name: str,
        message_template: str,
        max_runs: int,
        *,
        stop_signal: Optional[str] = None,
        delay_seconds: int = 0,
        timeout_per_run: Optional[int] = None,
        max_duration_seconds: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        no_progress_threshold: Optional[int] = None,
        on_failure: str = "abort",
        max_consecutive_failures: int = 3,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        started_by_user_id: Optional[int] = None,
        started_by_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
    ) -> dict:
        """Insert a new loop in `queued` status; return its dict snapshot."""
        loop_id = f"loop_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()
        allowed_tools_json = json.dumps(allowed_tools) if allowed_tools else None

        stmt = insert(agent_loops).values(
            id=loop_id,
            agent_name=agent_name,
            message_template=message_template,
            max_runs=max_runs,
            stop_signal=stop_signal,
            delay_seconds=delay_seconds,
            timeout_per_run=timeout_per_run,
            max_duration_seconds=max_duration_seconds,
            max_cost_usd=max_cost_usd,
            no_progress_threshold=no_progress_threshold,
            on_failure=on_failure,
            max_consecutive_failures=max_consecutive_failures,
            model=model,
            allowed_tools=allowed_tools_json,
            status="queued",
            runs_completed=0,
            failed_runs=0,
            stop_reason=None,
            last_response=None,
            error=None,
            started_by_user_id=started_by_user_id,
            started_by_user_email=started_by_user_email,
            source_agent_name=source_agent_name,
            source_mcp_key_id=source_mcp_key_id,
            source_mcp_key_name=source_mcp_key_name,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return {
            "id": loop_id,
            "agent_name": agent_name,
            "message_template": message_template,
            "max_runs": max_runs,
            "stop_signal": stop_signal,
            "delay_seconds": delay_seconds,
            "timeout_per_run": timeout_per_run,
            "max_duration_seconds": max_duration_seconds,
            "max_cost_usd": max_cost_usd,
            "no_progress_threshold": no_progress_threshold,
            "on_failure": on_failure,
            "max_consecutive_failures": max_consecutive_failures,
            "model": model,
            "allowed_tools": allowed_tools,
            "status": "queued",
            "runs_completed": 0,
            "failed_runs": 0,
            "stop_reason": None,
            "last_response": None,
            "error": None,
            "started_by_user_id": started_by_user_id,
            "started_by_user_email": started_by_user_email,
            "source_agent_name": source_agent_name,
            "source_mcp_key_id": source_mcp_key_id,
            "source_mcp_key_name": source_mcp_key_name,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
        }

    def get_loop(self, loop_id: str) -> Optional[dict]:
        stmt = select(agent_loops).where(agent_loops.c.id == loop_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return _loop_row_to_dict(row) if row else None

    def mark_loop_running(self, loop_id: str) -> None:
        """Flip queued → running and stamp started_at."""
        stmt = (
            update(agent_loops)
            .where(and_(agent_loops.c.id == loop_id, agent_loops.c.status == "queued"))
            .values(status="running", started_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def update_loop_progress(
        self,
        loop_id: str,
        *,
        runs_completed: int,
        last_response: Optional[str],
        failed_runs: Optional[int] = None,
    ) -> None:
        """Bump runs_completed + last_response after each iteration.

        `failed_runs` (#1167) is written only when provided, so the success
        path can omit it. `last_response` carries the last *successful* response
        even on a tolerated-failure iteration (continue mode), preserving
        `{{previous_response}}` semantics.
        """
        values: dict = {"runs_completed": runs_completed, "last_response": last_response}
        if failed_runs is not None:
            values["failed_runs"] = failed_runs
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.id == loop_id)
            .values(**values)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def finalize_loop(
        self,
        loop_id: str,
        *,
        status: str,
        stop_reason: str,
        error: Optional[str] = None,
        failed_runs: Optional[int] = None,
    ) -> None:
        """Set terminal status + stop_reason + completed_at.

        `failed_runs` (#1167) writes the authoritative tolerated-failure count
        when provided.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finalize_loop requires terminal status, got '{status}'")
        values: dict = {
            "status": status,
            "stop_reason": stop_reason,
            "error": error,
            "completed_at": utc_now_iso(),
        }
        if failed_runs is not None:
            values["failed_runs"] = failed_runs
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.id == loop_id)
            .values(**values)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def list_loops_for_agent(
        self,
        agent_name: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        conds: List[Any] = [agent_loops.c.agent_name == agent_name]
        if status:
            conds.append(agent_loops.c.status == status)
        stmt = (
            select(agent_loops)
            .where(and_(*conds))
            .order_by(agent_loops.c.created_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [_loop_row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def list_non_terminal_loops(self) -> List[dict]:
        """All loops in `queued` or `running` — used by restart-recovery."""
        stmt = select(agent_loops).where(
            agent_loops.c.status.in_(("queued", "running"))
        )
        with get_engine().connect() as conn:
            return [_loop_row_to_dict(r) for r in conn.execute(stmt).mappings()]

    # ---- Loop run rows -----------------------------------------------------

    def start_loop_run(
        self,
        loop_id: str,
        run_number: int,
        *,
        execution_id: Optional[str] = None,
    ) -> str:
        """Insert a new `running` loop-run row; return its id."""
        run_id = f"lr_{secrets.token_urlsafe(10)}"
        now = utc_now_iso()
        stmt = insert(agent_loop_runs).values(
            id=run_id,
            loop_id=loop_id,
            run_number=run_number,
            execution_id=execution_id,
            status="running",
            response=None,
            error=None,
            cost=None,
            duration_ms=None,
            started_at=now,
            completed_at=None,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)
        return run_id

    def finalize_loop_run(
        self,
        run_id: str,
        *,
        status: str,
        response: Optional[str],
        error: Optional[str],
        cost: Optional[float],
        duration_ms: Optional[int],
        execution_id: Optional[str] = None,
    ) -> None:
        stmt = (
            update(agent_loop_runs)
            .where(agent_loop_runs.c.id == run_id)
            .values(
                status=status,
                response=response,
                error=error,
                cost=cost,
                duration_ms=duration_ms,
                execution_id=func.coalesce(execution_id, agent_loop_runs.c.execution_id),
                completed_at=utc_now_iso(),
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def list_runs(self, loop_id: str) -> List[dict]:
        stmt = (
            select(agent_loop_runs)
            .where(agent_loop_runs.c.loop_id == loop_id)
            .order_by(agent_loop_runs.c.run_number.asc())
        )
        with get_engine().connect() as conn:
            return [_run_row_to_dict(r) for r in conn.execute(stmt).mappings()]

    def get_run_by_execution(self, execution_id: str) -> Optional[dict]:
        """The loop-run row an execution belongs to, or None (#2523).

        The terminal-driven advance starts from an ``execution_id`` and has to
        find the iteration it closes. ``agent_loop_runs.execution_id`` is
        stamped at dispatch (``start_loop_run(execution_id=...)``) precisely so
        this lookup exists before the terminal lands — the pre-#2523 runner
        filled it in afterwards, when it already knew which run it was awaiting.
        """
        stmt = (
            select(agent_loop_runs)
            .where(agent_loop_runs.c.execution_id == execution_id)
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return _run_row_to_dict(row) if row else None

    # ---- Terminal-driven advance (#2523) -----------------------------------

    def claim_loop_advance(self, loop_id: str, run_number: int) -> bool:
        """CAS ``runs_completed`` from ``run_number - 1`` to ``run_number``.

        The idempotency gate for the whole advance. Pull is at-least-once, so
        the SAME terminal can arrive twice (a re-delivered lease, a late
        callback racing the reaper). Whoever wins this UPDATE owns advancing the
        loop; everyone else gets False and returns without dispatching, so a
        duplicate terminal cannot double-fire iteration N+1.

        Also excludes a loop that has already reached a terminal status, so a
        late terminal for a run of an aborted loop cannot resurrect it.
        """
        stmt = (
            update(agent_loops)
            .where(
                and_(
                    agent_loops.c.id == loop_id,
                    agent_loops.c.runs_completed == run_number - 1,
                    agent_loops.c.status.notin_(tuple(TERMINAL_STATUSES)),
                )
            )
            .values(runs_completed=run_number)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def request_loop_stop(self, loop_id: str) -> bool:
        """Stamp ``stop_requested_at`` on a non-terminal loop (#2523).

        Replaces flipping ``should_stop`` on an in-memory ``_LoopHandle``, which
        only worked in the process that started the loop. Returns True when a
        non-terminal row was stamped.
        """
        stmt = (
            update(agent_loops)
            .where(
                and_(
                    agent_loops.c.id == loop_id,
                    agent_loops.c.status.notin_(tuple(TERMINAL_STATUSES)),
                )
            )
            .values(stop_requested_at=utc_now_iso())
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def schedule_next_run(self, loop_id: str, next_run_at: str) -> None:
        """Park the loop until ``next_run_at`` (the ``delay_seconds`` pause)."""
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.id == loop_id)
            .values(next_run_at=next_run_at)
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def claim_due_loop(self, loop_id: str, next_run_at: str) -> bool:
        """Atomically take ownership of a due loop by clearing ``next_run_at``.

        The sweep runs in every backend worker, so the claim must be a CAS on
        the exact value read — two workers seeing the same due row means exactly
        one clears it and dispatches.
        """
        stmt = (
            update(agent_loops)
            .where(
                and_(
                    agent_loops.c.id == loop_id,
                    agent_loops.c.next_run_at == next_run_at,
                    agent_loops.c.status.notin_(tuple(TERMINAL_STATUSES)),
                )
            )
            .values(next_run_at=None)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def list_due_loops(self, now: str, *, limit: int = 100) -> List[dict]:
        """Non-terminal loops whose ``next_run_at`` has arrived, oldest first.

        **Filtered in SQL, ordered in Python** (ent#99, Invariant #16). Every
        `next_run_at` this module writes is `utc_now_iso()`-shaped, so a
        lexicographic SQL sort would in fact be correct today — but the sibling
        column on `agent_schedules` is mixed-format (the scheduler writes `Z`,
        the backend writes the schedule's own UTC offset) and a lexicographic
        sort there orders by LOCAL WALL CLOCK. Sorting over parsed instants
        costs nothing at this size and cannot inherit that bug if a second
        writer ever appears.

        `limit` is a safety valve on a pathological backlog, not a cursor: the
        parked set is bounded by the instance's concurrently-running loops, and
        the sweep re-runs every ~5s.
        """
        stmt = (
            select(agent_loops)
            .where(
                and_(
                    agent_loops.c.next_run_at.isnot(None),
                    agent_loops.c.next_run_at <= now,
                    agent_loops.c.status.notin_(tuple(TERMINAL_STATUSES)),
                )
            )
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = [_loop_row_to_dict(r) for r in conn.execute(stmt).mappings()]
        return sorted(rows, key=lambda r: _parse_instant(r["next_run_at"]))
