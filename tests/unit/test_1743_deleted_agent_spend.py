"""Fleet totals stay reconcilable with the per-agent surfaces (#1743).

`GET /api/executions/stats` counts every accessible execution row. Those rows
deliberately outlive their agent — cost is billing truth and a soft-deleted agent
is recoverable — but `GET /api/agents/execution-stats` (the Dashboard tiles)
renders only live agents. So the fleet cards and the sum of the tiles differed by
the deleted agents' share, with nothing on either surface explaining the gap.

Measured on a dev instance before the fix (24h window): fleet 16 executions /
$0.8933 versus 12 / $0.6291 across the tiles — 4 executions and $0.2642, 30% of
the window's spend, attributable to nothing.

The fix reports the difference rather than hiding it: `deleted_agent_count` /
`deleted_agent_cost` are the slice belonging to no live agent, so
`total - deleted_agent_count` reconciles with the tiles by subtraction and the
spend stays visible.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def stats_db(tmp_path, monkeypatch):
    """Sqlite with schedule_executions + agent_ownership, seeded with a live
    agent, a soft-deleted one, and a hard-purged one."""
    db_file = tmp_path / "trinity-stats.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, schedule_executions, agent_ownership, users
    m.create_all(get_engine(), tables=[schedule_executions, agent_ownership, users])

    from sqlalchemy import insert
    from utils.helpers import utc_now_iso

    now = utc_now_iso()
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          created_at="t", updated_at="t"))
        # live agent, soft-deleted agent — and "ghost" gets NO ownership row at
        # all, standing in for a hard-purged one.
        conn.execute(insert(agent_ownership).values(
            agent_name="live-agent", owner_id=1, created_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="gone-agent", owner_id=1, created_at="t", deleted_at=now))

        def _exec(eid, agent, status, cost):
            conn.execute(insert(schedule_executions).values(
                id=eid, schedule_id="__manual__", agent_name=agent, status=status,
                started_at=now, completed_at=now, message="m",
                triggered_by="manual", cost=cost, duration_ms=100))

        _exec("e1", "live-agent", "success", 1.0)
        _exec("e2", "live-agent", "failed", 0.5)
        _exec("e3", "gone-agent", "success", 2.0)      # soft-deleted
        _exec("e4", "ghost", "success", 4.0)           # no ownership row at all
    yield str(db_file)


def _stats(agent_names=None, hours=0):
    from database import db
    return db.get_fleet_execution_stats(agent_names, hours=hours)


def test_deleted_agent_slice_is_reported(stats_db):
    s = _stats()
    assert s["total"] == 4
    assert s["total_cost"] == pytest.approx(7.5)
    # e3 (soft-deleted) + e4 (purged)
    assert s["deleted_agent_count"] == 2
    assert s["deleted_agent_cost"] == pytest.approx(6.0)


def test_totals_reconcile_with_the_live_agent_view(stats_db):
    """THE invariant: fleet total minus the deleted slice is what the per-agent
    surfaces can actually account for."""
    s = _stats()
    assert s["total"] - s["deleted_agent_count"] == 2          # e1 + e2
    assert s["total_cost"] - s["deleted_agent_cost"] == pytest.approx(1.5)


def test_soft_deleted_and_purged_are_both_counted(stats_db):
    """A soft-deleted agent (row with deleted_at) and a purged one (no row) are
    equally invisible to the tiles, so both belong in the slice — the LEFT JOIN
    is on `deleted_at IS NULL` precisely so they collapse together."""
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM schedule_executions WHERE id = 'e4'"))
    soft_only = _stats()
    assert soft_only["deleted_agent_count"] == 1               # the soft-deleted one
    assert soft_only["deleted_agent_cost"] == pytest.approx(2.0)


def test_no_deleted_agents_reports_zero(stats_db):
    """A clean fleet reports 0/0 so the UI line stays hidden — the explanation
    only appears when there is something to explain."""
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM schedule_executions WHERE agent_name IN ('gone-agent','ghost')"))
    s = _stats()
    assert s["deleted_agent_count"] == 0 and s["deleted_agent_cost"] == 0.0
    assert s["total"] == 2


def test_agent_filtered_stats_still_split(stats_db):
    """A non-admin caller passes an explicit agent list; the split must still be
    computed over exactly that set (the JOIN must not bypass the filter)."""
    s = _stats(agent_names=["live-agent"])
    assert s["total"] == 2 and s["deleted_agent_count"] == 0
    s2 = _stats(agent_names=["gone-agent"])
    assert s2["total"] == 1 and s2["deleted_agent_count"] == 1


def test_empty_accessible_set_short_circuits_with_the_new_fields(stats_db):
    """The early-return path for a caller with no accessible agents must still
    carry the fields, or the response model loses them for that caller."""
    s = _stats(agent_names=[])
    assert s["deleted_agent_count"] == 0 and s["deleted_agent_cost"] == 0.0


def test_windowed_query_splits_correctly(stats_db):
    """The split rides the same time window as the totals it explains."""
    s = _stats(hours=24)
    assert s["total"] == 4 and s["deleted_agent_count"] == 2
    assert s["total_cost"] - s["deleted_agent_cost"] == pytest.approx(1.5)


def test_response_model_exposes_the_fields():
    """Additive with defaults, so a client predating the field still validates."""
    from models import FleetExecutionStats

    m = FleetExecutionStats(total=0, success_count=0, failed_count=0,
                            running_count=0, queued_count=0, total_cost=0.0,
                            success_rate=0.0, hours=24)
    assert m.deleted_agent_count == 0 and m.deleted_agent_cost == 0.0
    full = FleetExecutionStats(total=4, success_count=3, failed_count=1,
                               running_count=0, queued_count=0, total_cost=7.5,
                               success_rate=75.0, hours=24,
                               deleted_agent_count=2, deleted_agent_cost=6.0)
    assert full.deleted_agent_cost == 6.0
