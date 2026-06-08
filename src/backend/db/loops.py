"""
Loop operations for sequential bounded task execution (#740).

`agent_loops` is the parent row: configuration, status, terminal-reason.
`agent_loop_runs` is one row per iteration with the per-run summary
(execution_id joins back to `schedule_executions`).
"""

import json
import secrets
from typing import Any, List, Optional

from db.connection import get_db_connection
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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO agent_loops (
                    id, agent_name, message_template, max_runs, stop_signal,
                    delay_seconds, timeout_per_run, model, allowed_tools,
                    status, runs_completed, stop_reason, last_response, error,
                    started_by_user_id, started_by_user_email,
                    source_agent_name, source_mcp_key_id, source_mcp_key_name,
                    created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL,
                          ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    loop_id, agent_name, message_template, max_runs, stop_signal,
                    delay_seconds, timeout_per_run, model, allowed_tools_json,
                    "queued",
                    started_by_user_id, started_by_user_email,
                    source_agent_name, source_mcp_key_id, source_mcp_key_name,
                    now,
                ),
            )
            conn.commit()

        return {
            "id": loop_id,
            "agent_name": agent_name,
            "message_template": message_template,
            "max_runs": max_runs,
            "stop_signal": stop_signal,
            "delay_seconds": delay_seconds,
            "timeout_per_run": timeout_per_run,
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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM agent_loops WHERE id = ?", (loop_id,))
            row = cursor.fetchone()
            return _loop_row_to_dict(row) if row else None

    def mark_loop_running(self, loop_id: str) -> None:
        """Flip queued → running and stamp started_at."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_loops SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (utc_now_iso(), loop_id),
            )
            conn.commit()

    def update_loop_progress(
        self,
        loop_id: str,
        *,
        runs_completed: int,
        last_response: Optional[str],
    ) -> None:
        """Bump runs_completed + last_response after each iteration."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_loops SET runs_completed = ?, last_response = ? WHERE id = ?",
                (runs_completed, last_response, loop_id),
            )
            conn.commit()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_loops
                   SET status = ?, stop_reason = ?, error = ?, completed_at = ?
                 WHERE id = ?
                """,
                (status, stop_reason, error, utc_now_iso(), loop_id),
            )
            conn.commit()

    def list_loops_for_agent(
        self,
        agent_name: str,
        *,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        sql = "SELECT * FROM agent_loops WHERE agent_name = ?"
        params: List[Any] = [agent_name]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            return [_loop_row_to_dict(r) for r in cursor.fetchall()]

    def list_non_terminal_loops(self) -> List[dict]:
        """All loops in `queued` or `running` — used by restart-recovery."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_loops WHERE status IN ('queued', 'running')"
            )
            return [_loop_row_to_dict(r) for r in cursor.fetchall()]

    def mark_orphans_interrupted(self) -> int:
        """Bulk-flip all non-terminal loops to `interrupted` (startup hook).

        Returns the row count affected. Idempotent: if there are no
        non-terminal rows, no-op.
        """
        now = utc_now_iso()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_loops
                   SET status = 'interrupted',
                       stop_reason = 'interrupted',
                       completed_at = COALESCE(completed_at, ?)
                 WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            affected = cursor.rowcount
            conn.commit()
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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO agent_loop_runs (
                    id, loop_id, run_number, execution_id, status,
                    response, error, cost, duration_ms, started_at, completed_at
                ) VALUES (?, ?, ?, ?, 'running', NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (run_id, loop_id, run_number, execution_id, now),
            )
            conn.commit()
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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_loop_runs
                   SET status = ?,
                       response = ?,
                       error = ?,
                       cost = ?,
                       duration_ms = ?,
                       execution_id = COALESCE(?, execution_id),
                       completed_at = ?
                 WHERE id = ?
                """,
                (
                    status, response, error, cost, duration_ms,
                    execution_id, utc_now_iso(), run_id,
                ),
            )
            conn.commit()

    def list_runs(self, loop_id: str) -> List[dict]:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM agent_loop_runs WHERE loop_id = ? ORDER BY run_number ASC",
                (loop_id,),
            )
            return [_run_row_to_dict(r) for r in cursor.fetchall()]
