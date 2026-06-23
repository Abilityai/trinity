"""Unit tests for #679 (F4): a user cancel is neither a success nor a failure
for the agent's ``consecutive_failures`` health counter.

``AgentState.record_task_finish`` feeds ``consecutive_failures``, which the
dispatch circuit breaker (#526) and ``/health`` consume. Before F4 the two
cancel sub-paths disagreed: the SIGKILL→504 branch recorded ``success=False``
(incrementing the counter — operator cancels could trip the breaker on a healthy
agent) while the graceful-exit-0 branch recorded ``success=True`` (resetting it).
F4 makes a cancel NEUTRAL: it finishes the task (decrement active count, stamp
last_task_at) but touches neither counter, on BOTH sub-paths.

The conftest preloads the agent_server namespace package.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import agent_server.routers.chat as chat_mod
from agent_server.state import AgentState

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# (a) state-level: record_task_finish(None) is neutral
# ---------------------------------------------------------------------------
class TestNeutralFinish:
    def test_none_leaves_failure_counter_unchanged(self):
        st = AgentState()
        st.consecutive_failures = 2
        st.record_task_start()
        before = st.consecutive_failures
        st.record_task_finish(success=None)
        assert st.consecutive_failures == before          # neither reset nor incremented
        assert st.active_task_count == 0                   # task still finished
        assert st.last_task_at is not None

    def test_false_still_increments(self):
        st = AgentState()
        st.consecutive_failures = 2
        st.record_task_finish(success=False)
        assert st.consecutive_failures == 3

    def test_true_still_resets(self):
        st = AgentState()
        st.consecutive_failures = 5
        st.record_task_finish(success=True)
        assert st.consecutive_failures == 0


# ---------------------------------------------------------------------------
# (b) execute_task records NEUTRAL for a cancel, success/failure otherwise
# ---------------------------------------------------------------------------
def _req(**over):
    base = dict(
        message="do the thing", model="sonnet", allowed_tools=None,
        system_prompt=None, timeout_seconds=300, max_turns=None,
        execution_id="exec-1", resume_session_id=None, persist_session=False,
        images=None, async_result=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _runtime(*, returns=None, raises=None):
    rt = MagicMock()
    if raises is not None:
        rt.execute_headless = AsyncMock(side_effect=raises)
    else:
        rt.execute_headless = AsyncMock(return_value=returns)
    return rt


def _ok_return():
    md = MagicMock()
    md.model_dump.return_value = {"cost_usd": 0.03}
    return ("final answer", [{"type": "result"}], md, "sess-1")


def _drive(request, *, runtime, was_terminated):
    """Run execute_task; return the agent_state mock so callers inspect
    record_task_finish."""
    registry = MagicMock()
    registry.was_terminated.return_value = was_terminated
    state_mock = MagicMock()

    with (
        patch.object(chat_mod, "get_runtime", return_value=runtime),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
        patch.object(chat_mod, "agent_state", state_mock),
        patch.object(chat_mod.result_callback, "try_spawn_async", return_value=False),
    ):
        try:
            asyncio.run(chat_mod.execute_task(request))
        except HTTPException:
            pass
    return state_mock


def _finish_kwarg(state_mock):
    """The `success=` value passed to the single record_task_finish call."""
    call = state_mock.record_task_finish.call_args
    assert call is not None, "record_task_finish was never called"
    if call.kwargs:
        return call.kwargs.get("success")
    return call.args[0]


class TestExecuteTaskNeutralAccounting:
    def test_graceful_cancel_records_neutral(self):
        st = _drive(_req(), runtime=_runtime(returns=_ok_return()), was_terminated=True)
        assert _finish_kwarg(st) is None

    def test_success_records_true(self):
        st = _drive(_req(), runtime=_runtime(returns=_ok_return()), was_terminated=False)
        assert _finish_kwarg(st) is True

    def test_504_cancel_records_neutral(self):
        st = _drive(
            _req(),
            runtime=_runtime(raises=HTTPException(status_code=504, detail="t")),
            was_terminated=True,
        )
        assert _finish_kwarg(st) is None

    def test_genuine_failure_records_false(self):
        st = _drive(
            _req(),
            runtime=_runtime(raises=HTTPException(status_code=500, detail="boom")),
            was_terminated=False,
        )
        assert _finish_kwarg(st) is False
