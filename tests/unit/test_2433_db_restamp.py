"""#2433 — ``restamp_execution_dispatch`` re-anchors a RUNNING row at dispatch.

Real schema (db_harness — SQLite always, PostgreSQL when TEST_POSTGRES_URL is
set): the CAS moves ``started_at`` to now, keeps the admission instant in
``queued_at`` (only when NULL — a drained backlog row keeps its own), and
refuses terminal rows and pull-leased rows.

Module under test: src/backend/db/schedules/executions.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun  # noqa: E402,F401  (pytest fixture)


@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


@pytest.fixture
def ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(exec_id: str, *, status="running", age_seconds=600, queued_at=None, lease=None):
    started = _iso(datetime.now(timezone.utc) - timedelta(seconds=age_seconds))
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, "
        " claude_session_id, queued_at, lease_expires_at) "
        "VALUES (:id, '__manual__', 'test-agent', :st, :sa, 'msg', 'schedule', "
        " 'dispatched', :qa, :lease)",
        id=exec_id, st=status, sa=started, qa=queued_at, lease=lease,
    )
    return started


def _row(exec_id: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        r = conn.execute(
            text("SELECT status, started_at, queued_at FROM schedule_executions WHERE id = :id"),
            {"id": exec_id},
        ).mappings().first()
    return dict(r)


def test_restamp_moves_started_at_and_keeps_admission_in_queued_at(ops):
    admitted = _seed("exec-park")
    assert ops.restamp_execution_dispatch("exec-park") is True
    row = _row("exec-park")
    assert row["queued_at"] == admitted, "the admission instant survives in queued_at"
    new_started = datetime.strptime(row["started_at"][:19], "%Y-%m-%dT%H:%M:%S")
    assert datetime.now(timezone.utc).replace(tzinfo=None) - new_started < timedelta(seconds=5)
    assert row["started_at"] > admitted


def test_restamp_preserves_an_existing_queued_at(ops):
    _seed("exec-drained", queued_at="2026-01-01T00:00:00Z")
    assert ops.restamp_execution_dispatch("exec-drained") is True
    assert _row("exec-drained")["queued_at"] == "2026-01-01T00:00:00Z"


def test_restamp_refuses_terminal_rows(ops):
    started = _seed("exec-done", status="success")
    assert ops.restamp_execution_dispatch("exec-done") is False
    assert _row("exec-done")["started_at"] == started


def test_restamp_refuses_pull_leased_rows(ops):
    started = _seed("exec-leased", lease="2099-01-01T00:00:00Z")
    assert ops.restamp_execution_dispatch("exec-leased") is False
    assert _row("exec-leased")["started_at"] == started


def test_restamp_unknown_id_is_false(ops):
    assert ops.restamp_execution_dispatch("nope") is False
