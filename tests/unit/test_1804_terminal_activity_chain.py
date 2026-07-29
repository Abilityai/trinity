"""#1804 chain tests — the execution row and its dispatch activity must AGREE,
proven against real SQL rather than mocks.

The whole fix rests on two compare-and-set predicates in two different tables
lining up (``db/schedules/executions.py::update_execution_status`` and
``db/activities.py::_close_predicate``), and on a lookup that can actually reach
the row the write is allowed to touch. Mocks cannot show that: they encode
whatever the author believed. These tests run the real statements against the
real schema (db_harness, #300 — SQLite always, PostgreSQL when
``TEST_POSTGRES_URL`` is set) and assert the two tables never disagree.

Covers R1 (late SUCCESS upgrades a reaper-FAILED activity, end to end) and R3
(a second closer never clobbers a real duration).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    from db.activities import ActivityOperations
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock()), ActivityOperations()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_running_with_activity(exec_id: str, act_id: str, *, age_seconds: int = 7_600):
    """A `running` execution plus the open dispatch activity execute_task opens."""
    started = _iso(datetime.now(timezone.utc) - timedelta(seconds=age_seconds))
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by, "
        " claude_session_id) "
        "VALUES (:id, '__manual__', 'test-agent', 'running', :sa, 'msg', 'schedule', "
        " 'dispatched')",
        id=exec_id, sa=started,
    )
    _hrun(
        "INSERT INTO agent_activities "
        "(id, agent_name, activity_type, activity_state, started_at, triggered_by, "
        " related_execution_id, created_at) "
        "VALUES (:id, 'test-agent', 'chat_start', 'started', :sa, 'schedule', :eid, :sa)",
        id=act_id, sa=started, eid=exec_id,
    )
    return started


def _exec_row(exec_id: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, duration_ms, completed_at FROM schedule_executions "
                "WHERE id = :id"
            ),
            {"id": exec_id},
        ).mappings().first()
    return dict(row)


def _act_row(act_id: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT activity_state, duration_ms, completed_at, error "
                "FROM agent_activities WHERE id = :id"
            ),
            {"id": act_id},
        ).mappings().first()
    return dict(row)


@pytest.mark.unit
class TestBulkSweepChain:
    def test_stale_sweep_leaves_both_tables_terminal(self, tmp_db, ops):
        """The #1804 headline: after the bulk watchdog sweep the execution is
        `failed` AND its activity is closed — no row left `started` for the
        120-minute duration-fabricating backstop."""
        schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-chain", "act-chain")

        failed_rows: list = []
        count = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts=None, buffer_seconds=300, collect_failed=failed_rows
        )
        assert count == 1
        # Exactly the rows #1714 already collects — no new query, no second pass.
        closed = activity_ops.close_open_activities_for_executions(
            [eid for eid, _agent in failed_rows], "failed", error="swept as stale"
        )

        assert closed == 1
        assert _exec_row("exec-chain")["status"] == "failed"
        assert _act_row("act-chain")["activity_state"] == "failed"

    def test_sweep_that_loses_its_cas_closes_no_activity(self, tmp_db, ops):
        """A SUCCESS that landed between the SELECT and the guarded UPDATE keeps
        the row — and, because `collect_failed` carries only CAS-won rows, keeps
        its activity too."""
        schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-won", "act-won")
        _hrun(
            "UPDATE schedule_executions SET status = 'success' WHERE id = 'exec-won'"
        )

        failed_rows: list = []
        count = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts=None, buffer_seconds=300, collect_failed=failed_rows
        )

        assert count == 0
        assert failed_rows == []
        assert activity_ops.close_open_activities_for_executions([], "failed") == 0
        assert _act_row("act-won")["activity_state"] == "started"


@pytest.mark.unit
class TestLateSuccessUpgradeChain:
    def test_r1_late_success_upgrades_the_reaper_failed_activity(self, tmp_db, ops):
        """[R1] End-to-end, through the real lookup and both real CAS predicates.

        Sequence: the lease reaper FAILs a wedged execution and closes its
        activity FAILED; the worker's genuine SUCCESS then arrives late. The
        execution CAS lets it through (a FAILED row is not CANCELLED —
        pull_coordination_service relies on this: "a FAILED row falls through so
        a late SUCCESS can still correct it"). The activity must follow, or the
        pair is left permanently `execution=success, activity=failed` — #1804
        inverted, and exactly what a lookup narrower than the CAS would produce.
        """
        from models import ActivityState, TaskExecutionStatus

        schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-late", "act-late")

        # 1. Reaper: FAIL the row, close the activity FAILED.
        assert schedule_ops.update_execution_status(
            execution_id="exec-late",
            status=TaskExecutionStatus.FAILED,
            error="lease_expired: slot lease expired (no result callback)",
        ) is True
        assert activity_ops.complete_activity(
            "act-late", ActivityState.FAILED, error="lease_expired"
        ).name == "UPDATED"
        assert _act_row("act-late")["activity_state"] == "failed"

        # 2. Late SUCCESS wins the execution CAS.
        assert schedule_ops.update_execution_status(
            execution_id="exec-late",
            status=TaskExecutionStatus.SUCCESS,
            response="done",
        ) is True

        # 3. The close must FIND the failed activity (authoritative lookup) and
        #    be ALLOWED to upgrade it (lattice predicate).
        act_id = activity_ops.get_open_activity_id_for_execution(
            "exec-late", include_failed=True
        )
        assert act_id == "act-late"
        assert activity_ops.complete_activity(
            act_id, ActivityState.COMPLETED
        ).name == "UPDATED"

        assert _exec_row("exec-late")["status"] == "success"
        assert _act_row("act-late")["activity_state"] == "completed"

    def test_a_cancel_is_never_overwritten_by_a_late_success(self, tmp_db, ops):
        """The mirror-image guarantee, on both tables: the execution CAS blocks a
        SUCCESS over a CANCELLED row (#671), and the activity CAS blocks a
        COMPLETED over a CANCELLED activity (#1332)."""
        from models import ActivityState, TaskExecutionStatus

        schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-cxl", "act-cxl")

        assert schedule_ops.update_execution_status(
            execution_id="exec-cxl",
            status=TaskExecutionStatus.CANCELLED,
            error="Execution terminated by user",
        ) is True
        assert activity_ops.complete_activity(
            "act-cxl", ActivityState.CANCELLED, error="Execution terminated by user"
        ).name == "UPDATED"

        assert schedule_ops.update_execution_status(
            execution_id="exec-cxl",
            status=TaskExecutionStatus.SUCCESS,
            response="too late",
        ) is False
        assert activity_ops.complete_activity(
            "act-cxl", ActivityState.COMPLETED
        ).name == "ALREADY_CLOSED"

        assert _exec_row("exec-cxl")["status"] == "cancelled"
        assert _act_row("act-cxl")["activity_state"] == "cancelled"


@pytest.mark.unit
class TestDoubleCloseChain:
    def test_r3_second_closer_never_clobbers_a_real_duration(self, tmp_db, ops):
        """[R2/R3] The race the close CAS exists for: the terminate path closes
        the activity, then a losing in-process writer closes it again. Before the
        CAS the second UPDATE overwrote completed_at/duration_ms/error — the
        wrong-duration symptom reintroduced by a new route."""
        from models import ActivityState

        _schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-double", "act-double", age_seconds=900)

        assert activity_ops.complete_activity(
            "act-double", ActivityState.CANCELLED, error="Execution terminated by user"
        ).name == "UPDATED"
        first = _act_row("act-double")
        assert first["duration_ms"] is not None

        # The losing writer's reconcile close, arriving second.
        assert activity_ops.complete_activity(
            "act-double", ActivityState.FAILED, error="superseded by cancelled"
        ).name == "ALREADY_CLOSED"

        assert _act_row("act-double") == first

    def test_bulk_close_after_a_single_close_is_a_noop(self, tmp_db, ops):
        """The bulk path shares the same predicate, so a row an individual writer
        already closed is skipped rather than re-dated."""
        from models import ActivityState

        _schedule_ops, activity_ops = ops
        _seed_running_with_activity("exec-mix", "act-mix", age_seconds=600)

        activity_ops.complete_activity("act-mix", ActivityState.COMPLETED)
        before = _act_row("act-mix")

        assert activity_ops.close_open_activities_for_executions(
            ["exec-mix"], "failed", error="swept as stale"
        ) == 0
        assert _act_row("act-mix") == before
