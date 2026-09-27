"""Unit tests for #2106: abnormal task terminations must not be labelled as a
schedule timeout when the run never approached the configured limit.

Before #2106 the ``except httpx.TimeoutException`` branch in
``execute_task`` always wrote "Task execution timed out after N seconds"
(``error_code=TIMEOUT``), even for a 305s run against a 2700s/3600s limit —
an upstream cutoff mislabelled as a schedule timeout, misleading operators
into raising limits that were never reached.

Fix: ``_classify_timeout_failure`` attributes a timeout only when the run
actually approached the limit (30s attribution grace, mirroring the issue's
evidence); anything else is recorded as ``NETWORK`` with the run's real
duration and the limit kept as context.

Module under test:
    src/backend/services/task_execution_service.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestClassifyTimeoutFailure:
    """Pure-function coverage of the #2106 attribution logic."""

    pytestmark = pytest.mark.unit

    def test_genuine_timeout_keeps_historical_message_and_code(self):
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            _classify_timeout_failure,
        )

        msg, code = _classify_timeout_failure(elapsed_s=3600, timeout_seconds=3600)
        assert msg == "Task execution timed out after 3600 seconds"
        assert code == TaskExecutionErrorCode.TIMEOUT

    def test_run_within_grace_of_limit_is_still_a_timeout(self):
        """#2106 evidence: every genuine timeout sat within 30s of the limit."""
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            _classify_timeout_failure,
        )

        _, code = _classify_timeout_failure(elapsed_s=3590, timeout_seconds=3600)
        assert code == TaskExecutionErrorCode.TIMEOUT

    def test_short_run_with_large_limit_is_upstream_cutoff_not_timeout(self):
        """The issue's headline case: 305s run against a 2700s limit."""
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            _classify_timeout_failure,
        )

        msg, code = _classify_timeout_failure(elapsed_s=305, timeout_seconds=2700)
        assert code == TaskExecutionErrorCode.NETWORK
        assert "305s" in msg
        assert "2700 seconds" in msg
        assert "timed out after" not in msg

    def test_message_keeps_real_exception_context(self):
        from services.task_execution_service import _classify_timeout_failure

        msg, _ = _classify_timeout_failure(
            elapsed_s=305,
            timeout_seconds=3600,
            exc=httpx.ReadTimeout("read timed out"),
        )
        assert "ReadTimeout" in msg

    def test_unset_limit_never_labels_timeout(self):
        from services.task_execution_service import (
            TaskExecutionErrorCode,
            _classify_timeout_failure,
        )

        msg, code = _classify_timeout_failure(elapsed_s=305, timeout_seconds=None)
        assert code == TaskExecutionErrorCode.NETWORK
        assert "unset" in msg


class TestExecuteTaskTimeoutBranch:
    """The except branch drives the same attribution end-to-end.

    ``elapsed`` is real wall-clock (sub-second), so a 1s limit lands on the
    timeout side and a 3600s limit lands on the upstream-cutoff side without
    needing to mock ``datetime.utcnow``.
    """

    pytestmark = pytest.mark.unit

    def _run_execute(self, timeout_seconds: int):
        from services.task_execution_service import TaskExecutionService

        mock_db = MagicMock()
        mock_db.get_execution_timeout.return_value = timeout_seconds
        mock_db.create_task_execution.return_value = MagicMock(id="exec-2106")

        mock_capacity = MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted")),
                                  release=AsyncMock())
        mock_activity = MagicMock(track_activity=AsyncMock(return_value="act-2106"))

        async def _agent_boom(*args, **kwargs):
            raise httpx.ReadTimeout("read timed out")

        with (
            patch("services.task_execution_service.db", mock_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
            patch("services.task_execution_service.activity_service", mock_activity),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
            patch("services.task_execution_service.dispatch_async_eligible", return_value=False),
            patch("services.task_execution_service.note_unreachable_pull_trigger"),
            patch("services.task_execution_service.settings_service.get_platform_default_model", return_value="sonnet"),
            patch("services.task_execution_service.CircuitState", return_value=MagicMock(allow_request=lambda: True)),
            patch("services.task_execution_service._resolve_agent_runtime", return_value="claude"),
            patch("services.task_execution_service.compose_system_prompt", return_value="prompt"),
            patch("services.task_execution_service.is_execution_context_enabled", return_value=False),
            patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_boom),
            patch("services.task_execution_service.terminate_execution_on_agent", new=AsyncMock()),
            patch("services.task_execution_service._write_terminal_and_gate", new=AsyncMock()),
        ):
            svc = TaskExecutionService()
            result = _await(
                svc.execute_task(
                    agent_name="test-agent",
                    message="do the thing",
                    triggered_by="schedule",
                    timeout_seconds=timeout_seconds,
                )
            )
        return result

    def test_short_run_with_large_limit_records_network_not_timeout(self):
        result = self._run_execute(timeout_seconds=3600)
        assert result.status == "failed"
        assert result.error_code == "network"
        assert "aborted after" in result.error
        assert "3600 seconds" in result.error
        assert "timed out after" not in result.error

    def test_run_approaching_limit_keeps_timeout_label(self):
        result = self._run_execute(timeout_seconds=1)
        assert result.status == "failed"
        assert result.error_code == "timeout"
        assert result.error == "Task execution timed out after 1 seconds"
