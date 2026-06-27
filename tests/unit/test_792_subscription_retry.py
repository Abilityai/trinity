"""Unit tests for #792 — retry the triggering execution after a successful
SUB-003 subscription auto-switch.

When the agent server returns a 429 / auth-class failure and SUB-003 successfully
switches the agent's subscription, `TaskExecutionService.execute_task` now re-issues
the turn ONCE with the same `execution_id` (pre-raise, mirroring the #678 reader-race
retry) so a one-shot trigger (manual / webhook / mcp) recovers instead of landing
FAILED.

These are pure unit tests — they drive `execute_task` with mocked dependencies and
run without a backend (the `tests/unit/` conftest overrides the parent's
api_client-dependent autouse fixtures). Harness mirrors
`tests/unit/test_terminal_write_cas_gate.py`.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------

def _resp(status: int, body: dict) -> MagicMock:
    """An agent response. For >=400, raise_for_status raises an
    httpx.HTTPStatusError whose `.response` carries the same status/body (so the
    except handler can read agent_status_code)."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(body)
    resp.json.return_value = body
    if status >= 400:
        err = httpx.HTTPStatusError(f"HTTP {status}", request=MagicMock(), response=resp)
        resp.raise_for_status = MagicMock(side_effect=err)
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _resp_429(cost: float | None = None) -> MagicMock:
    meta = {"cost_usd": cost} if cost is not None else {}
    return _resp(429, {"detail": {"message": "rate limited", "metadata": meta}})


def _resp_200(cost: float = 0.05) -> MagicMock:
    return _resp(200, {
        "response": "done",
        "session_id": "sess-001",
        "metadata": {"cost_usd": cost, "context_window": 200000},
        "execution_log": [],
    })


def _resp_reader_race_502(cost: float = 0.01) -> MagicMock:
    """A 502 matching the #678 reader-race signature (cheap → retryable)."""
    return _resp(502, {"detail": {
        "message": "Execution completed without a result message",
        "metadata": {"num_turns": 1, "cost_usd": cost},
        "raw_message_count": 0,
        "parse_failure_count": 0,
        "recovery_attempted": True,
    }})


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run(*, responses, switch_result, timeout_seconds=300):
    """Drive execute_task. `responses` is the agent_post_with_retry side_effect
    (one per HTTP attempt); `switch_result` is what handle_subscription_failure
    returns. Returns (result, ctx) where ctx exposes the mocks + recorded
    per-attempt timeouts."""
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution.return_value = MagicMock(id="exec-792", status="running")
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

    timeouts: list[float] = []

    async def _agent_post(agent_name, endpoint, payload, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if not responses:
            raise AssertionError("agent_post_with_retry called more times than responses provided")
        return responses.pop(0)

    mock_switch = AsyncMock(return_value=switch_result)
    mock_audit = MagicMock(log=AsyncMock())

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.platform_audit_service", mock_audit),
        patch("services.task_execution_service._SWITCH_RETRY_DELAY_S", 0),
        patch("services.subscription_auto_switch.handle_subscription_failure", mock_switch),
    ):
        svc = TaskExecutionService()
        result = _await(svc.execute_task(
            agent_name="test-agent",
            message="hello",
            triggered_by="schedule",
            execution_id="exec-792",
            timeout_seconds=timeout_seconds,
            model="sonnet",
        ))

    ctx = MagicMock(db=mock_db, switch=mock_switch, audit=mock_audit, timeouts=timeouts)
    ctx.agent_call_count = len(timeouts)
    return result, ctx


def _success_update_kwargs(mock_db):
    from services.task_execution_service import TaskExecutionStatus
    for call in mock_db.update_execution_status.call_args_list:
        if call.kwargs.get("status") == TaskExecutionStatus.SUCCESS:
            return call.kwargs
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_retry_succeeds_after_switch():
    """429 → switch → 200 retry: SUCCESS, two agent calls, one switch,
    first-attempt cost salvaged into the terminal cost, retry_count recorded."""
    from services.task_execution_service import TaskExecutionStatus

    result, ctx = _run(
        responses=[_resp_429(cost=0.02), _resp_200(cost=0.05)],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.SUCCESS
    assert ctx.agent_call_count == 2
    ctx.switch.assert_awaited_once()
    kwargs = _success_update_kwargs(ctx.db)
    assert kwargs is not None
    assert kwargs["retry_count"] == 1
    # 0.05 retry cost + 0.02 salvaged first-attempt cost (Codex #3)
    assert kwargs["cost"] == pytest.approx(0.07)


def test_auth_class_body_triggers_retry():
    """A non-429/503 status whose BODY trips is_auth_failure still switches+retries
    (decision 4 — full SUB-003 switch surface, not just status codes)."""
    from services.task_execution_service import TaskExecutionStatus

    auth_500 = _resp(500, {"detail": "unauthorized: token expired"})
    result, ctx = _run(
        responses=[auth_500, _resp_200()],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.SUCCESS
    assert ctx.agent_call_count == 2
    ctx.switch.assert_awaited_once()
    assert ctx.switch.await_args.kwargs["failure_kind"] == "auth"


@pytest.mark.parametrize("status", [503, 401, 403, 402])
def test_auth_status_codes_trigger_retry(status):
    """The explicit ``code in (503, 401, 403, 402)`` branch of
    classify_switch_failure switches+retries as ``failure_kind="auth"`` on the
    status code ALONE — body need not trip is_auth_failure. Covers the
    status-only surface the body-text 500 case (above) doesn't reach."""
    from services.task_execution_service import TaskExecutionStatus

    # Body deliberately auth-neutral so only the status-code branch can fire.
    auth_status = _resp(status, {"detail": "service unavailable"})
    result, ctx = _run(
        responses=[auth_status, _resp_200()],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.SUCCESS
    assert ctx.agent_call_count == 2
    ctx.switch.assert_awaited_once()
    assert ctx.switch.await_args.kwargs["failure_kind"] == "auth"


def test_no_alternative_subscription_no_retry():
    """handle_subscription_failure returns None (no viable alternative) ⇒ no
    retry; one switch attempt; FAILED."""
    from services.task_execution_service import TaskExecutionStatus

    result, ctx = _run(responses=[_resp_429()], switch_result=None)

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 1
    ctx.switch.assert_awaited_once()


def test_cascade_fails_with_exactly_one_switch():
    """429 → switch → still 429: FAILED, exactly one retry AND exactly one switch
    (the subscription_switch_attempted guard blocks the except-handler from
    switching again)."""
    from services.task_execution_service import TaskExecutionStatus

    result, ctx = _run(
        responses=[_resp_429(), _resp_429()],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 2  # original + one retry, no third
    ctx.switch.assert_awaited_once()  # NOT twice — no cascade double-switch


def test_678_interplay_both_retries_fire():
    """A #678 reader-race retry (retry_count=1) does NOT suppress a later SUB-003
    switch+retry, because the guard is the dedicated subscription_switch_attempted
    flag, not retry_count (Codex #1). 502 → #678 retry → 429 → switch → 200."""
    from services.task_execution_service import TaskExecutionStatus

    result, ctx = _run(
        responses=[_resp_reader_race_502(cost=0.01), _resp_429(cost=0.02), _resp_200(cost=0.05)],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.SUCCESS
    assert ctx.agent_call_count == 3  # original + #678 retry + #792 switch retry
    ctx.switch.assert_awaited_once()
    kwargs = _success_update_kwargs(ctx.db)
    assert kwargs["retry_count"] == 2  # both inline retries counted


def test_retry_timeout_bounded():
    """The post-switch retry timeout is capped (≤ _AUTO_RETRY_MAX_TIMEOUT_S and
    > 0) against the remaining budget, not flat +_AUTO_RETRY_MAX_TIMEOUT_S
    (Codex #9)."""
    from services.task_execution_service import _AUTO_RETRY_MAX_TIMEOUT_S

    _result, ctx = _run(
        responses=[_resp_429(), _resp_200()],
        switch_result={"switched": True, "new_subscription": "sub-b"},
        timeout_seconds=300,
    )

    assert ctx.agent_call_count == 2
    retry_timeout = ctx.timeouts[1]
    assert 0 < retry_timeout <= _AUTO_RETRY_MAX_TIMEOUT_S


def test_non_switch_failure_no_retry():
    """Negative control: a plain non-switch failure (500, no auth text) does not
    switch or retry — FAILED with a single attempt."""
    from services.task_execution_service import TaskExecutionStatus

    result, ctx = _run(
        responses=[_resp(500, {"detail": "internal server error"})],
        switch_result={"switched": True, "new_subscription": "sub-b"},
    )

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 1
    ctx.switch.assert_not_awaited()


# Case 8 (idempotency snapshot) is covered by construction: the
# /api/internal/execute-task router stores execute_task's RETURN value via
# idempotency_service.complete(); a successful post-switch retry returns SUCCESS,
# so the snapshot reflects SUCCESS (not the intermediate failure). The router
# wrapper is unchanged by #792 and exercised by tests/test_idempotency.py.
