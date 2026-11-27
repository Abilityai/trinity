"""A backend shutdown does not fail a turn the agent is still running (#4408).

Cancelling the dispatch coroutine does not stop the agent. Both cancellation
handlers used to write the row FAILED with "Execution cancelled (backend
shutdown)" whatever the turn's state, so a deploy recorded a failure for work
that then completed, and the real result lost the compare-and-set.

The rule now, in one writer (`settle_cancelled_dispatch`): a turn already
handed to the agent keeps its row `running` for the result path; a turn that
never reached the agent is closed at once, with its activity (#767, #1804).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.task_execution_service as tes  # noqa: E402
from models import TaskExecutionStatus  # noqa: E402

pytestmark = pytest.mark.unit


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _row(status=TaskExecutionStatus.RUNNING, marker=None):
    return SimpleNamespace(id="exec-1", status=status, claude_session_id=marker)


def _settle(row, *, won=True, activity_id=None):
    mock_db = MagicMock()
    mock_db.get_execution.return_value = row
    mock_db.update_execution_status.return_value = won
    mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
    with patch.object(tes, "db", mock_db), patch.object(tes, "activity_service", mock_activity):
        outcome = _await(tes.settle_cancelled_dispatch("exec-1", activity_id=activity_id))
    return outcome, mock_db, mock_activity


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [
    "dispatched",                              # sync dispatch
    "dispatched_async",                        # fire-and-forget, before its 202
    "0b4c7f0e-6d1b-4c58-9c0e-0a1f6f0b7d11",   # a real session id
])
def test_a_turn_the_agent_holds_is_left_running(marker):
    outcome, mock_db, mock_activity = _settle(_row(marker=marker), activity_id="act-1")

    assert outcome == "left_running"
    mock_db.update_execution_status.assert_not_called()
    # The activity stays open for whoever settles the row (#1804: closed once,
    # by the terminal's winner).
    mock_activity.close_execution_activity.assert_not_awaited()


@pytest.mark.parametrize("marker", [None, ""])
def test_a_turn_that_never_reached_the_agent_is_failed_and_its_activity_closed(marker):
    outcome, mock_db, mock_activity = _settle(_row(marker=marker), activity_id="act-1")

    assert outcome == "failed"
    kwargs = mock_db.update_execution_status.call_args.kwargs
    assert kwargs["execution_id"] == "exec-1"
    assert kwargs["status"] == TaskExecutionStatus.FAILED
    assert kwargs["result"].error == tes.SHUTDOWN_CANCEL_ERROR
    mock_activity.close_execution_activity.assert_awaited_once()
    args, kw = mock_activity.close_execution_activity.await_args
    assert args == ("exec-1", TaskExecutionStatus.FAILED)
    assert kw == {"error": tes.SHUTDOWN_CANCEL_ERROR, "activity_id": "act-1"}


def test_a_queued_turn_that_never_reached_the_agent_is_failed_too():
    outcome, mock_db, _ = _settle(_row(status=TaskExecutionStatus.QUEUED))

    assert outcome == "failed"
    mock_db.update_execution_status.assert_called_once()


def test_a_lost_write_does_not_close_the_activity():
    """#1804: only the writer that wins the terminal closes the activity."""
    outcome, _, mock_activity = _settle(_row(), won=False)

    assert outcome == "failed"
    mock_activity.close_execution_activity.assert_not_awaited()


@pytest.mark.parametrize("status", [
    TaskExecutionStatus.SUCCESS, TaskExecutionStatus.FAILED, TaskExecutionStatus.CANCELLED,
])
def test_a_row_already_terminal_is_not_touched(status):
    outcome, mock_db, mock_activity = _settle(_row(status=status, marker="dispatched"))

    assert outcome == "already_terminal"
    mock_db.update_execution_status.assert_not_called()
    mock_activity.close_execution_activity.assert_not_awaited()


def test_a_missing_row_or_no_id_writes_nothing():
    outcome, mock_db, _ = _settle(None)
    assert outcome == "not_found"
    mock_db.update_execution_status.assert_not_called()

    assert _await(tes.settle_cancelled_dispatch(None)) == "not_found"


def test_a_database_fault_never_masks_the_cancellation():
    mock_db = MagicMock()
    mock_db.get_execution.side_effect = RuntimeError("db down")
    with patch.object(tes, "db", mock_db):
        assert _await(tes.settle_cancelled_dispatch("exec-1")) == "error"


# ---------------------------------------------------------------------------
# execute_task: where the cancellation lands decides the outcome
# ---------------------------------------------------------------------------

def _execute_cancelled(*, cancel_at: str):
    """Run the real `execute_task` and cancel it either before the turn is
    handed to the agent or while the agent call is in flight. The row is one
    object, and the real `mark_execution_dispatched` call stamps it."""
    row = _row()
    mock_db = MagicMock()
    mock_db.get_execution.return_value = row
    mock_db.update_execution_status.return_value = True
    mock_db.get_max_parallel_tasks.return_value = 3

    def _mark(execution_id, async_dispatch=False):
        row.claude_session_id = "dispatched_async" if async_dispatch else "dispatched"
        return True
    mock_db.mark_execution_dispatched.side_effect = _mark

    admitted = MagicMock()
    admitted.state = "admitted"
    mock_capacity = MagicMock(acquire=AsyncMock(return_value=admitted), release=AsyncMock())
    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-1"),
        close_execution_activity=AsyncMock(return_value=True),
    )
    circuit = MagicMock()
    circuit.allow_request.return_value = True

    patches = [
        patch.object(tes, "db", mock_db),
        patch.object(tes, "get_capacity_manager", return_value=mock_capacity),
        patch.object(tes, "activity_service", mock_activity),
        patch.object(tes, "CircuitState", return_value=circuit),
        patch.object(tes, "dispatch_breaker_active", return_value=False),
    ]
    if cancel_at == "before_dispatch":
        # After the activity is tracked, before the dispatch marker is written.
        patches.append(patch.object(
            tes.TaskExecutionService, "_breaker_fast_fail",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ))
    else:
        patches.append(patch.object(
            tes.TaskExecutionService, "_call_agent_with_retries",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ))

    for p in patches:
        p.start()
    try:
        svc = tes.TaskExecutionService()
        with pytest.raises(asyncio.CancelledError):
            _await(svc.execute_task(
                agent_name="test-agent", message="hello", triggered_by="schedule",
                execution_id="exec-1", timeout_seconds=300, model="claude-sonnet-4-6",
            ))
    finally:
        for p in reversed(patches):
            p.stop()
    return row, mock_db, mock_activity, mock_capacity


def test_cancelled_while_the_agent_runs_the_turn_writes_no_terminal():
    row, mock_db, mock_activity, _ = _execute_cancelled(cancel_at="agent_call")

    assert row.claude_session_id == "dispatched", "the turn was not handed over"
    mock_db.update_execution_status.assert_not_called()
    mock_activity.close_execution_activity.assert_not_awaited()


def test_cancelled_before_the_turn_reaches_the_agent_is_failed():
    row, mock_db, mock_activity, _ = _execute_cancelled(cancel_at="before_dispatch")

    assert row.claude_session_id is None
    kwargs = mock_db.update_execution_status.call_args.kwargs
    assert kwargs["status"] == TaskExecutionStatus.FAILED
    assert kwargs["result"].error == tes.SHUTDOWN_CANCEL_ERROR
    mock_activity.close_execution_activity.assert_awaited_once()
    assert mock_activity.close_execution_activity.await_args.kwargs["activity_id"] == "act-1"


def test_the_cancellation_still_propagates_and_the_slot_is_released():
    _, _, _, mock_capacity = _execute_cancelled(cancel_at="agent_call")

    mock_capacity.release.assert_awaited_once()


# ---------------------------------------------------------------------------
# The background wrapper: the second place a cancellation lands
# ---------------------------------------------------------------------------

def _background_cancelled(row):
    import routers.internal as internal

    mock_db = MagicMock()
    mock_db.get_execution.return_value = row
    mock_db.update_execution_status.return_value = True
    mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
    task_service = MagicMock(execute_task=AsyncMock(side_effect=asyncio.CancelledError()))
    request = MagicMock(execution_id="exec-1", agent_name="test-agent")
    # Patched through the writer the router actually holds: another test module
    # can re-import the service, and a patch on a second copy would miss.
    writer_globals = internal.settle_cancelled_dispatch.__globals__
    with patch.dict(writer_globals, {"db": mock_db, "activity_service": mock_activity}):
        with pytest.raises(asyncio.CancelledError):
            _await(internal._execute_task_internal_background(task_service, request))
    return mock_db, mock_activity


def test_the_background_wrapper_leaves_a_turn_the_agent_holds():
    mock_db, mock_activity = _background_cancelled(_row(marker="dispatched"))

    mock_db.update_execution_status.assert_not_called()
    mock_activity.close_execution_activity.assert_not_awaited()


def test_the_background_wrapper_fails_a_turn_that_never_reached_the_agent():
    mock_db, mock_activity = _background_cancelled(_row())

    assert mock_db.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.FAILED
    mock_activity.close_execution_activity.assert_awaited_once()


@pytest.mark.parametrize("won", [True, False])
def test_an_escaped_exception_fails_the_row_and_closes_the_activity_it_won(won):
    """#1804, found while moving the cancel handler's close out of this
    function: the generic failure writer here never closed its activity."""
    import routers.internal as internal

    mock_db = MagicMock()
    mock_db.get_execution.return_value = _row(marker="dispatched")
    mock_db.update_execution_status.return_value = won
    mock_activity = MagicMock(close_execution_activity=AsyncMock(return_value=True))
    task_service = MagicMock(execute_task=AsyncMock(side_effect=RuntimeError("boom")))
    request = MagicMock(execution_id="exec-1", agent_name="test-agent")

    with patch.object(internal, "db", mock_db), \
            patch.object(internal, "activity_service", mock_activity):
        _await(internal._execute_task_internal_background(task_service, request))

    assert mock_db.update_execution_status.call_args.kwargs["status"] == TaskExecutionStatus.FAILED
    if won:
        mock_activity.close_execution_activity.assert_awaited_once()
        args, kwargs = mock_activity.close_execution_activity.await_args
        assert args == ("exec-1", TaskExecutionStatus.FAILED)
        assert "boom" in kwargs["error"]
    else:
        mock_activity.close_execution_activity.assert_not_awaited()


def test_both_handlers_use_the_one_writer():
    """Two copies of this rule is how one of them would drift."""
    import ast

    for rel in ("services/task_execution_service.py", "routers/internal.py"):
        tree = ast.parse((_BACKEND / rel).read_text())
        handlers = [
            h for h in ast.walk(tree)
            if isinstance(h, ast.ExceptHandler)
            and isinstance(h.type, ast.Attribute) and h.type.attr == "CancelledError"
            and any(
                isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "backend shutdown" in n.value.lower()
                for n in ast.walk(h)
            )
        ]
        assert handlers == [], f"{rel} writes the shutdown terminal itself"

    assert tes.SHUTDOWN_CANCEL_ERROR == "Execution cancelled (backend shutdown)"
