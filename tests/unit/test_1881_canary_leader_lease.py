"""#1881 part 2 — cross-worker leader lease on the canary watcher loop.

The FastAPI lifespan starts `CanaryService` in EVERY uvicorn worker and prod
runs `uvicorn main:app --workers 2`. `self._lock` is an `asyncio.Lock`: it
guards re-entrancy inside one process and says nothing about cross-worker
exclusion, so both workers ran a full cycle every 5 minutes. Measured on eu2
before the fix: cycle log lines interleaving at ~2m15s / ~2m47s alternating gaps
(two 5-minute loops, offset) and 11,942 `canary_violations` rows in 24h.

Part 1 (forwarding `CANARY_ENABLED` into `docker-compose.prod.yml`) shipped
first, which is what turned this from dormant to live — hence the issue's
insistence that the two land together.

Covers:
  - distinct worker ids, and only one of two concurrent workers becomes leader
  - the holder refreshes and keeps leadership across cycles
  - release hands off immediately, and only ever deletes its OWN lease
  - TTL expiry (holder gone) lets a sibling take over — leadership fails over
  - Redis down / Redis erroring fails OPEN to leader (silence is the one
    direction a canary must never fail in)
  - `_loop` runs the cycle only when this worker is the leader, and a non-leader
    never reaches `collect_snapshot` — R-01's per-agent `docker exec` sweep is
    the cost being removed, so returning after it would fix nothing
  - a non-leader logs nothing on a steady cycle (leadership is logged on the
    TRANSITION only)
  - `run_cycle()` — the `POST /api/canary/run-cycle` path — is deliberately NOT
    gated
  - the lease does not tempt anyone into weakening H-01's / R-01's
    elapsed-wall-clock gates, which stay load-bearing because the lease fails
    open

Lives under ``tests/unit/`` deliberately: the main canary suite is
``tests/test_canary_invariants.py``, which **no CI workflow executes** — every
gating job runs ``cd tests && python -m pytest unit/`` (filed as #2037). A guard
placed only beside that suite would never go red.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import pytest


class FakeRedis:
    """Minimal in-memory stand-in for the shared breaker Redis client.

    Models the subset the leader lock uses: SET NX EX, GET, EXPIRE, DELETE.
    TTL is not time-simulated — expiry is modelled by the test deleting the key
    directly, which is exactly what a real TTL lapse presents to the next
    `set(nx=True)`. Mirrors `test_1464_monitoring_leader_lock.FakeRedis`.
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        if key not in self.store:
            return False
        self.ttls[key] = int(ttl)
        return True

    def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0


def _module():
    from services import canary_service as module

    return module


def _svc(interval: Optional[int] = None):
    """A CanaryService with a unique worker id (fresh instances differ)."""
    module = _module()
    if interval is None:
        return module.CanaryService()
    return module.CanaryService(interval_seconds=interval)


def _use_redis(monkeypatch, fake):
    monkeypatch.setattr(_module(), "get_breaker_redis", lambda: fake)


# ---------------------------------------------------------------------------
# Lease mechanics
# ---------------------------------------------------------------------------


def test_distinct_worker_ids():
    a, b = _svc(), _svc()
    assert a._worker_id != b._worker_id


def test_only_one_worker_becomes_leader(monkeypatch):
    """The headline AC: a second concurrent instance does not get the lease."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True   # acquires the free lease
    assert b._try_acquire_leadership() is False  # loses — a holds it
    assert fake.get(_module().REDIS_KEY_LEADER) == a._worker_id


def test_leader_refreshes_and_keeps_leadership(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    # Subsequent cycles refresh (get == self → expire) and stay leader, while
    # the sibling keeps losing. Leadership must not round-robin.
    for _ in range(3):
        assert a._try_acquire_leadership() is True
        assert b._try_acquire_leadership() is False


def test_refresh_extends_the_ttl(monkeypatch):
    """Own-lease refresh must actually re-arm the expiry.

    A refresh that acquired-or-nothing would let the lease lapse mid-run on a
    long cycle, a sibling would grab it, and leadership would flap — restoring
    the concurrent double-probing this lease exists to remove.
    """
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a = _svc()
    key = _module().REDIS_KEY_LEADER

    assert a._try_acquire_leadership() is True
    fake.ttls[key] = 1  # simulate the lease nearly expired
    assert a._try_acquire_leadership() is True
    assert fake.ttls[key] == a._leader_ttl()


def test_release_hands_off_immediately(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is False

    a._release_leadership()  # graceful shutdown
    assert _module().REDIS_KEY_LEADER not in fake.store
    assert b._try_acquire_leadership() is True  # sibling takes over at once


def test_release_only_deletes_own_lease(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    b._release_leadership()  # b must NOT be able to delete a's lease
    assert fake.get(_module().REDIS_KEY_LEADER) == a._worker_id


def test_stop_releases_leadership(monkeypatch):
    """`stop()` must hand off, not leave the sibling idle for a whole TTL."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    a.stop()
    assert b._try_acquire_leadership() is True


def test_ttl_expiry_lets_sibling_take_over(monkeypatch):
    """Leadership fails over when the holder stops (second half of the AC)."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    # Simulate a's death: its lease TTL lapses (key gone). b acquires next cycle,
    # with no restart and no operator action.
    fake.delete(_module().REDIS_KEY_LEADER)
    assert b._try_acquire_leadership() is True
    assert fake.get(_module().REDIS_KEY_LEADER) == b._worker_id


# ---------------------------------------------------------------------------
# TTL sizing
# ---------------------------------------------------------------------------


def test_ttl_outlasts_a_cycle_plus_a_sleep():
    """The TTL is refreshed once at the TOP of a cycle, so it must comfortably
    outlast one cycle plus the inter-cycle sleep. A TTL at or below the interval
    guarantees a mid-cycle lapse and permanent leadership flapping."""
    svc = _svc()
    assert svc._leader_ttl() > svc.interval


def test_ttl_has_a_floor_independent_of_the_interval():
    """A canary cycle's cost is dominated by R-01's per-agent `docker exec`
    sweep, which scales with FLEET SIZE and is bounded by no timeout — not with
    how often we look. So `interval * 3` alone is the wrong shape at a short
    interval: shortening the interval must not shorten the TTL below what one
    sweep can take."""
    module = _module()
    assert _svc(interval=5)._leader_ttl() == module._LEADER_TTL_FLOOR_SECONDS
    # The floor is a no-op at the default interval — the default is unchanged.
    assert _svc()._leader_ttl() == _svc().interval * 3


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_redis_down_fails_open_to_leader(monkeypatch):
    """No Redis → every worker acts as leader.

    Deliberate, and the tradeoff is NOT monitoring #1464's: a duplicate canary
    cycle is not inert (it re-runs the docker sweep and double-persists rows).
    We take it anyway, because failing closed would silently stop the one
    subsystem whose job is noticing that something went quiet — the silent-green
    failure H-01 exists to catch, one level up, where no invariant can see it.
    """
    monkeypatch.setattr(_module(), "get_breaker_redis", lambda: None)
    a, b = _svc(), _svc()
    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is True


def test_redis_error_fails_open_to_leader(monkeypatch):
    class BoomRedis(FakeRedis):
        def set(self, *a, **k):
            raise RuntimeError("redis boom")

    _use_redis(monkeypatch, BoomRedis())
    assert _svc()._try_acquire_leadership() is True


def test_release_never_raises(monkeypatch):
    """Shutdown must not be blocked by a Redis failure."""
    class BoomRedis(FakeRedis):
        def get(self, *a, **k):
            raise RuntimeError("redis boom")

    _use_redis(monkeypatch, BoomRedis())
    _svc()._release_leadership()  # must not raise


# ---------------------------------------------------------------------------
# The loop actually honours the lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("leader,expected_calls", [(True, 1), (False, 0)])
async def test_loop_runs_cycle_only_when_leader(monkeypatch, leader, expected_calls):
    module = _module()
    svc = _svc(interval=1)
    monkeypatch.setattr(svc, "_is_cycle_leader", lambda: leader)

    calls = []

    async def _cycle(*a, **k):
        calls.append(1)

    monkeypatch.setattr(svc, "run_cycle", _cycle)

    # Collapse the 30s warm-up and stop the loop after its first sleep, so it
    # runs exactly one iteration.
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            svc._running = False

    monkeypatch.setattr(module.asyncio, "sleep", _sleep)

    svc._running = True
    await svc._loop()

    assert len(calls) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("leader,expected_collections", [(False, 0), (True, 1)])
async def test_non_leader_never_collects_a_snapshot(
    monkeypatch, leader, expected_collections
):
    """The saving is the probe, not the bookkeeping.

    R-01 `docker exec`s into every running agent container inside
    `collect_snapshot`. A gate that let a non-leader collect and then discarded
    the result would leave the fleet load — the headline consequence in the
    issue — completely unchanged. So this drives the REAL `run_cycle` /
    `_run_cycle_inner` path (db, Redis and the alert sink stubbed) rather than a
    mock of it, and counts collections.

    The `leader=True` case is the positive control, and it is load-bearing: a
    recorder that is never called proves nothing unless the same harness is
    shown to record when the cycle does run. `_loop` swallows every exception
    from a cycle (`except Exception: logger.exception(...)`), so a
    raise-on-call probe here would be silently eaten and pass vacuously with or
    without the gate.
    """
    from canary.snapshot import Snapshot

    module = _module()
    svc = _svc(interval=1)
    monkeypatch.setattr(svc, "_is_cycle_leader", lambda: leader)

    collections = []

    def _collect():
        collections.append(1)
        return Snapshot(snapshot_time="2026-08-06T12:00:00Z")

    class _StubDB:
        def get_latest_canary_violation_per_invariant(self):
            return {}

    monkeypatch.setattr(module, "collect_snapshot", _collect)
    monkeypatch.setattr(module, "run_invariants", lambda snap, ids=None: {})
    monkeypatch.setattr(module, "db", _StubDB())
    monkeypatch.setattr(module.CanaryService, "_redis", staticmethod(FakeRedis))

    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            svc._running = False

    monkeypatch.setattr(module.asyncio, "sleep", _sleep)

    svc._running = True
    await svc._loop()

    assert len(collections) == expected_collections


# ---------------------------------------------------------------------------
# Log noise
# ---------------------------------------------------------------------------


def test_steady_non_leader_logs_nothing(monkeypatch, caplog):
    """AC: "Non-leader workers skip the cycle without logging noise every 5
    minutes." One line per cycle forever is how real canary output gets tuned
    out — which would defeat the harness by a different route."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()
    assert a._try_acquire_leadership() is True

    with caplog.at_level(logging.DEBUG, logger=_module().logger.name):
        for _ in range(5):
            assert b._is_cycle_leader() is False

    assert caplog.records == []


def test_leadership_transitions_are_logged_once(monkeypatch, caplog):
    """Acquiring and yielding are single events, each worth exactly one line."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()
    key = _module().REDIS_KEY_LEADER

    with caplog.at_level(logging.INFO, logger=_module().logger.name):
        for _ in range(3):
            assert a._is_cycle_leader() is True          # acquire, then steady
        acquired = [r for r in caplog.records if "acquired leadership" in r.message]
        assert len(acquired) == 1

        # A sibling steals the lease (a's death and revival, or a clock lapse).
        fake.store[key] = b._worker_id
        for _ in range(3):
            assert a._is_cycle_leader() is False         # yield, then steady
        yielded = [r for r in caplog.records if "yielded leadership" in r.message]
        assert len(yielded) == 1


# ---------------------------------------------------------------------------
# The on-demand endpoint is deliberately exempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cycle_is_not_leader_gated(monkeypatch):
    """`POST /api/canary/run-cycle` lands on an arbitrary worker.

    Gating it would make an explicit admin request return an empty result about
    half the time under `--workers 2` — structurally identical to a green cycle,
    which is precisely the ambiguity the `skipped`/409 contract exists to
    remove. Regression guard against "the lease belongs on run_cycle".
    """
    module = _module()
    svc = _svc()
    # This worker is emphatically NOT the leader.
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: False)

    ran = []

    async def _inner(invariant_ids):
        ran.append(invariant_ids)
        return module.CycleResult(snapshot_time="2026-08-06T12:00:00Z")

    monkeypatch.setattr(svc, "_run_cycle_inner", _inner)

    result = await svc.run_cycle(invariant_ids=["H-01"])

    assert ran == [["H-01"]]
    assert result.skipped is False


# ---------------------------------------------------------------------------
# The lease does not make the confirmation gates redundant
# ---------------------------------------------------------------------------


def test_confirmation_gates_remain_elapsed_wall_clock():
    """H-01's `CONFIRMATION_MIN_SECONDS` and R-01's `DWELL_SECONDS` were written
    to be correct under multiple workers (learnings.md 2026-07-29). The lease
    does not retire them, and this pins that.

    Two independent reasons, either one sufficient. (1) The lease **fails open**
    — see `test_redis_down_fails_open_to_leader` — so concurrent loops over the
    shared markers (`canary:h01:suspect_since`, `agent:canary_zombie:{name}`)
    remain reachable exactly when Redis is down, which is one of the states the
    harness exists to report. (2) More fundamentally, both gates ride out a
    *real-time* transient — a container finishing teardown, a `claude` child
    awaiting its parent's `wait()` — which is a single-worker property that a
    cycle count never expressed correctly at any worker count.

    If a future change makes either gate look redundant, the lease has been
    mis-modelled: it is best-effort, not mutual exclusion.
    """
    from canary.invariants import h01_collector_blindness as h01
    from canary.invariants import r01_no_zombie_claude as r01

    assert h01.CONFIRMATION_MIN_SECONDS > 0
    assert r01.DWELL_SECONDS > 0
    # Both must remain strictly shorter than the lease TTL, or a single leader
    # failover would be indistinguishable from a confirmed condition.
    assert h01.CONFIRMATION_MIN_SECONDS < _svc()._leader_ttl()


def test_fail_open_lets_two_workers_share_the_markers(monkeypatch):
    """The concrete mechanism behind the test above: with Redis unreachable,
    both workers are leader, so both run cycles against the same Redis markers —
    the precise pre-#1881 condition the elapsed-time gates were built for."""
    monkeypatch.setattr(_module(), "get_breaker_redis", lambda: None)
    a, b = _svc(), _svc()
    assert (a._is_cycle_leader(), b._is_cycle_leader()) == (True, True)


# ---------------------------------------------------------------------------
# Key naming
# ---------------------------------------------------------------------------


def test_leader_key_follows_the_precedent_shape():
    """`<service>:leader`, in the `canary:` namespace the harness already owns
    (`canary:last_cycle_at`, `canary:e02:terminal_seen`, `canary:h01:suspect_since`).

    Not `agent:`-prefixed, so #1560's `CLEARED_KEYSPACES` parity guard correctly
    does not apply — the lease is global, not per-agent, and clearing it on an
    agent lifecycle event would drop leadership for the whole fleet.
    """
    key = _module().REDIS_KEY_LEADER
    assert key == "canary:leader"
    assert not key.startswith("agent:")
