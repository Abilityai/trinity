"""#2433 — the backend agent-call limiter's in-flight dispatch registry.

The semaphore wait in ``acquire_agent_call_slot`` is a queue nothing else
could see: the row was ``running`` and its slot held, but the agent had never
heard of the execution, so the cleanup watchdog orphaned it after 60s.

Pinned here:
- ``track_inflight_dispatch`` registers an entry for the WHOLE call and
  unregisters in ``finally``; ``execution_id=None`` is a no-op
- the refresher writes one marker per live entry per tick in ONE pipeline
  (fakeredis), with the 60s TTL, only for entries older than the grace, and
  renews the capacity-slot lease for PARKED entries; deletes are flushed by
  the refresher (sole writer/deleter); a fast acquire never touches Redis
- Redis None / raising never raises into the caller; a failure is
  negative-cached and logged once per episode
- ``inflight_verdicts``: alive (in-process) / alive (marker) / absent /
  unknown (an established client raised) / absent (no client at all)
- an entry past its deadline is invisible (no immortal running row)
- ``acquire_agent_call_slot``: phase parked→calling; a cancel flagged while
  parked raises ``BackendAgentCallCancelled`` at grant (a subclass of
  ``BackendAgentCallBudgetExhausted``); a cross-worker cancel key is honoured;
  ``on_granted`` fires only when the park reached the re-stamp threshold and
  receives the parked seconds; a raising ``on_granted`` never blocks dispatch
- the >5s queue-wait warning fires on the DEFAULT (timeout > 0) branch
- ``_reset_for_testing`` clears the registry and installs the seams

Module under test: src/backend/services/agent_call_limiter.py
Harness mirrors tests/unit/test_904_agent_call_limiter.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis
import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_STUBBED_MODULE_NAMES = ["database", "db_models"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    _database_mod = sys.modules.get("database")
    _saved_db = getattr(_database_mod, "db", None) if _database_mod is not None else None
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if _database_mod is not None and _saved_db is not None:
            _database_mod.db = _saved_db


def _install_db_stub() -> None:
    db_stub = sys.modules.get("database")
    if db_stub is None:
        db_stub = type(sys)("database")
        sys.modules["database"] = db_stub

    class _Db:
        def get_max_parallel_tasks(self, agent_name: str) -> int:
            raise KeyError(agent_name)

    db_stub.db = _Db()


def _limiter_mod():
    _install_db_stub()
    if "services.agent_call_limiter" in sys.modules:
        return sys.modules["services.agent_call_limiter"]
    import importlib
    return importlib.import_module("services.agent_call_limiter")


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def limiter(fake_redis, monkeypatch):
    mod = _limiter_mod()
    renewals: list = []
    mod._reset_for_testing(
        global_limit=100, queue_timeout_s=10.0,
        client_factory=lambda: fake_redis,
        slot_renewer=lambda agent, eid: renewals.append((agent, eid)) or True,
    )
    monkeypatch.setattr(mod, "INFLIGHT_TICK_SECONDS", 0.01)
    monkeypatch.setattr(mod, "INFLIGHT_MARKER_GRACE_SECONDS", 0.0)
    mod._test_renewals = renewals  # type: ignore[attr-defined]
    yield mod
    mod._reset_for_testing()


async def _settle(mod, ticks: int = 3):
    """Let the refresher run a few ticks (tick is 10ms under the fixture)."""
    for _ in range(ticks):
        await asyncio.sleep(0.03)


# ---------------------------------------------------------------------------
# registry lifecycle + refresher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_track_inflight_registers_for_the_whole_call_and_unregisters(limiter, fake_redis):
    async with limiter.track_inflight_dispatch("exec-1", "agent-a", http_timeout=120) as entry:
        assert entry is not None and entry.execution_id == "exec-1"
        assert limiter.inflight_entry("exec-1") is entry
        await _settle(limiter)
        raw = fake_redis.get("execution:inflight:exec-1")
        assert raw is not None
        payload = json.loads(raw)
        assert payload["agent"] == "agent-a"
        assert payload["phase"] == "parked"
        assert 0 < fake_redis.ttl("execution:inflight:exec-1") <= limiter.INFLIGHT_MARKER_TTL_SECONDS
    # Unregistered on exit; the refresher flushes the delete.
    assert limiter.inflight_entry("exec-1") is None
    await _settle(limiter)
    assert fake_redis.get("execution:inflight:exec-1") is None


@pytest.mark.asyncio
async def test_track_inflight_without_execution_id_is_a_noop(limiter, fake_redis):
    async with limiter.track_inflight_dispatch(None, "agent-a") as entry:
        assert entry is None
    assert not limiter._INFLIGHT
    assert fake_redis.keys("execution:inflight:*") == []


@pytest.mark.asyncio
async def test_fast_acquire_never_touches_redis(limiter, fake_redis, monkeypatch):
    monkeypatch.setattr(limiter, "INFLIGHT_MARKER_GRACE_SECONDS", 5.0)
    async with limiter.track_inflight_dispatch("exec-fast", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-fast"):
            await _settle(limiter)
            assert fake_redis.keys("execution:inflight:*") == []


@pytest.mark.asyncio
async def test_refresher_renews_slot_lease_only_while_parked(limiter, fake_redis):
    async with limiter.track_inflight_dispatch("exec-p", "agent-a", http_timeout=60) as entry:
        entry.phase = "parked"
        await _settle(limiter)
        assert ("agent-a", "exec-p") in limiter._test_renewals
        limiter._test_renewals.clear()
        entry.phase = "calling"
        await _settle(limiter)
        assert ("agent-a", "exec-p") not in limiter._test_renewals


@pytest.mark.asyncio
async def test_unregister_queues_delete_even_when_registry_is_empty(limiter, fake_redis):
    async with limiter.track_inflight_dispatch("exec-d", "agent-a", http_timeout=60):
        await _settle(limiter)
        assert fake_redis.get("execution:inflight:exec-d") is not None
    assert not limiter._INFLIGHT
    await _settle(limiter)
    assert fake_redis.get("execution:inflight:exec-d") is None


@pytest.mark.asyncio
async def test_entry_past_deadline_is_invisible(limiter):
    entry = limiter.register_inflight("exec-old", "agent-a", http_timeout=60)
    entry.deadline = 0.0
    assert limiter.inflight_entry("exec-old") is None
    assert (await limiter.inflight_verdicts(["exec-old"]))["exec-old"] == "absent"
    limiter.unregister_inflight("exec-old")


def test_deadline_bounds_queue_wait_plus_call_timeout(limiter):
    entry = limiter.register_inflight("exec-b", "agent-a", http_timeout=600)
    import time as _t
    expected_min = _t.monotonic() + 10.0 + 600 + limiter.INFLIGHT_DEADLINE_SLACK_SECONDS - 1
    assert entry.deadline >= expected_min
    limiter.unregister_inflight("exec-b")


# ---------------------------------------------------------------------------
# Redis failure modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_none_never_raises_and_markers_are_off(limiter, caplog):
    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: None)
    with caplog.at_level(logging.WARNING, logger=limiter.logger.name):
        async with limiter.track_inflight_dispatch("exec-n", "agent-a", http_timeout=60):
            await _settle(limiter)
    assert limiter._INFLIGHT == {}


@pytest.mark.asyncio
async def test_redis_raise_is_negative_cached_and_logged_once(limiter, caplog):
    calls = {"n": 0}

    class _Boom:
        def pipeline(self, transaction=False):
            calls["n"] += 1
            raise ConnectionError("redis down")

    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: _Boom())
    with caplog.at_level(logging.WARNING, logger=limiter.logger.name):
        async with limiter.track_inflight_dispatch("exec-r", "agent-a", http_timeout=60):
            await _settle(limiter, ticks=6)
    assert calls["n"] == 1, "a failing client must be negative-cached, not re-hit every tick"
    warnings = [r for r in caplog.records if "Redis write failed" in r.getMessage()]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verdicts_alive_absent_and_marker(limiter, fake_redis):
    limiter.register_inflight("exec-local", "agent-a", http_timeout=60)
    fake_redis.set("execution:inflight:exec-remote", json.dumps({"agent": "agent-b", "phase": "parked"}), ex=60)
    verdicts = await limiter.inflight_verdicts(["exec-local", "exec-remote", "exec-gone", ""])
    assert verdicts == {"exec-local": "alive", "exec-remote": "alive", "exec-gone": "absent"}
    limiter.unregister_inflight("exec-local")


@pytest.mark.asyncio
async def test_verdicts_unknown_when_established_client_raises(limiter):
    class _Flaky:
        def mget(self, keys):
            raise TimeoutError("slow redis")

    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: _Flaky())
    verdicts = await limiter.inflight_verdicts(["exec-x", "exec-y"])
    assert verdicts == {"exec-x": "unknown", "exec-y": "unknown"}


@pytest.mark.asyncio
async def test_verdicts_absent_when_no_client_at_all(limiter):
    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: None)
    verdicts = await limiter.inflight_verdicts(["exec-x"])
    assert verdicts == {"exec-x": "absent"}


@pytest.mark.asyncio
async def test_verdict_guards_against_non_string_marker_values(limiter):
    """A MagicMock-shaped client (the sys.modules leak class) must not read as
    'alive' for everything."""
    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: MagicMock())
    verdicts = await limiter.inflight_verdicts(["exec-x"])
    assert verdicts == {"exec-x": "absent"}


# ---------------------------------------------------------------------------
# acquire: phases, cancel, on_granted, warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acquire_flips_phase_and_fires_on_granted_after_a_park(limiter, monkeypatch):
    monkeypatch.setattr(limiter, "DISPATCH_RESTAMP_THRESHOLD_SECONDS", 0.05)
    granted: list = []

    async def on_granted(parked_s: float):
        granted.append(parked_s)

    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]  # per-agent cap is 3
    await asyncio.sleep(0.02)

    async with limiter.track_inflight_dispatch("exec-w", "agent-a", http_timeout=60) as entry:
        async def waiter():
            async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-w", on_granted=on_granted):
                return entry.phase

        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)
        assert entry.phase == "parked" and entry.parked_since is not None
        release.set()
        phase_inside = await w
    assert phase_inside == "calling"
    assert len(granted) == 1 and granted[0] >= 0.05
    await asyncio.gather(*holders)


@pytest.mark.asyncio
async def test_on_granted_not_fired_for_a_short_wait(limiter):
    granted: list = []

    async def on_granted(parked_s: float):
        granted.append(parked_s)

    async with limiter.track_inflight_dispatch("exec-s", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-s", on_granted=on_granted):
            pass
    assert granted == []


@pytest.mark.asyncio
async def test_raising_on_granted_never_blocks_dispatch(limiter, monkeypatch):
    monkeypatch.setattr(limiter, "DISPATCH_RESTAMP_THRESHOLD_SECONDS", 0.0)

    async def on_granted(parked_s: float):
        raise RuntimeError("bookkeeping broke")

    async with limiter.track_inflight_dispatch("exec-g", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-g", on_granted=on_granted):
            entered = True
    assert entered


@pytest.mark.asyncio
async def test_cancel_while_parked_raises_cancelled_at_grant(limiter):
    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]
    await asyncio.sleep(0.02)

    async with limiter.track_inflight_dispatch("exec-c", "agent-a", http_timeout=60):
        async def waiter():
            async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-c"):
                return "dispatched"

        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert limiter.cancel_inflight("exec-c") == "parked"
        release.set()
        with pytest.raises(limiter.BackendAgentCallCancelled) as ei:
            await w
    assert isinstance(ei.value, limiter.BackendAgentCallBudgetExhausted)
    assert "cancelled while queued" in str(ei.value)
    await asyncio.gather(*holders)
    # The slots were released by the cancelled acquire — a fresh acquire works.
    async with limiter.acquire_agent_call_slot("agent-a"):
        pass


@pytest.mark.asyncio
async def test_cross_worker_cancel_key_is_honoured_at_grant(limiter, fake_redis, monkeypatch):
    monkeypatch.setattr(limiter, "INFLIGHT_MARKER_GRACE_SECONDS", 0.0)
    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]
    await asyncio.sleep(0.02)

    async with limiter.track_inflight_dispatch("exec-x", "agent-a", http_timeout=60):
        async def waiter():
            async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-x"):
                return "dispatched"

        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        # Another worker: sees the marker, sets the cancel key.
        assert await limiter.request_cross_worker_cancel("exec-x") == "parked"
        assert fake_redis.get("execution:cancel:exec-x") == "1"
        release.set()
        with pytest.raises(limiter.BackendAgentCallCancelled):
            await w
    await asyncio.gather(*holders)


@pytest.mark.asyncio
async def test_cross_worker_cancel_returns_none_without_marker(limiter, fake_redis):
    assert await limiter.request_cross_worker_cancel("exec-nobody") is None
    assert fake_redis.get("execution:cancel:exec-nobody") is None


@pytest.mark.asyncio
async def test_cancel_inflight_returns_none_for_unknown(limiter):
    assert limiter.cancel_inflight("exec-unknown") is None


@pytest.mark.asyncio
async def test_queue_wait_warning_fires_on_default_timeout_branch(limiter, caplog, monkeypatch):
    """Before #2433 the >5s warning existed only when the queue timeout was
    disabled — the default configuration parked calls in silence."""
    async def _fast_warn(agent_name, where, agent_cap, global_cap, t0):
        try:
            await asyncio.sleep(0.02)
            limiter.logger.warning("[TaskExecService] Agent-call queue wait > 5s (%s) for %s", where, agent_name)
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(limiter, "_log_long_queue_wait", _fast_warn)
    assert limiter.BACKEND_AGENT_CALL_QUEUE_TIMEOUT_S > 0
    release = asyncio.Event()

    async def hold():
        async with limiter.acquire_agent_call_slot("agent-a"):
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]
    await asyncio.sleep(0.02)
    with caplog.at_level(logging.WARNING, logger=limiter.logger.name):
        async def waiter():
            async with limiter.acquire_agent_call_slot("agent-a"):
                pass

        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.1)
        release.set()
        await w
    await asyncio.gather(*holders)
    assert any("queue wait > 5s" in r.getMessage() for r in caplog.records)


def test_docstring_default_matches_code(limiter):
    src = Path(limiter.__file__).read_text()
    assert "default 3600" in src.split('"""')[1], "module docstring must state the real default"
    assert "default 30\n" not in src.split('"""')[1]


@pytest.mark.asyncio
async def test_watchdog_read_bypasses_the_negative_cache(limiter):
    """The refresher's 30s negative cache must not turn a flapping Redis into
    an `absent` verdict for the sweep that lands inside the window: the
    watchdog read asks for real and maps a raise to `unknown`."""
    calls = {"n": 0}

    class _Flaky:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis down")

        def mget(self, keys):
            calls["n"] += 1
            raise TimeoutError("still down")

    limiter._reset_for_testing(global_limit=100, queue_timeout_s=10.0, client_factory=lambda: _Flaky())
    # Arm the negative cache the way the refresher would.
    limiter._tick_sync([limiter.register_inflight("exec-x", "agent-a", http_timeout=60)], [])
    limiter.unregister_inflight("exec-x")
    assert limiter._get_client() is None, "negative cache armed"
    verdicts = await limiter.inflight_verdicts(["exec-y"])
    assert calls["n"] == 1, "the watchdog read must ask Redis despite the negative cache"
    assert verdicts == {"exec-y": "unknown"}


# ---------------------------------------------------------------------------
# agent-scoped cancel (review finding: a cancel is authorised on an AGENT, never
# on a bare execution id)
# ---------------------------------------------------------------------------

def test_cancel_inflight_is_agent_scoped(limiter):
    entry = limiter.register_inflight("exec-a", "agent-a", http_timeout=60)
    assert limiter.cancel_inflight("exec-a", agent_name="agent-b") is None
    assert entry.cancel_requested is False, "a foreign agent's cancel must not flag the entry"
    assert limiter.cancel_inflight("exec-a", agent_name="agent-a") == "parked"
    assert entry.cancel_requested is True
    limiter.unregister_inflight("exec-a")


@pytest.mark.asyncio
async def test_cross_worker_cancel_is_agent_scoped(limiter, fake_redis):
    fake_redis.set("execution:inflight:exec-r", json.dumps({"agent": "agent-a", "phase": "parked"}), ex=60)
    assert await limiter.request_cross_worker_cancel("exec-r", agent_name="agent-b") is None
    assert fake_redis.get("execution:cancel:exec-r") is None, "no cancel key for a foreign agent"
    assert await limiter.request_cross_worker_cancel("exec-r", agent_name="agent-a") == "parked"
    assert fake_redis.get("execution:cancel:exec-r") == "1"


@pytest.mark.asyncio
async def test_cross_worker_cancel_refuses_marker_without_agent(limiter, fake_redis):
    fake_redis.set("execution:inflight:exec-m", json.dumps({"phase": "parked"}), ex=60)
    assert await limiter.request_cross_worker_cancel("exec-m", agent_name="agent-a") is None
    assert fake_redis.get("execution:cancel:exec-m") is None
