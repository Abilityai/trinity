"""Unit tests for #679 on the fire-and-forget result-callback endpoint (#1083):
``routers.agents.agent_execution_result`` maps a ``cancelled`` envelope to the
CANCELLED terminal (3-way status map: success→SUCCESS, cancelled→CANCELLED,
else→FAILED).

Mirrors test_1083_callback_endpoint.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, auth_header="Bearer k"):
        self.headers = {"Authorization": auth_header}


def _execution(*, agent_name="agent-a", status="running", claude_session_id="dispatched_async"):
    return SimpleNamespace(
        id="exec-1", agent_name=agent_name, status=status,
        claude_session_id=claude_session_id,
    )


def _drive(payload, *, execution=None, apply_status="cancelled"):
    from routers.agents import agent_execution_result

    if execution is None:
        execution = _execution()
    mock_db = MagicMock()
    mock_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "agent-a"}
    mock_db.get_execution.return_value = execution
    mock_db.get_open_activity_id_for_execution.return_value = "act-1"
    apply_mock = AsyncMock(return_value=SimpleNamespace(status=apply_status))
    svc = MagicMock(apply_result=apply_mock)
    with (
        patch("routers.agents.db", mock_db),
        patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
    ):
        resp = _await(
            agent_execution_result("agent-a", "exec-1", payload, _FakeRequest())
        )
    return resp, apply_mock


def test_cancelled_payload_maps_to_cancelled_envelope():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(
        status="cancelled", response="stopped", terminal_reason="cancelled",
        metadata={"cost_usd": 0.03},
    )
    resp, apply_mock = _drive(payload)

    apply_mock.assert_awaited_once()
    envelope = apply_mock.await_args.args[1]
    assert envelope.status == TaskExecutionStatus.CANCELLED
    # release_slot=True (the callback owns the lease)
    assert apply_mock.await_args.kwargs["release_slot"] is True
    assert resp["status"] == "cancelled"


def test_success_payload_still_maps_to_success():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(status="success", response="done", metadata={})
    _resp, apply_mock = _drive(payload, apply_status="success")
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.SUCCESS


def test_failed_payload_still_maps_to_failed():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(status="failed", error="boom", metadata={})
    _resp, apply_mock = _drive(payload, apply_status="failed")
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.FAILED


def test_unknown_status_maps_to_failed():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(status="weird", error="?", metadata={})
    _resp, apply_mock = _drive(payload, apply_status="failed")
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.FAILED


def test_cancelled_row_already_terminal_is_replayed_noop():
    """A terminate-wrote-first race: the row is already CANCELLED → idempotent
    replay ACK, apply_result not re-run (CANCELLED ∈ _AUTHORITATIVE_TERMINALS)."""
    from models import ExecutionResultEnvelope

    payload = ExecutionResultEnvelope(status="cancelled", metadata={})
    resp, apply_mock = _drive(payload, execution=_execution(status="cancelled"))
    assert resp["replayed"] is True
    apply_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# #679 Finding 2 (CSO 2026-06-22): backend-side auth/rate-vs-cancel guard.
# The callback is the backend trust boundary — a buggy or mixed-version agent
# that POSTs status:"cancelled" carrying an auth/rate terminal must NOT be
# reclassified as a clean cancellation (which would silently dodge the AUTH
# dispatch breaker / SUB-003 auto-switch). Mirrors result_callback._is_auth_or_rate.
# ---------------------------------------------------------------------------
def test_cancelled_with_auth_error_code_maps_to_failed():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import (
        TaskExecutionErrorCode,
        TaskExecutionStatus,
    )

    payload = ExecutionResultEnvelope(
        status="cancelled", error="no api key", error_code="auth", metadata={},
    )
    _resp, apply_mock = _drive(payload, apply_status="failed")
    envelope = apply_mock.await_args.args[1]
    assert envelope.status == TaskExecutionStatus.FAILED
    # error_code preserved so the AUTH dispatch breaker still records the failure
    assert envelope.error_code == TaskExecutionErrorCode.AUTH


def test_cancelled_with_rate_limit_terminal_reason_maps_to_failed():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(
        status="cancelled", error="rate limited", terminal_reason="rate_limit", metadata={},
    )
    _resp, apply_mock = _drive(payload, apply_status="failed")
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.FAILED


def test_cancelled_with_auth_terminal_reason_maps_to_failed():
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(
        status="cancelled", terminal_reason="auth", metadata={},
    )
    _resp, apply_mock = _drive(payload, apply_status="failed")
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.FAILED


def test_genuine_cancelled_still_maps_to_cancelled():
    """Regression guard: a real cancel (no auth/rate signal) still → CANCELLED."""
    from models import ExecutionResultEnvelope
    from services.task_execution_service import TaskExecutionStatus

    payload = ExecutionResultEnvelope(
        status="cancelled", response="stopped", terminal_reason="cancelled", metadata={},
    )
    _resp, apply_mock = _drive(payload)
    assert apply_mock.await_args.args[1].status == TaskExecutionStatus.CANCELLED
