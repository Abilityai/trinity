"""A share holder's execution reads are narrowed in SQL, before the LIMIT
(abilityai/trinity-enterprise#875).

A stub that models the filter in Python cannot prove the WHERE precedes the
LIMIT, and that ordering is the fix: filtered after the limit, another person's
busy day pushes the caller's own turns off the list, and a `search` can still
reach text that is not theirs. So this drives the REAL accessors against a real
SQLite through the same engine the backend uses, with three users, a limit below
the row count, and the caller's own rows the OLDEST (the worst case).

Also pins the data-shape traps the plan review named: a NULL `source_user_id`
(scheduled, channel, legacy rows) never matches `=` and so is hidden; the
fleet OR-clause keeps its precedence next to `search`; the owner's call, which
passes no filter, is unchanged.
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

ME, OTHER, THIRD = 7, 8, 9


def _make_schema(conn: sqlite3.Connection) -> None:
    # Derived from the SAME metadata the accessors select from, never hand-copied.
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from db.tables import agent_activities, schedule_executions

    for table in (schedule_executions, agent_activities):
        conn.execute(str(CreateTable(table).compile(dialect=sqlite_dialect.dialect())))
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
        from db.activities import ActivityOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    sched = ScheduleOperations(user_ops, AgentOperations(user_ops))
    return sched, ActivityOperations()


def _insert_exec(rows):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions), rows)


def _exec(i, uid, agent="a1", **extra):
    # `started_at DESC` is the accessor order, so a higher index is newer.
    return {
        "id": f"e{i:05d}", "schedule_id": extra.pop("schedule_id", "__manual__"),
        "agent_name": agent, "status": "success",
        "started_at": f"2026-10-16T{i:05d}Z", "message": extra.pop("message", f"prompt {i}"),
        "triggered_by": "chat", "source_user_id": uid, **extra,
    }


def _seed_busy_other_user():
    """My 3 turns are the OLDEST; another user's 30 turns are all newer; plus
    NULL-user rows (a scheduled run, a channel turn) in between."""
    rows = [_exec(i, ME) for i in range(3)]
    rows += [_exec(10 + i, None, triggered_by="schedule") for i in range(2)]
    rows += [_exec(100 + i, OTHER) for i in range(30)]
    _insert_exec(rows)


# ---- the per-agent list ----------------------------------------------------------

def test_the_filter_runs_before_the_limit_so_my_own_turns_survive(ops):
    sched, _ = ops
    _seed_busy_other_user()
    out = sched.get_agent_executions_summary("a1", limit=5, source_user_id=ME)
    assert [r["id"] for r in out] == ["e00002", "e00001", "e00000"], (
        "limit 5 over 35 rows must still return my 3: filtering AFTER the limit "
        "would have returned the newest 5 (all another user's) and then dropped them"
    )
    assert {r["source_user_id"] for r in out} == {ME}


def test_without_the_filter_the_owner_still_sees_everyone(ops):
    sched, _ = ops
    _seed_busy_other_user()
    out = sched.get_agent_executions_summary("a1", limit=100)
    assert len(out) == 35


def test_a_null_source_user_id_is_never_a_share_holders_row(ops):
    """Scheduled, channel and legacy rows carry no user. `=` is never true for
    NULL, so they stay hidden (fail closed). Do not rewrite as IS NOT DISTINCT."""
    sched, _ = ops
    _seed_busy_other_user()
    out = sched.get_agent_executions_summary("a1", limit=100, source_user_id=ME)
    assert all(r["source_user_id"] is not None for r in out)
    assert not any(r["triggered_by"] == "schedule" for r in out)


def test_another_agents_rows_are_never_mixed_in(ops):
    sched, _ = ops
    _insert_exec([_exec(1, ME, agent="a1"), _exec(2, ME, agent="a2")])
    out = sched.get_agent_executions_summary("a1", limit=10, source_user_id=ME)
    assert [r["agent_name"] for r in out] == ["a1"]


def test_a_user_with_no_rows_gets_an_empty_list(ops):
    sched, _ = ops
    _seed_busy_other_user()
    assert sched.get_agent_executions_summary("a1", limit=10, source_user_id=THIRD) == []


# ---- a schedule's history ---------------------------------------------------------

def test_a_schedules_history_is_narrowed_before_its_limit(ops):
    sched, _ = ops
    rows = [_exec(i, ME, schedule_id="s1") for i in range(2)]
    rows += [_exec(50 + i, OTHER, schedule_id="s1") for i in range(10)]
    rows += [_exec(90 + i, None, schedule_id="s1", triggered_by="schedule") for i in range(3)]
    _insert_exec(rows)
    mine = sched.get_schedule_executions("s1", limit=4, source_user_id=ME)
    assert sorted(e.id for e in mine) == ["e00000", "e00001"]
    assert len(sched.get_schedule_executions("s1", limit=100)) == 15


# ---- a fan-out batch ----------------------------------------------------------------

def test_a_fan_out_batch_reads_back_only_my_rows(ops):
    sched, _ = ops
    _insert_exec([
        _exec(1, ME, fan_out_id="fo_x", fan_out_task_id="t1"),
        _exec(2, OTHER, fan_out_id="fo_x", fan_out_task_id="t2"),
        _exec(3, ME, fan_out_id="fo_y", fan_out_task_id="t1"),
    ])
    mine = sched.get_fan_out_executions("a1", "fo_x", source_user_id=ME)
    assert [r["id"] for r in mine] == ["e00001"]
    assert len(sched.get_fan_out_executions("a1", "fo_x")) == 2   # the platform's own call


# ---- the fleet list -------------------------------------------------------------------

def _fleet_seed():
    """a-shared: the caller only holds a share. a-owned: the caller owns it (exempt)."""
    rows = []
    rows += [_exec(1 + i, ME, agent="a-shared", message="mine on shared") for i in range(2)]
    rows += [_exec(20 + i, OTHER, agent="a-shared", message="secret theirs on shared") for i in range(6)]
    rows += [_exec(40 + i, OTHER, agent="a-owned", message="secret on owned") for i in range(3)]
    rows += [_exec(60, None, agent="a-shared", message="scheduled on shared", triggered_by="schedule")]
    # An owned-agent row that does NOT match the searches below: if the OR clause
    # lost its parentheses, the owned-agent branch would escape the search filter
    # and this row would be returned for a search it does not match.
    rows += [_exec(70, OTHER, agent="a-owned", message="ordinary on owned")]
    _insert_exec(rows)


def test_the_fleet_list_narrows_every_agent_except_the_ones_the_caller_owns(ops):
    sched, _ = ops
    _fleet_seed()
    out = sched.get_fleet_executions(
        ["a-shared", "a-owned"], hours=0, limit=50,
        own_rows_user_id=ME, own_rows_exempt_agents=["a-owned"])
    by_agent = {}
    for r in out:
        by_agent.setdefault(r["agent_name"], []).append(r)
    assert {r["source_user_id"] for r in by_agent["a-shared"]} == {ME}
    assert len(by_agent["a-shared"]) == 2
    assert len(by_agent["a-owned"]) == 4, "an agent the caller OWNS keeps every row"


def test_the_fleet_narrowing_applies_before_the_limit(ops):
    sched, _ = ops
    _fleet_seed()
    out = sched.get_fleet_executions(
        ["a-shared"], hours=0, limit=2,
        own_rows_user_id=ME, own_rows_exempt_agents=["a-owned"])
    assert {r["id"] for r in out} == {"e00001", "e00002"}


def test_search_cannot_reach_another_persons_text_on_a_shared_agent(ops):
    """`search` is a LIKE on the prompt text, so it is the leak amplifier."""
    sched, _ = ops
    _fleet_seed()
    out = sched.get_fleet_executions(
        ["a-shared", "a-owned"], hours=0, search="secret",
        own_rows_user_id=ME, own_rows_exempt_agents=["a-owned"])
    assert {r["agent_name"] for r in out} == {"a-owned"}, (
        "'secret' matches another user's prompts on the shared agent; only the "
        "agent the caller owns may answer it — the OR clause must not outrank AND")
    assert out and all("secret" in r["message"] for r in out), (
        "the search must still apply on the agent the caller owns: 'ordinary on "
        "owned' does not match it")


def test_own_rows_on_every_agent_when_nothing_is_exempt(ops):
    sched, _ = ops
    _fleet_seed()
    out = sched.get_fleet_executions(
        ["a-shared", "a-owned"], hours=0, own_rows_user_id=ME, own_rows_exempt_agents=None)
    assert {r["source_user_id"] for r in out} == {ME}


def test_an_agent_missing_from_the_exempt_list_is_narrowed_not_shown_in_full(ops):
    """The failure direction: narrowing is the default and only a PROVABLY owned
    agent escapes it. If the owned list is empty or stale, the caller sees too
    few rows, never everyone's (found in review: the first design listed the
    agents to narrow, so a gap in that list failed open)."""
    sched, _ = ops
    _fleet_seed()
    out = sched.get_fleet_executions(
        ["a-shared", "a-owned"], hours=0, own_rows_user_id=ME, own_rows_exempt_agents=[])
    assert {r["source_user_id"] for r in out} == {ME}
    assert "a-owned" not in {r["agent_name"] for r in out}


def test_the_fleet_list_is_unchanged_without_the_new_arguments(ops):
    sched, _ = ops
    _fleet_seed()
    assert len(sched.get_fleet_executions(["a-shared", "a-owned"], hours=0)) == 13


def test_a_no_rows_user_id_matches_nothing(ops):
    from dependencies import NO_ROWS_USER_ID
    sched, _ = ops
    _fleet_seed()
    assert sched.get_fleet_executions(
        ["a-shared"], hours=0, own_rows_user_id=NO_ROWS_USER_ID, own_rows_exempt_agents=None) == []


# ---- the activity timeline ----------------------------------------------------------------

def _activity(i, uid, agent):
    return {
        "id": f"act{i}", "agent_name": agent, "activity_type": "chat_start",
        "activity_state": "completed", "user_id": uid,
        "started_at": f"2026-10-16T{i:05d}Z", "created_at": f"2026-10-16T{i:05d}Z",
        "details": '{"message_preview": "prompt %d"}' % i,
    }


def test_the_timeline_narrows_a_shared_agent_before_the_limit(ops):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_activities
    _, act = ops
    rows = [_activity(1, ME, "a-shared"), _activity(2, ME, "a-shared")]
    rows += [_activity(10 + i, OTHER, "a-shared") for i in range(8)]
    rows += [_activity(30, OTHER, "a-owned"), _activity(31, None, "a-shared")]
    with get_engine().begin() as conn:
        conn.execute(insert(agent_activities), rows)
    out = act.get_activities_in_range(
        limit=5, agent_names=["a-shared", "a-owned"],
        own_rows_user_id=ME, own_rows_exempt_agents=["a-owned"])
    got = {(r["agent_name"], r["user_id"]) for r in out}
    assert ("a-shared", OTHER) not in got and ("a-shared", None) not in got
    assert ("a-shared", ME) in got and ("a-owned", OTHER) in got
    unfiltered = act.get_activities_in_range(limit=100, agent_names=["a-shared", "a-owned"])
    assert len(unfiltered) == 12
