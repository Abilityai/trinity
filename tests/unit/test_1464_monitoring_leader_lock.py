"""Unit tests for #1464 — cross-worker leader election on the fleet-health loop.

The FastAPI lifespan starts MonitoringService in EVERY uvicorn worker (prod runs
`--workers 2`). Without a guard, N workers each probe the whole fleet per
interval — duplicate `agent_health_checks` rows and an N× `record_failure()`
feed into the per-agent circuit breaker (amplifies #1463). The fix gives the
loop a Redis leader lease so exactly one worker probes per cycle, with automatic
failover when the holder dies.

Covers:
  - only one of two workers (distinct worker ids, shared Redis) becomes leader
  - the holder refreshes and keeps leadership across cycles
  - Redis-down fails open to leader (single-worker dev keeps probing)
  - graceful release hands leadership to a sibling immediately
  - TTL-expiry (holder gone) lets a sibling take over
  - _run_loop only runs the check cycle when this worker is the leader
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Load monitoring_service directly to bypass services/__init__.py side effects
# (same pattern as test_monitoring_health_check_classification.py).
_spec = importlib.util.spec_from_file_location(
    "monitoring_service_lock_under_test",
    str(_BACKEND / "services" / "monitoring_service.py"),
)
monitoring_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_service)


class FakeRedis:
    """Minimal in-memory stand-in for the shared breaker Redis client.

    Models the subset the leader lock uses: SET NX EX, GET, EXPIRE, DELETE.
    TTL is not time-simulated — expiry is modelled by the test deleting the key
    directly (the handoff scenario), which is exactly what a real TTL lapse
    presents to the next `set(nx=True)`.
    """

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        return key in self.store

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _svc():
    """A MonitoringService with a unique worker id (fresh instances differ)."""
    return monitoring_service.MonitoringService()


def _use_redis(monkeypatch, fake):
    monkeypatch.setattr(monitoring_service, "get_breaker_redis", lambda: fake)


def test_distinct_worker_ids():
    a, b = _svc(), _svc()
    assert a._worker_id != b._worker_id


def test_only_one_worker_becomes_leader(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True   # acquires the free lease
    assert b._try_acquire_leadership() is False  # loses — a holds it
    # a's id is what's stored
    assert fake.get(monitoring_service._LEADER_KEY) == a._worker_id


def test_leader_refreshes_and_keeps_leadership(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a = _svc()

    assert a._try_acquire_leadership() is True
    # subsequent cycles refresh (get==self → expire) and stay leader
    assert a._try_acquire_leadership() is True
    assert a._try_acquire_leadership() is True


def test_redis_down_fails_open_to_leader(monkeypatch):
    monkeypatch.setattr(monitoring_service, "get_breaker_redis", lambda: None)
    a, b = _svc(), _svc()
    # No Redis → every worker acts as leader (single-worker dev keeps probing;
    # in Redis-down prod the breaker is itself fail-open so a doubled feed is inert).
    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is True


def test_redis_error_fails_open_to_leader(monkeypatch):
    class BoomRedis(FakeRedis):
        def set(self, *a, **k):
            raise RuntimeError("redis boom")

    _use_redis(monkeypatch, BoomRedis())
    assert _svc()._try_acquire_leadership() is True


def test_release_hands_off_immediately(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is False

    a._release_leadership()                       # graceful shutdown
    assert monitoring_service._LEADER_KEY not in fake.store
    assert b._try_acquire_leadership() is True     # sibling takes over at once


def test_release_only_deletes_own_lease(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    # b must NOT be able to delete a's lease
    b._release_leadership()
    assert fake.get(monitoring_service._LEADER_KEY) == a._worker_id


def test_ttl_expiry_lets_sibling_take_over(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    # Simulate a's death: its lease TTL lapses (key gone). b acquires next cycle.
    fake.delete(monitoring_service._LEADER_KEY)
    assert b._try_acquire_leadership() is True
    assert fake.get(monitoring_service._LEADER_KEY) == b._worker_id


@pytest.mark.asyncio
async def test_run_loop_runs_cycle_only_when_leader(monkeypatch):
    """The loop probes iff this worker holds the lease."""
    for leader, expected_calls in ((True, 1), (False, 0)):
        svc = _svc()
        svc.config.docker_check_interval = 1
        monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: leader)

        cycle = AsyncMock()
        monkeypatch.setattr(svc, "_run_check_cycle", cycle)

        # Stop the loop after its first sleep so it runs exactly one iteration.
        async def _sleep_once(_):
            svc._running = False
        monkeypatch.setattr(monitoring_service.asyncio, "sleep", _sleep_once)

        svc._running = True
        await svc._run_loop()

        assert cycle.await_count == expected_calls
