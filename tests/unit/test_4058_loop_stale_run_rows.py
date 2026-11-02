"""#4058 — run rows duplicated before the #3317 fix no longer strand reconcile.

Before #4023 the reconcile-versus-advance race could insert two
`agent_loop_runs` rows with one `(loop_id, run_number)`. A duplicate whose
terminal arrives after the loop moved past its number is refused by the #3316
repair and stays `running`. `rearm_lost_dispatch` took the first `running` row
by run number as the loop's open run, so reconcile kept retrying that stale row
and missed a newer open run whose terminal was lost, or never re-armed at all.

Pinned here:

* `stale_running_run_ids` (the one decision, `db/loop_run_hygiene.py`);
* `rearm_lost_dispatch` returns the real open run past a stale twin, and
  re-arms a loop whose only `running` row is stale;
* reconcile, through `LoopService`, advances the real open run;
* the one-time cleanup (`run_loop_runs_stale_cleanup`, both tracks) closes the
  stale rows `abandoned`: the issue's two audit queries return nothing after it,
  `failed_runs` does not move, and a second run closes nothing.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_harness import db_backend, sqlite_only_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

_AUDIT_DUPLICATES = (
    "SELECT loop_id, run_number, count(*) FROM agent_loop_runs "
    "GROUP BY loop_id, run_number HAVING count(*) > 1"
)
_AUDIT_RUNNING_UNDER_ENDED_LOOPS = (
    "SELECT r.id, r.loop_id, r.run_number FROM agent_loop_runs r "
    "JOIN agent_loops l ON l.id = r.loop_id "
    "WHERE r.status = 'running' "
    "AND l.status IN ('completed', 'completed_with_errors', 'stopped', 'failed', 'interrupted')"
)


# ---------------------------------------------------------------------------
# 1. The decision
# ---------------------------------------------------------------------------

def _stale(runs, *, runs_completed, terminal=False):
    from db.loop_run_hygiene import stale_running_run_ids
    return stale_running_run_ids(loop_is_terminal=terminal, runs_completed=runs_completed,
                                 runs=runs)


def _r(id_, number, status="running", started="2026-10-01T00:00:00.000000Z"):
    return {"id": id_, "run_number": number, "status": status, "started_at": started}


def test_the_run_in_flight_and_a_3316_unclosed_run_are_never_stale():
    assert _stale([_r("a", 1, "completed"), _r("b", 2)], runs_completed=1) == set()
    assert _stale([_r("a", 1, "completed"), _r("b", 2)], runs_completed=2) == set()


def test_a_running_twin_of_a_closed_run_is_stale():
    runs = [_r("a", 1, "completed"), _r("dup", 1), _r("b", 2)]
    assert _stale(runs, runs_completed=1) == {"dup"}


def test_a_running_row_below_runs_completed_is_stale():
    assert _stale([_r("old", 1), _r("b", 3)], runs_completed=2) == {"old"}


def test_of_two_running_twins_the_earliest_stays_open():
    runs = [_r("late", 2, started="2026-10-01T00:00:02.000000Z"),
            _r("early", 2, started="2026-10-01T00:00:01.000000Z")]
    assert _stale(runs, runs_completed=1) == {"late"}


def test_every_running_row_of_an_ended_loop_is_stale():
    runs = [_r("a", 1, "completed"), _r("b", 2)]
    assert _stale(runs, runs_completed=1, terminal=True) == {"b"}


# ---------------------------------------------------------------------------
# 2. The runtime choice, against the real SQL
# ---------------------------------------------------------------------------

def _ops():
    from db.loops import LoopOperations
    return LoopOperations()


def _insert_stale_twin(loop_id, run_number, execution_id, *, started="2000-01-01T00:00:00.000000Z"):
    """What the pre-#4023 race left behind: a second `running` row for a run
    number. Raw SQL, because `start_loop_run` (the only production inserter)
    refuses it now."""
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_loop_runs (id, loop_id, run_number, execution_id, status, started_at) "
            "VALUES (:id, :loop_id, :n, :e, 'running', :s)"),
            {"id": f"lr_dup_{execution_id}", "loop_id": loop_id, "n": run_number,
             "e": execution_id, "s": started})
    return f"lr_dup_{execution_id}"


def _loop_with_stale_twin_and_run2_open():
    """Run 1 closed, a stale running twin of run 1, run 2 in flight."""
    ops = _ops()
    lid = ops.create_loop(agent_name="a4058", message_template="m", max_runs=5)["id"]
    ops.mark_loop_running(lid)
    rid = ops.start_loop_run(lid, 1, execution_id="e_run1")
    assert ops.claim_loop_advance(lid, 1)
    assert ops.finalize_loop_run(rid, status="completed", response="r", error=None,
                                 cost=0.0, duration_ms=1)
    dup = _insert_stale_twin(lid, 1, "e_run1_dup")
    assert ops.start_loop_run(lid, 2, execution_id="e_run2") is not None
    return lid, dup


def _never(_loop, _runs):
    raise AssertionError("due_for called while a run is open")


def test_rearm_returns_the_real_open_run_past_a_stale_twin(db_backend):  # noqa: F811
    lid, _dup = _loop_with_stale_twin_and_run2_open()

    outcome, run = _ops().rearm_lost_dispatch(lid, due_for=_never)

    assert outcome == "open_run"
    assert (run["run_number"], run["execution_id"]) == (2, "e_run2")


def test_a_loop_whose_only_running_row_is_stale_is_re_armed(db_backend):  # noqa: F811
    ops = _ops()
    lid, _dup = _loop_with_stale_twin_and_run2_open()
    [run2] = [r for r in ops.list_runs(lid) if r["run_number"] == 2]
    assert ops.claim_loop_advance(lid, 2)
    assert ops.finalize_loop_run(run2["id"], status="completed", response="r", error=None,
                                 cost=0.0, duration_ms=1)

    outcome, run = ops.rearm_lost_dispatch(lid, due_for=lambda loop, runs: "2000-01-01T00:00:00.000000Z")

    assert (outcome, run) == ("armed", None), "the stale twin kept the loop from re-arming"


def test_reconcile_advances_the_real_open_run_whose_terminal_was_lost(db_backend, monkeypatch):  # noqa: F811
    from database import DatabaseManager
    from db.loops import LoopOperations
    from services import loop_service as ls

    facade = object.__new__(DatabaseManager)
    facade._loop_ops = LoopOperations()
    monkeypatch.setattr(ls, "db", facade)
    lid, _dup = _loop_with_stale_twin_and_run2_open()

    advanced = []
    svc = ls.LoopService()
    monkeypatch.setattr(ls.LoopService, "_execution_is_live", staticmethod(lambda _e: False))

    async def _advance(execution_id):
        advanced.append(execution_id)
        return True

    monkeypatch.setattr(svc, "advance_on_terminal", _advance)

    assert asyncio.run(svc.reconcile_after_restart()) == 1
    assert advanced == ["e_run2"], "reconcile retried the stale twin instead of the real open run"


# ---------------------------------------------------------------------------
# 3. The one-time cleanup (the SQLite and Alembic halves share this runner)
# ---------------------------------------------------------------------------

def _run_adapter():
    from sqlalchemy import text
    from db.engine import get_engine

    def _run(sql, params, fetch=False):
        with get_engine().begin() as conn:
            result = conn.execute(text(sql), params)
            return result.fetchall() if fetch else None
    return _run


def _audit(sql):
    return _run_adapter()(sql, {}, fetch=True)


def test_the_cleanup_closes_every_stale_row_and_nothing_else(db_backend):  # noqa: F811
    from db.loop_run_hygiene import ABANDONED
    from db.migrations import run_loop_runs_stale_cleanup
    from services.loop_service import _DerivedState

    ops = _ops()
    lid, dup = _loop_with_stale_twin_and_run2_open()
    ended = ops.create_loop(agent_name="a4058e", message_template="m", max_runs=5)["id"]
    ops.mark_loop_running(ended)
    orphan = ops.start_loop_run(ended, 1, execution_id="e_ended_run1")
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE agent_loops SET status = 'stopped' WHERE id = :id"), {"id": ended})
    failed_before = _DerivedState(ops.list_runs(lid)).failed_runs

    assert _audit(_AUDIT_DUPLICATES) and _audit(_AUDIT_RUNNING_UNDER_ENDED_LOOPS), "control"

    closed = run_loop_runs_stale_cleanup(_run_adapter(), now_iso="2026-10-29T00:00:00.000000Z")

    assert closed == 2
    by_id = {r["id"]: r for r in ops.list_runs(lid) + ops.list_runs(ended)}
    assert by_id[dup]["status"] == ABANDONED and by_id[orphan]["status"] == ABANDONED
    assert [r["status"] for r in ops.list_runs(lid) if r["run_number"] == 2] == ["running"], (
        "the run in flight was closed"
    )
    assert _DerivedState(ops.list_runs(lid)).failed_runs == failed_before
    # The issue's audit: no running duplicate is left, and no ended loop holds a
    # running row. (The closed twin stays as history, so count only running ones.)
    assert not [row for row in _audit(_AUDIT_DUPLICATES.replace(
        "FROM agent_loop_runs", "FROM agent_loop_runs WHERE status = 'running'"))]
    assert _audit(_AUDIT_RUNNING_UNDER_ENDED_LOOPS) == []
    assert run_loop_runs_stale_cleanup(_run_adapter(), now_iso="2026-10-29T00:00:01.000000Z") == 0


def test_the_sqlite_migration_is_registered():
    from db import migrations

    assert "loop_runs_stale_duplicates" in [name for name, _ in migrations.MIGRATIONS]


def test_the_sqlite_migration_closes_the_stale_rows_through_its_cursor(sqlite_only_backend):  # noqa: F811
    """The SQLite half drives the shared runner through a raw `sqlite3` cursor
    (named parameters, `fetchall` tuples), not the SQLAlchemy adapter above."""
    from db.engine import get_engine
    from db.loop_run_hygiene import ABANDONED
    from db.migrations import _migrate_loop_runs_stale_duplicates

    ops = _ops()
    lid, dup = _loop_with_stale_twin_and_run2_open()
    raw = get_engine().raw_connection()
    try:
        _migrate_loop_runs_stale_duplicates(raw.cursor(), raw)
    finally:
        raw.close()

    statuses = {r["id"]: r["status"] for r in ops.list_runs(lid)}
    assert statuses[dup] == ABANDONED
    assert sorted(statuses.values()) == sorted([ABANDONED, "completed", "running"])
