"""Unit tests for #2789 — the SUB-003 post-switch auto-retry must get the
turn's remaining budget, not the #678 reader-race ceiling.

The reported failure: an agent with `execution_timeout_seconds=3600` took a
429 ~30s into a Workspace turn, SUB-003 switched it to a healthy subscription,
and the retry — a full re-run of the user's turn — was dispatched with
`timeout_seconds=300` because both paths read `_AUTO_RETRY_MAX_TIMEOUT_S`. The
agent server killed its own process group at exactly 300s with
`stop_reason=tool_use` (i.e. actively working), the turn was discarded, and the
five minutes were billed. Re-sending the identical message succeeded, proving
the turn was viable all along.

The reader-race ceiling is still correct for #678, which re-dispatches a turn
that never started. It is wrong for a re-run, which earns the budget the turn
was given. `remaining_s` is already a hard wall-clock bound, so removing the
second ceiling cannot balloon slot time past TIMEOUT-001.

Harness mirrors `tests/unit/test_792_subscription_retry.py`, with the dispatched
PAYLOAD recorded as well as the HTTP timeout — the agent-side `timeout_seconds`
is the number the agent server actually kills on, and it is the one the issue
is about.

Module under test:
    src/backend/services/task_execution_service.py
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


def _resp_429() -> MagicMock:
    return _resp(429, {"detail": {"message": "rate limited", "metadata": {}}})


def _resp_200() -> MagicMock:
    return _resp(200, {
        "response": "done",
        "session_id": "sess-2789",
        "metadata": {"cost_usd": 0.05, "context_window": 200000},
        "execution_log": [],
    })


def _resp_reader_race_502() -> MagicMock:
    return _resp(502, {"detail": {
        "message": "Execution completed without a result message",
        "metadata": {"num_turns": 1, "cost_usd": 0.01},
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


def _run(*, responses, switch_result, timeout_seconds):
    """Drive execute_task, recording each attempt's (http_timeout, payload)."""
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution.return_value = MagicMock(id="exec-2789", status="running")
    mock_db.update_execution_status.return_value = True  # CAS won

    mock_capacity = MagicMock()
    admitted = MagicMock()
    admitted.state = "admitted"
    mock_capacity.acquire = AsyncMock(return_value=admitted)
    mock_capacity.release = AsyncMock()

    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True

    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-2789"),
        complete_activity=AsyncMock(),
    )

    attempts: list[tuple[float, dict]] = []

    async def _agent_post(agent_name, endpoint, payload, **kwargs):
        attempts.append((kwargs.get("timeout"), dict(payload)))
        if not responses:
            raise AssertionError("agent_post_with_retry called more times than responses provided")
        return responses.pop(0)

    mock_switch = AsyncMock(return_value=switch_result)

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.platform_audit_service", MagicMock(log=AsyncMock())),
        patch("services.task_execution_service._SWITCH_RETRY_DELAY_S", 0),
        patch("services.subscription_auto_switch.handle_subscription_failure", mock_switch),
    ):
        svc = TaskExecutionService()
        result = _await(svc.execute_task(
            agent_name="test-agent",
            message="hello",
            triggered_by="schedule",
            execution_id="exec-2789",
            timeout_seconds=timeout_seconds,
            model="sonnet",
        ))

    ctx = MagicMock(db=mock_db, switch=mock_switch, attempts=attempts)
    return result, ctx


_SWITCHED = {"switched": True, "new_subscription": "sub-b"}


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

def test_post_switch_retry_gets_the_agents_own_timeout_not_300s():
    """THE regression test. A 3600s agent's post-switch retry is dispatched with
    (near) its whole 3600s, not the reader-race 300s.

    This is the number the agent server kills on: the issue's evidence is
    `[Headless Task] Task ... timed out after 300s — killing process group` on
    a turn whose configured limit was 3600s.
    """
    _result, ctx = _run(
        responses=[_resp_429(), _resp_200()],
        switch_result=_SWITCHED,
        timeout_seconds=3600,
    )

    assert len(ctx.attempts) == 2
    _http, retry_payload = ctx.attempts[1]
    retry_agent_timeout = retry_payload["timeout_seconds"]

    assert retry_agent_timeout > 300, (
        "the SUB-003 retry is still wearing the #678 reader-race ceiling — "
        f"got {retry_agent_timeout}s for a 3600s agent"
    )
    # The first attempt in this harness is instantaneous, so effectively the
    # whole budget survives. Bounded above by the agent's own configured cap.
    assert 3500 <= retry_agent_timeout <= 3600


@pytest.mark.parametrize("configured", [60, 300, 900, 3600, 7200])
def test_retry_never_exceeds_the_operators_configured_cap(configured):
    """Across the whole PUT-validated TIMEOUT-001 range (60–7200), the retry's
    agent-side budget stays within what the operator configured.

    Removing the 300s ceiling must not let a retry outrun the cap — the bound
    that remains (`remaining_s`, derived from `effective_timeout`) is the one
    doing the work, and it is asserted here rather than assumed.
    """
    _result, ctx = _run(
        responses=[_resp_429(), _resp_200()],
        switch_result=_SWITCHED,
        timeout_seconds=configured,
    )

    _http, retry_payload = ctx.attempts[1]
    assert 0 < retry_payload["timeout_seconds"] <= configured


def test_retry_keeps_the_http_slack_over_the_agent_budget():
    """The backend's read budget stays wider than the agent's own, on the retry
    too — so the agent's structured 504 wins the race and we terminate with its
    error detail instead of a bare ReadTimeout.

    Before #2789 the retry collapsed both onto the same value (`retry_agent_
    timeout = min(timeout_seconds, retry_http_timeout)`), which the 300s clamp
    hid: with the clamp gone they would have landed on the same instant.
    """
    from services.task_execution_service import _AGENT_HTTP_SLACK_S

    _result, ctx = _run(
        responses=[_resp_429(), _resp_200()],
        switch_result=_SWITCHED,
        timeout_seconds=1800,
    )

    first_http, first_payload = ctx.attempts[0]
    retry_http, retry_payload = ctx.attempts[1]

    assert first_http - first_payload["timeout_seconds"] == pytest.approx(_AGENT_HTTP_SLACK_S)
    assert retry_http - retry_payload["timeout_seconds"] >= _AGENT_HTTP_SLACK_S - 1


# ---------------------------------------------------------------------------
# The ceiling that is still correct
# ---------------------------------------------------------------------------

def test_reader_race_retry_keeps_its_300s_ceiling():
    """Negative control: #678 is a re-dispatch of a turn that never started, so
    its flat 5-minute ceiling is deliberate and must survive #2789.

    `client_portal.portal_attempt_ceiling_seconds` imports that constant to size
    the Workspace in-flight marker, adding it exactly once on this path's
    behalf — widening it here would silently grow every marker TTL.
    """
    from services.task_execution_service import _AUTO_RETRY_MAX_TIMEOUT_S

    _result, ctx = _run(
        responses=[_resp_reader_race_502(), _resp_200()],
        switch_result=_SWITCHED,
        timeout_seconds=3600,
    )

    assert len(ctx.attempts) == 2
    _http, retry_payload = ctx.attempts[1]
    assert retry_payload["timeout_seconds"] == int(_AUTO_RETRY_MAX_TIMEOUT_S)
    ctx.switch.assert_not_awaited()


def test_portal_attempt_ceiling_still_covers_one_worst_attempt():
    """The Workspace marker/wait budget is derived from the #678 ceiling and must
    keep covering the worst legitimate single attempt after #2789.

    The SUB-003 retry now genuinely is bounded by the remaining budget — which
    is what `portal_attempt_ceiling_seconds`' docstring already claimed — so the
    derivation needs no addend for it, and this pins that the two agree.
    """
    from client_portal import service as svc
    from services import task_execution_service as tes

    for t in (60, 300, 3600, 7200):
        # worst attempt = one full turn + HTTP slack + a whole reader-race retry
        worst = t + int(tes._AGENT_HTTP_SLACK_S) + int(tes._AUTO_RETRY_MAX_TIMEOUT_S)
        assert svc.portal_attempt_ceiling_seconds(t) >= worst
        # ...and a SUB-003 retry, being remaining-bounded, adds nothing on top.
        assert svc.portal_attempt_ceiling_seconds(t) >= t + int(tes._AGENT_HTTP_SLACK_S)


# ---------------------------------------------------------------------------
# Attribution + visibility
# ---------------------------------------------------------------------------

def test_timeout_after_a_retry_is_judged_against_the_retrys_own_budget():
    """#2789's "Compounding" paragraph: `state.start_time` is reset before an
    inline retry, so `_handle_timeout` measures the RETRY's elapsed time. It must
    judge that against the retry's own limit.

    Judging a 300s reader-race retry against the turn's untouched 3600s made
    #2106 label a self-inflicted ceiling `NETWORK` — "Task execution aborted
    after 300s of 3600 seconds allowed" — and every downstream consumer then
    read an upstream fault where there was none.
    """
    from datetime import datetime

    from services.task_execution_service import (
        TaskExecutionErrorCode,
        _AttemptState,
        _classify_timeout_failure,
    )

    state = _AttemptState(start_time=datetime.utcnow())
    assert state.applied_timeout_seconds is None  # first attempt: nothing applied

    # A retry that ran its full applied 300s, against a 3600s configured turn.
    _msg, code = _classify_timeout_failure(300, 300, exc=None)
    assert code == TaskExecutionErrorCode.TIMEOUT

    # The old reading — same run, judged against the configured limit.
    _msg_old, code_old = _classify_timeout_failure(300, 3600, exc=None)
    assert code_old == TaskExecutionErrorCode.NETWORK


def test_terminal_reports_the_retrys_limit_end_to_end():
    """...and `_handle_timeout` actually reads it, rather than the field merely
    existing. Drives the whole branch: reader-race 502, then the retry dies of a
    ReadTimeout, on a 3600s turn.

    The terminal must name the 300s ceiling the retry was given. Naming 3600s
    is the exact sentence the #2789 report had to reverse-engineer, and it sends
    an operator to raise a limit that was never reached.
    """
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution.return_value = MagicMock(id="exec-2789", status="running")
    mock_db.update_execution_status.return_value = True

    responses = [_resp_reader_race_502()]

    async def _agent_post(agent_name, endpoint, payload, **kwargs):
        if responses:
            return responses.pop(0)
        raise httpx.ReadTimeout("read timed out")

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted")),
                                     release=AsyncMock())),
        patch("services.task_execution_service.activity_service",
              MagicMock(track_activity=AsyncMock(return_value="act-2789"),
                        complete_activity=AsyncMock())),
        patch("services.task_execution_service.CircuitState",
              return_value=MagicMock(allow_request=lambda: True)),
        patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.platform_audit_service", MagicMock(log=AsyncMock())),
        patch("services.task_execution_service.terminate_execution_on_agent", new=AsyncMock()),
        patch("services.task_execution_service._write_terminal_and_gate", new=AsyncMock()),
    ):
        svc = TaskExecutionService()
        result = _await(svc.execute_task(
            agent_name="test-agent",
            message="hello",
            triggered_by="schedule",
            execution_id="exec-2789",
            timeout_seconds=3600,
            model="sonnet",
        ))

    assert result.status == "failed"
    assert "300 seconds allowed" in result.error, result.error
    assert "3600 seconds allowed" not in result.error


def test_clamped_retry_budget_is_logged(caplog):
    """The report had to INFER the 300s cap from "aborted after 300s of 3600
    seconds allowed" — the applied budget appeared in no log line. A clamp is
    now stated where it is decided.
    """
    import logging

    from services.task_execution_service import _warn_if_retry_budget_clamped

    with caplog.at_level(logging.WARNING):
        _warn_if_retry_budget_clamped("agent-x", "reader-race", 300, 3600)
    assert any("clamped to 300s" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _warn_if_retry_budget_clamped("agent-x", "subscription-switch", 3600, 3600)
    assert not caplog.records, "a retry that lost nothing must stay quiet"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _warn_if_retry_budget_clamped("agent-x", "subscription-switch", 600, None)
    assert not caplog.records, "no configured limit means nothing was taken away"
