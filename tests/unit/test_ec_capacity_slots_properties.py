"""/edge-cases 2026-10-06 — Hypothesis properties for slot accounting.

Modules under test:
  src/backend/services/slot_service.py
  src/backend/services/capacity_manager.py  (in-memory overflow FIFO only)

Companion to ``test_ec_capacity_slots_edges.py`` (the deterministic matrix).
These properties drive the REAL SlotService / CapacityManager helpers on
``fakeredis`` against small Python oracles:

P1  model-based: any interleaving of acquire / release / renew / double-release
    on one agent keeps ZCARD == |oracle set| <= max(cap, 0); acquire returns
    True iff the oracle had room; the drain callback fires exactly once per
    release that freed a held id (the #1083 replay gate) and never otherwise.
P2  meter bounds: for any cap (incl. 0 / negative — a lowered ceiling) and any
    active count, ``0 <= available`` and ``available == max(0, cap - active)``.
P3  TTL oracle + monotonicity: the per-agent sweep reaps exactly
    ``{slot : age > own_timeout + buffer (or fallback when the hash is gone)}``,
    and anything reaped at time t is reaped at any later time.
P4  in-memory overflow: enqueue/pop sequences match a deque oracle, positions
    are ``depth + 1``, depth never exceeds IN_MEMORY_DEPTH, listing is FIFO.

Single-process only: cross-worker atomicity is
``test_r14`` in the edges file, not a property here.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis
import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_REAL = {
    n: importlib.import_module(n)
    for n in ("services", "services.settings_service", "services.slot_service",
              "services.capacity_manager")
}
_SS = _REAL["services.slot_service"]
_CM = _REAL["services.capacity_manager"]
SLOT_TTL_BUFFER = _SS.SLOT_TTL_BUFFER

_SETTINGS = settings(max_examples=200, deadline=None,
                     suppress_health_check=[HealthCheck.function_scoped_fixture])


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t


def _svc():
    svc = _SS.SlotService.__new__(_SS.SlotService)
    svc.redis = fakeredis.FakeRedis(decode_responses=True)
    svc.slots_prefix = "agent:slots:"
    svc.metadata_prefix = "agent:slot:"
    svc._on_release_callbacks = []
    return svc


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    for n, m in _REAL.items():
        monkeypatch.setitem(sys.modules, n, m)


# ---------------------------------------------------------------------------
# P1 — model-based acquire/release/renew
# ---------------------------------------------------------------------------

_ids = st.sampled_from(["e0", "e1", "e2", "e3", "e4", "e5"])
_ops = st.lists(
    st.tuples(st.sampled_from(["acquire", "release", "renew"]), _ids),
    max_size=40,
)


@_SETTINGS
@given(cap=st.integers(min_value=-2, max_value=4), ops=_ops)
@example(cap=1, ops=[("acquire", "e0"), ("release", "e0"), ("release", "e0")])
@example(cap=0, ops=[("acquire", "e0"), ("release", "e0")])
@example(cap=2, ops=[("acquire", "e0"), ("acquire", "e0"), ("acquire", "e1"),
                     ("acquire", "e2")])
def test_p1_slot_accounting_matches_set_oracle(cap, ops):
    svc = _svc()
    drains = []

    async def cb(agent):
        drains.append(agent)

    svc.register_on_release(cb)
    held: set = set()
    expected_drains = 0

    async def go():
        nonlocal expected_drains
        for op, eid in ops:
            if op == "acquire":
                room = len(held) < cap
                got = await svc.acquire_slot("a", eid, cap, timeout_seconds=900)
                # Re-acquire of a held id still counts itself (ZCARD includes it).
                assert got is room, (op, eid, held, cap)
                if got:
                    held.add(eid)
            elif op == "release":
                if eid in held:
                    expected_drains += 1
                    held.discard(eid)
                await svc.release_slot("a", eid)
            else:
                assert svc.renew_slot("a", eid) is (eid in held)
            await asyncio.sleep(0)
            assert svc.redis.zcard("agent:slots:a") == len(held)
            assert len(held) <= max(cap, 0)
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(go())
    assert set(svc.redis.zrange("agent:slots:a", 0, -1)) == held
    for eid in held:
        assert svc.redis.exists(svc._metadata_key("a", eid))
    assert len(drains) == expected_drains


# ---------------------------------------------------------------------------
# P2 — meter bounds
# ---------------------------------------------------------------------------


@_SETTINGS
@given(cap=st.integers(min_value=-5, max_value=40),
       active=st.integers(min_value=0, max_value=12))
@example(cap=0, active=0)
@example(cap=2, active=5)
@example(cap=-1, active=1)
def test_p2_available_slots_is_floored_difference(cap, active):
    svc = _svc()
    for i in range(active):
        svc.redis.zadd("agent:slots:a", {f"e{i}": 1.0 + i})
    state = asyncio.run(svc.get_slot_state("a", cap))
    assert state.active_slots == active
    assert state.available_slots == max(0, cap - active)
    assert state.available_slots >= 0
    bulk = asyncio.run(svc.get_all_slot_states({"a": cap}))
    assert bulk == {"a": {"max": cap, "active": active}}


# ---------------------------------------------------------------------------
# P3 — TTL oracle + monotonicity
# ---------------------------------------------------------------------------

_slot = st.tuples(
    st.integers(min_value=0, max_value=20_000),            # age seconds
    st.one_of(st.none(), st.integers(min_value=0, max_value=7200)),  # stored timeout
)


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(slots=st.lists(_slot, max_size=8),
       fallback=st.one_of(st.none(), st.integers(min_value=0, max_value=10_000)),
       later=st.integers(min_value=0, max_value=5_000))
@example(slots=[(1200, None)], fallback=None, later=0)
@example(slots=[(1201, None)], fallback=None, later=0)
@example(slots=[(300, 0)], fallback=None, later=1)
def test_p3_sweep_reaps_exactly_expired_and_is_monotone(monkeypatch, slots, fallback, later):
    clock = _Clock()
    monkeypatch.setattr(_SS, "time", clock)
    fb = fallback if fallback is not None else _SS.DEFAULT_SLOT_TTL_SECONDS
    t0 = clock.t  # scores anchored here; only the clock moves for "later"

    def build():
        svc = _svc()
        for i, (age, timeout) in enumerate(slots):
            eid = f"e{i}"
            svc.redis.zadd("agent:slots:a", {eid: t0 - age})
            if timeout is not None:
                svc.redis.hset(svc._metadata_key("a", eid), "timeout_seconds", str(timeout))
        return svc

    def oracle(shift):
        out = set()
        for i, (age, timeout) in enumerate(slots):
            ttl = timeout + SLOT_TTL_BUFFER if timeout is not None else fb
            if age + shift > ttl:
                out.add(f"e{i}")
        return out

    now_reaped = set(asyncio.run(build()._cleanup_stale_slots_for_agent("a", fallback)))
    assert now_reaped == oracle(0)

    clock.t += later
    later_reaped = set(asyncio.run(build()._cleanup_stale_slots_for_agent("a", fallback)))
    assert later_reaped == oracle(later)
    assert now_reaped <= later_reaped


# ---------------------------------------------------------------------------
# P4 — in-memory overflow FIFO
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setattr(_CM.redis, "from_url",
                        lambda *_a, **_k: fakeredis.FakeRedis(decode_responses=True))
    slots = AsyncMock()
    slots.register_on_release = lambda cb: None
    return _CM.CapacityManager(redis_url="redis://t", slot_service=slots,
                               backlog_service=AsyncMock())


@_SETTINGS
@given(ops=st.lists(st.sampled_from(["push", "pop"]), max_size=30))
@example(ops=["push"] * 4)
@example(ops=["pop", "push", "pop", "pop"])
def test_p4_in_memory_queue_matches_deque_oracle(mgr, ops):
    mgr._redis.flushall()
    model: deque = deque()
    n = 0
    for op in ops:
        if op == "push":
            eid = f"q{n}"
            n += 1
            if len(model) >= _CM.IN_MEMORY_DEPTH:
                with pytest.raises(_CM.CapacityFull):
                    mgr._mem_enqueue(agent_name="a", execution_id=eid,
                                     source=_CM.ExecutionSource.USER, source_agent=None,
                                     source_user_id=None, source_user_email=None,
                                     message="")
            else:
                pos = mgr._mem_enqueue(agent_name="a", execution_id=eid,
                                       source=_CM.ExecutionSource.USER, source_agent=None,
                                       source_user_id=None, source_user_email=None,
                                       message="")
                assert pos == len(model) + 1
                model.append(eid)
        else:
            mgr._mem_pop("a")
            if model:
                model.popleft()
        assert [e.id for e in mgr._mem_list("a")] == list(model)
        assert len(model) <= _CM.IN_MEMORY_DEPTH
