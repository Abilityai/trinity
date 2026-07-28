"""Unit tests for #1804 — a CAS-won terminal write owns closing its paired
``agent_activities`` dispatch row.

Before this, only the dispatching coroutine closed the activity, and only when
it won the CAS. Every recovery writer (watchdog, startup recovery, the two bulk
sweeps, both backend-shutdown ``CancelledError`` handlers, the lease reaper, the
pull sink) wrote the execution terminal and walked away — the activity stayed
``started`` until a generic 120-minute backstop closed it with a fabricated
``duration_ms``.

Layout:
  * ``db_layer``   — the lattice CAS + tri-state outcome + widened lookup +
                     the set-wise bulk close, against a real schema (db_harness).
  * ``service``    — ``activity_service.close_execution_activity`` / the sync
                     spawn wrapper (mocked db).
  * ``cas_loss`` / ``shutdown`` / ``cleanup`` / ``pull`` / ``requeue`` — the
    wired terminal writers.

Mandatory regressions (plan §4): R1 FAILED→COMPLETED upgrade *through the
lookup*; R2 terminate-then-CAS-loss double close; R3 an already-closed close
never clobbers; R4 terminate keeps its #1332 behaviour; R5 the shutdown
handlers close.
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

from db_harness import db_backend, run as _hrun  # noqa: E402,F401  (pytest fixture)


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ===========================================================================
# db layer — real schema
# ===========================================================================
@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    """Active backend with a fresh full production schema (db_harness, #300)."""
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


@pytest.fixture
def activity_ops(tmp_db):
    from db.activities import ActivityOperations

    return ActivityOperations()


def _insert_activity(
    *,
    act_id: str,
    exec_id: str,
    activity_type: str = "chat_start",
    activity_state: str = "started",
    started_at: str | None = None,
    created_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
):
    started_at = started_at or _ago_iso(60)
    created_at = created_at or started_at
    _hrun(
        "INSERT INTO agent_activities "
        "(id, agent_name, activity_type, activity_state, started_at, completed_at, "
        " duration_ms, triggered_by, related_execution_id, error, created_at) "
        "VALUES (:id, 'test-agent', :atype, :astate, :sa, :ca_at, :dur, 'schedule', "
        " :eid, :err, :ca)",
        id=act_id, atype=activity_type, astate=activity_state, sa=started_at,
        ca_at=completed_at, dur=duration_ms, eid=exec_id, err=error, ca=created_at,
    )


def _fetch_activity(act_id: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT activity_state, completed_at, duration_ms, error "
                "FROM agent_activities WHERE id = :id"
            ),
            {"id": act_id},
        ).mappings().first()
    return dict(row) if row else {}


@pytest.mark.unit
class TestCompleteActivityCas:
    """``db.complete_activity`` is a lattice CAS returning a tri-state outcome."""

    def test_db_layer_started_row_is_updated(self, tmp_db, activity_ops):
        from models import ActivityCloseOutcome

        _insert_activity(act_id="act-1", exec_id="exec-1")
        outcome = activity_ops.complete_activity("act-1", "completed")
        assert outcome is ActivityCloseOutcome.UPDATED
        row = _fetch_activity("act-1")
        assert row["activity_state"] == "completed"
        assert row["completed_at"] is not None
        assert row["duration_ms"] is not None

    def test_db_layer_missing_row_is_not_found(self, tmp_db, activity_ops):
        """[404 semantics] routers/internal.py 404s on NOT_FOUND, and only there."""
        from models import ActivityCloseOutcome

        assert activity_ops.complete_activity("nope", "completed") is ActivityCloseOutcome.NOT_FOUND

    def test_db_layer_already_closed_never_clobbers(self, tmp_db, activity_ops):
        """[R3] The double-close hazard the CAS exists to defuse: a second closer
        must not overwrite completed_at / duration_ms / error."""
        from models import ActivityCloseOutcome

        _insert_activity(
            act_id="act-2", exec_id="exec-2", activity_state="completed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=900_000, error=None,
        )
        before = _fetch_activity("act-2")
        outcome = activity_ops.complete_activity("act-2", "failed", error="late failure")
        assert outcome is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-2") == before

    def test_db_layer_failed_row_upgrades_to_completed(self, tmp_db, activity_ops):
        """[R1] An authoritative close MAY upgrade a provisional FAILED — the
        #1083 late-SUCCESS-after-lease-expiry path."""
        from models import ActivityCloseOutcome

        _insert_activity(
            act_id="act-3", exec_id="exec-3", activity_state="failed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=7_200_000,
            error="lease_expired",
        )
        outcome = activity_ops.complete_activity("act-3", "completed")
        assert outcome is ActivityCloseOutcome.UPDATED
        row = _fetch_activity("act-3")
        assert row["activity_state"] == "completed"
        assert row["duration_ms"] != 7_200_000
        assert row["error"] is None

    def test_db_layer_failed_row_refuses_second_failed(self, tmp_db, activity_ops):
        """[R1] A provisional close never overwrites a provisional close."""
        from models import ActivityCloseOutcome

        _insert_activity(
            act_id="act-4", exec_id="exec-4", activity_state="failed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=1_000, error="first",
        )
        assert activity_ops.complete_activity(
            "act-4", "failed", error="second"
        ) is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-4")["error"] == "first"

    def test_db_layer_cancelled_row_refuses_completed(self, tmp_db, activity_ops):
        """Nothing overwrites an authoritative close — mirrors the execution CAS,
        where a SUCCESS write loses only to CANCELLED (#671/#1332)."""
        from models import ActivityCloseOutcome

        _insert_activity(
            act_id="act-5", exec_id="exec-5", activity_state="cancelled",
            completed_at="2026-07-28T10:00:00Z", duration_ms=1_000,
            error="Execution terminated by user",
        )
        assert activity_ops.complete_activity(
            "act-5", "completed"
        ) is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-5")["activity_state"] == "cancelled"

    def test_db_layer_details_merge_preserved_on_update(self, tmp_db, activity_ops):
        """The pre-existing detail-merge behaviour survives the CAS rewrite."""
        from sqlalchemy import text
        from db.engine import get_engine
        import json

        _insert_activity(act_id="act-6", exec_id="exec-6")
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE agent_activities SET details = :d WHERE id = 'act-6'"),
                {"d": json.dumps({"keep": 1})},
            )
        activity_ops.complete_activity("act-6", "completed", details={"add": 2})
        with get_engine().connect() as conn:
            raw = conn.execute(
                text("SELECT details FROM agent_activities WHERE id = 'act-6'")
            ).scalar()
        assert json.loads(raw) == {"keep": 1, "add": 2}


@pytest.mark.unit
class TestOpenActivityLookup:
    """The lookup must agree with the CAS, or the widened predicate is inert."""

    def test_lookup_authoritative_finds_failed_row(self, tmp_db, activity_ops):
        """[R1] The decisive pairing: an authoritative close searches
        ``started|failed`` so it can actually reach the row it may upgrade."""
        _insert_activity(act_id="act-f", exec_id="exec-f", activity_state="failed")
        assert activity_ops.get_open_activity_id_for_execution("exec-f") is None
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-f", include_failed=True)
            == "act-f"
        )

    def test_lookup_prefers_started_over_failed(self, tmp_db, activity_ops):
        """With both present the OPEN row wins regardless of created_at order."""
        _insert_activity(
            act_id="act-open", exec_id="exec-both", activity_state="started",
            created_at=_ago_iso(120),
        )
        _insert_activity(
            act_id="act-closed", exec_id="exec-both", activity_state="failed",
            created_at=_ago_iso(10),
        )
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-both", include_failed=True)
            == "act-open"
        )

    def test_lookup_never_returns_completed_or_cancelled(self, tmp_db, activity_ops):
        _insert_activity(act_id="act-c", exec_id="exec-c", activity_state="completed")
        _insert_activity(act_id="act-x", exec_id="exec-x", activity_state="cancelled")
        for eid in ("exec-c", "exec-x"):
            assert activity_ops.get_open_activity_id_for_execution(eid, include_failed=True) is None

    def test_lookup_excludes_shared_eid_tool_call_row(self, tmp_db, activity_ops):
        """Codex #8 (#1083) preserved under the widened lookup."""
        _insert_activity(
            act_id="act-dispatch", exec_id="exec-shared",
            activity_type="chat_start", activity_state="failed",
            created_at=_ago_iso(30),
        )
        _insert_activity(
            act_id="act-tool", exec_id="exec-shared",
            activity_type="tool_call", activity_state="started",
            created_at=_ago_iso(1),
        )
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-shared", include_failed=True)
            == "act-dispatch"
        )


@pytest.mark.unit
class TestBulkCloseOpenActivities:
    """The bulk sweeps close set-wise in one transaction, no per-row WS."""

    def test_db_layer_bulk_closes_every_open_row_for_the_id_set(self, tmp_db, activity_ops):
        """Set-wise (Codex 6): a re-queued execution can own more than one open
        dispatch activity — an ``eid → one activity_id`` map would drop the rest."""
        _insert_activity(act_id="a1", exec_id="e1")
        _insert_activity(act_id="a2", exec_id="e1", activity_type="schedule_start")
        _insert_activity(act_id="b1", exec_id="e2")
        _insert_activity(act_id="untouched", exec_id="e3")

        closed = activity_ops.close_open_activities_for_executions(
            ["e1", "e2"], "failed", error="marked failed by cleanup sweep"
        )
        assert closed == 3
        for act_id in ("a1", "a2", "b1"):
            row = _fetch_activity(act_id)
            assert row["activity_state"] == "failed"
            assert row["duration_ms"] is not None
            assert row["error"] == "marked failed by cleanup sweep"
        assert _fetch_activity("untouched")["activity_state"] == "started"

    def test_db_layer_bulk_skips_already_closed_rows(self, tmp_db, activity_ops):
        _insert_activity(
            act_id="done", exec_id="e9", activity_state="completed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=42,
        )
        assert activity_ops.close_open_activities_for_executions(["e9"], "failed") == 0
        assert _fetch_activity("done")["duration_ms"] == 42

    def test_db_layer_bulk_empty_input_is_zero(self, tmp_db, activity_ops):
        assert activity_ops.close_open_activities_for_executions([], "failed") == 0

    def test_db_layer_bulk_chunks_past_the_host_param_cap(
        self, tmp_db, activity_ops, monkeypatch
    ):
        """The IN (...) list is chunked at ``_SQLITE_MAX_IN_VARS`` (precedent:
        db/schedules/git_config.py) — monkeypatched small to exercise it."""
        import db.activities as activities_mod

        monkeypatch.setattr(activities_mod, "_SQLITE_MAX_IN_VARS", 2)
        ids = [f"bulk-{i}" for i in range(5)]
        for i, eid in enumerate(ids):
            _insert_activity(act_id=f"bulk-act-{i}", exec_id=eid)

        assert activity_ops.close_open_activities_for_executions(ids, "failed") == 5
        for i in range(5):
            assert _fetch_activity(f"bulk-act-{i}")["activity_state"] == "failed"


# ===========================================================================
# service layer — the single owner of the close contract
# ===========================================================================
def _svc_with_db(mock_db):
    """A fresh ActivityService with its module-level ``db`` swapped whole.

    Swapping the WHOLE object (never ``setattr(database.db, ...)``) avoids the
    method-less ``database.db`` stub leak that makes attribute patches fail
    under pytest-randomly (test_904).
    """
    import services.activity_service as act_mod

    svc = act_mod.ActivityService()
    return svc, act_mod


@pytest.mark.unit
class TestCloseExecutionActivityService:
    def test_service_maps_terminal_via_shared_helper(self):
        from models import ActivityCloseOutcome
        from models import ActivityState, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        mock_db.complete_activity.return_value = ActivityCloseOutcome.UPDATED
        svc, mod = _svc_with_db(mock_db)

        cases = {
            TaskExecutionStatus.SUCCESS: ActivityState.COMPLETED,
            TaskExecutionStatus.CANCELLED: ActivityState.CANCELLED,
            TaskExecutionStatus.FAILED: ActivityState.FAILED,
            TaskExecutionStatus.SKIPPED: ActivityState.FAILED,
        }
        for terminal, expected in cases.items():
            with patch.object(mod, "db", mock_db):
                assert _await(svc.close_execution_activity("exec-1", terminal)) is True
            assert mock_db.complete_activity.call_args[0][1] == expected

    def test_service_lookup_is_lattice_aware(self):
        """[test 6] Authoritative terminals search started|failed; provisional
        terminals search started only. The pairing that keeps the fix live."""
        from models import ActivityCloseOutcome
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        mock_db.complete_activity.return_value = ActivityCloseOutcome.UPDATED
        svc, mod = _svc_with_db(mock_db)

        for terminal, expected in (
            (TaskExecutionStatus.SUCCESS, True),
            (TaskExecutionStatus.CANCELLED, True),
            (TaskExecutionStatus.FAILED, False),
        ):
            with patch.object(mod, "db", mock_db):
                _await(svc.close_execution_activity("exec-1", terminal))
            assert (
                mock_db.get_open_activity_id_for_execution.call_args.kwargs["include_failed"]
                is expected
            )

    def test_service_caller_held_activity_id_skips_lookup(self):
        from models import ActivityCloseOutcome
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        mock_db.complete_activity.return_value = ActivityCloseOutcome.UPDATED
        svc, mod = _svc_with_db(mock_db)

        with patch.object(mod, "db", mock_db):
            assert _await(
                svc.close_execution_activity(
                    "exec-1", TaskExecutionStatus.FAILED, activity_id="held"
                )
            ) is True
        mock_db.get_open_activity_id_for_execution.assert_not_called()
        assert mock_db.complete_activity.call_args[0][0] == "held"

    def test_service_no_open_activity_is_a_noop(self):
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.return_value = None
        svc, mod = _svc_with_db(mock_db)

        with patch.object(mod, "db", mock_db):
            assert _await(
                svc.close_execution_activity("exec-1", TaskExecutionStatus.FAILED)
            ) is False
        mock_db.complete_activity.assert_not_called()

    def test_service_is_fail_open_when_db_raises(self):
        """[test 9] The close runs AFTER a committed terminal write — it must
        never raise into a caller that already billed the turn."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.side_effect = RuntimeError("db down")
        svc, mod = _svc_with_db(mock_db)

        with patch.object(mod, "db", mock_db):
            assert _await(
                svc.close_execution_activity("exec-1", TaskExecutionStatus.FAILED)
            ) is False

    def test_service_broadcasts_only_on_updated(self):
        """[test 10 / R3] An ALREADY_CLOSED refusal must not emit an
        agent_activity event claiming the activity just closed."""
        from models import ActivityCloseOutcome
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        svc, mod = _svc_with_db(mock_db)
        ws = MagicMock(broadcast=AsyncMock())
        svc.set_websocket_manager(ws)
        seen = []
        svc.subscribe(lambda e: seen.append(e))

        mock_db.complete_activity.return_value = ActivityCloseOutcome.UPDATED
        with patch.object(mod, "db", mock_db):
            _await(svc.close_execution_activity("exec-1", TaskExecutionStatus.FAILED))
        assert ws.broadcast.await_count == 1
        assert len(seen) == 1

        mock_db.complete_activity.return_value = ActivityCloseOutcome.ALREADY_CLOSED
        with patch.object(mod, "db", mock_db):
            _await(svc.close_execution_activity("exec-1", TaskExecutionStatus.FAILED))
        assert ws.broadcast.await_count == 1  # unchanged — no second broadcast
        assert len(seen) == 1

    def test_service_already_closed_still_reports_handled(self):
        """Only NOT_FOUND is False — routers/internal.py 404s on that, and only
        that, so an idempotent re-close does not start 404ing the scheduler."""
        from models import ActivityCloseOutcome

        mock_db = MagicMock()
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        svc, mod = _svc_with_db(mock_db)

        mock_db.complete_activity.return_value = ActivityCloseOutcome.ALREADY_CLOSED
        with patch.object(mod, "db", mock_db):
            assert _await(svc.complete_activity("act-1", "failed")) is True

        mock_db.complete_activity.return_value = ActivityCloseOutcome.NOT_FOUND
        with patch.object(mod, "db", mock_db):
            assert _await(svc.complete_activity("act-1", "failed")) is False

    def test_service_spawn_wrapper_schedules_the_close(self):
        """[test 16 part 1] The sync sink (pull_coordination_service) needs a
        no-await entry point; a strong ref keeps the task from being GC'd."""
        import asyncio
        from models import ActivityCloseOutcome
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        mock_db.get_activity.return_value = {"agent_name": "a", "activity_type": "chat_start"}
        mock_db.complete_activity.return_value = ActivityCloseOutcome.UPDATED
        svc, mod = _svc_with_db(mock_db)

        async def _drive():
            with patch.object(mod, "db", mock_db):
                svc.spawn_close_execution_activity("exec-1", TaskExecutionStatus.SUCCESS)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(_drive())
        mock_db.complete_activity.assert_called_once()

    def test_service_spawn_wrapper_without_a_loop_is_fail_open(self):
        """No running loop → skipped, never raised (the backstop still covers)."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        svc, mod = _svc_with_db(mock_db)
        with patch.object(mod, "db", mock_db):
            svc.spawn_close_execution_activity("exec-1", TaskExecutionStatus.SUCCESS)


# ===========================================================================
# the wired terminal writers (mocked)
# ===========================================================================
@pytest.mark.unit
class TestWriteTerminalAndGate:
    """[R2] The CAS-loss branch — the asymmetry the issue is built on: the
    SUCCESS applier has reconciled a lost CAS since #1332, this one never did."""

    def _run(self, *, won, persisted_status=None):
        import services.task_execution_service as tes
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.update_execution_status.return_value = won
        if persisted_status is not None:
            row = MagicMock()
            row.status = persisted_status
            mock_db.get_execution.return_value = row
        else:
            mock_db.get_execution.return_value = None
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        with (
            patch.object(tes, "db", mock_db),
            patch.object(tes, "activity_service", mock_activity),
            patch.object(tes, "event_dispatch_service", MagicMock()),
            patch.object(tes, "channel_completion_report", MagicMock()),
        ):
            _await(
                tes._write_terminal_and_gate(
                    "exec-1804",
                    "act-1",
                    status=TaskExecutionStatus.FAILED,
                    error="Task execution timed out",
                    agent_name="worker-a",
                )
            )
        return mock_activity

    def test_cas_loss_closes_with_the_persisted_state(self):
        from models import TaskExecutionStatus

        mact = self._run(won=False, persisted_status=TaskExecutionStatus.CANCELLED)

        mact.close_execution_activity.assert_awaited_once()
        args, kwargs = mact.close_execution_activity.await_args
        assert args[1] == TaskExecutionStatus.CANCELLED
        # Same phrasing as the SUCCESS applier's own lost-CAS reconcile.
        assert kwargs["error"].startswith("superseded by")
        assert "CANCELLED" in kwargs["error"].upper()
        assert kwargs["activity_id"] == "act-1"

    def test_cas_loss_with_no_row_falls_back_to_failed(self):
        from models import TaskExecutionStatus

        mact = self._run(won=False, persisted_status=None)
        assert mact.close_execution_activity.await_args[0][1] == TaskExecutionStatus.FAILED

    def test_cas_win_closes_with_its_own_terminal(self):
        from models import TaskExecutionStatus

        mact = self._run(won=True)
        args, kwargs = mact.close_execution_activity.await_args
        assert args[1] == TaskExecutionStatus.FAILED
        assert kwargs["error"] == "Task execution timed out"


@pytest.mark.unit
class TestShutdownHandlersCloseActivity:
    """[R5] The two backend-shutdown CancelledError writers — the issue's own
    reproduction step. Both write FAILED, which makes the row invisible to
    startup recovery (it scans `running`), so nothing but the 120-minute
    backstop ever closed their activity."""

    def test_task_execution_service_shutdown_closes(self):
        import asyncio

        import services.task_execution_service as tes
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        row = MagicMock()
        row.status = TaskExecutionStatus.RUNNING
        mock_db.get_execution.return_value = row
        mock_db.update_execution_status.return_value = True
        mock_db.get_max_parallel_tasks.return_value = 3

        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity = MagicMock(acquire=AsyncMock(return_value=admitted), release=AsyncMock())
        mock_activity = MagicMock(
            track_activity=AsyncMock(return_value="act-shutdown"),
            close_execution_activity=AsyncMock(return_value=True),
        )
        circuit = MagicMock()
        circuit.allow_request.return_value = True

        with (
            patch.object(tes, "db", mock_db),
            patch.object(tes, "get_capacity_manager", return_value=mock_capacity),
            patch.object(tes, "activity_service", mock_activity),
            patch.object(tes, "CircuitState", return_value=circuit),
            patch.object(tes, "dispatch_breaker_active", return_value=False),
            # Raise INSIDE the try, after the activity is tracked — the shape a
            # shutdown cancellation takes mid-turn.
            patch.object(tes, "_resolve_agent_runtime", side_effect=asyncio.CancelledError()),
        ):
            svc = tes.TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                _await(
                    svc.execute_task(
                        agent_name="test-agent",
                        message="hello",
                        triggered_by="schedule",
                        execution_id="exec-shutdown",
                        timeout_seconds=300,
                    )
                )

        mock_activity.close_execution_activity.assert_awaited_once()
        args, kwargs = mock_activity.close_execution_activity.await_args
        assert args[0] == "exec-shutdown"
        assert args[1] == TaskExecutionStatus.FAILED
        assert "backend shutdown" in kwargs["error"]
        assert kwargs["activity_id"] == "act-shutdown"

    def test_internal_router_shutdown_closes(self):
        import asyncio

        import routers.internal as internal
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        row = MagicMock()
        row.status = TaskExecutionStatus.RUNNING
        mock_db.get_execution.return_value = row
        mock_db.update_execution_status.return_value = True
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        task_service = MagicMock(execute_task=AsyncMock(side_effect=asyncio.CancelledError()))
        request = MagicMock(execution_id="exec-shutdown-2", agent_name="test-agent")

        with (
            patch.object(internal, "db", mock_db),
            patch.object(internal, "activity_service", mock_activity),
        ):
            with pytest.raises(asyncio.CancelledError):
                _await(internal._execute_task_internal_background(task_service, request))

        mock_activity.close_execution_activity.assert_awaited_once()
        args, _kwargs = mock_activity.close_execution_activity.await_args
        assert args[0] == "exec-shutdown-2"
        assert args[1] == TaskExecutionStatus.FAILED

    def test_internal_router_shutdown_skips_close_on_lost_cas(self):
        import asyncio

        import routers.internal as internal
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        row = MagicMock()
        row.status = TaskExecutionStatus.RUNNING
        mock_db.get_execution.return_value = row
        mock_db.update_execution_status.return_value = False  # lost to a real terminal
        mock_activity = MagicMock(close_execution_activity=AsyncMock())
        task_service = MagicMock(execute_task=AsyncMock(side_effect=asyncio.CancelledError()))
        request = MagicMock(execution_id="exec-shutdown-3", agent_name="test-agent")

        with (
            patch.object(internal, "db", mock_db),
            patch.object(internal, "activity_service", mock_activity),
        ):
            with pytest.raises(asyncio.CancelledError):
                _await(internal._execute_task_internal_background(task_service, request))

        mock_activity.close_execution_activity.assert_not_awaited()


@pytest.mark.unit
class TestCleanupRecoverySites:
    def _service(self):
        from services.cleanup_service import CleanupService

        return CleanupService(poll_interval=300)

    def _run_watchdog_recover(self, *, updated):
        import services.cleanup_service as cs
        from services.cleanup_service import CleanupReport

        svc = self._service()
        svc._broadcast_watchdog_event = AsyncMock()
        mock_db = MagicMock()
        mock_db.mark_execution_failed_by_watchdog.return_value = updated
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        report = CleanupReport()
        with (
            patch.object(cs, "db", mock_db),
            patch.object(cs, "get_capacity_manager", return_value=MagicMock(
                release_if_matches=AsyncMock()
            )),
            patch("services.activity_service.activity_service", mock_activity),
        ):
            recovered = _await(
                svc._recover_execution(
                    "exec-w", "agent-w", "orphaned", "orphan_recovered", None, report
                )
            )
        return recovered, mock_activity, report

    def test_watchdog_recovery_closes_on_won_cas(self):
        from models import TaskExecutionStatus

        recovered, mact, report = self._run_watchdog_recover(updated=True)

        assert recovered is True
        mact.close_execution_activity.assert_awaited_once()
        args, _kwargs = mact.close_execution_activity.await_args
        assert args[0] == "exec-w"
        assert args[1] == TaskExecutionStatus.FAILED
        assert report.activities_closed_on_recovery == 1

    def test_watchdog_recovery_does_not_close_on_lost_cas(self):
        """A real completion landed between check and update — the activity
        belongs to that writer, not to us."""
        recovered, mact, report = self._run_watchdog_recover(updated=False)

        assert recovered is False
        mact.close_execution_activity.assert_not_awaited()
        assert report.activities_closed_on_recovery == 0

    def _run_startup_recover(self, *, won):
        import services.cleanup_service as cs

        mock_db = MagicMock()
        mock_db.mark_execution_failed_by_watchdog.return_value = won
        mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
        capacity = MagicMock(release=AsyncMock())
        stats = {"activities_closed": 0}
        with (
            patch.object(cs, "db", mock_db),
            patch("services.activity_service.activity_service", mock_activity),
        ):
            result = _await(
                cs._recover_execution({"id": "exec-s"}, "agent-s", capacity, stats)
            )
        return result, mock_activity, stats

    def test_startup_recovery_closes_and_returns_the_cas_bool(self):
        result, mact, stats = self._run_startup_recover(won=True)

        assert result is True
        mact.close_execution_activity.assert_awaited_once()
        assert stats["activities_closed"] == 1

    def test_startup_recovery_lost_cas_returns_false_and_skips_close(self):
        """The CAS bool used to be DISCARDED here — the function returned True
        unconditionally, so a lost recovery was counted as a recovery."""
        result, mact, stats = self._run_startup_recover(won=False)

        assert result is False
        mact.close_execution_activity.assert_not_awaited()
        assert stats["activities_closed"] == 0


@pytest.mark.unit
class TestBulkSweepClose:
    def _service(self):
        from services.cleanup_service import CleanupService

        return CleanupService(poll_interval=300)

    def test_bulk_close_runs_with_no_event_subscribers(self):
        """[test 15] The #1714 subscriber gate scopes the EVENT, never the close.
        Folding the close into _emit_bulk_terminal_events would skip it on every
        install with no subscribers — which is most of them."""
        import services.cleanup_service as cs

        svc = self._service()
        mock_db = MagicMock()
        mock_db.has_task_terminal_subscribers.return_value = False
        mock_db.close_open_activities_for_executions.return_value = 2
        with patch.object(cs, "db", mock_db):
            closed = _await(
                svc._close_bulk_swept_activities([("e1", "a1"), ("e2", "a2")])
            )
        assert closed == 2
        assert mock_db.close_open_activities_for_executions.call_args[0][0] == ["e1", "e2"]

    def test_bulk_close_empty_rows_is_zero(self):
        import services.cleanup_service as cs

        svc = self._service()
        mock_db = MagicMock()
        with patch.object(cs, "db", mock_db):
            assert _await(svc._close_bulk_swept_activities([])) == 0
        mock_db.close_open_activities_for_executions.assert_not_called()

    def test_bulk_close_is_fail_open(self):
        import services.cleanup_service as cs

        svc = self._service()
        mock_db = MagicMock()
        mock_db.close_open_activities_for_executions.side_effect = RuntimeError("boom")
        with patch.object(cs, "db", mock_db):
            assert _await(svc._close_bulk_swept_activities([("e1", "a1")])) == 0

    def test_stale_activity_backstop_runs_after_the_slot_reaper(self):
        """Ordering regression: the 120-minute duration fabricator must not beat
        a legitimate closer within a single cycle."""
        import inspect

        import services.cleanup_service as cs

        src = inspect.getsource(cs.CleanupService._run_cleanup_inner)
        assert src.index("_sweep_stale_slots(") < src.index("_sweep_stale_activities(")


@pytest.mark.unit
class TestPullSinkAndLeaseReaper:
    def test_pull_sink_closes_on_applied(self):
        """[test 16] The sink is sync but runs inside an async router handler —
        it uses the spawn wrapper, exactly as it does for the #1578 emit."""
        import services.pull_coordination_service as pcs
        from models import TaskExecutionStatus

        execution = MagicMock()
        execution.status = TaskExecutionStatus.RUNNING
        execution.agent_name = "worker-a"
        mock_db = MagicMock()
        mock_db.get_execution.return_value = execution
        mock_db.update_execution_status.return_value = True
        mock_activity = MagicMock()

        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "event_dispatch_service", MagicMock()),
            patch.object(pcs, "activity_service", mock_activity),
        ):
            outcome = pcs.apply_task_result(
                "exec-pull", "tok", status="success", content="done"
            )

        assert outcome.kind == "applied"
        mock_activity.spawn_close_execution_activity.assert_called_once()
        args, _kwargs = mock_activity.spawn_close_execution_activity.call_args
        assert args[0] == "exec-pull"
        assert args[1] == TaskExecutionStatus.SUCCESS

    def test_pull_sink_does_not_close_on_replay(self):
        import services.pull_coordination_service as pcs
        from models import TaskExecutionStatus

        execution = MagicMock()
        execution.status = TaskExecutionStatus.SUCCESS  # authoritative terminal
        mock_db = MagicMock()
        mock_db.get_execution.return_value = execution
        mock_activity = MagicMock()

        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "activity_service", mock_activity),
        ):
            outcome = pcs.apply_task_result(
                "exec-pull", "tok", status="success", content="done"
            )

        assert outcome.kind == "replayed"
        mock_activity.spawn_close_execution_activity.assert_not_called()

    def test_pull_sink_does_not_close_on_conflict(self):
        import services.pull_coordination_service as pcs
        from models import TaskExecutionStatus

        execution = MagicMock()
        execution.status = TaskExecutionStatus.RUNNING
        execution.agent_name = "worker-a"
        mock_db = MagicMock()
        mock_db.get_execution.return_value = execution
        mock_db.update_execution_status.return_value = False  # stale/wrong token
        mock_activity = MagicMock()

        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "event_dispatch_service", MagicMock()),
            patch.object(pcs, "activity_service", mock_activity),
        ):
            outcome = pcs.apply_task_result(
                "exec-pull", "tok", status="failed", content="nope"
            )

        assert outcome.kind == "conflict"
        mock_activity.spawn_close_execution_activity.assert_not_called()

    def test_lease_reaper_reports_requeued_execution_ids(self):
        """[test 17] The re-queue preserves execution_id, so the superseded
        attempt's activity must be closable by the caller."""
        from services import lease_reaper_service

        mock_db = MagicMock()
        mock_db.find_expired_leases.return_value = [
            {"id": "exec-rq", "agent_name": "worker-a", "redelivery_count": 0}
        ]
        mock_db.requeue_expired_lease.return_value = True

        report = lease_reaper_service.reap_expired_leases(mock_db, max_redelivery=3)

        assert report.requeued == 1
        assert report.requeued_execution_ids == ["exec-rq"]
        assert report.parked_execution_ids == []


@pytest.mark.unit
class TestCallbackLateSuccessLookup:
    """[R1, the #1083 path it was written for] The result-callback endpoint IS
    the late-SUCCESS path: a lease reaper that beat the callback already FAILED
    the row and closed the activity FAILED, and the execution CAS deliberately
    lets a genuine late SUCCESS correct it. A `started`-only lookup returns None
    there, so the pair would settle at execution=success, activity=failed —
    #1804 inverted, on the exact path the lattice exists for."""

    def test_callback_lookup_includes_failed_activities(self):
        import inspect

        import routers.agents as agents_router

        src = inspect.getsource(agents_router)
        assert (
            "get_open_activity_id_for_execution(execution_id, include_failed=True)" in src
        ), (
            "#1804: the #1083 result-callback must look up with include_failed=True, "
            "or a late SUCCESS can never upgrade the reaper-FAILED activity."
        )
