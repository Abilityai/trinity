"""Unit tests for #679 backend finalization:

(a) ``apply_result`` honors ``envelope.status`` on the failure-style branch — a
    CANCELLED envelope writes/returns CANCELLED, and a FAILED envelope still
    writes/returns FAILED (the generalization is a no-op for every current
    caller — CRITICAL regression).
(b) ``execute_task`` sync cross-validation — a 200 agent reply carrying
    ``status:"cancelled"`` finalizes CANCELLED (never SUCCESS), while a 200 with
    no ``status`` (old image) is unchanged SUCCESS (backward-compat regression).

Pure unit tests — no backend. Mocks mirror test_1083_apply_result.py and
test_terminal_write_cas_gate.py.
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
    ex.id = "exec-679"
    ex.status = status
    return ex


# ---------------------------------------------------------------------------
# (a) apply_result honors envelope.status
# ---------------------------------------------------------------------------
def _run_apply(envelope, *, cas_won=True, activity_id="act-1", release_slot=False):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    mock_db.get_execution.return_value = _make_execution("cancelled")

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
                "test-agent", envelope, activity_id=activity_id,
                breaker_enabled=False, release_slot=release_slot,
            )
        )
    return result, (mock_db, mock_activity, mock_capacity, mock_record)


class TestApplyResultHonorsStatus:
    pytestmark = pytest.mark.unit

    def test_cancelled_envelope_writes_and_returns_cancelled(self):
        from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus
        from models import ActivityState

        env = TerminalEnvelope(
            execution_id="exec-679",
            status=TaskExecutionStatus.CANCELLED,
            error="Execution cancelled by user",
            metadata={"cost_usd": 0.05, "input_tokens": 100, "context_window": 200000},
        )
        result, (mdb, mact, mcap, mrec) = _run_apply(env)

        assert mdb.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.CANCELLED
        assert result.status == TaskExecutionStatus.CANCELLED
        assert result.error == "Execution cancelled by user"
        # cost salvaged from metadata so the cancelled row records real spend.
        assert mdb.update_execution_status.call_args.kwargs["cost"] == 0.05
        # #1332: the dispatch activity closes as CANCELLED (not FAILED) so a
        # user-cancel doesn't pollute activity-derived views. A cancel is not an
        # AUTH failure → no breaker outcome recorded.
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.CANCELLED
        mrec.assert_not_awaited()

    def test_failed_envelope_still_failed_regression(self):
        """CRITICAL: the generalization must be a no-op for the existing FAILED caller."""
        from services.task_execution_service import (
            TerminalEnvelope, TaskExecutionStatus,
        )

        env = TerminalEnvelope(
            execution_id="exec-679",
            status=TaskExecutionStatus.FAILED,
            error="agent said no",
            metadata={"cost_usd": 0.02},
        )
        result, (mdb, *_rest) = _run_apply(env)
        assert mdb.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.FAILED
        assert result.status == TaskExecutionStatus.FAILED

    def test_cancelled_release_slot_gated_on_cas(self):
        from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

        env = TerminalEnvelope(
            execution_id="exec-679", status=TaskExecutionStatus.CANCELLED, error="x",
            metadata={},
        )
        # Won → released.
        _r, (_mdb, _ma, mcap, _mr) = _run_apply(env, cas_won=True, release_slot=True)
        mcap.release.assert_awaited_once_with("test-agent", "exec-679")
        # Lost CAS → not released.
        _r2, (_mdb2, _ma2, mcap2, _mr2) = _run_apply(env, cas_won=False, release_slot=True)
        mcap2.release.assert_not_awaited()


# ---------------------------------------------------------------------------
# (b) execute_task sync cross-validation
# ---------------------------------------------------------------------------
def _agent_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _run_execute_task(response_payload: dict):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution.return_value = _make_execution("running")
    mock_db.update_execution_status.return_value = True  # CAS won

    mock_capacity = MagicMock()
    admitted = MagicMock()
    admitted.state = "admitted"
    mock_capacity.acquire = AsyncMock(return_value=admitted)
    mock_capacity.release = AsyncMock()

    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True

    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-001"),
        complete_activity=AsyncMock(),
    )

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry",
              AsyncMock(return_value=_agent_response(response_payload))),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
    ):
        svc = TaskExecutionService()
        result = _await(
            svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-679",
                timeout_seconds=300,
                model="sonnet",
            )
        )
    return result, mock_db


class TestSyncCrossValidation:
    pytestmark = pytest.mark.unit

    def test_200_status_cancelled_finalizes_cancelled(self):
        from services.task_execution_service import TaskExecutionStatus

        result, mock_db = _run_execute_task({
            "status": "cancelled",
            "response": "stopped mid-task",
            "session_id": None,
            "metadata": {"cost_usd": 0.03, "context_window": 200000},
            "execution_log": [],
        })
        assert result.status == TaskExecutionStatus.CANCELLED
        assert mock_db.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.CANCELLED

    def test_200_without_status_unchanged_success(self):
        """Backward-compat: an old agent image omits `status` → SUCCESS path."""
        from services.task_execution_service import TaskExecutionStatus

        result, mock_db = _run_execute_task({
            "response": "done",
            "session_id": "sess-1",
            "metadata": {"cost_usd": 0.01, "input_tokens": 100, "context_window": 200000},
            "execution_log": [],
        })
        assert result.status == TaskExecutionStatus.SUCCESS
        assert mock_db.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.SUCCESS

    def test_200_status_success_explicit_is_success(self):
        from services.task_execution_service import TaskExecutionStatus

        result, _mdb = _run_execute_task({
            "status": "success",
            "response": "done",
            "session_id": "sess-1",
            "metadata": {"cost_usd": 0.01, "context_window": 200000},
            "execution_log": [],
        })
        assert result.status == TaskExecutionStatus.SUCCESS
