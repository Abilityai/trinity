"""/edge-cases 2026-10-06 — slot accounting + capacity facade boundaries.

Modules under test:
  src/backend/services/slot_service.py      (SlotService — Redis ZSET count gate)
  src/backend/services/capacity_manager.py  (CapacityManager — the public facade)

Why this file exists: before it, the unit suite never executed
``SlotService.acquire_slot`` / ``release_slot`` / ``get_slot_state`` /
``get_all_slot_states`` / ``is_at_capacity`` / ``force_clear_slots`` at all —
``test_capacity_manager.py`` drives the facade against an ``AsyncMock`` slot
service, so the one function that decides "admit or 429" had 0% branch
coverage. Everything here runs the REAL SlotService on ``fakeredis`` (the same
seam ``test_2433_slot_renew.py`` uses) and, for the facade, a real
CapacityManager wired to that real SlotService with only the persistent
backlog mocked. No Docker, network, or real Redis.

What is pinned, by matrix row (the 2026-10-06 /edge-cases matrix):

* capacity math at the boundaries — cap 0 / 1 / negative, exactly-at-cap,
  release-then-reacquire, re-acquire of an id already held;
* TTL arithmetic — the strict ``age > ttl`` boundary, ``"0"`` / ``""`` /
  negative stored timeouts, ``default_slot_ttl=0`` vs ``None`` (the
  ``is not None`` guard), per-agent fallback in the fleet sweep, SCAN paging;
* double-release / replay — the #1083 gate that a release which frees nothing
  fires NO drain, plus callback isolation;
* meter math — ``available`` never negative when a lowered ceiling leaves
  ``active > max``; pipelined bulk meter fails open to zero;
* facade overflow — in-memory FIFO depth bound, FIFO order, the
  ``release_if_matches`` non-holder path leaving the queue alone,
  ``force_release`` with only queued work.

Real bugs are kept as ``xfail(strict=True)`` with reachability evidence in the
reason; see the matrix file for the classification of every row.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis
import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# #1582 pattern: capture the real modules at collection time so a sibling's
# sys.modules stub can't make our patch target a different object.
_REAL = {
    n: importlib.import_module(n)
    for n in ("services", "services.settings_service", "services.slot_service",
              "services.capacity_manager")
}
_SS = _REAL["services.slot_service"]
_CM = _REAL["services.capacity_manager"]
SlotService = _SS.SlotService
SLOT_TTL_BUFFER = _SS.SLOT_TTL_BUFFER
DEFAULT_SLOT_TTL_SECONDS = _SS.DEFAULT_SLOT_TTL_SECONDS


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Clock:
    """Stand-in for the ``time`` module inside slot_service (only .time())."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def time(self) -> float:
        return self.t


def _svc(server=None) -> SlotService:
    svc = SlotService.__new__(SlotService)
    svc.redis = fakeredis.FakeRedis(server=server, decode_responses=True)
    svc.slots_prefix = "agent:slots:"
    svc.metadata_prefix = "agent:slot:"
    svc._on_release_callbacks = []
    return svc


def _run(coro):
    return asyncio.run(coro)


def _put_slot(svc, agent, eid, score, timeout=None):
    svc.redis.zadd(svc._slots_key(agent), {eid: score})
    if timeout is not None:
        k = svc._metadata_key(agent, eid)
        svc.redis.hset(k, mapping={"timeout_seconds": str(timeout), "started_at": "x"})
        svc.redis.expire(k, 10**6)


@pytest.fixture(autouse=True)
def _pin_modules(monkeypatch):
    for n, m in _REAL.items():
        monkeypatch.setitem(sys.modules, n, m)
    # Deterministic ceiling (no settings DB): the doc default.
    monkeypatch.setattr(_REAL["services.settings_service"],
                        "get_max_parallel_tasks_ceiling", lambda: 10)
    monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(_SS, "time", c)
    return c


# ===========================================================================
# SlotService.acquire_slot — capacity math
# ===========================================================================


@pytest.mark.parametrize(
    "cap, n_attempts, expected_admits",
    [
        pytest.param(1, 2, 1, id="r1-cap1-second-rejected"),
        pytest.param(3, 3, 3, id="r2-exactly-at-cap-all-admitted"),
        pytest.param(3, 4, 3, id="r3-cap-plus-one-rejected"),
        pytest.param(0, 2, 0, id="r4-cap0-never-admits"),
        pytest.param(-1, 2, 0, id="r5-negative-cap-never-admits"),
    ],
)
def test_acquire_admits_exactly_cap(cap, n_attempts, expected_admits):
    svc = _svc()
    results = [
        _run(svc.acquire_slot("a", f"e{i}", cap, timeout_seconds=900))
        for i in range(n_attempts)
    ]
    assert sum(results) == expected_admits
    assert results == [True] * expected_admits + [False] * (n_attempts - expected_admits)
    assert svc.redis.zcard(svc._slots_key("a")) == expected_admits


def test_r6_release_frees_exactly_one_seat():
    svc = _svc()
    assert _run(svc.acquire_slot("a", "e1", 1))
    assert not _run(svc.acquire_slot("a", "e2", 1))
    _run(svc.release_slot("a", "e1"))
    assert _run(svc.acquire_slot("a", "e2", 1))
    assert not _run(svc.acquire_slot("a", "e3", 1))


def test_r7_rejected_acquire_writes_nothing():
    svc = _svc()
    _run(svc.acquire_slot("a", "e1", 1))
    assert not _run(svc.acquire_slot("a", "e2", 1))
    assert svc.redis.zscore(svc._slots_key("a"), "e2") is None
    assert not svc.redis.exists(svc._metadata_key("a", "e2"))


def test_r8_metadata_shape_and_ttl():
    svc = _svc()
    _run(svc.acquire_slot("a", "e1", 3, message_preview="x" * 250, timeout_seconds=600))
    k = svc._metadata_key("a", "e1")
    md = svc.redis.hgetall(k)
    assert md["message_preview"] == "x" * 100
    assert md["timeout_seconds"] == "600"
    assert md["slot_number"] == "1"
    assert md["started_at"].endswith("Z")
    assert 600 + SLOT_TTL_BUFFER - 2 <= svc.redis.ttl(k) <= 600 + SLOT_TTL_BUFFER


@pytest.mark.parametrize("preview", [None, ""], ids=["r9-none", "r9-empty"])
def test_r9_empty_or_none_preview_stored_as_empty(preview):
    svc = _svc()
    assert _run(svc.acquire_slot("a", "e1", 1, message_preview=preview))
    assert svc.redis.hget(svc._metadata_key("a", "e1"), "message_preview") == ""


def test_r10_agents_are_isolated():
    svc = _svc()
    assert _run(svc.acquire_slot("a", "e1", 1))
    assert _run(svc.acquire_slot("b", "e1", 1))  # same id, other agent
    _run(svc.release_slot("a", "e1"))
    assert svc.redis.zscore(svc._slots_key("b"), "e1") is not None


def test_r11_reacquire_same_id_under_cap_does_not_double_count():
    """Re-acquiring a held id under the cap is idempotent: True, counted once."""
    svc = _svc()
    assert _run(svc.acquire_slot("a", "e1", 3))
    assert _run(svc.acquire_slot("a", "e1", 3))
    assert svc.redis.zcard(svc._slots_key("a")) == 1


def test_r12_acquire_reaps_stale_slot_before_counting(clock):
    """A stale slot must not hold capacity hostage: acquire runs the per-agent
    sweep first, so a slot past its own TTL frees the seat in the same call."""
    svc = _svc()
    _put_slot(svc, "a", "old", clock.t - (900 + SLOT_TTL_BUFFER) - 1, timeout=900)
    assert _run(svc.acquire_slot("a", "new", 1, timeout_seconds=900))
    assert svc.redis.zscore(svc._slots_key("a"), "old") is None


def test_r13_acquire_does_not_reap_live_long_slot_with_short_own_timeout(clock):
    """#869: the acquirer's timeout is only the FALLBACK; a slot with its own
    longer stored timeout keeps its seat."""
    svc = _svc()
    _put_slot(svc, "a", "long", clock.t - 4000, timeout=7200)
    assert not _run(svc.acquire_slot("a", "short", 1, timeout_seconds=60))
    assert svc.redis.zscore(svc._slots_key("a"), "long") is not None


def test_r14_concurrent_acquire_across_workers_never_overshoots_cap(monkeypatch):
    """#3318 / #3305: two backend workers (docker-compose.prod.yml --workers 2)
    admitting concurrently must not both pass a stale count<cap read. The other
    worker admits right after this worker's capacity read; WATCH invalidates
    that read, so the retry sees the cap as full."""
    server = fakeredis.FakeServer()
    worker_a = _svc(server)
    worker_b = _svc(server)

    # The capacity read happens on the WATCHed pipeline, so hook it there.
    pipeline_cls = type(worker_a.redis.pipeline())
    real_zcard = pipeline_cls.zcard
    fired = []

    def zcard_then_other_worker_admits(self, key):
        n = real_zcard(self, key)
        if not fired:
            fired.append(True)
            t = threading.Thread(
                target=lambda: asyncio.run(worker_b.acquire_slot("a", "eB", 1))
            )
            t.start()
            t.join()
        return n

    monkeypatch.setattr(pipeline_cls, "zcard", zcard_then_other_worker_admits)
    admitted_a = _run(worker_a.acquire_slot("a", "eA", 1))
    assert fired, "the interleaving hook never ran — the test would be vacuous"
    assert not admitted_a
    assert worker_b.redis.zrange("agent:slots:a", 0, -1) == ["eB"]


# ===========================================================================
# SlotService.release_slot — double release / replay / callbacks
# ===========================================================================


def _with_callbacks(svc, n=1, fail_first=False):
    calls = []

    def make(i):
        async def cb(agent):
            if fail_first and i == 0:
                raise RuntimeError("boom")
            calls.append((i, agent))
        return cb

    for i in range(n):
        svc.register_on_release(make(i))
    return calls


async def _release_and_settle(svc, agent, *eids):
    for e in eids:
        await svc.release_slot(agent, e)
    for _ in range(5):
        await asyncio.sleep(0)


def test_r15_double_release_drains_exactly_once():
    svc = _svc()
    calls = _with_callbacks(svc)

    async def go():
        await svc.acquire_slot("a", "e1", 1)
        await _release_and_settle(svc, "a", "e1", "e1")

    _run(go())
    assert calls == [(0, "a")]


def test_r16_release_of_never_held_id_fires_no_drain_and_frees_nothing():
    svc = _svc()
    calls = _with_callbacks(svc)

    async def go():
        await svc.acquire_slot("a", "held", 1)
        await _release_and_settle(svc, "a", "ghost")

    _run(go())
    assert calls == []
    assert svc.redis.zcard(svc._slots_key("a")) == 1


def test_r17_release_deletes_metadata_even_when_member_already_gone():
    """Release after a reap/restart: the ZSET member is gone but a metadata hash
    lingers — release still cleans it (and fires no drain)."""
    svc = _svc()
    calls = _with_callbacks(svc)
    k = svc._metadata_key("a", "e1")
    svc.redis.hset(k, "timeout_seconds", "900")

    async def go():
        await _release_and_settle(svc, "a", "e1")

    _run(go())
    assert not svc.redis.exists(k)
    assert calls == []


def test_r18_failing_callback_does_not_starve_siblings():
    svc = _svc()
    calls = _with_callbacks(svc, n=3, fail_first=True)

    async def go():
        await svc.acquire_slot("a", "e1", 1)
        await _release_and_settle(svc, "a", "e1")

    _run(go())
    assert sorted(i for i, _ in calls) == [1, 2]


# ===========================================================================
# SlotService._cleanup_stale_slots_for_agent — TTL arithmetic
# ===========================================================================


@pytest.mark.parametrize(
    "age_offset, reaped",
    [
        pytest.param(-1, False, id="r19-ttl-minus-1-kept"),
        pytest.param(0, False, id="r20-exactly-ttl-kept-strict-gt"),
        pytest.param(1, True, id="r21-ttl-plus-1-reaped"),
    ],
)
def test_cleanup_ttl_boundary(clock, age_offset, reaped):
    svc = _svc()
    ttl = 900 + SLOT_TTL_BUFFER
    _put_slot(svc, "a", "e1", clock.t - (ttl + age_offset), timeout=900)
    out = _run(svc._cleanup_stale_slots_for_agent("a"))
    assert (out == ["e1"]) is reaped
    assert (svc.redis.zscore(svc._slots_key("a"), "e1") is None) is reaped


@pytest.mark.parametrize(
    "stored, age, reaped",
    [
        # "0" is a truthy string → int("0") + buffer = 300s, NOT the fallback.
        pytest.param("0", 299, False, id="r22-stored-0-uses-buffer-only-kept"),
        pytest.param("0", 301, True, id="r22b-stored-0-reaped-after-buffer"),
        # "" is falsy → fallback (default 1200).
        pytest.param("", 1199, False, id="r23-empty-string-falls-back-kept"),
        pytest.param("", 1201, True, id="r23b-empty-string-falls-back-reaped"),
        pytest.param("12.5", 1199, False, id="r24-float-string-unparseable-fallback"),
    ],
)
def test_cleanup_stored_timeout_parsing(clock, stored, age, reaped):
    svc = _svc()
    svc.redis.zadd(svc._slots_key("a"), {"e1": clock.t - age})
    svc.redis.hset(svc._metadata_key("a", "e1"), "timeout_seconds", stored)
    out = _run(svc._cleanup_stale_slots_for_agent("a"))
    assert (out == ["e1"]) is reaped


@pytest.mark.parametrize(
    "default_ttl, age, reaped",
    [
        pytest.param(None, DEFAULT_SLOT_TTL_SECONDS - 1, False, id="r25-none-means-module-default"),
        pytest.param(None, DEFAULT_SLOT_TTL_SECONDS + 1, True, id="r25b-none-default-reaped"),
        # 0 is NOT None: an explicit 0 fallback reaps any metadata-less slot.
        pytest.param(0, 1, True, id="r26-explicit-zero-is-honoured"),
        pytest.param(5000, 4999, False, id="r27-explicit-fallback-kept"),
    ],
)
def test_cleanup_fallback_ttl_selection(clock, default_ttl, age, reaped):
    svc = _svc()
    svc.redis.zadd(svc._slots_key("a"), {"e1": clock.t - age})  # no metadata
    out = _run(svc._cleanup_stale_slots_for_agent("a", default_slot_ttl=default_ttl))
    assert (out == ["e1"]) is reaped


def test_r28_cleanup_future_score_is_never_reaped(clock):
    """Clock skew: a score in the future gives a negative age → kept."""
    svc = _svc()
    _put_slot(svc, "a", "e1", clock.t + 10_000, timeout=60)
    assert _run(svc._cleanup_stale_slots_for_agent("a")) == []


def test_r29_cleanup_deletes_metadata_of_reaped_only(clock):
    svc = _svc()
    _put_slot(svc, "a", "old", clock.t - 10_000, timeout=60)
    _put_slot(svc, "a", "live", clock.t - 10, timeout=60)
    assert _run(svc._cleanup_stale_slots_for_agent("a")) == ["old"]
    assert not svc.redis.exists(svc._metadata_key("a", "old"))
    assert svc.redis.exists(svc._metadata_key("a", "live"))


# ===========================================================================
# SlotService.cleanup_stale_slots — fleet sweep
# ===========================================================================


def test_r30_fleet_sweep_uses_per_agent_fallback_and_default(clock):
    svc = _svc()
    age = 2000  # > default 1200, < 3600+300
    svc.redis.zadd(svc._slots_key("custom"), {"c1": clock.t - age})
    svc.redis.zadd(svc._slots_key("plain"), {"p1": clock.t - age})
    out = _run(svc.cleanup_stale_slots(agent_timeouts={"custom": 3600}))
    assert out == {"plain": ["p1"]}


@pytest.mark.parametrize("timeouts", [None, {}], ids=["r31-none", "r31b-empty-dict"])
def test_r31_fleet_sweep_without_timeouts_uses_default(clock, timeouts):
    svc = _svc()
    svc.redis.zadd(svc._slots_key("x"), {"e": clock.t - (DEFAULT_SLOT_TTL_SECONDS + 1)})
    assert _run(svc.cleanup_stale_slots(agent_timeouts=timeouts)) == {"x": ["e"]}


def test_r32_fleet_sweep_pages_past_one_scan_batch(clock):
    """SCAN count=100: >100 agent keys must all be visited (cursor loop).

    Each key keeps one LIVE member so the sweep never deletes a key mid-SCAN:
    fakeredis's index-based cursor skips keys when earlier ones vanish, which
    real Redis's SCAN guarantee does not (a key present for the whole scan is
    always returned) — first run here reported 150/250, a harness artifact."""
    svc = _svc()
    for i in range(250):
        svc.redis.zadd(svc._slots_key(f"ag{i}"),
                       {"e": clock.t - 99_999, "live": clock.t})
    out = _run(svc.cleanup_stale_slots())
    assert len(out) == 250


def test_r33_fleet_sweep_ignores_metadata_hash_keys(clock):
    """`agent:slot:` (metadata) must not match the `agent:slots:*` pattern."""
    svc = _svc()
    svc.redis.hset("agent:slot:a:e1", "timeout_seconds", "60")
    assert _run(svc.cleanup_stale_slots()) == {}


# ===========================================================================
# SlotService meters — get_slot_state / get_all_slot_states / is_at_capacity
# ===========================================================================


def test_r34_available_never_negative_when_ceiling_lowered_below_active():
    svc = _svc()
    for i in range(4):
        _run(svc.acquire_slot("a", f"e{i}", 10))
    st = _run(svc.get_slot_state("a", 2))
    assert st.active_slots == 4
    assert st.available_slots == 0


def test_r35_slot_state_tolerates_missing_metadata(clock):
    svc = _svc()
    svc.redis.zadd(svc._slots_key("a"), {"e1": clock.t - 42})
    st = _run(svc.get_slot_state("a", 3))
    (s,) = st.slots
    assert (s.slot_number, s.started_at, s.message_preview) == (0, "", "")
    assert s.duration_seconds == 42


def test_r36_empty_agent_slot_state():
    st = _run(_svc().get_slot_state("nobody", 3))
    assert (st.active_slots, st.available_slots, st.slots) == (0, 3, [])


def test_r37_bulk_meter_maps_counts_to_the_right_agents():
    svc = _svc()
    _run(svc.acquire_slot("b", "e1", 5))
    _run(svc.acquire_slot("b", "e2", 5))
    _run(svc.acquire_slot("c", "e1", 5))
    out = _run(svc.get_all_slot_states({"a": 1, "b": 5, "c": 2}))
    assert out == {"a": {"max": 1, "active": 0},
                   "b": {"max": 5, "active": 2},
                   "c": {"max": 2, "active": 1}}


def test_r38_bulk_meter_empty_and_fail_open():
    svc = _svc()
    assert _run(svc.get_all_slot_states({})) == {}

    class _Broken:
        def pipeline(self, *a, **k):
            raise ConnectionError("down")

    svc.redis = _Broken()
    assert _run(svc.get_all_slot_states({"a": 3})) == {"a": {"max": 3, "active": 0}}


@pytest.mark.parametrize(
    "held, cap, expected",
    [
        pytest.param(2, 3, False, id="r39-below-cap"),
        pytest.param(3, 3, True, id="r40-exactly-cap"),
        pytest.param(0, 0, True, id="r41-cap0-always-full"),
    ],
)
def test_is_at_capacity_boundary(held, cap, expected):
    svc = _svc()
    for i in range(held):
        _run(svc.acquire_slot("a", f"e{i}", 99))
    assert _run(svc.is_at_capacity("a", cap)) is expected


def test_r42_force_clear_counts_clears_metadata_and_is_idempotent():
    svc = _svc()
    calls = _with_callbacks(svc)

    async def go():
        await svc.acquire_slot("a", "e1", 5)
        await svc.acquire_slot("a", "e2", 5)
        n1 = await svc.force_clear_slots("a")
        n2 = await svc.force_clear_slots("a")
        for _ in range(3):
            await asyncio.sleep(0)
        return n1, n2

    assert _run(go()) == (2, 0)
    assert not svc.redis.exists(svc._slots_key("a"))
    assert not svc.redis.exists(svc._metadata_key("a", "e1"))
    # Emergency clear bypasses the drain callback (orphan drain covers it).
    assert calls == []


def test_r43_renew_with_stored_zero_timeout_sets_buffer_ttl(clock):
    svc = _svc()
    _put_slot(svc, "a", "e1", clock.t - 100, timeout=0)
    assert svc.renew_slot("a", "e1") is True
    assert 0 < svc.redis.ttl(svc._metadata_key("a", "e1")) <= SLOT_TTL_BUFFER
    assert svc.redis.zcard(svc._slots_key("a")) == 1


# ===========================================================================
# CapacityManager — facade over a REAL SlotService
# ===========================================================================


@pytest.fixture
def cm(monkeypatch):
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        _CM.redis, "from_url",
        lambda *_a, **_k: fakeredis.FakeRedis(server=server, decode_responses=True),
    )
    # Ephemeral gate: not an ephemeral agent (fail-open path is separate).
    import database
    monkeypatch.setattr(database.db, "get_agent_ephemeral_info", lambda _n: None,
                        raising=False)
    slots = _svc(server)
    backlog = AsyncMock()
    backlog.enqueue = AsyncMock(return_value=True)
    backlog.drain_next = AsyncMock(return_value=False)
    mgr = _CM.CapacityManager(redis_url="redis://t", slot_service=slots,
                              backlog_service=backlog)
    mgr._test_backlog = backlog
    return mgr


def _acq(mgr, eid, cap=1, policy="reject", **kw):
    return mgr.acquire(agent_name="a", execution_id=eid, max_concurrent=cap,
                       overflow_policy=policy, **kw)


def test_r44_facade_cap_zero_rejects_with_capacity_full(cm):
    with pytest.raises(_CM.CapacityFull) as ei:
        _run(_acq(cm, "e1", cap=0))
    assert ei.value.reason == "rejected"


def test_r45_facade_clamps_cap_above_ceiling(cm, monkeypatch):
    monkeypatch.setattr(_REAL["services.settings_service"],
                        "get_max_parallel_tasks_ceiling", lambda: 2)
    assert _run(_acq(cm, "e1", cap=50)).state == "admitted"
    assert _run(_acq(cm, "e2", cap=50)).state == "admitted"
    with pytest.raises(_CM.CapacityFull):
        _run(_acq(cm, "e3", cap=50))


def test_r46_in_memory_overflow_positions_and_depth_bound(cm):
    assert _run(_acq(cm, "run", policy="queue_in_memory")).state == "admitted"
    positions = [
        _run(_acq(cm, f"q{i}", policy="queue_in_memory", message=f"m{i}")).queue_position
        for i in range(_CM.IN_MEMORY_DEPTH)
    ]
    assert positions == list(range(1, _CM.IN_MEMORY_DEPTH + 1))
    with pytest.raises(_CM.CapacityFull) as ei:
        _run(_acq(cm, "overflow", policy="queue_in_memory"))
    assert ei.value.reason == "in_memory_full"
    assert ei.value.depth == _CM.IN_MEMORY_DEPTH


def test_r47_in_memory_queue_is_fifo_through_release(cm):
    _run(_acq(cm, "run", policy="queue_in_memory"))
    for i in range(3):
        _run(_acq(cm, f"q{i}", policy="queue_in_memory"))
    st = _run(cm.get_status("a", 1))
    assert [e.id for e in st.queued_executions] == ["q0", "q1", "q2"]
    _run(cm.release("a", "run"))
    st = _run(cm.get_status("a", 1))
    assert [e.id for e in st.queued_executions] == ["q1", "q2"]


def test_r48_release_if_matches_non_holder_leaves_queue_and_slots(cm):
    _run(_acq(cm, "run", policy="queue_in_memory"))
    _run(_acq(cm, "q0", policy="queue_in_memory"))
    assert _run(cm.release_if_matches("a", "ghost")) is False
    st = _run(cm.get_status("a", 1))
    assert st.is_busy and st.queue_length == 1


def test_r49_release_if_matches_holder_frees_seat(cm):
    _run(_acq(cm, "run"))
    assert _run(cm.release_if_matches("a", "run")) is True
    assert _run(_acq(cm, "next")).state == "admitted"


def test_r50_facade_double_release_drains_backlog_once(cm):
    async def go():
        await _acq(cm, "run", cap=1)
        await cm.release("a", "run")
        await cm.release("a", "run")
        for _ in range(5):
            await asyncio.sleep(0)

    _run(go())
    assert cm._test_backlog.drain_next.await_count == 1


def test_r51_queue_persistent_without_payload_takes_no_slot_when_full(cm):
    _run(_acq(cm, "run"))
    with pytest.raises(ValueError):
        _run(_acq(cm, "e2", policy="queue_persistent"))
    assert cm._slots.redis.zcard("agent:slots:a") == 1
    cm._test_backlog.enqueue.assert_not_awaited()


def test_r52_unknown_policy_only_raises_on_overflow(cm):
    assert _run(_acq(cm, "e1", policy="bogus")).state == "admitted"
    with pytest.raises(ValueError, match="Unknown overflow_policy"):
        _run(_acq(cm, "e2", policy="bogus"))


@pytest.mark.parametrize(
    "setup, was_running, cleared",
    [
        pytest.param("none", False, 0, id="r53-idle"),
        pytest.param("queue_only", True, 0, id="r54-queued-only-counts-as-running"),
        pytest.param("slot", True, 1, id="r55-slot-held"),
    ],
)
def test_force_release_matrix(cm, setup, was_running, cleared):
    if setup == "queue_only":
        cm._redis.lpush(cm._mem_queue_key("a"), "{}")
    elif setup == "slot":
        _run(_acq(cm, "run"))
    res = _run(cm.force_release("a"))
    assert (res.was_running, res.slots_cleared) == (was_running, cleared)
    assert not cm._redis.exists(cm._mem_queue_key("a"))


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: get_status surfaces slots[0] after a sort by slot_number, but "
        "slot_number=ZCARD+1 at acquire (slot_service.py:146) is reused after "
        "out-of-order releases, so current_execution is not the oldest running "
        "slot (capacity_manager.py:514-516 promises 'the oldest active slot') — "
        "#3319"
    ),
)
def test_r56_get_status_current_execution_is_the_oldest_slot(cm, clock):
    for i, eid in enumerate(["A", "B", "C"]):
        clock.t = 1_000_000.0 + i
        _run(_acq(cm, eid, cap=3))
    _run(cm.release("a", "A"))
    _run(cm.release("a", "B"))
    clock.t = 1_000_010.0
    _run(_acq(cm, "D", cap=3))  # gets slot_number 2 < C's 3
    st = _run(cm.get_status("a", 3))
    assert st.current_execution.id == "C"


# ===========================================================================
# Pull-pilot physical-occupancy meter (#1081 Phase 3) + renew of a reaped id
# ===========================================================================


def test_r57_pilot_slot_state_adds_leased_and_refloors_available(cm, monkeypatch):
    import database
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "a")
    monkeypatch.setattr(database.db, "count_active_leased", lambda _n: 5, raising=False)
    _run(_acq(cm, "push1", cap=3))
    st = _run(cm.get_slot_state("a", 3))
    assert st.active_slots == 6
    assert st.available_slots == 0  # floored, never -3


def test_r58_pilot_bulk_meter_adds_leased_only_for_pilots(cm, monkeypatch):
    import database
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "a, ghost")
    monkeypatch.setattr(database.db, "count_active_leased_by_agent",
                        lambda names: {"a": 2}, raising=False)
    _run(_acq(cm, "push1", cap=3))
    out = _run(cm.get_all_states({"a": 3, "b": 3}))
    assert out == {"a": {"max": 3, "active": 3}, "b": {"max": 3, "active": 0}}


def test_r59_renew_refuses_reaped_member_even_if_hash_lingers(clock):
    """Hash present but ZSET member already reaped/released: ZADD XX must not
    resurrect the seat, and the stale hash TTL is left alone."""
    svc = _svc()
    k = svc._metadata_key("a", "e1")
    svc.redis.hset(k, "timeout_seconds", "900")
    svc.redis.expire(k, 30)
    assert svc.renew_slot("a", "e1") is False
    assert svc.redis.zcard(svc._slots_key("a")) == 0
    assert svc.redis.ttl(k) <= 30


# ---- mutation-gate killers (Stage 5b survivors #11, #12) --------------------


def test_r24b_unparseable_stored_timeout_uses_callers_fallback_not_module_default(clock):
    """A garbage `timeout_seconds` falls back to the CALLER's fallback
    (the acquirer's / the fleet sweep's per-agent TTL), not the 20-min default."""
    svc = _svc()
    svc.redis.zadd(svc._slots_key("a"), {"e1": clock.t - 2000})
    svc.redis.hset(svc._metadata_key("a", "e1"), "timeout_seconds", "abc")
    assert _run(svc._cleanup_stale_slots_for_agent("a", default_slot_ttl=5000)) == []


def test_r30b_fleet_sweep_fallback_includes_the_buffer(clock):
    """agent_timeouts value is an execution timeout; the slot fallback TTL is
    timeout + SLOT_TTL_BUFFER (#226), so a hash-less slot just past the bare
    timeout but inside the buffer is kept."""
    svc = _svc()
    svc.redis.zadd(svc._slots_key("x"), {"e": clock.t - (1000 + SLOT_TTL_BUFFER // 2)})
    assert _run(svc.cleanup_stale_slots(agent_timeouts={"x": 1000})) == {}
