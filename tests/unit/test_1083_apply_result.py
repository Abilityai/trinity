"""Unit tests for ``TaskExecutionService.apply_result`` (#1083, PR1.A).

``apply_result`` is the single terminal applier shared by the inline sync path
and the fire-and-forget result-callback. These tests pin its behavior directly
(not through ``execute_task``):

- Golden output (parity scope, Codex #11): the exact ``update_execution_status``
  kwargs for the two branches apply_result OWNS — SUCCESS and classified-FAILURE
  (incl. #678 salvage + cost rollup). Timeout/budget/cancel/classification stay
  in ``execute_task`` and are tested there separately.
- CAS-gated side effects (Codex #1/#12): activity completion, breaker outcome,
  and slot release run ONLY on a won CAS — a lost CAS releases NO slot (no
  double-drain / over-admit).

Pure unit tests — no backend. Mocks mirror tests/unit/test_terminal_write_cas_gate.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_execution(status: str):
    ex = MagicMock()
    ex.id = "exec-1083"
    ex.status = status
    return ex


def _run_apply(envelope, *, cas_won=True, reconciled_status="cancelled",
               activity_id="act-1", breaker_enabled=False, release_slot=False):
    """Drive apply_result with mocked db/activity/capacity/breaker.

    Returns (result, mocks) where mocks = (db, activity, capacity, record).
    """
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    mock_db.get_execution.return_value = _make_execution(reconciled_status)

    mock_activity = MagicMock(complete_activity=AsyncMock())
    mock_capacity = MagicMock(release=AsyncMock())
    mock_record = AsyncMock()

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service._record_dispatch_terminal", mock_record),
    ):
        svc = TaskExecutionService()
        result = _await(
            svc.apply_result(
                "test-agent",
                envelope,
                activity_id=activity_id,
                breaker_enabled=breaker_enabled,
                release_slot=release_slot,
            )
        )
    return result, (mock_db, mock_activity, mock_capacity, mock_record)


def _success_envelope(**over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    base = dict(
        execution_id="exec-1083",
        status=TaskExecutionStatus.SUCCESS,
        response="all done",
        metadata={"cost_usd": 0.05, "input_tokens": 100, "context_window": 200000,
                  "session_id": "meta-sess"},
        execution_log=[{"type": "tool_use", "name": "Bash"}],
        session_id="resp-sess",
        execution_time_ms=1234,
    )
    base.update(over)
    return TerminalEnvelope(**base)


def _failed_envelope(**over):
    from services.task_execution_service import (
        TerminalEnvelope, TaskExecutionStatus, TaskExecutionErrorCode,
    )

    base = dict(
        execution_id="exec-1083",
        status=TaskExecutionStatus.FAILED,
        error="agent said no",
        error_code=None,
        metadata={"cost_usd": 0.02, "input_tokens": 50, "context_window": 200000},
    )
    base.update(over)
    if "error_code" in over and over["error_code"] == "AUTH":
        base["error_code"] = TaskExecutionErrorCode.AUTH
    return TerminalEnvelope(**base)


class TestSuccessGolden:
    pytestmark = pytest.mark.unit

    def test_success_won_writes_full_row(self):
        from services.task_execution_service import TaskExecutionStatus
        from models import ActivityState

        result, (mdb, mact, mcap, mrec) = _run_apply(_success_envelope())

        assert result.status == TaskExecutionStatus.SUCCESS
        assert result.response == "all done"
        # Golden kwargs for the SUCCESS write.
        kw = mdb.update_execution_status.call_args.kwargs
        assert kw["status"] == TaskExecutionStatus.SUCCESS
        assert kw["response"] == "all done"
        assert kw["cost"] == 0.05
        assert kw["context_used"] == 100           # input_tokens (no cache signal)
        assert kw["context_max"] == 200000
        # claude_session_id prefers raw response session id, falls back to meta.
        assert kw["claude_session_id"] == "resp-sess"
        assert kw["tool_calls"] is not None and "Bash" in kw["tool_calls"]
        assert kw["execution_log"] == kw["tool_calls"]
        # Side effects on a won CAS.
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.COMPLETED
        mrec.assert_awaited_once_with("test-agent", False, None)

    def test_success_session_id_falls_back_to_metadata(self):
        result, (mdb, *_rest) = _run_apply(_success_envelope(session_id=None))
        assert mdb.update_execution_status.call_args.kwargs["claude_session_id"] == "meta-sess"

    def test_success_cost_rollup_includes_previous_attempt(self):
        """#678 R2: previous_attempt_cost is summed into the terminal cost."""
        result, (mdb, *_rest) = _run_apply(
            _success_envelope(metadata={"cost_usd": 0.03}, previous_attempt_cost=0.10)
        )
        assert mdb.update_execution_status.call_args.kwargs["cost"] == pytest.approx(0.13)

    def test_success_lost_cas_reconciles_no_side_effects(self):
        from services.task_execution_service import (
            TaskExecutionStatus, TaskExecutionErrorCode,
        )
        from models import ActivityState

        result, (mdb, mact, mcap, mrec) = _run_apply(
            _success_envelope(), cas_won=False, reconciled_status="cancelled",
        )
        assert result.status == TaskExecutionStatus.CANCELLED
        assert result.error_code == TaskExecutionErrorCode.RECONCILED
        # #1332: SUCCESS only ever loses the CAS to a CANCELLED row (schedules.py
        # update_execution_status SUCCESS guard = `status != CANCELLED`), so the
        # activity closes as CANCELLED ("superseded by cancelled"), NEVER
        # COMPLETED and no longer collapsed to FAILED; no breaker.
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.CANCELLED
        mrec.assert_not_awaited()


class TestFailedGolden:
    pytestmark = pytest.mark.unit

    def test_failed_won_writes_salvage_row(self):
        from services.task_execution_service import TaskExecutionStatus
        from models import ActivityState

        result, (mdb, mact, mcap, mrec) = _run_apply(_failed_envelope())

        assert result.status == TaskExecutionStatus.FAILED
        assert result.error == "agent said no"
        kw = mdb.update_execution_status.call_args.kwargs
        assert kw["status"] == TaskExecutionStatus.FAILED
        assert kw["error"] == "agent said no"
        assert kw["cost"] == 0.02                   # salvaged from partial metadata
        assert kw["context_used"] == 50
        assert kw["context_max"] == 200000
        # FAILED write must NOT carry response/tool_calls/execution_log/session.
        assert "response" not in kw or kw.get("response") is None
        assert kw.get("tool_calls") is None
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.FAILED
        # Non-AUTH failure → breaker untouched.
        mrec.assert_not_awaited()

    def test_failed_auth_records_breaker(self):
        from services.task_execution_service import TaskExecutionErrorCode

        result, (mdb, mact, mcap, mrec) = _run_apply(
            _failed_envelope(error_code="AUTH"), breaker_enabled=True,
        )
        mrec.assert_awaited_once_with("test-agent", True, TaskExecutionErrorCode.AUTH)

    def test_failed_empty_metadata_writes_null_context(self):
        result, (mdb, *_rest) = _run_apply(_failed_envelope(metadata={}))
        kw = mdb.update_execution_status.call_args.kwargs
        assert kw["context_used"] is None
        assert kw["context_max"] is None
        assert kw["cost"] is None

    def test_failed_salvage_cost_rollup(self):
        result, (mdb, *_rest) = _run_apply(
            _failed_envelope(metadata={"cost_usd": 0.02}, previous_attempt_cost=0.05)
        )
        assert mdb.update_execution_status.call_args.kwargs["cost"] == pytest.approx(0.07)

    def test_failed_lost_cas_skips_all_side_effects(self):
        from services.task_execution_service import TaskExecutionStatus

        result, (mdb, mact, mcap, mrec) = _run_apply(
            _failed_envelope(error_code="AUTH"), cas_won=False, breaker_enabled=True,
        )
        # Still returns FAILED, but NO activity / breaker / release.
        assert result.status == TaskExecutionStatus.FAILED
        mact.complete_activity.assert_not_awaited()
        mrec.assert_not_awaited()


class TestSlotReleaseCasGate:
    """Codex #12: capacity.release runs ONLY on a won CAS (no double-drain)."""

    pytestmark = pytest.mark.unit

    def test_success_won_release_slot_releases(self):
        result, (mdb, mact, mcap, mrec) = _run_apply(_success_envelope(), release_slot=True)
        mcap.release.assert_awaited_once_with("test-agent", "exec-1083")

    def test_success_lost_cas_release_slot_does_not_release(self):
        result, (mdb, mact, mcap, mrec) = _run_apply(
            _success_envelope(), cas_won=False, release_slot=True,
        )
        mcap.release.assert_not_awaited()

    def test_failed_won_release_slot_releases(self):
        result, (mdb, mact, mcap, mrec) = _run_apply(_failed_envelope(), release_slot=True)
        mcap.release.assert_awaited_once_with("test-agent", "exec-1083")

    def test_failed_lost_cas_release_slot_does_not_release(self):
        result, (mdb, mact, mcap, mrec) = _run_apply(
            _failed_envelope(), cas_won=False, release_slot=True,
        )
        mcap.release.assert_not_awaited()

    def test_sync_path_release_slot_false_never_releases(self):
        """The sync path passes release_slot=False — apply_result must not touch
        the slot (execute_task's `finally` owns it)."""
        result, (mdb, mact, mcap, mrec) = _run_apply(_success_envelope(), release_slot=False)
        mcap.release.assert_not_awaited()
