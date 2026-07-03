"""Unit tests for #1332 — a user-cancelled execution's dispatch activity must be
recorded as ``ActivityState.CANCELLED``, not collapsed into FAILED.

Covers:
  (a) the ``ActivityState.CANCELLED`` enum value + the ``activity_state_for_terminal``
      mapping helper (SUCCESS→COMPLETED, CANCELLED→CANCELLED, everything else→FAILED).
  (b) Path B — the operator-terminate handler (``routers/chat.py
      terminate_agent_execution``) closes the open dispatch activity as CANCELLED
      when the CANCELLED row write wins (``status == "terminated"``), is a no-op on
      ``already_finished``, and never throws when there is no open activity / the
      close raises.
  (c) the collaboration / self-task close helpers map a cancelled result to a
      CANCELLED activity via the shared helper.

Pure unit tests — no backend. Mocks mirror test_679_backend_cancel.py.
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


# ---------------------------------------------------------------------------
# (a) enum value + activity_state_for_terminal helper
# ---------------------------------------------------------------------------
class TestActivityStateEnumAndHelper:
    pytestmark = pytest.mark.unit

    def test_cancelled_enum_value(self):
        from models import ActivityState

        assert ActivityState.CANCELLED == "cancelled"
        assert ActivityState.CANCELLED.value == "cancelled"

    def test_helper_success_to_completed(self):
        from models import activity_state_for_terminal, ActivityState, TaskExecutionStatus

        assert activity_state_for_terminal(TaskExecutionStatus.SUCCESS) == ActivityState.COMPLETED

    def test_helper_cancelled_to_cancelled(self):
        from models import activity_state_for_terminal, ActivityState, TaskExecutionStatus

        assert activity_state_for_terminal(TaskExecutionStatus.CANCELLED) == ActivityState.CANCELLED

    def test_helper_failed_to_failed(self):
        from models import activity_state_for_terminal, ActivityState, TaskExecutionStatus

        assert activity_state_for_terminal(TaskExecutionStatus.FAILED) == ActivityState.FAILED

    def test_helper_other_terminals_to_failed(self):
        """A non-success, non-cancel terminal (and any future status) falls to FAILED."""
        from models import activity_state_for_terminal, ActivityState, TaskExecutionStatus

        for status in (
            TaskExecutionStatus.SKIPPED,
            TaskExecutionStatus.PENDING_RETRY,
            TaskExecutionStatus.RUNNING,
        ):
            assert activity_state_for_terminal(status) == ActivityState.FAILED

    def test_helper_accepts_bare_string(self):
        """The persisted status may arrive as its bare string value."""
        from models import activity_state_for_terminal, ActivityState

        assert activity_state_for_terminal("cancelled") == ActivityState.CANCELLED
        assert activity_state_for_terminal("success") == ActivityState.COMPLETED
        assert activity_state_for_terminal("failed") == ActivityState.FAILED


# ---------------------------------------------------------------------------
# (b) Path B — terminate handler closes dispatch activity as CANCELLED
# ---------------------------------------------------------------------------
class _AsyncCM:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


def _run_terminate(*, agent_status="terminated", open_activity_id="act-1",
                   complete_raises=False, cancel_won=True, reconciled_status=None):
    import routers.chat as chat
    from models import TaskExecutionStatus

    # Running (not queued) row so the backlog short-circuit is skipped.
    exec_row = MagicMock()
    exec_row.status = TaskExecutionStatus.RUNNING

    mock_db = MagicMock()
    # get_execution is read early (backlog short-circuit, sees RUNNING) and again
    # in the #1332 CAS-lost branch (re-read sees the row's real terminal state).
    if reconciled_status is not None:
        reconciled_row = MagicMock()
        reconciled_row.status = reconciled_status
        mock_db.get_execution.side_effect = [exec_row, reconciled_row]
    else:
        mock_db.get_execution.return_value = exec_row
    mock_db.update_execution_status.return_value = cancel_won
    mock_db.get_open_activity_id_for_execution.return_value = open_activity_id

    container = MagicMock()
    container.status = "running"

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"status": agent_status, "returncode": -15}
    client = MagicMock(post=AsyncMock(return_value=response))

    complete = AsyncMock(side_effect=RuntimeError("boom")) if complete_raises else AsyncMock()
    mock_activity = MagicMock(track_activity=AsyncMock(), complete_activity=complete)

    mock_capacity = MagicMock()
    mock_capacity.force_release = AsyncMock(return_value=MagicMock(was_running=True, slots_cleared=1))

    current_user = MagicMock(id=1)

    with (
        patch.object(chat, "db", mock_db),
        patch.object(chat, "get_agent_container", return_value=container),
        patch.object(chat, "agent_httpx_client", lambda *a, **k: _AsyncCM(client)),
        patch.object(chat, "get_capacity_manager", return_value=mock_capacity),
        patch.object(chat, "activity_service", mock_activity),
    ):
        result = _await(
            chat.terminate_agent_execution(
                execution_id="exec-1332",
                task_execution_id="texec-1332",
                name="test-agent",
                current_user=current_user,
            )
        )
    return result, mock_db, mock_activity


class TestTerminateHandlerPathB:
    pytestmark = pytest.mark.unit

    def test_terminated_closes_dispatch_activity_cancelled(self):
        from models import ActivityState

        result, mdb, mact = _run_terminate(agent_status="terminated")

        # The CANCELLED row write happened, then the open dispatch activity is
        # looked up and closed as CANCELLED.
        mdb.get_open_activity_id_for_execution.assert_called_once_with("texec-1332")
        mact.complete_activity.assert_awaited_once()
        assert mact.complete_activity.await_args.kwargs["activity_id"] == "act-1"
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.CANCELLED
        assert result["status"] == "terminated"

    def test_already_finished_does_not_close_as_cancelled(self):
        """On already_finished the agent's real terminal stands — no cancel close."""
        result, mdb, mact = _run_terminate(agent_status="already_finished")

        mdb.update_execution_status.assert_not_called()
        mdb.get_open_activity_id_for_execution.assert_not_called()
        mact.complete_activity.assert_not_awaited()
        assert result["status"] == "already_finished"

    def test_no_open_activity_is_noop(self):
        """Activity already closed → lookup returns None → no complete_activity call, no throw."""
        result, mdb, mact = _run_terminate(open_activity_id=None)

        mdb.get_open_activity_id_for_execution.assert_called_once_with("texec-1332")
        mact.complete_activity.assert_not_awaited()
        assert result["status"] == "terminated"

    def test_close_failure_does_not_fail_terminate(self):
        """complete_activity raising is swallowed — the terminate request still succeeds."""
        result, _mdb, mact = _run_terminate(complete_raises=True)

        mact.complete_activity.assert_awaited_once()
        assert result["status"] == "terminated"

    def test_cas_lost_to_success_closes_activity_completed_not_cancelled(self):
        """#1332 CAS-gating: if the CANCELLED row-write loses the CAS to a row that
        already went terminal as SUCCESS, the activity is closed in the row's REAL
        state (COMPLETED), never CANCELLED — the activity must not disagree with the
        row. error is dropped for a COMPLETED close."""
        from models import ActivityState, TaskExecutionStatus

        result, mdb, mact = _run_terminate(
            cancel_won=False, reconciled_status=TaskExecutionStatus.SUCCESS,
        )

        mact.complete_activity.assert_awaited_once()
        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.COMPLETED
        assert mact.complete_activity.await_args.kwargs["error"] is None
        assert result["status"] == "terminated"

    def test_cas_lost_to_failed_closes_activity_failed(self):
        """#1332 CAS-gating: a lost CAS to a FAILED row closes the activity FAILED."""
        from models import ActivityState, TaskExecutionStatus

        result, mdb, mact = _run_terminate(
            cancel_won=False, reconciled_status=TaskExecutionStatus.FAILED,
        )

        assert mact.complete_activity.await_args.kwargs["status"] == ActivityState.FAILED
        assert result["status"] == "terminated"


# ---------------------------------------------------------------------------
# (c) collaboration / self-task closes map a cancelled result to CANCELLED
# ---------------------------------------------------------------------------
def _cancelled_result():
    from models import TaskExecutionStatus

    r = MagicMock()
    r.status = TaskExecutionStatus.CANCELLED
    r.response = "stopped"
    r.error = "Execution cancelled by user"
    r.cost = 0.01
    r.context_used = 100
    r.context_max = 200000
    return r


class TestCollaborationAndSelfTaskCloses:
    pytestmark = pytest.mark.unit

    def test_collaboration_close_cancelled(self):
        import routers.chat as chat
        from models import ActivityState

        mock_activity = MagicMock(complete_activity=AsyncMock())
        with patch.object(chat, "activity_service", mock_activity):
            _await(
                chat._complete_collaboration_activity(
                    "collab-act", _cancelled_result(), "exec-1332", 1234,
                )
            )
        assert mock_activity.complete_activity.await_args.kwargs["status"] == ActivityState.CANCELLED

    def test_self_task_close_cancelled(self):
        import routers.chat as chat
        from models import ActivityState

        request = MagicMock(inject_result=False, chat_session_id=None)
        mock_activity = MagicMock(complete_activity=AsyncMock())
        with (
            patch.object(chat, "activity_service", mock_activity),
            patch.object(chat, "_websocket_manager", None),
        ):
            _await(
                chat._finalize_self_task(
                    is_self_task=True,
                    self_task_activity_id="self-act",
                    agent_name="test-agent",
                    request=request,
                    result=_cancelled_result(),
                    execution_id="exec-1332",
                    user_id=1,
                    user_email="u@example.com",
                    execution_time_ms=1234,
                )
            )
        assert mock_activity.complete_activity.await_args.kwargs["status"] == ActivityState.CANCELLED
