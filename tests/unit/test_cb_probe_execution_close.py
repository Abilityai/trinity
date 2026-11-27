"""
Unit tests for #767: CB probe executions left open until backend restart.

Two bugs fixed:
- Fix A: execute_task now fast-fails (closes the execution record) when the
  circuit breaker is open, rather than attempting a long-running HTTP call
  that would be left dangling until cleanup_service's 120-minute stale sweep.
- Fix B: asyncio.CancelledError (Python 3.11+ BaseException) is now caught in
  both execute_task and _execute_task_internal_background so that execution
  records are closed synchronously on backend shutdown, preventing the cleanup
  service from inflating failure duration.

Lives under tests/unit/ since #4620. In tests/ root it stubbed `database`,
`utils.helpers`, `redis` and more into `sys.modules` with `setdefault`, which
keeps whatever an earlier-collected file left there. In the live tier that was
`tests/git_sync/test_s5_conflict_classifier.py`'s load-time `database` stub,
whose `db` is None, so every service `execute_task` reaches was bound to None
and seven tests died before they got to the circuit breaker or the cancel
handler. Here the conftest provides the real `database` on a per-process
file, so this module installs no stubs; it imports the service at collection
instead (below), so the service's collaborators are bound to that database.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from unit._write_params import flat_kwargs

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Bound at collection, while `database` is still the conftest's real module.
# A test elsewhere that swaps `sys.modules["database"]` for its own double and
# is the first to import a service leaves that service holding the double for
# the rest of the process; `execute_task` here then reads settings through it
# (seen with `test_backlog.py` run just before this file, and four others).
import services.task_execution_service  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_execution(status="running"):
    ex = MagicMock()
    ex.id = "exec-test-001"
    ex.status = status
    return ex


# ===========================================================================
# Fix A: Circuit breaker fast-fail
# ===========================================================================

def _activity_double(*, activity_id=None):
    """The activity-service seam, with EVERY awaited method awaitable.

    #1804 added `close_execution_activity` to the terminal path
    (`task_execution_service._write_terminal_and_gate`) — and this double was
    inlined at SEVEN call sites, each wiring only `track_activity` and
    `complete_activity`. So the new await got a plain `MagicMock` and three tests
    died with "object MagicMock can't be used in 'await' expression": not noise,
    but three fast-fail assertions that had quietly stopped running (#2242).

    One factory, so the next seam change is one edit instead of seven silent
    drifts. Anything the service awaits belongs here.
    """
    return MagicMock(
        track_activity=AsyncMock(return_value=activity_id),
        complete_activity=AsyncMock(),
        close_execution_activity=AsyncMock(),
    )

class TestCircuitBreakerFastFail:
    """execute_task closes the execution record immediately when CB is open."""

    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_cb_open_fails_execution_record(self):
        """When CB is open, execute_task marks execution FAILED immediately."""
        from services.task_execution_service import (
            TaskExecutionService, TaskExecutionStatus, TaskExecutionErrorCode,
        )

        mock_db = MagicMock()
        existing_exec = _make_execution("running")
        mock_db.get_execution.return_value = existing_exec
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = existing_exec
        mock_db.mark_execution_dispatched.return_value = None

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_activity_svc = _activity_double(activity_id="act-001")

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", mock_activity_svc),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        ):
            svc = TaskExecutionService()
            result = await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        assert result.status == TaskExecutionStatus.FAILED
        assert result.error_code == TaskExecutionErrorCode.CIRCUIT_OPEN
        assert "circuit breaker" in result.error.lower()

        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED

        # #2242: restoring the await is not the same as testing what the seam now
        # does. #1804's rule is that the CAS winner OWNS the activity close — a
        # fast-fail that writes FAILED and leaves the activity `started` is
        # exactly the bug that issue fixed, and it would satisfy every assertion
        # above. `update_execution_status` returns a truthy MagicMock here, so the
        # CAS is won and the close must have happened in the terminal's own state.
        mock_activity_svc.close_execution_activity.assert_awaited_once()
        close_args = mock_activity_svc.close_execution_activity.await_args
        assert close_args.args[0] == "exec-test-001"
        assert close_args.args[1] == TaskExecutionStatus.FAILED

    @pytest.mark.asyncio
    async def test_cb_open_does_not_call_agent(self):
        """When CB is open, agent_post_with_retry is never called."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        mock_post = AsyncMock()

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double(activity_id="act-001")),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_cb_open_does_not_mark_dispatched(self):
        """When CB is open, execution is NOT marked dispatched (keeps caught by short-circuit cleanup)."""
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = False

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double()),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        ):
            svc = TaskExecutionService()
            await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_db.mark_execution_dispatched.assert_not_called()

    @pytest.mark.asyncio
    async def test_cb_closed_proceeds_normally(self):
        """When CB is closed, execute_task calls agent_post_with_retry as usual."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus
        import httpx

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True  # CB closed

        # Simulate a successful agent response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "response": "done",
            "session_id": "sess-001",
            "metadata": {"cost_usd": 0.01, "input_tokens": 100, "context_window": 200000},
            "execution_log": [],
        }
        mock_post = AsyncMock(return_value=mock_response)

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double(activity_id="act-001")),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            result = await svc.execute_task(
                agent_name="test-agent",
                message="hello",
                triggered_by="schedule",
                execution_id="exec-test-001",
            )

        mock_post.assert_called_once()
        assert result.status == TaskExecutionStatus.SUCCESS


# ===========================================================================
# Fix B: asyncio.CancelledError in execute_task
# ===========================================================================

class TestCancelledErrorInExecuteTask:
    """execute_task closes the execution record when cancelled by backend shutdown."""

    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_cancelled_error_marks_execution_failed(self):
        """When execute_task is cancelled mid-flight, the open execution is closed."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True

        # Simulate cancellation during agent HTTP call
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double()),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

        # Execution must be closed with FAILED status
        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED
        assert "cancelled" in (flat_kwargs(call_kwargs).get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self):
        """CancelledError must propagate after cleanup so asyncio can cancel the task."""
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double()),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

    @pytest.mark.asyncio
    async def test_cancelled_error_skips_already_terminal_execution(self):
        """If execution is already terminal, cancel handler doesn't overwrite it."""
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        mock_db = MagicMock()
        # Already marked CANCELLED by another path
        mock_db.get_execution.return_value = _make_execution(TaskExecutionStatus.CANCELLED)
        mock_db.get_max_parallel_tasks.return_value = 3
        mock_db.create_task_execution.return_value = _make_execution("running")

        mock_capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        mock_capacity.acquire = AsyncMock(return_value=admitted)
        mock_capacity.release = AsyncMock()

        mock_circuit = MagicMock()
        mock_circuit.allow_request.return_value = True
        mock_post = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", _activity_double()),
            patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
            patch("services.task_execution_service.agent_post_with_retry", mock_post),
        ):
            svc = TaskExecutionService()
            with pytest.raises(asyncio.CancelledError):
                await svc.execute_task(
                    agent_name="test-agent",
                    message="hello",
                    triggered_by="schedule",
                    execution_id="exec-test-001",
                )

        mock_db.update_execution_status.assert_not_called()


# ===========================================================================
# Fix B2: asyncio.CancelledError in _execute_task_internal_background
# ===========================================================================

class TestCancelledErrorInBackground:
    """_execute_task_internal_background closes execution on cancel."""

    pytestmark = pytest.mark.unit

    @pytest.mark.asyncio
    async def test_background_cancelled_marks_execution_failed(self):
        """When execute_task raises CancelledError, background wrapper closes the record."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.execution_id = "exec-bg-001"
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

        mock_db.update_execution_status.assert_called_once()
        call_kwargs = mock_db.update_execution_status.call_args
        assert call_kwargs.kwargs.get("status") == TaskExecutionStatus.FAILED
        assert "cancelled" in (flat_kwargs(call_kwargs).get("error") or "").lower()

    @pytest.mark.asyncio
    async def test_background_cancelled_reraises(self):
        """CancelledError must propagate from the background wrapper."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution("running")

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.execution_id = "exec-bg-001"
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

    @pytest.mark.asyncio
    async def test_background_cancelled_skips_already_terminal(self):
        """Cancel handler doesn't overwrite already-terminal execution in background."""
        from models import TaskExecutionStatus

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _make_execution(TaskExecutionStatus.SUCCESS)

        mock_task_service = MagicMock()
        mock_task_service.execute_task = AsyncMock(side_effect=asyncio.CancelledError())

        request = MagicMock()
        request.agent_name = "test-agent"
        request.execution_id = "exec-bg-001"
        request.message = "hello"
        request.triggered_by = "schedule"
        request.model = None
        request.timeout_seconds = 900
        request.allowed_tools = None
        request.attempt = 1
        request.schedule_id = None
        request.schedule_name = None
        request.schedule_cron = None
        request.schedule_next_run = None

        with patch("routers.internal.db", mock_db):
            import routers.internal as internal_mod
            with pytest.raises(asyncio.CancelledError):
                await internal_mod._execute_task_internal_background(mock_task_service, request)

        mock_db.update_execution_status.assert_not_called()
