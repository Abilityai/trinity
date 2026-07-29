"""Unit tests for the terminal-write CAS gate (#671 / H4, doc §6.3).

`db.update_execution_status` is an atomic compare-and-set that returns the CAS
winner (`bool`). Every terminal-write caller in
`TaskExecutionService.execute_task` must consume that verdict: a writer that
LOST the race (the row was already terminalised — e.g. a user/operator cancel)
must NOT run "won-only side effects" (complete the activity, reset the dispatch
breaker, or report success). Lost CAS ⇒ reconcile/skip, never act.

The DB-layer CAS itself is covered by
`tests/unit/test_cancelled_not_overwritten.py`; this file covers the SERVICE
layer honouring the verdict.

These are pure unit tests — they run without a backend (the `tests/unit/`
conftest overrides the parent's api_client-dependent autouse fixtures).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _make_execution(status: str = "running"):
    ex = MagicMock()
    ex.id = "exec-test-001"
    ex.status = status
    return ex


def _success_response() -> MagicMock:
    """A healthy 200 agent response that drives execute_task to the SUCCESS
    terminal (mirrors tests/test_cb_probe_execution_close.py)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "response": "done",
        "session_id": "sess-001",
        "metadata": {"cost_usd": 0.01, "input_tokens": 100, "context_window": 200000},
        "execution_log": [],
    }
    return resp


class TestSuccessPathCasGate:
    """SUCCESS terminal must consume the CAS winner (#671/H4)."""

    pytestmark = pytest.mark.unit

    def _run(self, *, cas_won: bool, reconciled_status: str = "cancelled"):
        """Drive execute_task down the SUCCESS path with a 200 response and a
        configurable CAS outcome. Returns (result, mock_activity, mock_record).
        """
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.get_execution.return_value = _make_execution(reconciled_status)
        mock_db.update_execution_status.return_value = cas_won

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True  # transport CB closed

        mock_activity = MagicMock(
            track_activity=AsyncMock(return_value="act-001"),
            complete_activity=AsyncMock(),
        )
        mock_record = AsyncMock()

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", mock_activity),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", AsyncMock(return_value=_success_response())),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
            patch("services.task_execution_service._record_dispatch_terminal", mock_record),
        ):
            svc = TaskExecutionService()
            result = self._await(
                svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                    timeout_seconds=300,
                    model="sonnet",
                )
            )
        return result, mock_activity, mock_record

    @staticmethod
    def _await(coro):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_success_lost_cas_reconciles_and_skips_side_effects(self):
        """When the SUCCESS write loses the CAS (row already cancelled), the
        turn must reconcile to the persisted terminal and run NO won-only side
        effects — no COMPLETED activity, no breaker reset, no SUCCESS result."""
        from models import ActivityState
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            TaskExecutionStatus,
        )

        result, mock_activity, mock_record = self._run(cas_won=False, reconciled_status="cancelled")

        # Reconciled to the row's real terminal, NOT reported as success.
        assert result.status != TaskExecutionStatus.SUCCESS
        assert result.status == TaskExecutionStatus.CANCELLED
        assert result.error_code == TaskExecutionErrorCode.RECONCILED

        # Breaker must NOT be reset by a writer that lost the CAS.
        mock_record.assert_not_awaited()

        # Activity must NOT be completed as COMPLETED.
        completed = [
            c
            for c in mock_activity.complete_activity.await_args_list
            if c.kwargs.get("status") == ActivityState.COMPLETED
        ]
        assert not completed, "lost-CAS turn must not complete the activity as COMPLETED"

    def test_success_won_cas_runs_side_effects(self):
        """When the SUCCESS write wins the CAS, the happy path is unchanged —
        SUCCESS result, COMPLETED activity, breaker reset. Pins against
        over-gating."""
        from models import ActivityState
        from services.task_execution_service import TaskExecutionStatus

        result, mock_activity, mock_record = self._run(cas_won=True)

        assert result.status == TaskExecutionStatus.SUCCESS
        mock_record.assert_awaited_once_with("test-agent", False, None)
        assert any(
            c.kwargs.get("status") == ActivityState.COMPLETED
            for c in mock_activity.complete_activity.await_args_list
        ), "won SUCCESS turn must complete the activity as COMPLETED"


class TestFailedPathCasGate:
    """Non-success terminals route through `_write_terminal_and_gate`, which
    completes the activity only on a won CAS (#671/H4). Exercised via the
    circuit-breaker fast-fail terminal (Site 3)."""

    pytestmark = pytest.mark.unit

    def _run_cb_open(self, *, cas_won: bool):
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.get_execution.return_value = _make_execution("cancelled")
        mock_db.update_execution_status.return_value = cas_won

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False  # transport CB OPEN → fast-fail

        mock_activity = MagicMock(
            track_activity=AsyncMock(return_value="act-001"),
            complete_activity=AsyncMock(),
            # #1804: non-success terminals close through the shared owner.
            close_execution_activity=AsyncMock(return_value=True),
        )

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", mock_activity),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        ):
            svc = TaskExecutionService()
            result = TestSuccessPathCasGate._await(
                svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                    timeout_seconds=300,
                    model="sonnet",
                )
            )
        return result, mock_activity

    def test_failed_lost_cas_closes_activity_in_the_persisted_state(self):
        """#1804 (was: "lost CAS skips the close"). A FAILED terminal that lost
        the CAS must still close the activity — in the state of the terminal that
        actually stands. Skipping it was the #1804 bug: the row goes terminal and
        the activity orphans until the 120-minute duration-fabricating backstop.

        Here the persisted row is CANCELLED, so the close carries CANCELLED — the
        activity can never disagree with the row (#1332)."""
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            TaskExecutionStatus,
        )

        result, mock_activity = self._run_cb_open(cas_won=False)

        mock_activity.close_execution_activity.assert_awaited_once()
        args, kwargs = mock_activity.close_execution_activity.await_args
        assert args[1] == TaskExecutionStatus.CANCELLED  # the persisted terminal
        assert "superseded by" in kwargs["error"]
        # The FAILED result is still reported (the return is unconditional;
        # only the won-only side effects gate).
        assert result.status == TaskExecutionStatus.FAILED
        assert result.error_code == TaskExecutionErrorCode.CIRCUIT_OPEN

    def test_failed_won_cas_completes_activity(self):
        """A FAILED terminal that won the CAS closes the activity as FAILED."""
        from models import TaskExecutionStatus

        result, mock_activity = self._run_cb_open(cas_won=True)

        mock_activity.close_execution_activity.assert_awaited_once()
        args, _kwargs = mock_activity.close_execution_activity.await_args
        assert args[1] == TaskExecutionStatus.FAILED
