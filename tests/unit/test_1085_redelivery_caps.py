"""Re-delivery rate caps at the callback endpoint (#1085, Part B).

Drives ``routers.agents.agent_execution_result`` directly (no live server),
asserting the governor gate's caps behavior:

  * fleet OR per-agent cap exceeded → **503 + Retry-After** (NOT 429), and the
    terminal is never applied;
  * both caps allowed → the terminal flows to ``apply_result`` (accept);
  * fail-open: a Redis outage (check returns allowed) admits the callback;
  * the replay-ACK short-circuits BEFORE the governor gate (an already-final row
    still ACKs 200 while throttled — never starved);
  * cross-check: 503 ∉ ``result_callback._PERMANENT_STATUSES`` (the parity
    guarantee — a throttled callback stays retryable, never permanently dropped).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

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


def _execution(status="running", claude_session_id="dispatched_async"):
    return SimpleNamespace(
        id="exec-1", agent_name="agent-a", status=status,
        claude_session_id=claude_session_id,
    )


def _result(fleet_ok=True, agent_ok=True, fleet_retry=7, agent_retry=11):
    from services.rate_limiter import RateLimitResult

    def _check(key, limit, window):
        if key == "redelivery:fleet":
            return RateLimitResult(fleet_ok, 0, 0 if fleet_ok else fleet_retry, limit)
        return RateLimitResult(agent_ok, 0, 0 if agent_ok else agent_retry, limit)

    return _check


def _call(*, execution=None, payload=None, check_fn=None, paused=False, flag_on=True):
    from routers.agents import agent_execution_result
    from models import ExecutionResultEnvelope
    import config

    if execution is None:
        execution = _execution()
    if payload is None:
        payload = ExecutionResultEnvelope(status="success", response="ok", metadata={"cost_usd": 0.01})
    if check_fn is None:
        check_fn = _result()

    mock_db = MagicMock()
    mock_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "agent-a"}
    mock_db.get_execution.return_value = execution
    mock_db.get_open_activity_id_for_execution.return_value = "act-1"
    apply_mock = AsyncMock(return_value=SimpleNamespace(status="success"))
    svc = MagicMock(apply_result=apply_mock)
    mock_gov = MagicMock(is_paused=MagicMock(return_value=paused))
    check_mock = MagicMock(side_effect=check_fn)

    req = SimpleNamespace(headers={"Authorization": "Bearer trinity_mcp_k"})

    with (
        patch("routers.agents.db", mock_db),
        patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.redelivery_governor.get_redelivery_governor", return_value=mock_gov),
        patch("services.rate_limiter.check", check_mock),
        patch.object(config, "REDELIVERY_GOVERNOR_ENABLED", flag_on),
    ):
        try:
            resp = _await(agent_execution_result("agent-a", "exec-1", payload, req))
            return SimpleNamespace(resp=resp, exc=None, apply=apply_mock, check=check_mock)
        except HTTPException as exc:
            return SimpleNamespace(resp=None, exc=exc, apply=apply_mock, check=check_mock)


class TestCaps:
    def test_fleet_cap_exceeded_503(self):
        out = _call(check_fn=_result(fleet_ok=False, fleet_retry=9))
        assert out.exc is not None and out.exc.status_code == 503
        assert out.exc.headers["Retry-After"] == "9"
        out.apply.assert_not_awaited()

    def test_agent_cap_exceeded_503(self):
        out = _call(check_fn=_result(agent_ok=False, agent_retry=13))
        assert out.exc.status_code == 503
        assert out.exc.headers["Retry-After"] == "13"
        out.apply.assert_not_awaited()

    def test_retry_after_is_max_of_both(self):
        out = _call(check_fn=_result(fleet_ok=False, fleet_retry=5,
                                     agent_ok=False, agent_retry=22))
        assert out.exc.status_code == 503
        assert out.exc.headers["Retry-After"] == "22"

    def test_both_allowed_accepts(self):
        out = _call(check_fn=_result(fleet_ok=True, agent_ok=True))
        assert out.exc is None
        assert out.resp["replayed"] is False
        out.apply.assert_awaited_once()

    def test_fail_open_admits(self):
        # Redis-down → check() fails open (returns allowed via in-process
        # fallback). The endpoint must admit the callback.
        out = _call(check_fn=_result(fleet_ok=True, agent_ok=True))
        out.apply.assert_awaited_once()

    def test_flag_off_skips_caps_entirely(self):
        # With the master flag OFF the gate is inert — rate_limiter.check is
        # never consulted even though it would block.
        out = _call(check_fn=_result(fleet_ok=False), flag_on=False)
        assert out.exc is None
        out.check.assert_not_called()
        out.apply.assert_awaited_once()


class TestReplayNotStarved:
    def test_authoritative_replay_acks_while_capped(self):
        # An already-final (SUCCESS) row must ACK {replayed: true} BEFORE the
        # governor gate, so a throttled fleet never starves replays.
        out = _call(
            execution=_execution(status="success"),
            check_fn=_result(fleet_ok=False, agent_ok=False),
            paused=True,
        )
        assert out.exc is None
        assert out.resp["replayed"] is True
        out.check.assert_not_called()       # gate never reached
        out.apply.assert_not_awaited()


class TestRetryableParity:
    def test_503_not_permanent_agent_side(self):
        # The never-drop guarantee: a 503 throttle is retryable, so the agent's
        # _deliver keeps the envelope persisted and retries on its backoff.
        from agent_server.services import result_callback as rc

        assert 503 not in rc._PERMANENT_STATUSES
        # Sanity: the genuinely-permanent 4xx ARE in the set.
        assert {403, 404, 409, 413}.issubset(rc._PERMANENT_STATUSES)
