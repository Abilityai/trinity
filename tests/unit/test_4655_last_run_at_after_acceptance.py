"""#4655 — last_run_at must be written only after the backend accepts the run.

The scheduler used to write last_run_at at fire time, before the HTTP call to
the backend. A dispatch exception (container unavailable, queue full, network
error) would leave last_run_at freshly updated, making a schedule that never
reached the model look healthy to liveness monitors.

The fix: last_run_at is now written inside _dispatch_and_record_outcome, only
when the backend returns a response (any status) or when the exception path
detects that the backend already processed the run.

Three cases:
1. Dispatch exception + unclaimed RUNNING row (backend never received it):
   last_run_at must NOT be advanced.
2. Dispatch succeeds (any status): last_run_at MUST be advanced.
3. Dispatch exception + already-finalized row (backend ran it, scheduler timed
   out): last_run_at MUST be advanced.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

SCHED_ID = "sched-4655"
EXEC_ID = "exec-4655"
PILOT = "pilot-4655"
SENTINEL = "2020-01-01T00:00:00Z"  # a recognisably old value


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_db(tmp_path, *, exec_status="running", last_run_at=SENTINEL):
    """Build a minimal SQLite DB with one schedule and one execution."""
    from db.schema import TABLES
    import src.scheduler.database as scheduler_database

    path = tmp_path / "trinity.db"
    conn = sqlite3.connect(path)
    conn.execute(TABLES["agent_schedules"])
    conn.execute(TABLES["schedule_executions"])
    conn.execute(
        "INSERT INTO agent_schedules "
        "(id, agent_name, name, cron_expression, message, owner_id, created_at, "
        "updated_at, last_run_at, next_run_at) "
        "VALUES (?, ?, 'nightly', '0 0 * * *', 'go', 1, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?, NULL)",
        (SCHED_ID, PILOT, last_run_at),
    )
    conn.execute(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES (?, ?, ?, ?, '2026-11-19T10:00:00Z', 'go', 'schedule')",
        (EXEC_ID, SCHED_ID, PILOT, exec_status),
    )
    conn.commit()
    conn.close()
    return path, scheduler_database.SchedulerDatabase(str(path))


def _last_run_at(path):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT last_run_at FROM agent_schedules WHERE id = ?", (SCHED_ID,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _make_svc(db, *, backend_result=None, backend_raises=None):
    import src.scheduler.service as scheduler_service

    svc = object.__new__(scheduler_service.SchedulerService)
    svc.db = db
    svc._active_poll_tasks = set()
    svc._publish_event = AsyncMock()
    if backend_raises is not None:
        svc._call_backend_execute_task = AsyncMock(side_effect=backend_raises)
    else:
        svc._call_backend_execute_task = AsyncMock(return_value=backend_result)
    svc._poll_and_finalize = AsyncMock()
    return svc


def _run(svc, *, exec_id=EXEC_ID, sched_id=SCHED_ID):
    schedule = MagicMock(
        id=sched_id,
        agent_name=PILOT,
        model=None,
        timeout_seconds=600,
        allowed_tools=None,
        deliver_to_workspace_email=None,
    )
    schedule.name = "nightly"
    execution = MagicMock(id=exec_id)

    async def go():
        await svc._dispatch_and_record_outcome(schedule, execution, "go", "schedule")
        await asyncio.sleep(0)

    _await(go())


class TestPhantomFireDoesNotAdvanceLastRunAt:
    """The core defect from #4655."""

    def test_dispatch_exception_leaves_last_run_at_unchanged(self, tmp_path):
        """Backend unreachable → exception → last_run_at must stay at SENTINEL."""
        path, db = _make_db(tmp_path, exec_status="running")
        svc = _make_svc(db, backend_raises=Exception("connection refused"))
        _run(svc)
        assert _last_run_at(path) == SENTINEL, (
            "last_run_at must not advance when the backend never received the dispatch"
        )

    def test_dispatch_exception_unclaimed_running_is_marked_failed(self, tmp_path):
        """Sanity: the execution is still marked failed — only last_run_at changes."""
        path, db = _make_db(tmp_path, exec_status="running")
        svc = _make_svc(db, backend_raises=Exception("timeout"))
        _run(svc)
        conn = sqlite3.connect(path)
        status = conn.execute(
            "SELECT status FROM schedule_executions WHERE id = ?", (EXEC_ID,)
        ).fetchone()[0]
        conn.close()
        assert status == "failed"


class TestSuccessfulDispatchAdvancesLastRunAt:
    """Any backend response means the run was accepted."""

    def test_success_response_advances_last_run_at(self, tmp_path):
        path, db = _make_db(tmp_path)
        svc = _make_svc(db, backend_result={"status": "success"})
        _run(svc)
        assert _last_run_at(path) != SENTINEL

    def test_dispatched_response_advances_last_run_at(self, tmp_path):
        path, db = _make_db(tmp_path)
        svc = _make_svc(db, backend_result={"status": "dispatched"})
        _run(svc)
        assert _last_run_at(path) != SENTINEL

    def test_failed_response_advances_last_run_at(self, tmp_path):
        """Backend ran it and returned failed — still reached the model."""
        path, db = _make_db(tmp_path)
        svc = _make_svc(db, backend_result={"status": "failed", "error": "oops"})
        _run(svc)
        assert _last_run_at(path) != SENTINEL


class TestExceptionWithFinalizedRow:
    """SCHED-ASYNC-001: scheduler timed out but backend already finished."""

    def test_already_finalized_row_advances_last_run_at(self, tmp_path):
        """Backend completed the run before the scheduler's exception — advance."""
        path, db = _make_db(tmp_path, exec_status="success")
        svc = _make_svc(db, backend_raises=Exception("polling timeout"))
        _run(svc)
        assert _last_run_at(path) != SENTINEL
