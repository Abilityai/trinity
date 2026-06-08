"""Test ScheduleOperations.fail_queued_for_agent — the #526 drain-on-trip kill.

AC: queued rows for the agent close FAILED (NOT CANCELLED, which is what
cancel_queued_for_agent does), running rows and other agents' rows are
untouched.

Isolation: ``fail_queued_for_agent`` resolves ``get_db_connection`` from its
own ``__globals__`` (the dict of the module that *defined* it), so we patch
exactly that dict to point at a factory bound to a throwaway sqlite file. We
target ``__globals__`` rather than ``sys.modules["db.schedules"]`` because the
evict/re-import + conftest-baseline-restore dance other test files run (#660,
#762) can leave the imported module object and the method's defining-module
object as two DIFFERENT objects — a plain ``setattr(module, ...)`` would patch
the wrong one. ``ScheduleOperations.__init__`` merely stores its collaborators
(unused by this method), so ``ScheduleOperations(None, None)`` is enough.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.integration

_SCHEDULE_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS schedule_executions (
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
    queued_at TEXT
)
"""


@pytest.fixture
def ops(monkeypatch):
    """ScheduleOperations whose get_db_connection points at a throwaway DB."""
    from db.schedules import ScheduleOperations

    db_path = str(Path(tempfile.mkdtemp()) / "trinity.db")

    @contextmanager
    def _conn():
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Patch the EXACT dict the method's get_db_connection() call resolves
    # against (its defining-module __globals__), so the test DB is used even
    # when sys.modules["db.schedules"] is a different object (see docstring).
    method_globals = ScheduleOperations.fail_queued_for_agent.__globals__
    monkeypatch.setitem(method_globals, "get_db_connection", _conn)
    with _conn() as c:
        c.execute(_SCHEDULE_EXECUTIONS_DDL)
    return ScheduleOperations(None, None), _conn


def _insert(get_conn, *, agent_name, status):
    eid = f"ex-{uuid.uuid4().hex[:12]}"
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO schedule_executions
                (id, schedule_id, agent_name, status, started_at, message, triggered_by, queued_at)
            VALUES (?, ?, ?, ?, datetime('now'), ?, 'manual', datetime('now'))
            """,
            (eid, f"sched-{uuid.uuid4().hex[:8]}", agent_name, status, "hi"),
        )
    return eid


def _row(get_conn, eid):
    with get_conn() as c:
        r = c.execute("SELECT status, error FROM schedule_executions WHERE id=?", (eid,)).fetchone()
    return r["status"], r["error"]


def test_fail_queued_sets_failed_not_cancelled(ops):
    schedule_ops, get_conn = ops
    agent = f"a-{uuid.uuid4().hex[:8]}"
    other = f"b-{uuid.uuid4().hex[:8]}"

    q1 = _insert(get_conn, agent_name=agent, status="queued")
    q2 = _insert(get_conn, agent_name=agent, status="queued")
    running = _insert(get_conn, agent_name=agent, status="running")
    other_q = _insert(get_conn, agent_name=other, status="queued")

    n = schedule_ops.fail_queued_for_agent(agent, reason="circuit_open: dispatch breaker open")
    assert n == 2

    for eid in (q1, q2):
        st, err = _row(get_conn, eid)
        assert st == "failed", f"{eid} should be failed, got {st}"
        assert st != "cancelled"
        assert "circuit_open" in (err or "")

    assert _row(get_conn, running)[0] == "running"      # running untouched
    assert _row(get_conn, other_q)[0] == "queued"        # other agent untouched


def test_fail_queued_no_rows_returns_zero(ops):
    schedule_ops, _get_conn = ops
    assert schedule_ops.fail_queued_for_agent(f"nobody-{uuid.uuid4().hex[:6]}", reason="x") == 0
