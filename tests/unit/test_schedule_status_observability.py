"""
Schedule status observability log (Issue #378)

Regression test for the narrowly-scoped WARNING log in
`db.schedules.ScheduleOperations.update_execution_status`: when a row whose
error matches the Phase-3 phantom-stale pattern is overwritten by SUCCESS,
we emit a log line tagged "residual race condition (#378)" so we can
observe residual races in production without changing update semantics.

Scoped to the stale-slot error pattern so other legitimate FAILED→SUCCESS
transitions (startup recovery, Phase 0 auto-terminate, Phase 1 stale
cleanup) do NOT misfire the log.

Covered scenarios:
1. FAILED row with stale-slot pattern → SUCCESS → log emitted
2. FAILED row with a DIFFERENT error → SUCCESS → log NOT emitted
3. RUNNING row → SUCCESS → log NOT emitted (happy path, no prior failure)
4. FAILED row with stale-slot pattern → FAILED (same status) → log NOT emitted
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable. Copy of the bootstrap from
# tests/unit/test_backlog.py so the same path-shadow issues are handled.
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


PHANTOM_ERROR_PATTERN = "Stale execution — slot TTL expired"
RESIDUAL_LOG_MARKER = "residual race condition (#378)"


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
            claude_session_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    # Re-import modules that read DB_PATH at import time.
    for mod in ("db.connection", "db.schedules"):
        sys.modules.pop(mod, None)

    yield db_path


@pytest.fixture
def schedule_ops(tmp_db):
    """Fresh ScheduleOperations bound to tmp_db."""
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _insert(tmp_db: Path, *, execution_id: str, status: str, error: str | None):
    """Seed a schedule_executions row with a given status + error."""
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


class TestResidualRaceObservabilityLog:
    """Issue #378: warn when SUCCESS overwrites a Phase-3 phantom-stale FAILED."""

    pytestmark = pytest.mark.unit

    def test_logs_when_success_overwrites_phantom_stale_failed(
        self, tmp_db, schedule_ops, caplog
    ):
        """Row FAILED with stale-slot pattern, then SUCCESS → WARNING log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-378",
            status=TaskExecutionStatus.FAILED,
            error=f"{PHANTOM_ERROR_PATTERN} for agent 'agent-a', cleaned by cleanup service",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-378",
                status=TaskExecutionStatus.SUCCESS,
                response="agent returned result",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-378") == TaskExecutionStatus.SUCCESS

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert len(matching) == 1, (
            f"Expected exactly one #378 residual-race log, got "
            f"{len(matching)}. Messages: {[r.getMessage() for r in caplog.records]}"
        )
        assert "exec-378" in matching[0].getMessage()

    def test_does_not_log_when_failed_error_is_from_other_cleanup_path(
        self, tmp_db, schedule_ops, caplog
    ):
        """Codex Point 6: startup recovery / Phase 0 auto-terminate / Phase 1
        stale cleanup also write FAILED via unguarded update_execution_status.
        Those FAILED→SUCCESS transitions must NOT trigger the #378 log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-other",
            status=TaskExecutionStatus.FAILED,
            error="Execution auto-terminated after 16 minutes by watchdog "
            "(exceeded timeout of 900s)",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-other",
                status=TaskExecutionStatus.SUCCESS,
                response="late agent response",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-other") == TaskExecutionStatus.SUCCESS

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == [], (
            "#378 log misfired on a non-stale-slot FAILED→SUCCESS transition. "
            f"Messages: {[r.getMessage() for r in caplog.records]}"
        )

    def test_does_not_log_on_running_to_success_happy_path(
        self, tmp_db, schedule_ops, caplog
    ):
        """The normal happy path (RUNNING → SUCCESS, no prior failure) must
        not trigger the log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-happy",
            status=TaskExecutionStatus.RUNNING,
            error=None,
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-happy",
                status=TaskExecutionStatus.SUCCESS,
                response="ok",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-happy") == TaskExecutionStatus.SUCCESS
        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == []

    def test_does_not_log_on_same_status_write(self, tmp_db, schedule_ops, caplog):
        """FAILED → FAILED with stale-slot pattern → no log (not an overwrite
        of FAILED by SUCCESS, just a re-write)."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-same",
            status=TaskExecutionStatus.FAILED,
            error=f"{PHANTOM_ERROR_PATTERN} for agent 'agent-a', cleaned by cleanup service",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            schedule_ops.update_execution_status(
                execution_id="exec-same",
                status=TaskExecutionStatus.FAILED,
                error="re-fail",
            )

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == []
