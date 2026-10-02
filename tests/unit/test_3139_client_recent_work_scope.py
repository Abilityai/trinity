"""#3139 — an external client's agent page listed OTHER people's runs.

The Workspace agent page projected away the prompt, the cost and the model of
every row, but never scoped the ROWS to the viewer: a client sharing an agent
with someone else received that person's run ids, triggers, start and end
times and durations (10 of 12 rows in the ent#610 review walk).

The fix scopes, in SQL and before the LIMIT, to the work a client can account
for: their own turns (`source_user_email` or the inherited
`source_channel_client`) plus the agent's scheduled runs. The same scope feeds
the stats band, the first-try rate and "last active", so the numbers can never
describe rows the list withholds. The platform view passes no viewer and is
unchanged.

Real SQLite through the backend's own engine and accessors, not stubs: the
scope IS a WHERE clause, and only SQL can show it precedes the LIMIT and the
aggregates (the #2423 lesson, `test_2423_executions_summary_exclude.py`).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

ALICE = "alice@client.test"
BOB = "bob@client.test"


@pytest.fixture
def ops(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from db.tables import schedule_executions, agent_schedules
    for t in (schedule_executions, agent_schedules):
        conn.execute(str(CreateTable(t).compile(dialect=sqlite_dialect.dialect())))
    conn.commit()
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


@pytest.fixture
def page(ops, monkeypatch):
    """The real page module, its `db` pointed at the real accessors above."""
    from client_portal import agent_page as m

    class _Db:
        def get_agent_executions_summary(self, *a, **kw):
            return ops.get_agent_executions_summary(*a, **kw)

        def get_agent_analytics(self, *a, **kw):
            return ops.get_agent_analytics(*a, **kw)

        def get_agent_schedule_names(self, agent_name):
            return {}

    monkeypatch.setattr(m, "db", _Db(), raising=False)
    return m


def _ts(minutes_ago: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(rid, minutes_ago, trigger, *, email=None, client=None, status="success",
         retry_count=0, agent="a1", schedule_id="__manual__"):
    return {
        "id": rid, "schedule_id": schedule_id, "agent_name": agent,
        "status": status, "started_at": _ts(minutes_ago), "completed_at": _ts(minutes_ago - 1),
        "duration_ms": 60000, "message": "secret prompt", "triggered_by": trigger,
        "source_user_email": email, "source_channel_client": client,
        "retry_count": retry_count,
    }


def _insert(rows):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions), rows)


@pytest.fixture
def shared_agent():
    """One agent, two clients, plus the operator's and the schedule's work."""
    _insert([
        _row("alice-1", 50, "chat", email=ALICE, client=ALICE),
        _row("alice-2", 40, "chat", email=ALICE, client=ALICE, status="failed"),
        # A follow-up run Alice's turn spawned: no user email, the client inherited.
        _row("alice-child", 39, "agent", client=ALICE),
        _row("bob-1", 30, "chat", email=BOB, client=BOB),
        _row("bob-2", 20, "chat", email=BOB, client=BOB),
        _row("sched-1", 15, "schedule", retry_count=1),
        _row("op-manual", 10, "manual", email="owner@company.test"),
        _row("op-mcp", 5, "mcp", email="owner@company.test"),
        _row("loop-1", 2, "loop", email="owner@company.test"),
    ])


def _ids(rows):
    return {r["id"] for r in rows}


# --- the list -------------------------------------------------------------------

def test_each_client_sees_only_their_own_turns_and_the_schedule(page, shared_agent):
    assert _ids(page._recent_work("a1", viewer_email=ALICE)) == {
        "alice-1", "alice-2", "alice-child", "sched-1"}
    assert _ids(page._recent_work("a1", viewer_email=BOB)) == {"bob-1", "bob-2", "sched-1"}


def test_build_page_scopes_a_client_and_not_the_platform(page, shared_agent):
    client = page.build_page(ALICE, "a1", None, is_platform=False)
    assert not {"bob-1", "bob-2", "op-manual", "op-mcp", "loop-1"} & _ids(client["recent_work"])
    platform = page.build_page("owner@company.test", "a1", None, is_platform=True)
    assert _ids(platform["recent_work"]) == {
        "alice-1", "alice-2", "alice-child", "bob-1", "bob-2",
        "sched-1", "op-manual", "op-mcp", "loop-1"}


def test_the_email_match_ignores_case(page, shared_agent):
    assert "alice-1" in _ids(page._recent_work("a1", viewer_email="Alice@Client.TEST"))


def test_the_scope_runs_before_the_limit(page, ops):
    """Twenty of Bob's newer turns must not push Alice's one turn off her list."""
    _insert([_row("alice-old", 100, "chat", email=ALICE, client=ALICE)]
            + [_row(f"bob-{i}", 50 - i, "chat", email=BOB, client=BOB) for i in range(30)])
    out = page._recent_work("a1", limit=20, viewer_email=ALICE)
    assert _ids(out) == {"alice-old"}


def test_last_active_is_the_viewers_newest_visible_row(page, shared_agent):
    alice = page._last_active("a1", viewer_email=ALICE)
    rows = page._recent_work("a1", viewer_email=ALICE)
    assert alice == max(r["started_at"] for r in rows)
    # The platform's newest row is the loop run two minutes ago, which Alice must not learn.
    assert page._last_active("a1", is_platform=True) > alice


def test_the_accessor_without_a_viewer_is_unchanged(ops, shared_agent):
    assert len(ops.get_agent_executions_summary("a1", limit=50)) == 9
    assert ops.get_agent_analytics("a1", 24)["total_executions"] == 9


# --- the stats band -------------------------------------------------------------

def test_a_clients_stats_count_exactly_the_rows_they_can_see(page, shared_agent):
    for viewer in (ALICE, BOB):
        rows = page._recent_work("a1", viewer_email=viewer)
        stats = page._stats("a1", "7d", viewer_email=viewer)
        assert stats["total_executions"] == len(rows)
        assert sum(day["total"] for day in stats["timeline"]) == len(rows)


def test_a_clients_rates_are_over_their_own_rows(page, shared_agent):
    # Alice: alice-1 ok, alice-2 failed, alice-child ok (first try), sched-1 ok on a retry.
    s = page._stats("a1", "7d", viewer_email=ALICE)
    assert s["success_rate"] == pytest.approx(3 / 4)
    assert s["first_try"] == {"terminal": 4, "first_try": 2, "rate": 2 / 4}


def test_the_platform_stats_are_unchanged(page, shared_agent):
    s = page._stats("a1", "7d", is_platform=True)
    assert s["total_executions"] == 9
    assert s["first_try"]["terminal"] == 9


# --- scheduled runs that belong to one person (cso finding on #3139) ----------

def _schedule(sid, deliver_to=None, agent="a1"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_schedules
    with get_engine().begin() as conn:
        conn.execute(insert(agent_schedules), [{
            "id": sid, "agent_name": agent, "name": f"brief {sid}", "cron_expression": "0 8 * * *",
            "message": "m", "enabled": 1, "timezone": "UTC",
            "deliver_to_workspace_email": deliver_to,
        }])


@pytest.fixture
def seat_briefs():
    """A shared nightly run, and a morning brief delivered to Bob's seat (#498)."""
    _schedule("s-shared")
    _schedule("s-bob", deliver_to="Bob@Client.test")
    _insert([
        _row("shared-run", 30, "schedule", schedule_id="s-shared"),
        _row("bob-brief", 20, "schedule", schedule_id="s-bob"),
    ])


def test_a_brief_delivered_to_one_person_is_theirs_alone(page, seat_briefs):
    assert _ids(page._recent_work("a1", viewer_email=ALICE)) == {"shared-run"}
    assert _ids(page._recent_work("a1", viewer_email=BOB)) == {"shared-run", "bob-brief"}


def test_the_stats_follow_the_same_rule(page, seat_briefs):
    assert page._stats("a1", "7d", viewer_email=ALICE)["total_executions"] == 1
    assert page._stats("a1", "7d", viewer_email=BOB)["total_executions"] == 2
    assert page._stats("a1", "7d", is_platform=True)["total_executions"] == 2


def test_a_viewerless_scope_admits_no_personal_brief(ops, seat_briefs):
    out = ops.get_agent_executions_summary("a1", limit=10, scope_to_viewer=True, viewer_email=None)
    assert _ids(out) == {"shared-run"}
