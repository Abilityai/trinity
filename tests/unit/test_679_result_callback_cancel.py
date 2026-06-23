"""Unit tests for #679 on the async fire-and-forget path (#1083): the agent-side
``result_callback._run_and_report`` relabels a cancelled turn before delivery.

After the turn's envelope is built (success or HTTPException), the handler
post-checks the ``was_terminated`` marker and, unless the envelope is an
auth/rate terminal (Issue 6/C6 — keep the AUTH dispatch breaker / SUB-003 firing
on async too), overrides it to ``cancelled``. Covers graceful exit-0 AND
SIGKILL→504.

The conftest preloads the agent_server namespace package.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agent_server.services import result_callback as rc  # noqa: E402

pytestmark = pytest.mark.unit


def _req(**over):
    base = dict(
        message="hi", model="sonnet", allowed_tools=None, system_prompt=None,
        timeout_seconds=300, max_turns=None, execution_id="exec-1",
        resume_session_id=None, persist_session=False, images=None,
        async_result=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _success_runtime():
    md = MagicMock()
    md.model_dump.return_value = {"cost_usd": 0.04, "session_id": "meta-sess"}
    rt = MagicMock()
    rt.execute_headless = AsyncMock(return_value=("final answer", [{"t": 1}], md, "sess-1"))
    return rt


def _raising_runtime(exc):
    rt = MagicMock()
    rt.execute_headless = AsyncMock(side_effect=exc)
    return rt


def _run(request, *, runtime, was_terminated):
    """Drive _run_and_report, returning the envelope handed to _deliver."""
    reg = MagicMock()
    reg.was_terminated.return_value = was_terminated
    deliver = AsyncMock(return_value=True)
    with (
        patch("agent_server.services.runtime_adapter.get_runtime", return_value=runtime),
        patch.object(rc, "get_process_registry", return_value=reg),
        patch.object(rc, "_persist", MagicMock()),
        patch.object(rc, "_delete", MagicMock()),
        patch.object(rc, "_deliver", deliver),
        patch.object(rc, "agent_state", MagicMock(agent_name="agent-a")),
    ):
        asyncio.run(rc._run_and_report(request, "http://b", "k", time.monotonic()))
    assert deliver.await_count == 1
    return deliver.call_args.args[2]


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_cancelled_override_keeps_payload_drops_error_code(self):
        env = {
            "status": "failed", "error": "boom", "error_code": "timeout",
            "terminal_reason": "max_duration", "metadata": {"cost_usd": 0.02},
            "response": "partial", "session_id": "s1", "execution_log": [{"t": 1}],
        }
        out = rc._cancelled_override(env)
        assert out["status"] == "cancelled"
        assert out["terminal_reason"] == "cancelled"
        assert out["error_code"] is None
        # response / metadata / session_id / execution_log preserved
        assert out["response"] == "partial"
        assert out["metadata"] == {"cost_usd": 0.02}
        assert out["session_id"] == "s1"
        assert out["execution_log"] == [{"t": 1}]

    @pytest.mark.parametrize("env,expected", [
        ({"error_code": "auth", "terminal_reason": "auth"}, True),
        ({"error_code": None, "terminal_reason": "rate_limit"}, True),
        ({"error_code": None, "terminal_reason": "max_duration"}, False),
        ({"status": "success", "terminal_reason": "completed"}, False),
        ({"error_code": None, "terminal_reason": "empty_result"}, False),
    ])
    def test_is_auth_or_rate(self, env, expected):
        assert rc._is_auth_or_rate(env) is expected


# ---------------------------------------------------------------------------
# _run_and_report relabel
# ---------------------------------------------------------------------------
class TestRunAndReportCancel:
    def test_success_terminated_overrides_to_cancelled(self):
        env = _run(_req(), runtime=_success_runtime(), was_terminated=True)
        assert env["status"] == "cancelled"
        assert env["terminal_reason"] == "cancelled"
        assert env["response"] == "final answer"  # graceful final message kept

    def test_504_terminated_overrides_to_cancelled(self):
        env = _run(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=504, detail="timed out")),
            was_terminated=True,
        )
        assert env["status"] == "cancelled"
        assert env["terminal_reason"] == "cancelled"

    def test_503_auth_terminated_stays_auth_failed(self):
        # C6: the AUTH dispatch breaker must still record on the async path.
        env = _run(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=503, detail="Authentication failure")),
            was_terminated=True,
        )
        assert env["status"] == "failed"
        assert env["error_code"] == "auth"

    def test_429_rate_terminated_stays_failed(self):
        env = _run(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=429, detail="rate limited")),
            was_terminated=True,
        )
        assert env["status"] == "failed"
        assert env["terminal_reason"] == "rate_limit"

    def test_success_not_terminated_keeps_success(self):
        env = _run(_req(), runtime=_success_runtime(), was_terminated=False)
        assert env["status"] == "success"

    def test_504_not_terminated_keeps_failed(self):
        env = _run(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=504, detail="timed out")),
            was_terminated=False,
        )
        assert env["status"] == "failed"
        assert env["terminal_reason"] == "max_duration"


def _run_capture_state(request, *, runtime, was_terminated):
    """Like _run but returns the `success=` value handed to record_task_finish,
    so F4 (neutral cancel accounting) can be asserted on the async path."""
    reg = MagicMock()
    reg.was_terminated.return_value = was_terminated
    state = MagicMock(agent_name="agent-a")
    with (
        patch("agent_server.services.runtime_adapter.get_runtime", return_value=runtime),
        patch.object(rc, "get_process_registry", return_value=reg),
        patch.object(rc, "_persist", MagicMock()),
        patch.object(rc, "_delete", MagicMock()),
        patch.object(rc, "_deliver", AsyncMock(return_value=True)),
        patch.object(rc, "agent_state", state),
    ):
        asyncio.run(rc._run_and_report(request, "http://b", "k", time.monotonic()))
    call = state.record_task_finish.call_args
    assert call is not None, "record_task_finish was never called"
    return call.kwargs.get("success") if call.kwargs else call.args[0]


class TestRunAndReportNeutralAccounting:
    """#679 F4 parity on the async path: a cancel must not touch the
    consecutive_failures counter the dispatch breaker (#526) reads — neither the
    graceful-exit-0 cancel (was wrongly counted as success) nor the 504/502
    cancel (was wrongly counted as failure)."""

    def test_success_terminated_records_neutral(self):
        assert _run_capture_state(_req(), runtime=_success_runtime(), was_terminated=True) is None

    def test_504_terminated_records_neutral(self):
        assert _run_capture_state(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=504, detail="t")),
            was_terminated=True,
        ) is None

    def test_503_auth_terminated_records_failure(self):
        # C6: auth stays a failure so the AUTH breaker still counts it.
        assert _run_capture_state(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=503, detail="Authentication failure")),
            was_terminated=True,
        ) is False

    def test_success_not_terminated_records_success(self):
        assert _run_capture_state(_req(), runtime=_success_runtime(), was_terminated=False) is True

    def test_failure_not_terminated_records_failure(self):
        assert _run_capture_state(
            _req(),
            runtime=_raising_runtime(HTTPException(status_code=500, detail="boom")),
            was_terminated=False,
        ) is False
