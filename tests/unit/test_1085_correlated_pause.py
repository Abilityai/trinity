"""Shared-cause re-delivery governor (#1085, Part C).

Covers the distinct-agent correlated-failure detector + auto-expiring pause:
  * N *distinct* agents crossing the threshold arms the pause (with a TTL);
  * the SAME agent N times is one distinct agent (a crash-loop never arms it);
  * non-correlated codes (timeout/agent_error) are ignored;
  * fail-open everywhere (Redis None → record no-op, is_paused False, count 0);
  * the recorder is gated on the CAS ``won`` bool in ``apply_result`` so a
    replayed/late callback never double-counts, and on the master flag;
  * the agent-side 429→BILLING mapping that feeds the detector;
  * reaper + drain hold-off while paused;
  * callback endpoint 503 while paused.

The governor is loaded standalone (no heavy ``services/__init__``) against a
fake Redis, mirroring tests/unit/test_rate_limiter.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

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
# Fake Redis: atomic ZSET + string get/set with explicit-expiry hook
# ---------------------------------------------------------------------------
class _FakeRedis:
    def __init__(self):
        self._lock = threading.Lock()
        self.z: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}
        self.set_calls: list = []

    # -- string ops --
    def set(self, key, value, ex=None):
        with self._lock:
            self.kv[key] = str(value)
            self.set_calls.append((key, value, ex))

    def get(self, key):
        with self._lock:
            return self.kv.get(key)

    def expire_key(self, key):  # test hook simulating TTL lapse
        with self._lock:
            self.kv.pop(key, None)

    # -- direct zset ops (used by distinct_failing_count) --
    def zremrangebyscore(self, key, lo, hi):
        with self._lock:
            z = self.z.get(key, {})
            for m in [m for m, s in z.items() if lo <= s <= hi]:
                z.pop(m, None)

    def zcard(self, key):
        with self._lock:
            return len(self.z.get(key, {}))

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, parent: _FakeRedis):
        self.p = parent
        self.ops: list = []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("zremrange", key, lo, hi)); return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping)); return self

    def zcard(self, key):
        self.ops.append(("zcard", key)); return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl)); return self

    def execute(self):
        res = []
        with self.p._lock:
            for op in self.ops:
                if op[0] == "zremrange":
                    _, key, lo, hi = op
                    z = self.p.z.setdefault(key, {})
                    for m in [m for m, s in z.items() if lo <= s <= hi]:
                        z.pop(m, None)
                    res.append(0)
                elif op[0] == "zadd":
                    _, key, mapping = op
                    self.p.z.setdefault(key, {}).update(mapping)
                    res.append(len(mapping))
                elif op[0] == "zcard":
                    _, key = op
                    res.append(len(self.p.z.get(key, {})))
                elif op[0] == "expire":
                    res.append(1)
        return res


@pytest.fixture
def gov(monkeypatch):
    """Load services/redelivery_governor.py standalone with a fake Redis."""
    spec = importlib.util.spec_from_file_location(
        "_redelivery_governor_under_test",
        str(_BACKEND / "services" / "redelivery_governor.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    fake = _FakeRedis()
    monkeypatch.setattr(module, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(module, "reset_breaker_redis_client", lambda: None)

    # Deterministic thresholds via the real config module the governor imports.
    import config
    monkeypatch.setattr(config, "CORRELATED_FAILURE_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(config, "CORRELATED_FAILURE_WINDOW_SECONDS", 120, raising=False)
    monkeypatch.setattr(config, "CORRELATED_PAUSE_TTL_SECONDS", 300, raising=False)

    g = module.RedeliveryGovernor()
    return SimpleNamespace(module=module, governor=g, fake=fake)


# ---------------------------------------------------------------------------
# Distinct-agent detection + pause arming
# ---------------------------------------------------------------------------
class TestDetector:
    def test_distinct_agents_cross_threshold_arms_pause(self, gov):
        g, fake = gov.governor, gov.fake
        assert g.is_paused() is False
        g.record_terminal_failure("agent-1", "auth")
        g.record_terminal_failure("agent-2", "auth")
        assert g.is_paused() is False           # 2 < threshold(3)
        g.record_terminal_failure("agent-3", "billing")
        assert g.is_paused() is True            # 3 distinct → armed
        # TTL passed to set == configured pause TTL.
        assert fake.set_calls[-1] == ("governor:pause", "1", 300)

    def test_same_agent_repeats_is_one_distinct(self, gov):
        g = gov.governor
        for _ in range(10):
            g.record_terminal_failure("agent-loop", "auth")
        assert g.distinct_failing_count() == 1
        assert g.is_paused() is False           # a crash-loop never arms the pause

    def test_non_correlated_codes_ignored(self, gov):
        g = gov.governor
        for name, code in [("a", "timeout"), ("b", "agent_error"),
                           ("c", "network"), ("d", None)]:
            g.record_terminal_failure(name, code)
        assert g.distinct_failing_count() == 0
        assert g.is_paused() is False

    def test_auto_expiry_recovers(self, gov):
        g, fake = gov.governor, gov.fake
        for n in ("a", "b", "c"):
            g.record_terminal_failure(n, "auth")
        assert g.is_paused() is True
        fake.expire_key("governor:pause")       # simulate TTL lapse
        assert g.is_paused() is False           # recovery is the TTL, no unpause


# ---------------------------------------------------------------------------
# Fail-open: Redis unreachable
# ---------------------------------------------------------------------------
class TestFailOpen:
    def test_no_redis_never_pauses(self, gov, monkeypatch):
        monkeypatch.setattr(gov.module, "get_breaker_redis", lambda: None)
        g = gov.governor
        for n in ("a", "b", "c", "d", "e"):
            g.record_terminal_failure(n, "auth")   # no-op, no raise
        assert g.is_paused() is False
        assert g.distinct_failing_count() == 0

    def test_redis_error_is_swallowed(self, gov, monkeypatch):
        boom = MagicMock()
        boom.pipeline.side_effect = RuntimeError("redis down")
        boom.get.side_effect = RuntimeError("redis down")
        monkeypatch.setattr(gov.module, "get_breaker_redis", lambda: boom)
        g = gov.governor
        g.record_terminal_failure("a", "auth")     # must not raise
        assert g.is_paused() is False              # fail-open


# ---------------------------------------------------------------------------
# 429 → BILLING agent-side mapping (feeds the detector)
# ---------------------------------------------------------------------------
class Test429BillingMap:
    def test_429_maps_to_billing_rate_limit(self):
        from agent_server.services import result_callback as rc
        from fastapi import HTTPException

        assert rc._STATUS_MAP[429] == ("billing", "rate_limit")
        env = rc._envelope_from_http_exception(HTTPException(status_code=429, detail="slow down"))
        assert env["error_code"] == "billing"
        assert env["terminal_reason"] == "rate_limit"
        # The cancel-relabel guard must still treat it as auth/rate (never a
        # clean cancellation), keyed off terminal_reason.
        assert rc._is_auth_or_rate(env) is True


# ---------------------------------------------------------------------------
# Recorder gated on CAS won + master flag (apply_result)
# ---------------------------------------------------------------------------
def _failed_envelope(error_code):
    from services.task_execution_service import (
        TerminalEnvelope, TaskExecutionStatus, TaskExecutionErrorCode,
    )
    return TerminalEnvelope(
        execution_id="exec-1085",
        status=TaskExecutionStatus.FAILED,
        error="boom",
        error_code=TaskExecutionErrorCode(error_code),
        metadata={"cost_usd": 0.0},
    )


def _run_apply_with_governor(error_code, *, cas_won, flag_on):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    mock_db.get_execution.return_value = SimpleNamespace(id="exec-1085", status="failed")
    mock_activity = MagicMock(complete_activity=AsyncMock())
    mock_capacity = MagicMock(release=AsyncMock())
    mock_governor = MagicMock(record_terminal_failure=MagicMock())

    import config
    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.redelivery_governor.get_redelivery_governor", return_value=mock_governor),
        patch.object(config, "REDELIVERY_GOVERNOR_ENABLED", flag_on),
    ):
        svc = TaskExecutionService()
        _await(svc.apply_result("agent-x", _failed_envelope(error_code),
                                activity_id="act-1", breaker_enabled=False, release_slot=True))
    return mock_governor


class TestRecorderGating:
    def test_records_auth_on_won(self):
        gov = _run_apply_with_governor("auth", cas_won=True, flag_on=True)
        gov.record_terminal_failure.assert_called_once_with("agent-x", "auth")

    def test_records_billing_on_won(self):
        gov = _run_apply_with_governor("billing", cas_won=True, flag_on=True)
        gov.record_terminal_failure.assert_called_once_with("agent-x", "billing")

    def test_not_recorded_on_lost_cas(self):
        # A replayed / late callback (lost CAS) must NOT double-count.
        gov = _run_apply_with_governor("auth", cas_won=False, flag_on=True)
        gov.record_terminal_failure.assert_not_called()

    def test_not_recorded_when_flag_off(self):
        gov = _run_apply_with_governor("auth", cas_won=True, flag_on=False)
        gov.record_terminal_failure.assert_not_called()

    def test_timeout_not_recorded(self):
        gov = _run_apply_with_governor("timeout", cas_won=True, flag_on=True)
        gov.record_terminal_failure.assert_not_called()


# ---------------------------------------------------------------------------
# Read points: reaper hold-off + drain hold-off while paused
# ---------------------------------------------------------------------------
class TestReaperHoldOff:
    def _run_sweep(self, *, flag_on, paused):
        from services.cleanup_service import CleanupService, CleanupReport
        import config

        svc = CleanupService()
        report = CleanupReport()
        mock_cap = MagicMock(reclaim_stale=AsyncMock(return_value={}))
        mock_gov = MagicMock(should_hold_reaper=MagicMock(return_value=paused))
        with (
            patch("services.cleanup_service.get_capacity_manager", return_value=mock_cap),
            patch("services.redelivery_governor.get_redelivery_governor", return_value=mock_gov),
            patch.object(config, "REDELIVERY_GOVERNOR_ENABLED", flag_on),
        ):
            _await(svc._sweep_stale_slots(report, set()))
        return mock_cap

    def test_reaper_held_while_paused(self):
        cap = self._run_sweep(flag_on=True, paused=True)
        cap.reclaim_stale.assert_not_awaited()      # early return, no destructive sweep

    def test_reaper_runs_when_not_paused(self):
        cap = self._run_sweep(flag_on=True, paused=False)
        cap.reclaim_stale.assert_awaited()

    def test_reaper_runs_when_flag_off(self):
        cap = self._run_sweep(flag_on=False, paused=True)
        cap.reclaim_stale.assert_awaited()          # governor inert when OFF


class TestDrainHoldOff:
    def _run_maint(self, *, flag_on, paused):
        from services.capacity_manager import CapacityManager
        import config

        cm = CapacityManager.__new__(CapacityManager)
        cm._backlog = MagicMock(
            expire_stale=AsyncMock(),
            drain_orphans_all=AsyncMock(),
        )
        cm._backstop_open_breaker_backlog = AsyncMock()
        cm._redis = MagicMock()
        mock_gov = MagicMock(should_hold_reaper=MagicMock(return_value=paused))
        with (
            patch("services.redelivery_governor.get_redelivery_governor", return_value=mock_gov),
            patch.object(config, "REDELIVERY_GOVERNOR_ENABLED", flag_on),
        ):
            _await(cm.run_maintenance())
        return cm

    def test_drain_held_while_paused(self):
        cm = self._run_maint(flag_on=True, paused=True)
        cm._backlog.expire_stale.assert_awaited()           # slow net always runs
        cm._backlog.drain_orphans_all.assert_not_awaited()  # held
        cm._backstop_open_breaker_backlog.assert_not_awaited()

    def test_drain_runs_when_not_paused(self):
        cm = self._run_maint(flag_on=True, paused=False)
        cm._backlog.drain_orphans_all.assert_awaited()
        cm._backstop_open_breaker_backlog.assert_awaited()


# ---------------------------------------------------------------------------
# Callback endpoint: 503 while paused
# ---------------------------------------------------------------------------
class TestEndpointPaused:
    def test_callback_503_while_paused(self):
        from routers.agents import agent_execution_result
        from models import ExecutionResultEnvelope
        import config

        execution = SimpleNamespace(
            id="exec-1", agent_name="agent-a", status="running",
            claude_session_id="dispatched_async",
        )
        mock_db = MagicMock()
        mock_db.validate_mcp_api_key.return_value = {"scope": "agent", "agent_name": "agent-a"}
        mock_db.get_execution.return_value = execution
        apply_mock = AsyncMock()
        svc = MagicMock(apply_result=apply_mock)
        mock_gov = MagicMock(is_paused=MagicMock(return_value=True))

        req = SimpleNamespace(headers={"Authorization": "Bearer trinity_mcp_k"})
        payload = ExecutionResultEnvelope(status="success", response="ok", metadata={})

        with (
            patch("routers.agents.db", mock_db),
            patch("services.heartbeat_service.authorize_heartbeat", return_value=True),
            patch("services.task_execution_service.get_task_execution_service", return_value=svc),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
            patch("services.redelivery_governor.get_redelivery_governor", return_value=mock_gov),
            patch.object(config, "REDELIVERY_GOVERNOR_ENABLED", True),
        ):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as ei:
                _await(agent_execution_result("agent-a", "exec-1", payload, req))
        assert ei.value.status_code == 503
        assert "Retry-After" in ei.value.headers
        apply_mock.assert_not_awaited()             # never finalized while paused
