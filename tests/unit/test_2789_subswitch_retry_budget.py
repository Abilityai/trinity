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


class _Clock:
    """A controllable `datetime.utcnow()` so a test can make an attempt take
    3000s without waiting for it."""

    def __init__(self):
        from datetime import datetime as _dt
        self.now = _dt(2026, 9, 16, 12, 0, 0)

    def advance(self, seconds: float):
        from datetime import timedelta
        self.now = self.now + timedelta(seconds=seconds)

    def utcnow(self):
        return self.now


def _run_interplay_with_clock(*, timeout_seconds: int, attempt_seconds: list):
    """502 (reader-race) → #678 retry → 429 → SUB-003 retry, with each agent
    call advancing a fake clock by the next value in `attempt_seconds`. Returns
    (clock_at_each_dispatch, http_timeout_at_each_dispatch, payload_timeouts)."""
    from datetime import datetime as _real_dt
    from services.task_execution_service import TaskExecutionService

    clock = _Clock()

    class _FakeDatetime(_real_dt):
        @classmethod
        def utcnow(cls):
            return clock.utcnow()

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution.return_value = MagicMock(id="exec-2789", status="running")
    mock_db.update_execution_status.return_value = True
    responses = [_resp_reader_race_502(), _resp_429(), _resp_200()]
    dispatched_at, http_timeouts, agent_timeouts = [], [], []
    durations = list(attempt_seconds)

    async def _agent_post(agent_name, endpoint, payload, **kwargs):
        dispatched_at.append((clock.utcnow() - _Clock().now).total_seconds())
        http_timeouts.append(kwargs.get("timeout"))
        agent_timeouts.append(payload.get("timeout_seconds"))
        clock.advance(durations.pop(0) if durations else 0)
        return responses.pop(0)

    with (
        patch("services.task_execution_service.datetime", _FakeDatetime),
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted")),
                                     release=AsyncMock())),
        patch("services.task_execution_service.activity_service",
              MagicMock(track_activity=AsyncMock(return_value="act-2789"), complete_activity=AsyncMock())),
        patch("services.task_execution_service.CircuitState", return_value=MagicMock(allow_request=lambda: True)),
        patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.platform_audit_service", MagicMock(log=AsyncMock())),
        patch("services.task_execution_service._SWITCH_RETRY_DELAY_S", 0),
        patch("services.subscription_auto_switch.handle_subscription_failure", AsyncMock(return_value=_SWITCHED)),
    ):
        svc = TaskExecutionService()
        result = _await(svc.execute_task(
            agent_name="test-agent", message="hello", triggered_by="schedule",
            execution_id="exec-2789", timeout_seconds=timeout_seconds, model="sonnet",
        ))
    assert result.status == "success", result
    assert len(dispatched_at) == 3, "expected attempt 1, the reader-race retry and the SUB-003 retry"
    return dispatched_at, http_timeouts, agent_timeouts


def test_the_interplay_cannot_outrun_the_turns_budget():
    """THE wall-clock bound, executed on the path that broke it (merge-train
    review C1 on #2817): 502 → reader-race retry → 429 → SUB-003 retry, where
    the reader-race retry has just RESET `state.start_time`. A budget derived
    from that clock measured only the reader-race retry and re-granted nearly
    the whole turn a third time — 6610s of slot time on a 3600s cap.

    Attempt 1 runs 3000s, the reader-race retry 5s. The SUB-003 retry may then
    have at most what the TURN has left: 3610 − 3005 = 605s, not 3605s.
    """
    from services.task_execution_service import _AGENT_HTTP_SLACK_S

    dispatched_at, http_timeouts, agent_timeouts = _run_interplay_with_clock(
        timeout_seconds=3600, attempt_seconds=[3000, 5],
    )
    effective = 3600 + _AGENT_HTTP_SLACK_S
    sub003_at, sub003_http = dispatched_at[2], http_timeouts[2]
    assert sub003_at == pytest.approx(3005)
    assert sub003_http <= effective - sub003_at + 1, (
        f"SUB-003 retry granted {sub003_http}s at t={sub003_at}s; the turn's cap is {effective}s"
    )
    # ...and the whole turn — every attempt's wall-clock plus the last grant —
    # stays inside the cap the slot lease, the watchdog and the portal marker are
    # all sized on.
    assert sub003_at + sub003_http <= effective + 1
    assert agent_timeouts[2] <= sub003_http


def test_the_portal_marker_covers_the_executed_worst_case():
    """`portal_attempt_ceiling_seconds` is what the Workspace in-flight marker
    is sized on. The previous test here asserted the derivation against its own
    addends (a tautology). This one asserts it against the EXECUTED worst case:
    attempt 1 runs to its cap, the reader-race retry runs to its ceiling, and
    the SUB-003 retry gets what is left — the sum must fit the marker."""
    from client_portal import service as svc
    from services.task_execution_service import _AUTO_RETRY_MAX_TIMEOUT_S

    t = 3600
    dispatched_at, http_timeouts, _ = _run_interplay_with_clock(
        timeout_seconds=t, attempt_seconds=[t, _AUTO_RETRY_MAX_TIMEOUT_S],
    )
    worst_wallclock = dispatched_at[2] + http_timeouts[2]
    assert worst_wallclock <= svc.portal_attempt_ceiling_seconds(t), (
        f"executed worst case {worst_wallclock}s exceeds the marker's "
        f"{svc.portal_attempt_ceiling_seconds(t)}s"
    )


# ---------------------------------------------------------------------------
# Attribution + visibility
# ---------------------------------------------------------------------------

def test_handle_timeout_judges_the_retry_against_its_applied_budget():
    """#2789's "Compounding" paragraph, driven through `_handle_timeout` itself
    (the previous version called the pure classifier with literals, which
    survives every mutation of the wiring): a retry that ran its full applied
    300s on a 3600s turn is a TIMEOUT against 300, not a NETWORK cutoff
    against 3600."""
    from datetime import datetime, timedelta

    from services.task_execution_service import (
        TaskExecutionErrorCode, TaskExecutionService, _AttemptState,
    )

    now = datetime.utcnow()
    state = _AttemptState(start_time=now - timedelta(seconds=300), turn_started_at=now - timedelta(seconds=3300))
    state.applied_timeout_seconds = 300
    with (
        patch("services.task_execution_service.terminate_execution_on_agent", new=AsyncMock()),
        patch("services.task_execution_service._write_terminal_and_gate", new=AsyncMock()),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
    ):
        result = _await(TaskExecutionService()._handle_timeout(
            agent_name="test-agent", execution_id="exec-2789", activity_id="act-2789",
            timeout_seconds=3600, state=state, exc=None,
        ))
    assert result.error_code == TaskExecutionErrorCode.TIMEOUT.value or result.error_code == "timeout", result
    assert "300 seconds" in result.error and "3600" not in result.error

    # First attempt: nothing applied — the configured limit is the judge.
    state = _AttemptState(start_time=now - timedelta(seconds=300), turn_started_at=now - timedelta(seconds=300))
    with (
        patch("services.task_execution_service.terminate_execution_on_agent", new=AsyncMock()),
        patch("services.task_execution_service._write_terminal_and_gate", new=AsyncMock()),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
    ):
        result = _await(TaskExecutionService()._handle_timeout(
            agent_name="test-agent", execution_id="exec-2789", activity_id="act-2789",
            timeout_seconds=3600, state=state, exc=None,
        ))
    assert result.error_code == "network" and "3600 seconds" in result.error


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


def test_retry_budget_is_logged_with_its_cause(caplog):
    """The report had to INFER the 300s cap from "aborted after 300s of 3600
    seconds allowed" — the applied budget appeared in no log line. It is now
    stated where it is decided, and the two causes read differently:

    * a reader-race CEILING is a WARNING ("clamped");
    * a SUB-003 re-run shorter only by what the first attempt spent is INFO
      with the breakdown — the first version of this helper warned "clamped
      to 3570s of 3600s" on every seat switch, calling a 30s first attempt a
      ceiling — and becomes a WARNING only once less than the reader-race
      ceiling is left, the likely-hopeless-and-still-billed case.
    """
    import logging

    from services.task_execution_service import _log_retry_budget

    # Only THIS logger's records: caplog is process-wide, and a background task
    # left running by an earlier test (order-dependent under a random seed)
    # can log an unrelated ERROR inside the window — which is how this read
    # `['ERROR', 'WARNING'] == ['WARNING']` on CI with the code correct.
    def mine():
        return [r for r in caplog.records if r.name == "services.task_execution_service"]

    with caplog.at_level(logging.INFO):
        _log_retry_budget("agent-x", "reader-race", 300, 3600, ceiling=300.0)
    assert [r.levelname for r in mine()] == ["WARNING"]
    assert "clamped to 300s" in mine()[0].message

    caplog.clear()
    with caplog.at_level(logging.INFO):
        # a spend-bounded retry with zero elapsed is NOT a clamp (review W4)
        _log_retry_budget("agent-x", "subscription-switch", 3590, 3600, elapsed_s=0)
    assert all("clamped" not in r.message for r in mine())

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_retry_budget("agent-x", "subscription-switch", 3570, 3600, elapsed_s=30)
    assert [r.levelname for r in mine()] == ["INFO"], "elapsed time is not a clamp"
    assert "clamped" not in mine()[0].message
    assert "30s already spent" in mine()[0].message

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_retry_budget("agent-x", "subscription-switch", 120, 3600, elapsed_s=3480)
    assert [r.levelname for r in mine()] == ["WARNING"], "under the ceiling is worth a look"
    assert "likely hopeless" in mine()[0].message

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_retry_budget("agent-x", "subscription-switch", 3600, 3600, elapsed_s=0)
    assert not mine(), "a retry that lost nothing must stay quiet"

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_retry_budget("agent-x", "subscription-switch", 600, None, elapsed_s=30)
    assert not mine(), "no configured limit means nothing was taken away"
