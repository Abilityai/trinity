"""The scheduler-retry side of #4408, confirmed.

The issue suspected that a schedule with retries enabled could re-run a turn
that was still executing: a backend shutdown wrote the row FAILED with
"Execution cancelled (backend shutdown)", and the scheduler's retry exclusions
do not name that error.

Confirmed below with the scheduler's real retry decision: such a row is
retried. That is right for a turn that never reached the agent, which is the
only case the backend still writes this error for, and it is why writing it for
a live turn started a second copy of the same work. A row the backend now
leaves `running` is not a terminal to the scheduler's poll, so nothing is
retried while the agent works.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from scheduler import service as service_module  # noqa: E402
from scheduler.database import SchedulerDatabase  # noqa: E402
from scheduler.models import ExecutionStatus  # noqa: E402
from scheduler.service import SchedulerService  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# The backend's text (`task_execution_service.SHUTDOWN_CANCEL_ERROR`); the
# scheduler is a separate package and cannot import it.
SHUTDOWN_CANCEL_ERROR = "Execution cancelled (backend shutdown)"


@pytest.fixture
def db(tmp_path) -> SchedulerDatabase:
    path = str(tmp_path / "scheduler.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, name TEXT NOT NULL,
            cron_expression TEXT NOT NULL, message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1, timezone TEXT DEFAULT 'UTC',
            description TEXT, owner_id INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT,
            model TEXT, timeout_seconds INTEGER, max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60, deleted_at TEXT
        );
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            duration_ms INTEGER, message TEXT NOT NULL, response TEXT, error TEXT,
            triggered_by TEXT NOT NULL, context_used INTEGER, context_max INTEGER,
            cost REAL, tool_calls TEXT, execution_log TEXT, claude_session_id TEXT,
            source_user_id INTEGER, source_user_email TEXT, source_agent_name TEXT,
            source_mcp_key_id TEXT, source_mcp_key_name TEXT, model_used TEXT,
            skip_reason TEXT, attempt_number INTEGER DEFAULT 1,
            retry_of_execution_id TEXT, retry_scheduled_at TEXT, claim_token TEXT,
            chain_depth INTEGER, subscription_id TEXT
        );
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
            autonomy_enabled INTEGER DEFAULT 0, created_at TEXT, deleted_at TEXT,
            subscription_id TEXT
        );
        """
    )
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, autonomy_enabled, created_at) "
        "VALUES ('test-agent', 1, 1, ?)", (now,),
    )
    conn.execute(
        "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, "
        "created_at, updated_at, max_retries) "
        "VALUES ('schedule-1', 'test-agent', 'Morning', '0 9 * * *', 'Run', ?, ?, 2)",
        (now, now),
    )
    conn.commit()
    conn.close()
    return SchedulerDatabase(database_path=path)


def _service(db: SchedulerDatabase) -> SchedulerService:
    service = SchedulerService(database=db, lock_manager=MagicMock())
    service._redis = MagicMock()
    service._schedule_retry_job = MagicMock()
    return service


def _run(db: SchedulerDatabase):
    return db.create_execution(
        schedule_id="schedule-1", agent_name="test-agent", message="Run",
        triggered_by="schedule",
    )


async def test_a_run_failed_by_a_backend_shutdown_is_retried(db):
    """The suspicion in the issue, confirmed: nothing excludes this error."""
    service = _service(db)
    run = _run(db)
    db.update_execution_status(run.id, ExecutionStatus.FAILED, error=SHUTDOWN_CANCEL_ERROR)

    await service._maybe_schedule_retry(db.get_execution(run.id), error_msg=SHUTDOWN_CANCEL_ERROR)

    service._schedule_retry_job.assert_called_once()


async def test_a_run_left_running_by_a_backend_shutdown_is_not_retried(db, monkeypatch):
    monkeypatch.setattr(service_module.config, "poll_interval", 0.01)
    service = _service(db)
    run = _run(db)  # `running`: what the backend now leaves for a live turn

    service._spawn_poll(run.id, 900, "test-agent")
    await asyncio.sleep(0.05)

    service._schedule_retry_job.assert_not_called()
    assert service._active_poll_tasks, "the scheduler stopped following a live run"

    # The agent finishes; the real terminal arrives and is not a failure.
    db.update_execution_status(run.id, ExecutionStatus.SUCCESS, response="done")
    await asyncio.wait_for(asyncio.gather(*service._active_poll_tasks), timeout=5)

    service._schedule_retry_job.assert_not_called()
