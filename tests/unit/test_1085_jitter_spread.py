"""Agent-side jitter for the fire-and-forget callback (#1085, Part A).

Covers the pure spreading logic (no container):
  * ``_jittered_backoff`` stays within [base, cap] and is non-degenerate (a
    fleet of retrying agents actually desynchronises);
  * the ``_deliver`` retry loop honors the deadline clamp and a server
    ``Retry-After`` as a floor (the #1085 governor 503);
  * ``resend_pending_results`` applies the initial + per-envelope startup-sweep
    sleeps so a restart storm is smeared, not synchronized.

The unit conftest preloads the real base-image ``agent_server`` package.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_server.services import result_callback as rc  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeClient:
    """Replays a list of status codes / (status, headers) / Exceptions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return _FakeResp(r[0], r[1])
        return _FakeResp(r)


# ---------------------------------------------------------------------------
# _jittered_backoff
# ---------------------------------------------------------------------------
class TestJitteredBackoff:
    def test_within_base_and_cap(self):
        # Across many prev_sleep values the result is always clamped to the band.
        for prev in (0.0, 0.5, 1.0, 5.0, 30.0, 1000.0):
            for _ in range(200):
                b = rc._jittered_backoff(prev)
                assert rc._BACKOFF_BASE <= b <= rc._BACKOFF_CAP

    def test_non_degenerate_variance(self):
        # The whole point: repeated draws at the same prev are NOT identical, so
        # a fleet of retrying agents spreads out instead of firing in lockstep.
        draws = {round(rc._jittered_backoff(5.0), 6) for _ in range(100)}
        assert len(draws) > 10

    def test_self_paces_upward(self):
        # Decorrelated jitter grows with prev_sleep (the ceiling is prev*3),
        # so the *average* backoff increases as retries accumulate.
        low = sum(rc._jittered_backoff(1.0) for _ in range(500)) / 500
        high = sum(rc._jittered_backoff(20.0) for _ in range(500)) / 500
        assert high > low

    def test_parse_retry_after(self):
        assert rc._parse_retry_after("30") == 30.0
        assert rc._parse_retry_after(45) == 45.0
        assert rc._parse_retry_after(None) == 0.0
        assert rc._parse_retry_after("") == 0.0
        assert rc._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") == 0.0  # HTTP-date → 0


# ---------------------------------------------------------------------------
# _deliver loop: deadline clamp + Retry-After floor
# ---------------------------------------------------------------------------
class TestDeliverJitter:
    def _run(self, responses, deadline_offset=1000.0, sleep_mock=None):
        import time

        deadline = time.monotonic() + deadline_offset
        sleep_mock = sleep_mock or AsyncMock()
        with (
            patch.object(rc.httpx, "AsyncClient", lambda **kw: _FakeClient(responses)),
            patch.object(rc.asyncio, "sleep", sleep_mock),
        ):
            ok = asyncio.run(
                rc._deliver("exec-1", "agent-a", {"status": "success"}, "http://b", "k", deadline)
            )
        return ok, sleep_mock

    def test_2xx_delivers_no_sleep(self):
        ok, sleep_mock = self._run([200])
        assert ok is True
        sleep_mock.assert_not_called()

    def test_transient_retries_then_succeeds(self):
        ok, sleep_mock = self._run([503, 503, 200])
        assert ok is True
        # Two transient responses → two backoff sleeps before the 200.
        assert sleep_mock.await_count == 2

    def test_sleeps_never_exceed_cap(self):
        ok, sleep_mock = self._run([500, 500, 500, 200])
        assert ok is True
        for call in sleep_mock.await_args_list:
            assert 0.0 <= call.args[0] <= rc._BACKOFF_CAP

    def test_retry_after_floors_backoff(self):
        # A 503 carrying Retry-After: 40 must make the next sleep >= 40 (then the
        # 200 lands). Floor beats the jittered draw (whose band for prev=base is
        # [1, 3]).
        ok, sleep_mock = self._run([(503, {"Retry-After": "40"}), 200])
        assert ok is True
        assert sleep_mock.await_count == 1
        slept = sleep_mock.await_args_list[0].args[0]
        assert slept >= 40.0

    def test_deadline_clamps_sleep(self):
        # With a tiny remaining budget, the sleep is clamped to it (never past
        # the deadline), even when the jitter/floor would be larger.
        sleep_mock = AsyncMock()
        # One transient then 200; budget ~0.2s.
        ok, sleep_mock = self._run([(503, {"Retry-After": "40"}), 200],
                                   deadline_offset=0.2, sleep_mock=sleep_mock)
        # Either it delivered the 200 within budget or hit the deadline; in both
        # cases any sleep issued must be <= the remaining budget.
        for call in sleep_mock.await_args_list:
            assert call.args[0] <= 0.2 + 1e-6


# ---------------------------------------------------------------------------
# resend_pending_results startup-sweep spread
# ---------------------------------------------------------------------------
class TestSweepSpread:
    def test_initial_and_per_envelope_sleeps(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
        monkeypatch.setenv("TRINITY_MCP_API_KEY", "k")
        monkeypatch.setattr(rc.agent_state, "agent_name", "agent-a")
        for i in range(3):
            (tmp_path / f"exec-{i}.json").write_text(
                json.dumps({"agent_name": "agent-a", "envelope": {"status": "success"}})
            )

        uniform_calls = []

        def _fake_uniform(lo, hi):
            uniform_calls.append((lo, hi))
            return 0.0  # deterministic: no real waiting

        sleep_mock = AsyncMock()
        with (
            patch.object(rc.random, "uniform", _fake_uniform),
            patch.object(rc.asyncio, "sleep", sleep_mock),
            patch.object(rc.httpx, "AsyncClient", lambda **kw: _FakeClient([200])),
        ):
            asyncio.run(rc.resend_pending_results())

        # One initial-jitter draw over [0, _SWEEP_INITIAL_JITTER_SECONDS] ...
        assert (0, rc._SWEEP_INITIAL_JITTER_SECONDS) in uniform_calls
        # ... plus one per-envelope draw over [0, _SWEEP_PER_ENVELOPE_JITTER_SECONDS]
        per_env = [c for c in uniform_calls if c == (0, rc._SWEEP_PER_ENVELOPE_JITTER_SECONDS)]
        assert len(per_env) == 3
        # All three envelopes delivered (200) and deleted.
        assert sorted(tmp_path.glob("*.json")) == []

    def test_sweep_noop_when_unconfigured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rc, "_PENDING_DIR", tmp_path)
        monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
        monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
        sleep_mock = AsyncMock()
        with patch.object(rc.asyncio, "sleep", sleep_mock):
            asyncio.run(rc.resend_pending_results())
        # Guard returns before the initial jitter sleep.
        sleep_mock.assert_not_called()
