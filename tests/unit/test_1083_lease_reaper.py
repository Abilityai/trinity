"""Unit tests for the #1083 lease reaper + stale-sweep buffer fix (PR2.G/H).

Two pieces:

1. ``mark_stale_executions_failed`` per-agent ``timeout + buffer`` window
   (Finding 1) — REGRESSION boundary: a running row at ``timeout+buffer-ε``
   SURVIVES; at ``+ε`` it is SWEPT. Plus legacy flat behaviour when no per-agent
   map is supplied.
2. ``CleanupService._close_stale_slot_activity`` — closes the open dispatch
   activity (via the filtered lookup) the absent fire-and-forget coroutine
   ``finally`` would have closed; no-op when none is open.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun  # noqa: E402,F401


# ===========================================================================
# Part 1 — stale-sweep per-agent buffer (real DB)
# ===========================================================================
@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _insert_running(*, exec_id: str, agent: str, age_seconds: int):
    started = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES (:id, '__manual__', :agent, 'running', :sa, 'msg', 'schedule')",
        id=exec_id, agent=agent, sa=started,
    )


def _status(exec_id: str):
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT status FROM schedule_executions WHERE id = :id"),
            {"id": exec_id},
        ).scalar()


class TestStaleSweepPerAgentBuffer:
    pytestmark = pytest.mark.unit

    # agent timeout 300s + buffer 300s ⇒ effective window 600s.
    TIMEOUT_S = 300
    BUFFER_S = 300
    EFFECTIVE = 600

    def test_row_just_under_window_survives(self, tmp_db, schedule_ops):
        """REGRESSION (Finding 1): a row at timeout+buffer-ε must NOT be swept."""
        _insert_running(exec_id="ex-survive", agent="ag1", age_seconds=self.EFFECTIVE - 30)
        failed = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts={"ag1": self.TIMEOUT_S}, buffer_seconds=self.BUFFER_S
        )
        assert failed == 0
        assert _status("ex-survive") == "running"

    def test_row_just_over_window_is_swept(self, tmp_db, schedule_ops):
        """A row at timeout+buffer+ε IS swept."""
        _insert_running(exec_id="ex-sweep", agent="ag1", age_seconds=self.EFFECTIVE + 30)
        failed = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts={"ag1": self.TIMEOUT_S}, buffer_seconds=self.BUFFER_S
        )
        assert failed == 1
        assert _status("ex-sweep") == "failed"

    def test_max_timeout_agent_not_failed_early(self, tmp_db, schedule_ops):
        """The original bug: a 7200s-timeout agent's turn at ~120min (the old
        flat window) but < timeout+buffer must survive."""
        # 7200s timeout + 300 buffer = 7500s window. A turn at 7300s (past the
        # old flat 7200s window, under the new 7500s window) must survive.
        _insert_running(exec_id="ex-maxto", agent="big", age_seconds=7300)
        failed = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts={"big": 7200}, buffer_seconds=300
        )
        assert failed == 0
        assert _status("ex-maxto") == "running"

    def test_legacy_flat_behaviour_when_no_map(self, tmp_db, schedule_ops):
        """agent_timeouts=None reproduces the prior flat timeout_minutes window."""
        # 5-min flat window; a 10-min-old row is swept, a 2-min-old row survives.
        _insert_running(exec_id="ex-old", agent="x", age_seconds=600)
        _insert_running(exec_id="ex-young", agent="x", age_seconds=120)
        failed = schedule_ops.mark_stale_executions_failed(5)  # 5 min = 300s
        assert failed == 1
        assert _status("ex-old") == "failed"
        assert _status("ex-young") == "running"

    def test_unknown_agent_uses_flat_fallback(self, tmp_db, schedule_ops):
        """A row whose agent is absent from the map uses the flat fallback
        window (conservative — never fails early)."""
        # Map covers only 'known'; 'ghost' (soft-deleted) falls back to 120min.
        _insert_running(exec_id="ex-ghost", agent="ghost", age_seconds=700)
        failed = schedule_ops.mark_stale_executions_failed(
            120, agent_timeouts={"known": 300}, buffer_seconds=300
        )
        # 700s < 7200s flat fallback ⇒ survives.
        assert failed == 0
        assert _status("ex-ghost") == "running"


# ===========================================================================
# Part 2 — activity close on lease expiry (mocked)
# ===========================================================================
def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCloseStaleSlotActivity:
    pytestmark = pytest.mark.unit

    def _service(self):
        from services.cleanup_service import CleanupService

        return CleanupService(poll_interval=300)

    def test_closes_open_activity_as_failed(self):
        """#1804: delegates to the shared owner, which does the filtered lookup
        and the lattice close CAS. The FAILED terminal + lease_expired tag are
        byte-identical to the pre-#1804 close."""
        from models import TaskExecutionStatus

        svc = self._service()
        mock_db = MagicMock()
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        with (
            patch("services.cleanup_service.db", mock_db),
            patch("services.activity_service.activity_service", mock_activity),
        ):
            _await(svc._close_stale_slot_activity("exec-1"))
        mock_activity.close_execution_activity.assert_awaited_once()
        args, kwargs = mock_activity.close_execution_activity.await_args
        assert args[0] == "exec-1"
        assert args[1] == TaskExecutionStatus.FAILED
        assert "lease_expired" in kwargs["error"]

    def test_requeued_activity_closes_cancelled_not_failed(self):
        """#1804: a lease re-delivery SUPERSEDES the dead worker's attempt — the
        superseded activity closes CANCELLED so activity-derived views don't
        record a failure that never happened (#1332)."""
        from models import TaskExecutionStatus

        svc = self._service()
        mock_db = MagicMock()
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        with (
            patch("services.cleanup_service.db", mock_db),
            patch("services.activity_service.activity_service", mock_activity),
        ):
            _await(svc._close_requeued_activity("exec-1"))
        args, kwargs = mock_activity.close_execution_activity.await_args
        assert args[1] == TaskExecutionStatus.CANCELLED
        assert "superseded" in kwargs["error"]

    def test_swallows_errors(self):
        """The real (unmocked) helper is fail-open — a raising db never escapes."""
        svc = self._service()
        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.side_effect = RuntimeError("boom")
        with (
            patch("services.cleanup_service.db", mock_db),
            patch("services.activity_service.db", mock_db),
        ):
            # Must not raise.
            _await(svc._close_stale_slot_activity("exec-1"))
