"""
Unit tests for services/heartbeat_service.py (RELIABILITY-004 / #307).

Covers the Redis-backed liveness primitives with a fake Redis double:
  * record_heartbeat — SETEX (TTL=15) + idempotent `seen` marker; Redis-None
    fail-soft.
  * read_heartbeat — hit / missing / malformed-JSON.
  * heartbeat_status — alive / stale / unsupported + Redis-None fail-open.
  * heartbeat_status_bulk — one pipeline round-trip, correct per-agent map.
  * process_watch_tick — returns (agent, kind) transitions; threshold/recovery.
  * _emit_heartbeat_alert — dispatches lost/recovered to monitoring_alerts and
    swallows alert failures so the loop can't die (#307 Option A).

Import-isolation pattern follows tests/unit/test_circuit_breaker.py: load the
module directly via importlib so services/__init__.py (Docker, models, …) is
never executed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_heartbeat_service():
    spec = importlib.util.spec_from_file_location(
        "heartbeat_service_under_test",
        str(_BACKEND / "services" / "heartbeat_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hb = _load_heartbeat_service()


# ---------------------------------------------------------------------------
# Fake Redis double
# ---------------------------------------------------------------------------
class _FakePipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def get(self, key):
        self._ops.append(("get", key))
        return self

    def execute(self):
        out = []
        for op, key in self._ops:
            if op == "get":
                out.append(self._store.get(key))
        self._ops = []
        return out


class FakeRedis:
    """Minimal Redis stand-in: tracks setex TTLs and supports get/set/pipeline."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.setex_calls = []

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        self.setex_calls.append((key, ttl, value))
        return True

    def set(self, key, value, nx=False):
        # nx=True (set-if-absent) mirrors the real client: no-op if present.
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def incr(self, key):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = val
        return val

    def expire(self, key, ttl):
        if key in self.store:
            self.ttls[key] = ttl
            return True
        return False

    def delete(self, *keys):
        # Real redis-py delete(*names) accepts multiple keys and returns the
        # count removed. clear_heartbeat() deletes all three keys in one call.
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
            self.store.pop(key, None)
            self.ttls.pop(key, None)
        return deleted

    def pipeline(self):
        return _FakePipeline(self.store)

    # Helper used by tests to simulate TTL expiry of the heartbeat key.
    def expire_now(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(hb, "_get_redis", lambda: r)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(hb, "_get_redis", lambda: None)


# ---------------------------------------------------------------------------
# record_heartbeat
# ---------------------------------------------------------------------------
def test_record_heartbeat_sets_ttl_and_seen(fake_redis):
    payload = {"memory_mb": 42.5, "active_executions": 2, "uptime_s": 100.0}
    ok = hb.record_heartbeat("agent-a", payload)

    assert ok is True
    hb_key = "agent:heartbeat:agent-a"
    seen_key = "agent:heartbeat:seen:agent-a"

    # SETEX with TTL == HEARTBEAT_TTL_SECONDS (15).
    assert fake_redis.ttls[hb_key] == hb.HEARTBEAT_TTL_SECONDS == 15
    stored = json.loads(fake_redis.store[hb_key])
    assert stored["memory_mb"] == 42.5
    assert stored["active_executions"] == 2
    # record_heartbeat stamps a server-side receive timestamp.
    assert "ts" in stored and isinstance(stored["ts"], (int, float))

    # seen marker set, no TTL.
    assert fake_redis.store[seen_key] == "1"
    assert seen_key not in fake_redis.ttls


def test_record_heartbeat_redis_none_returns_false(no_redis):
    assert hb.record_heartbeat("agent-a", {"memory_mb": 1}) is False


# ---------------------------------------------------------------------------
# clear_heartbeat — delete/rename cleanup of every heartbeat key
# ---------------------------------------------------------------------------
def test_clear_heartbeat_removes_all_keys(fake_redis):
    """The no-TTL `seen` marker would otherwise leak past delete/rename; clear
    removes hb + seen + misses in one call so a removed agent leaves nothing."""
    hb.record_heartbeat("agent-a", {"memory_mb": 1.0})
    fake_redis.store["agent:heartbeat:misses:agent-a"] = 2  # simulate active counter
    assert "agent:heartbeat:agent-a" in fake_redis.store
    assert "agent:heartbeat:seen:agent-a" in fake_redis.store

    hb.clear_heartbeat("agent-a")

    assert "agent:heartbeat:agent-a" not in fake_redis.store
    assert "agent:heartbeat:seen:agent-a" not in fake_redis.store
    assert "agent:heartbeat:misses:agent-a" not in fake_redis.store


def test_clear_heartbeat_redis_none_is_noop(no_redis):
    # Best-effort: must not raise when Redis is unavailable.
    hb.clear_heartbeat("agent-a")


# ---------------------------------------------------------------------------
# read_heartbeat
# ---------------------------------------------------------------------------
def test_read_heartbeat_hit(fake_redis):
    hb.record_heartbeat("agent-a", {"memory_mb": 7, "active_executions": 0})
    data = hb.read_heartbeat("agent-a")
    assert data is not None
    assert data["memory_mb"] == 7


def test_read_heartbeat_missing_returns_none(fake_redis):
    assert hb.read_heartbeat("ghost") is None


def test_read_heartbeat_malformed_returns_none(fake_redis):
    fake_redis.store["agent:heartbeat:agent-a"] = "{not json"
    assert hb.read_heartbeat("agent-a") is None


# ---------------------------------------------------------------------------
# heartbeat_status
# ---------------------------------------------------------------------------
def test_status_alive(fake_redis):
    hb.record_heartbeat("agent-a", {"memory_mb": 12.0, "active_executions": 3})
    st = hb.heartbeat_status("agent-a")

    assert st["heartbeat_state"] == "alive"
    assert st["heartbeat_alive"] is True
    assert st["heartbeat_active_executions"] == 3
    assert st["heartbeat_memory_mb"] == 12.0
    assert st["last_heartbeat_age_s"] is not None
    assert 0 <= st["last_heartbeat_age_s"] < 5


def test_status_stale(fake_redis):
    hb.record_heartbeat("agent-a", {"memory_mb": 12.0})
    # Heartbeat key expired but the seen marker persists.
    fake_redis.expire_now("agent:heartbeat:agent-a")
    st = hb.heartbeat_status("agent-a")

    assert st["heartbeat_state"] == "stale"
    assert st["heartbeat_alive"] is False


def test_status_unsupported_when_never_seen(fake_redis):
    st = hb.heartbeat_status("old-image-agent")
    assert st["heartbeat_state"] == "unsupported"
    assert st["heartbeat_alive"] is None


def test_status_redis_none_is_unsupported(no_redis):
    st = hb.heartbeat_status("agent-a")
    assert st["heartbeat_state"] == "unsupported"
    assert st["heartbeat_alive"] is None


def test_status_alive_with_missing_ts(fake_redis):
    """A seen agent whose payload has no `ts` (old/malformed) is still alive,
    with last_heartbeat_age_s=None — exercises the `isinstance(ts, ...)` guard
    so a missing timestamp can never crash the age computation."""
    fake_redis.store["agent:heartbeat:agent-a"] = json.dumps(
        {"memory_mb": 4.0, "active_executions": 1}
    )
    fake_redis.store["agent:heartbeat:seen:agent-a"] = "1"
    st = hb.heartbeat_status("agent-a")
    assert st["heartbeat_state"] == "alive"
    assert st["heartbeat_alive"] is True
    assert st["last_heartbeat_age_s"] is None
    assert st["heartbeat_active_executions"] == 1


def test_status_alive_with_nonnumeric_ts(fake_redis):
    """A non-numeric `ts` (corrupt payload) yields age=None, still alive."""
    fake_redis.store["agent:heartbeat:agent-a"] = json.dumps(
        {"memory_mb": 4.0, "ts": "not-a-number"}
    )
    fake_redis.store["agent:heartbeat:seen:agent-a"] = "1"
    st = hb.heartbeat_status("agent-a")
    assert st["heartbeat_state"] == "alive"
    assert st["heartbeat_alive"] is True
    assert st["last_heartbeat_age_s"] is None


# ---------------------------------------------------------------------------
# heartbeat_status_bulk
# ---------------------------------------------------------------------------
def test_bulk_mixed_states_single_round_trip(fake_redis, monkeypatch):
    hb.record_heartbeat("alive-agent", {"memory_mb": 5, "active_executions": 1})
    hb.record_heartbeat("stale-agent", {"memory_mb": 9})
    fake_redis.expire_now("agent:heartbeat:stale-agent")
    # "new-agent" never heartbeated -> unsupported.

    # Count how many pipelines are created — D4 says exactly one round-trip.
    created = {"count": 0}
    real_pipeline = fake_redis.pipeline

    def counting_pipeline():
        created["count"] += 1
        return real_pipeline()

    monkeypatch.setattr(fake_redis, "pipeline", counting_pipeline)

    result = hb.heartbeat_status_bulk(["alive-agent", "stale-agent", "new-agent"])

    assert created["count"] == 1  # one pipeline for all agents
    assert result["alive-agent"]["heartbeat_state"] == "alive"
    assert result["alive-agent"]["heartbeat_alive"] is True
    assert result["stale-agent"]["heartbeat_state"] == "stale"
    assert result["stale-agent"]["heartbeat_alive"] is False
    assert result["new-agent"]["heartbeat_state"] == "unsupported"
    assert result["new-agent"]["heartbeat_alive"] is None


def test_bulk_empty_list(fake_redis):
    assert hb.heartbeat_status_bulk([]) == {}


def test_bulk_redis_none_all_unsupported(no_redis):
    result = hb.heartbeat_status_bulk(["a", "b"])
    assert result["a"]["heartbeat_state"] == "unsupported"
    assert result["b"]["heartbeat_state"] == "unsupported"


# ---------------------------------------------------------------------------
# Watch loop (process_watch_tick) — returns (agent, kind) transitions; the
# async loop turns those into monitoring_alerts notifications (#307 Option A).
# ---------------------------------------------------------------------------
@pytest.fixture
def watch_env(fake_redis, monkeypatch):
    """Wire the watch loop's agent listing to a controllable double.

    Returns (fake_redis, agents_list). Mutate agents_list to control which
    agents the tick processes. `process_watch_tick()` now RETURNS the
    `(agent_name, kind)` transitions instead of writing health rows, so tests
    assert on the return value directly.
    """
    agents = []
    monkeypatch.setattr(hb, "_list_agent_names", lambda: list(agents))
    return fake_redis, agents


def _go_stale(fake_redis, name):
    """Mark an agent as previously-seen but currently expired (stale)."""
    hb.record_heartbeat(name, {"memory_mb": 1})
    fake_redis.expire_now(hb._hb_key(name))


def test_watch_increments_miss_counter_each_cycle(watch_env):
    fake_redis, agents = watch_env
    agents.append("agent-a")
    _go_stale(fake_redis, "agent-a")

    hb.process_watch_tick()
    assert int(fake_redis.store[hb._misses_key("agent-a")]) == 1
    hb.process_watch_tick()
    assert int(fake_redis.store[hb._misses_key("agent-a")]) == 2


def test_watch_degraded_transition_only_at_threshold_and_once(watch_env):
    fake_redis, agents = watch_env
    agents.append("agent-a")
    _go_stale(fake_redis, "agent-a")

    assert hb.process_watch_tick() == []  # miss 1
    assert hb.process_watch_tick() == []  # miss 2
    assert hb.process_watch_tick() == [("agent-a", "degraded")]  # miss 3

    # Further misses must NOT re-emit (transition-only).
    assert hb.process_watch_tick() == []  # miss 4
    assert hb.process_watch_tick() == []  # miss 5


def test_watch_ignores_alive_and_unsupported(watch_env):
    fake_redis, agents = watch_env
    agents.extend(["alive-agent", "old-agent"])
    hb.record_heartbeat("alive-agent", {"memory_mb": 1})  # alive
    # old-agent: never seen -> unsupported

    assert hb.process_watch_tick() == []

    # No miss counters created for alive or unsupported agents.
    assert hb._misses_key("alive-agent") not in fake_redis.store
    assert hb._misses_key("old-agent") not in fake_redis.store


def test_watch_recovery_emits_recovered_transition(watch_env):
    fake_redis, agents = watch_env
    agents.append("agent-a")
    _go_stale(fake_redis, "agent-a")

    last = []
    for _ in range(3):
        last = hb.process_watch_tick()  # downgrade at 3
    assert last == [("agent-a", "degraded")]

    # Heartbeat returns -> recovery transition.
    hb.record_heartbeat("agent-a", {"memory_mb": 1})
    assert hb.process_watch_tick() == [("agent-a", "recovered")]

    # Miss counter cleared.
    assert hb._misses_key("agent-a") not in fake_redis.store


def test_watch_recovery_before_downgrade_emits_nothing(watch_env):
    """Counting interrupted before threshold -> clear counter, no transitions."""
    fake_redis, agents = watch_env
    agents.append("agent-a")
    _go_stale(fake_redis, "agent-a")

    hb.process_watch_tick()  # miss 1
    hb.process_watch_tick()  # miss 2 (still below threshold)

    hb.record_heartbeat("agent-a", {"memory_mb": 1})  # recovered early
    # Never downgraded -> no recovery transition either.
    assert hb.process_watch_tick() == []
    assert hb._misses_key("agent-a") not in fake_redis.store


def test_watch_redis_none_survives_no_transition(no_redis, monkeypatch):
    monkeypatch.setattr(hb, "_list_agent_names", lambda: ["agent-a"])
    # Must not raise, must emit no transition.
    assert hb.process_watch_tick() == []




# ---------------------------------------------------------------------------
# authorize_heartbeat — the endpoint's auth predicate (T3, Option B)
# ---------------------------------------------------------------------------
def test_authorize_accepts_own_agent_scoped_key():
    res = {"scope": "agent", "agent_name": "my-agent"}
    assert hb.authorize_heartbeat(res, "my-agent") is True


def test_authorize_rejects_none_result():
    assert hb.authorize_heartbeat(None, "my-agent") is False


def test_authorize_rejects_user_scoped_key():
    res = {"scope": "user", "agent_name": None}
    assert hb.authorize_heartbeat(res, "my-agent") is False


def test_authorize_rejects_other_agents_key():
    """An agent may only heartbeat itself — name mismatch is rejected."""
    res = {"scope": "agent", "agent_name": "other-agent"}
    assert hb.authorize_heartbeat(res, "my-agent") is False


def test_authorize_rejects_system_scoped_key():
    res = {"scope": "system", "agent_name": None}
    assert hb.authorize_heartbeat(res, "my-agent") is False


def test_watch_R2_healthy_fresh_agent_not_degraded(watch_env):
    """R2 (mandatory regression): an agent with a fresh heartbeat is never
    degraded by the watch loop, no matter how many ticks run."""
    fake_redis, agents = watch_env
    agents.append("healthy-agent")
    hb.record_heartbeat("healthy-agent", {"memory_mb": 1, "active_executions": 0})

    for _ in range(10):
        # Re-beat each cycle, as a live agent would.
        hb.record_heartbeat("healthy-agent", {"memory_mb": 1})
        assert hb.process_watch_tick() == []

    assert hb._misses_key("healthy-agent") not in fake_redis.store


# ---------------------------------------------------------------------------
# _emit_heartbeat_alert — transition -> monitoring_alerts dispatch (#307 A)
# ---------------------------------------------------------------------------
class _FakeAlertService:
    """Records which alert method the watch loop dispatched."""

    def __init__(self, raise_on=None):
        self.calls = []
        self._raise_on = raise_on

    async def alert_heartbeat_lost(self, agent_name, missed_beats):
        if self._raise_on == "lost":
            raise RuntimeError("boom")
        self.calls.append(("lost", agent_name, missed_beats))
        return "notif-lost"

    async def alert_heartbeat_recovered(self, agent_name):
        if self._raise_on == "recovered":
            raise RuntimeError("boom")
        self.calls.append(("recovered", agent_name))
        return "notif-recovered"


def _install_fake_alert_service(monkeypatch, svc):
    """Inject a fake services.monitoring_alerts so _emit_heartbeat_alert's lazy
    import resolves without executing the real (Docker-heavy) services package.

    monkeypatch.setitem on sys.modules is the lint-approved seam (auto-restored)
    — see tests/lint_sys_modules.py.
    """
    import types

    fake_pkg = types.ModuleType("services")
    fake_mod = types.ModuleType("services.monitoring_alerts")
    fake_mod.get_alert_service = lambda: svc
    monkeypatch.setitem(sys.modules, "services", fake_pkg)
    monkeypatch.setitem(sys.modules, "services.monitoring_alerts", fake_mod)


def test_emit_degraded_dispatches_heartbeat_lost(monkeypatch):
    svc = _FakeAlertService()
    _install_fake_alert_service(monkeypatch, svc)

    import asyncio
    asyncio.run(hb._emit_heartbeat_alert("agent-a", "degraded", missed_beats=3))

    assert svc.calls == [("lost", "agent-a", 3)]


def test_emit_recovered_dispatches_heartbeat_recovered(monkeypatch):
    svc = _FakeAlertService()
    _install_fake_alert_service(monkeypatch, svc)

    import asyncio
    asyncio.run(hb._emit_heartbeat_alert("agent-a", "recovered"))

    assert svc.calls == [("recovered", "agent-a")]


def test_emit_swallows_alert_failure(monkeypatch):
    """An alert that raises must never propagate — the watch loop can't die."""
    svc = _FakeAlertService(raise_on="lost")
    _install_fake_alert_service(monkeypatch, svc)

    import asyncio
    # Should NOT raise.
    asyncio.run(hb._emit_heartbeat_alert("agent-a", "degraded"))
    assert svc.calls == []
