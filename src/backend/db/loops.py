"""
Loop operations for sequential bounded task execution (#740).

`agent_loops` is the parent row: configuration, status, terminal-reason.
`agent_loop_runs` is one row per iteration with the per-run summary
(execution_id joins back to `schedule_executions`).
"""

import json
import secrets
from typing import Any, List, Optional

from sqlalchemy import select, insert, update, func, and_

from .engine import get_engine
from .tables import agent_loops, agent_loop_runs
from utils.helpers import utc_now_iso


# Terminal statuses for restart-recovery and stop_loop logic.
TERMINAL_STATUSES = {"completed", "stopped", "failed", "interrupted"}


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
        "no_progress_threshold": row["no_progress_threshold"],
        "model": row["model"],
        "allowed_tools": json.loads(row["allowed_tools"]) if row["allowed_tools"] else None,
        "status": row["status"],
        "runs_completed": row["runs_completed"],
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
        no_progress_threshold: Optional[int] = None,
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
            no_progress_threshold=no_progress_threshold,
            model=model,
            allowed_tools=allowed_tools_json,
            status="queued",
            runs_completed=0,
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
            "no_progress_threshold": no_progress_threshold,
            "model": model,
            "allowed_tools": allowed_tools,
            "status": "queued",
            "runs_completed": 0,
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
    ) -> None:
        """Bump runs_completed + last_response after each iteration."""
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.id == loop_id)
            .values(runs_completed=runs_completed, last_response=last_response)
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
    ) -> None:
        """Set terminal status + stop_reason + completed_at."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finalize_loop requires terminal status, got '{status}'")
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.id == loop_id)
            .values(
                status=status,
                stop_reason=stop_reason,
                error=error,
                completed_at=utc_now_iso(),
            )
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

    def mark_orphans_interrupted(self) -> int:
        """Bulk-flip all non-terminal loops to `interrupted` (startup hook).

        Returns the row count affected. Idempotent: if there are no
        non-terminal rows, no-op.
        """
        now = utc_now_iso()
        stmt = (
            update(agent_loops)
            .where(agent_loops.c.status.in_(("queued", "running")))
            .values(
                status="interrupted",
                stop_reason="interrupted",
                completed_at=func.coalesce(agent_loops.c.completed_at, now),
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            affected = result.rowcount
        return affected

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
