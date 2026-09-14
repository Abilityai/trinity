"""#2423 review pass 2 — the exclusion must run in SQL, before the LIMIT.

WHY THIS FILE EXISTS SEPARATELY. `test_2423_client_loop_visibility.py` proves
`_recent_work` asks for the exclusion, but every stub in it is a Python function
that models what SQL does. A stub cannot prove the WHERE actually precedes the
LIMIT — and that ordering IS the fix. So this exercises the real accessor
against a real SQLite through the same engine the backend uses.

The bug it locks: the first fix filtered the RESULT and compensated by
over-fetching `MAX_RECENT_WORK * 5 = 100` rows. `models.MAX_RUNS_LIMIT` is 100,
so one loop at its documented maximum emits exactly 100 consecutive rows and
consumes the whole window — the client page reads "Nothing yet." for an agent
that has been working all day. A bigger multiplier does not fix it; only moving
the filter does.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _make_schema(conn: sqlite3.Connection) -> None:
    # Derived from the SAME metadata the accessor selects from, never
    # hand-copied — the #918 fixture's lesson: a hand-written CREATE TABLE
    # reports its own drift as a failure of the code under test.
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from db.tables import schedule_executions

    conn.execute(str(CreateTable(schedule_executions).compile(
        dialect=sqlite_dialect.dialect())))
    conn.commit()


@pytest.fixture
def ops(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_schema(conn)
    conn.close()
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    return ScheduleOperations(user_ops, AgentOperations(user_ops))


def _insert(rows):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions), rows)


def _row(i, trigger, agent="a1"):
    # `started_at DESC` is the accessor's order, so a zero-padded counter puts
    # the highest index newest.
    return {
        "id": f"e{i:05d}", "schedule_id": "__manual__", "agent_name": agent,
        "status": "success", "started_at": f"2026-08-31T{i:05d}Z",
        "message": "m", "triggered_by": trigger,
    }


def test_one_max_length_loop_does_not_hide_everything_else(ops):
    """The exact shape that defeats over-fetching.

    100 loop rows is not a pathological fixture — it is ONE loop run at
    `MAX_RUNS_LIMIT`, the ceiling the product advertises.
    """
    _insert([_row(i, "chat") for i in range(5)]
            + [_row(100 + i, "loop") for i in range(100)])

    out = ops.get_agent_executions_summary(
        "a1", limit=20, exclude_triggers=frozenset({"loop"}))

    assert len(out) == 5, (
        "the LIMIT must apply to rows that already survived the WHERE — this "
        "returned 0 rows while the operator saw 20"
    )
    assert {r["triggered_by"] for r in out} == {"chat"}


def test_the_limit_still_bounds_the_result(ops):
    _insert([_row(i, "chat") for i in range(50)])
    out = ops.get_agent_executions_summary(
        "a1", limit=20, exclude_triggers=frozenset({"loop"}))
    assert len(out) == 20


def test_it_returns_the_NEWEST_surviving_rows_not_the_oldest(ops):
    """Filtering before the limit must not quietly reorder the window.

    `WHERE ... ORDER BY started_at DESC LIMIT n` is the contract; getting the
    oldest 20 instead would be a subtler version of the same wrong list.
    """
    _insert([_row(i, "chat") for i in range(50)])
    out = ops.get_agent_executions_summary(
        "a1", limit=20, exclude_triggers=frozenset({"loop"}))
    assert [r["id"] for r in out] == [f"e{i:05d}" for i in range(49, 29, -1)]


def test_no_exclusion_is_the_unchanged_behaviour(ops):
    """Every other caller passes nothing and must see exactly what it saw."""
    _insert([_row(0, "chat"), _row(1, "loop"), _row(2, "schedule")])
    for kwargs in ({}, {"exclude_triggers": None}, {"exclude_triggers": frozenset()}):
        out = ops.get_agent_executions_summary("a1", limit=10, **kwargs)
        assert {r["triggered_by"] for r in out} == {"chat", "loop", "schedule"}


def test_a_NULL_trigger_is_not_treated_as_hidden(ops):
    """SQL `NOT IN` yields NULL for a NULL left side, which drops the row.

    `triggered_by` is NOT NULL in the schema, so this is defence rather than a
    live path — but silently vanishing an unclassified row is precisely the
    failure this parameter exists to prevent, and the explicit `IS NULL` arm is
    the only thing standing between the two.
    """
    _insert([_row(0, "chat")])
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    r = _row(1, "loop")
    r["triggered_by"] = None
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions), [r])

    out = ops.get_agent_executions_summary(
        "a1", limit=10, exclude_triggers=frozenset({"loop"}))
    assert {r["id"] for r in out} == {"e00000", "e00001"}


def test_more_than_one_excluded_trigger_works(ops):
    """The parameter is a set, not the single literal its first caller passes."""
    _insert([_row(0, "chat"), _row(1, "loop"), _row(2, "reminder")])
    out = ops.get_agent_executions_summary(
        "a1", limit=10, exclude_triggers=frozenset({"loop", "reminder"}))
    assert [r["triggered_by"] for r in out] == ["chat"]


def test_the_exclusion_is_scoped_to_the_agent(ops):
    """A WHERE added after the agent filter must not widen it."""
    _insert([_row(0, "chat", agent="a1"), _row(1, "chat", agent="a2")])
    out = ops.get_agent_executions_summary(
        "a1", limit=10, exclude_triggers=frozenset({"loop"}))
    assert [r["agent_name"] for r in out] == ["a1"]
