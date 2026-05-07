"""Regression tests for #671 — cancelled executions reported as `success`.

When the operator cancels a running execution via the UI, two writers race:

  Writer A (terminate handler in routers/chat.py:~1841):
      writes status = CANCELLED into schedule_executions

  Writer B (TaskExecutionService.execute_task success branch:498):
      writes status = SUCCESS once the agent's HTTP response lands —
      the agent often replies 200 because Claude Code catches the cancel
      signal, emits a graceful final message, and exits 0.

Pre-fix CAS (db.schedules.update_execution_status, RELIABILITY-005):
  SUCCESS writes were unconditional ("agent's own completion result always
  wins"). When A landed first and B landed second, B silently clobbered
  the CANCELLED status with SUCCESS. The schedule's `next_run_at`
  advanced as if the run had succeeded; cost telemetry counted the
  partial run as billable success; on-call had no signal that the
  deliverable was incomplete. See issue body for prod repro on
  bdr-agent / `Daily Lead Outreach`.

Post-fix CAS:
  SUCCESS writes are blocked when the row is already CANCELLED. All other
  RELIABILITY-005 invariants (FAILED/CANCELLED/SKIPPED guarded against
  overwriting any terminal) are preserved verbatim.

  Rationale: a user cancel is an authoritative "I no longer want this
  work — its outcome must not be reported as success." Ordering B
  (success lands first, cancel arrives via 'already_finished' branch
  in routers/chat.py) was already protected by the existing terminal
  guard on the cancel-write side.

Issue: https://github.com/abilityai/trinity/issues/671
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap — same shadow-handling as test_schedule_status_observability.py
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Minimal schedule_executions schema for update_execution_status."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL,
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL,
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            tool_calls TEXT,
            execution_log TEXT,
            claude_session_id TEXT,
            compact_metadata TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    for mod in ("db.connection", "db.schedules"):
        sys.modules.pop(mod, None)

    yield db_path


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _insert(tmp_db: Path, *, execution_id: str, status: str, error: str | None = None):
    conn = sqlite3.connect(str(tmp_db))
    conn.execute(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, error, triggered_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            execution_id,
            "sched-1",
            "agent-a",
            status,
            datetime.now(timezone.utc).isoformat(),
            "test message",
            error,
            "scheduler",
        ),
    )
    conn.commit()
    conn.close()


def _get_status(tmp_db: Path, execution_id: str) -> str:
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT status FROM schedule_executions WHERE id = ?",
        (execution_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _get_response(tmp_db: Path, execution_id: str) -> str:
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT response FROM schedule_executions WHERE id = ?",
        (execution_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


# ---------------------------------------------------------------------------
# Bug-fix coverage
# ---------------------------------------------------------------------------


class TestSuccessDoesNotOverwriteCancelled:
    """Issue #671 — the write race where the user-cancelled flag was lost."""

    def test_success_blocked_when_row_already_cancelled(self, tmp_db, schedule_ops):
        """The exact race from the prod repro on bdr-agent.

        Sequence:
            1. row inserted RUNNING
            2. terminate handler writes CANCELLED
            3. agent HTTP reply lands, success branch tries to write SUCCESS
            4. CAS rejects step 3 → row stays CANCELLED
        """
        from models import TaskExecutionStatus

        _insert(tmp_db, execution_id="exec-671", status=TaskExecutionStatus.RUNNING)

        # Step 2 — terminate handler.
        cancelled_ok = schedule_ops.update_execution_status(
            execution_id="exec-671",
            status=TaskExecutionStatus.CANCELLED,
            error="Execution terminated by user",
        )
        assert cancelled_ok is True
        assert _get_status(tmp_db, "exec-671") == TaskExecutionStatus.CANCELLED

        # Step 3 — agent's late "I'm done!" arrives.
        success_attempted = schedule_ops.update_execution_status(
            execution_id="exec-671",
            status=TaskExecutionStatus.SUCCESS,
            response="Final agent message that should NOT count as deliverable",
            cost=1.30,
        )

        # The whole point of the fix.
        assert success_attempted is False, (
            "SUCCESS write must be rejected once the row is CANCELLED — "
            "the operator pulled the plug, the run is not a success."
        )
        assert _get_status(tmp_db, "exec-671") == TaskExecutionStatus.CANCELLED
        # Response/cost from the late agent reply must NOT have been recorded.
        assert _get_response(tmp_db, "exec-671") in (None, "")

    def test_success_still_unconditional_over_running(self, tmp_db, schedule_ops):
        """Happy path stays happy — SUCCESS over RUNNING still wins."""
        from models import TaskExecutionStatus

        _insert(tmp_db, execution_id="exec-happy", status=TaskExecutionStatus.RUNNING)
        ok = schedule_ops.update_execution_status(
            execution_id="exec-happy",
            status=TaskExecutionStatus.SUCCESS,
            response="ok",
        )
        assert ok is True
        assert _get_status(tmp_db, "exec-happy") == TaskExecutionStatus.SUCCESS

    def test_success_still_overrides_failed_phantom_stale(self, tmp_db, schedule_ops):
        """RELIABILITY-005 / #378 invariant preserved.

        Phase-3 phantom-stale FAILED rows must still be overwritten by a real
        SUCCESS — that is the whole reason SUCCESS was unconditional in the
        first place. The fix narrows the carve-out to CANCELLED only.
        """
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-phantom",
            status=TaskExecutionStatus.FAILED,
            error="Stale execution — slot TTL expired for agent 'agent-a'",
        )
        ok = schedule_ops.update_execution_status(
            execution_id="exec-phantom",
            status=TaskExecutionStatus.SUCCESS,
            response="real result that arrived after cleanup misfired",
        )
        assert ok is True
        assert _get_status(tmp_db, "exec-phantom") == TaskExecutionStatus.SUCCESS

    def test_cancelled_blocks_failed_overwrite_unchanged(self, tmp_db, schedule_ops):
        """Existing terminal-guard for FAILED→CANCELLED is preserved."""
        from models import TaskExecutionStatus

        _insert(tmp_db, execution_id="exec-twf", status=TaskExecutionStatus.RUNNING)
        schedule_ops.update_execution_status(
            execution_id="exec-twf",
            status=TaskExecutionStatus.CANCELLED,
            error="user cancel",
        )
        # Cleanup service trying to mark the row FAILED later must be blocked.
        blocked = schedule_ops.update_execution_status(
            execution_id="exec-twf",
            status=TaskExecutionStatus.FAILED,
            error="watchdog timeout",
        )
        assert blocked is False
        assert _get_status(tmp_db, "exec-twf") == TaskExecutionStatus.CANCELLED

    def test_cancelled_blocks_success_specifically_not_other_paths(
        self, tmp_db, schedule_ops
    ):
        """Symmetry: SUCCESS doesn't overwrite CANCELLED, and a no-op
        repeat-CANCELLED also doesn't 'succeed' (rowcount > 0)."""
        from models import TaskExecutionStatus

        _insert(tmp_db, execution_id="exec-sym", status=TaskExecutionStatus.CANCELLED)
        # SUCCESS blocked.
        assert (
            schedule_ops.update_execution_status(
                execution_id="exec-sym",
                status=TaskExecutionStatus.SUCCESS,
                response="late",
            )
            is False
        )
        # Repeat-CANCELLED also blocked by the existing non-success terminal guard.
        assert (
            schedule_ops.update_execution_status(
                execution_id="exec-sym",
                status=TaskExecutionStatus.CANCELLED,
                error="duplicate cancel",
            )
            is False
        )
        assert _get_status(tmp_db, "exec-sym") == TaskExecutionStatus.CANCELLED
