"""#3651 — once an execution row is terminal, none of its companion activities
(the caller's `agent_collaboration` row, a `self_task` row) stays `started`,
whichever writer wrote the terminal (the #1804 class, sixth occurrence after
#45, #767, #1804, #3589, #3588).

The repro is a direct-dispatch `/task` hop: `create_task_context` opens the
caller's collaboration row with `related_execution_id` = the execution and
writes NO `backlog_metadata`, so a closer that finds companions through the
metadata finds nothing. Every out-of-band terminal writer below went through
that closer.

The invariant is a QUERY over the real activity table (`db_harness`, both
backends), never a call-shape mock: zero `started` rows of the in-scope types
for the execution. `tool_call` rows are out of scope on purpose — they share the
execution id and have their own closers (#45).

`services.*` are imported inside each test (the unit conftest pops them between
collection and test).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar, seed_execution, seed_schedule  # noqa: E402,F401

pytestmark = pytest.mark.unit

_T = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
_IN_SCOPE_TYPES = "'chat_start','schedule_start','agent_collaboration','self_task'"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _open_in_scope(eid: str) -> int:
    return _hscalar(
        "SELECT COUNT(*) FROM agent_activities WHERE related_execution_id = :e "
        f"AND activity_state = 'started' AND activity_type IN ({_IN_SCOPE_TYPES})",
        e=eid,
    )


def _state(aid: str) -> str:
    return _hscalar("SELECT activity_state FROM agent_activities WHERE id = :i", i=aid)


def _seed_exec(eid: str, status: str, *, completed=True, error=None) -> str:
    seed_schedule(f"sched-{eid}")
    seed_execution(
        f"sched-{eid}", "callee-a", exec_id=eid,
        started_at=_iso(_T - timedelta(seconds=90)),
        status=status, duration_ms=None, cost=None, triggered_by="agent",
    )
    _hrun(
        "UPDATE schedule_executions SET completed_at = :c, backlog_metadata = NULL, "
        "error = :err WHERE id = :i",
        c=_iso(_T) if completed else None, err=error, i=eid,
    )
    return eid


def _seed_activity(aid: str, type_: str, eid: str, *, state="started",
                   agent="caller-a", triggered_by="agent") -> str:
    _hrun(
        "INSERT INTO agent_activities (id, agent_name, activity_type, activity_state, "
        "started_at, related_execution_id, created_at, triggered_by, details) "
        "VALUES (:i, :a, :t, :s, :sa, :e, :sa, :tb, :d)",
        i=aid, a=agent, t=type_, s=state, sa=_iso(_T - timedelta(seconds=80)),
        e=eid, tb=triggered_by, d=json.dumps({"action": "parallel_task"}),
    )
    return aid


def _seed_direct_dispatch(eid: str, status: str, **kw) -> None:
    """A running `/task` hop as it sits when the writer fires: the callee's
    dispatch row plus the caller's collaboration row, no backlog_metadata."""
    _seed_exec(eid, status, **kw)
    _seed_activity(f"disp-{eid}", "chat_start", eid, agent="callee-a")
    _seed_activity(f"collab-{eid}", "agent_collaboration", eid)


def _fresh(monkeypatch):
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)


def _service():
    from services.cleanup_service import CleanupService

    svc = CleanupService(poll_interval=300)
    svc._broadcast_watchdog_event = AsyncMock()
    return svc


# ---------------------------------------------------------------------------
# Every out-of-band writer: terminal row => zero started companions
# ---------------------------------------------------------------------------

class TestRecoveryWritersCloseDirectDispatchCompanions:
    def test_watchdog_recover_execution(self, db_backend, monkeypatch):
        import services.cleanup_service as cs

        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r1", "running", completed=False)
        report = cs.CleanupReport()
        capacity = MagicMock(release_if_matches=AsyncMock())
        with patch.object(cs, "get_capacity_manager", return_value=capacity):
            ok = asyncio.run(_service()._recover_execution(
                "exec-r1", "callee-a", "orphaned", "orphan_recovered", None, report))
        assert ok is True
        assert _open_in_scope("exec-r1") == 0
        assert _state("collab-exec-r1") == "failed"

    def test_startup_recover_execution(self, db_backend, monkeypatch):
        import services.cleanup_service as cs

        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r2", "running", completed=False)
        capacity = MagicMock(release=AsyncMock())
        won = asyncio.run(cs._recover_execution({"id": "exec-r2"}, "callee-a", capacity, {}))
        assert won is True
        assert _open_in_scope("exec-r2") == 0

    def test_lease_reaper_park(self, db_backend, monkeypatch):
        """The reaper's db call has already written FAILED; the helper closes."""
        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r3", "failed", error="lease expired")
        asyncio.run(_service()._close_reaped_activity("exec-r3"))
        assert _open_in_scope("exec-r3") == 0

    def test_stale_slot_reclaim(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r4", "failed", error="slot lease expired")
        asyncio.run(_service()._close_stale_slot_activity("exec-r4"))
        assert _open_in_scope("exec-r4") == 0

    def test_bulk_stale_sweep(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r5", "failed", error="stale")
        _seed_activity("tool-r5", "tool_call", "exec-r5", agent="callee-a")
        closed = asyncio.run(_service()._close_bulk_swept_activities([("exec-r5", "callee-a")]))
        assert closed == 2
        assert _open_in_scope("exec-r5") == 0
        assert _state("tool-r5") == "started", "tool_call rows have their own closers (#45)"

    def test_lease_requeue_leaves_the_companion_to_the_redelivery(self, db_backend, monkeypatch):
        """A re-queued row is not terminal: its collaboration row belongs to the
        re-delivered attempt and must stay open."""
        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-r6", "queued", completed=False)
        asyncio.run(_service()._close_requeued_activity("exec-r6"))
        assert _state("disp-exec-r6") == "cancelled"
        assert _state("collab-exec-r6") == "started"


# ---------------------------------------------------------------------------
# The shared owner: the companion close is row-derived for every caller
# ---------------------------------------------------------------------------

class TestOwnerClosesCompanionsFromTheRow:
    def test_success_close_completes_a_direct_dispatch_companion(self, db_backend, monkeypatch):
        from models import TaskExecutionStatus
        from services.activity_service import activity_service

        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-o1", "success")
        asyncio.run(activity_service.close_execution_activity(
            "exec-o1", TaskExecutionStatus.SUCCESS))
        assert _state("collab-exec-o1") == "completed"

    def test_bulk_failed_companion_is_still_upgraded_by_a_late_success(self, db_backend, monkeypatch):
        """Lattice: started < failed < completed. A late real result still wins."""
        from models import TaskExecutionStatus
        from services.activity_service import activity_service

        _fresh(monkeypatch)
        _seed_direct_dispatch("exec-o2", "failed", error="stale")
        asyncio.run(_service()._close_bulk_swept_activities([("exec-o2", "callee-a")]))
        assert _state("collab-exec-o2") == "failed"
        _hrun("UPDATE schedule_executions SET status = 'success', error = NULL WHERE id = 'exec-o2'")
        asyncio.run(activity_service.close_execution_activity(
            "exec-o2", TaskExecutionStatus.SUCCESS))
        assert _state("collab-exec-o2") == "completed"

    def test_aauth_inbound_record_is_never_rewritten(self, db_backend, monkeypatch):
        """`aauth_inbound.record_activity` writes an already-closed collaboration
        row on the callee's execution — FAILED while the task is still
        `working`. It is the inbound call's record, not a companion: an
        authoritative close must not upgrade it."""
        from models import TaskExecutionStatus
        from services.activity_service import activity_service

        _fresh(monkeypatch)
        _seed_exec("exec-o3", "success")
        _seed_activity("inbound-o3", "agent_collaboration", "exec-o3",
                       state="failed", agent="callee-a", triggered_by="a2a")
        asyncio.run(activity_service.close_execution_activity(
            "exec-o3", TaskExecutionStatus.SUCCESS))
        assert _state("inbound-o3") == "failed"


# ---------------------------------------------------------------------------
# a2a queued-cancel: the CAS winner closes and emits the terminal
# ---------------------------------------------------------------------------

class TestA2AQueuedCancel:
    def test_bound_task_cancel_closes_and_emits(self, monkeypatch):
        import routers.a2a as a2a

        fake_db = MagicMock()
        fake_db.get_execution.return_value = {"agent_name": "callee-a", "status": "queued"}
        fake_db.cancel_queued_execution.return_value = True
        closer = MagicMock()
        monkeypatch.setattr(a2a, "db", fake_db)
        monkeypatch.setattr(a2a, "_close_unclaimed_turn", closer)
        monkeypatch.setattr(a2a.platform_audit_service, "log", AsyncMock())
        asyncio.run(a2a._bound_task_rpc("callee-a", "tasks/cancel", "exec-a1", 1, None))
        closer.assert_called_once()
        args = closer.call_args.args
        assert args[:2] == ("callee-a", "exec-a1")
        assert getattr(args[2], "value", args[2]) == "cancelled"

    def test_lost_cancel_closes_nothing(self, monkeypatch):
        import routers.a2a as a2a

        fake_db = MagicMock()
        fake_db.get_execution.return_value = {"agent_name": "callee-a", "status": "queued"}
        fake_db.cancel_queued_execution.return_value = False
        closer = MagicMock()
        monkeypatch.setattr(a2a, "db", fake_db)
        monkeypatch.setattr(a2a, "_close_unclaimed_turn", closer)
        asyncio.run(a2a._bound_task_rpc("callee-a", "tasks/cancel", "exec-a2", 1, None))
        closer.assert_not_called()
