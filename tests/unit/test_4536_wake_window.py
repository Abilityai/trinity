"""abilityai/trinity#4536 — an ending that collides with the 15 s wake window is
parked and delivered, never dropped.

The window (``caller_wake:{caller}:{bucket}``) is the storm bound: a cleanup
sweep that ends hundreds of rows must cost the delegator one turn. It used to
swallow every other ending in the window, which also swallowed the ending of a
run the wake turn itself had started: a fast callee (answers in under 15 s)
ended inside the window its own wake had claimed, and a delegate→wake loop
stalled silently after a few rounds.

Pinned here, against the real idempotency claim (one real DB per backend) and
an in-memory Redis:

* a finished wake releases its window, so the next ending wakes at once — the
  reported repro, twelve fast rounds, twelve wakes;
* an ending that lands while the wake still runs, and ended after that wake
  claimed the window, is parked and delivered once when the wake ends — from
  the wake row's own terminal, which every terminal writer reaches;
* an ending from before the claim is covered by that wake's
  ``list_recent_executions`` and still collapses: a 300-row sweep is one wake;
* the holder releasing between the collision and the park still delivers it;
* Redis trouble on the park does not raise, and the drop is audited;
* a holder cancelled by a backend stop, or closed with its loop, leaves the
  parked ending for the wake row's terminal; a follow-on key is released, so a
  second ending parked on the same wake is delivered; a drain wakes only the
  caller it parked for; a holder that raises or fails still delivers;
* a finished wake covers nothing after it: a recovered run with a back-dated
  ``completed_at``, a parked ending drained by a later window's wake, and
  a FAILED wake in an earlier window all still wake.

The only stubs are the dispatch (it writes the wake row a real dispatch would),
the container probe, the policy, and the spawn of a follow-on wake (collected,
then run, so the test owns the order).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.write_params import TaskExecutionFields  # noqa: E402
from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import services.caller_wake_service as _WAKE  # noqa: E402
from database import db  # noqa: E402

pytestmark = pytest.mark.unit

BOSS, WORKER = "zq4536-boss", "zq4536-worker"
MISSED = "trinity:wake_missed:"
# Mid-bucket, so nothing in a test crosses a window boundary by accident.
NOW = 1_800_000_000 - (1_800_000_000 % _WAKE.WAKE_BUCKET_SECONDS) + 5


@pytest.fixture
def world(db_backend, monkeypatch):
    wakes = []
    spawned = []
    audits = []
    gate = {"event": None}
    held = []

    async def _dispatch_wake(**kwargs):
        # What `dispatch_and_await_terminal` writes before the agent runs.
        row = db.create_task_execution(
            agent_name=kwargs["agent_name"],
            message=kwargs["message"],
            triggered_by=kwargs["triggered_by"],
            fields=TaskExecutionFields(chain_depth=kwargs.get("chain_depth")),
        )
        wakes.append({"id": row.id, "message": kwargs["message"]})
        ev = gate["event"]
        if ev is not None:
            held.append(ev)
            await ev.wait()                  # the wake turn is still running
        return SimpleNamespace(execution_id=row.id, status="completed", error=None)

    async def _state(_agent):
        return "running"

    async def _audit(**kw):
        audits.append(kw)

    def _policy(_agent):
        from services.wake_policy import WakeVerdict
        return WakeVerdict(enabled=True, source="instance")

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch_wake)
    monkeypatch.setattr(_WAKE, "_container_state", _state)
    monkeypatch.setattr(_WAKE, "_audit", _audit)
    monkeypatch.setattr(_WAKE, "_resolve_policy", _policy)
    monkeypatch.setattr(_WAKE, "_caller_subscribes_to", lambda *a: False)
    monkeypatch.setattr(_WAKE, "_spawn_followon", lambda coro: spawned.append(coro), raising=False)
    monkeypatch.setattr(_WAKE, "time", SimpleNamespace(time=lambda: NOW))
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: redis)
    return SimpleNamespace(wakes=wakes, spawned=spawned, audits=audits, gate=gate, held=held, redis=redis)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _callee_run() -> str:
    return db.create_task_execution(
        agent_name=WORKER, message="next number", triggered_by="agent",
        fields=TaskExecutionFields(source_agent_name=BOSS),
    ).id


def _end(execution_id: str, at: datetime | None = None) -> None:
    at = at or datetime.now(timezone.utc)
    _hrun(
        "UPDATE schedule_executions SET status='success', completed_at=:t WHERE id=:i",
        t=_iso(at), i=execution_id,
    )


async def _wake_turn_ends(world, wake_id: str) -> None:
    """The wake row's own terminal: what every CAS-won writer fans out."""
    _end(wake_id)
    await _WAKE.maybe_wake_caller(wake_id)
    while world.spawned:
        await world.spawned.pop(0)


def test_a_fast_delegate_wake_loop_gets_every_round(world):
    """The reported repro: each wake starts the next run, the run ends in the
    same window, twelve rounds must be twelve wakes."""
    async def loop():
        for _round in range(12):
            run = _callee_run()
            _end(run)
            woken = await _WAKE.maybe_wake_caller(run)
            assert woken, f"round {_round + 1}: the ending was dropped"
            await _wake_turn_ends(world, woken)

    asyncio.run(loop())
    assert len(world.wakes) == 12


def test_an_ending_while_the_wake_runs_is_delivered_when_it_ends(world):
    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)                   # the holder claims and is running
        assert len(world.wakes) == 1

        late = _callee_run()                     # started by the running wake turn
        _end(late)
        assert await _WAKE.maybe_wake_caller(late) is None   # parked, not dispatched
        assert [a["status"] for a in world.audits if a["status"] == "parked"] == ["parked"]

        world.gate["event"] = None
        holder_wake = world.wakes[0]["id"]
        await _wake_turn_ends(world, holder_wake)
        # Delivered from the wake row's own terminal, before the holder's
        # dispatch returns: a worker restarted mid-turn never returns at all.
        assert len(world.wakes) == 2
        await _finish(world, holder)
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2, "the parked ending must be delivered exactly once"
    assert late in world.wakes[1]["message"]


async def _finish(world, holder):
    """The holder's dispatch returns: its wake turn is over."""
    for ev in world.held:
        ev.set()
    return await holder


def test_an_ending_parked_after_the_wake_terminal_is_drained_by_the_holder(world):
    """The wake row's terminal fanned out (nothing parked yet), then an ending
    collides with the window the holder has not released yet. The holder's
    own drain, after it releases, delivers it."""
    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)
        world.gate["event"] = None
        await _wake_turn_ends(world, world.wakes[0]["id"])   # drains nothing
        late = _callee_run()
        _end(late)
        assert await _WAKE.maybe_wake_caller(late) is None   # parked
        await _finish(world, holder)
        while world.spawned:
            await world.spawned.pop(0)
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]


def test_a_sweep_that_ended_before_the_claim_is_one_wake(world):
    async def scenario():
        world.gate["event"] = asyncio.Event()
        rows = [_callee_run() for _ in range(300)]
        earlier = datetime.now(timezone.utc) - timedelta(seconds=2)
        for r in rows:
            _end(r, earlier)
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(rows[0]))
        await asyncio.sleep(0)
        for r in rows[1:]:
            assert await _WAKE.maybe_wake_caller(r) is None
        world.gate["event"] = None
        await _wake_turn_ends(world, world.wakes[0]["id"])
        await _finish(world, holder)

    asyncio.run(scenario())
    assert len(world.wakes) == 1
    assert world.redis.get(f"{MISSED}{BOSS}") is None


def test_endings_after_the_claim_collapse_into_one_follow_on(world):
    """Many fast callees ending while the wake runs: one follow-on, not one each."""
    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)
        for _ in range(20):
            r = _callee_run()
            _end(r)
            assert await _WAKE.maybe_wake_caller(r) is None
        world.gate["event"] = None
        await _wake_turn_ends(world, world.wakes[0]["id"])
        await _finish(world, holder)

    asyncio.run(scenario())
    assert len(world.wakes) == 2


def test_the_holder_releasing_before_the_park_still_delivers(world, monkeypatch):
    """The collision sees the window held; the holder ends and drains (nothing
    parked yet) before the marker lands. The ender must take it itself."""
    claims = []
    real_begin = _WAKE._begin_idempotency

    def _begin(scope, key):
        decision = real_begin(scope, key)
        claims.append(decision)
        return decision

    monkeypatch.setattr(_WAKE, "_begin_idempotency", _begin)
    real_set = world.redis.set

    def _set(name, value, *a, **k):
        if name.startswith(MISSED):
            # The holder's turn ends right here: it releases its window.
            from services import idempotency_service
            idempotency_service.fail(claims[0])
        return real_set(name, value, *a, **k)

    monkeypatch.setattr(world.redis, "set", _set)

    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)
        world.gate["event"] = None
        late = _callee_run()
        _end(late)
        await _WAKE.maybe_wake_caller(late)
        while world.spawned:
            await world.spawned.pop(0)
        await _finish(world, holder)
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]


def test_redis_down_on_the_park_is_audited_not_raised(world, monkeypatch):
    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)

        class _Down:
            def __getattr__(self, _name):
                raise ConnectionError("redis down")

        monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: _Down())
        late = _callee_run()
        _end(late)
        assert await _WAKE.maybe_wake_caller(late) is None
        world.gate["event"] = None
        await _finish(world, holder)

    asyncio.run(scenario())
    assert "skipped_window" in [a["status"] for a in world.audits]


# --- review follow-ups on PR #4545 ------------------------------------------

_REAL_SPAWN = _WAKE._spawn_followon


async def _hold_first_wake(world):
    """A delegated run ends and its wake claims the window and keeps running."""
    world.gate["event"] = asyncio.Event()
    first = _callee_run()
    _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
    holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
    await asyncio.sleep(0)
    assert len(world.wakes) == 1
    return holder


async def _park_one(world) -> str:
    late = _callee_run()
    _end(late)
    assert await _WAKE.maybe_wake_caller(late) is None
    assert world.redis.get(f"{MISSED}{BOSS}") == late
    return late


def test_a_cancelled_holder_leaves_the_parked_ending_for_the_wake_terminal(world):
    """A clean backend stop cancels the holder mid-wake. A follow-on spawned
    on the closing loop never runs, so the holder must leave the marker for
    the wake row's terminal (written after the restart) to deliver."""
    async def scenario():
        holder = await _hold_first_wake(world)
        late = await _park_one(world)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        for coro in world.spawned:          # the loop closes: nothing spawned runs
            coro.close()
        world.spawned.clear()
        assert world.redis.get(f"{MISSED}{BOSS}") == late, "the stop consumed the parked ending"

        world.gate["event"] = None
        await _wake_turn_ends(world, world.wakes[0]["id"])
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]


def test_a_second_ending_parked_after_the_follow_on_finished_is_delivered(world):
    """X parks and is delivered from the wake row's terminal; that follow-on
    finishes; Y parks on the window the holder still holds. The holder's own
    drain must wake for Y."""
    async def scenario():
        holder = await _hold_first_wake(world)
        world.gate["event"] = None                     # later wakes run straight through
        await _park_one(world)
        await _wake_turn_ends(world, world.wakes[0]["id"])
        assert len(world.wakes) == 2                   # X delivered, its wake finished
        y = await _park_one(world)
        await _finish(world, holder)
        while world.spawned:
            await world.spawned.pop(0)
        return y

    y = asyncio.run(scenario())
    assert len(world.wakes) == 3, "Y was dropped on the follow-on key"
    assert y in world.wakes[2]["message"]


def test_a_drain_wakes_only_the_caller_it_was_parked_for(world):
    other = db.create_task_execution(
        agent_name=WORKER, message="x", triggered_by="agent",
        fields=TaskExecutionFields(source_agent_name="zq4536-other"),
    ).id
    _end(other)
    woken = asyncio.run(_WAKE.maybe_wake_caller(
        other, _key=_WAKE._follow_on_key(BOSS, "w"), _expect_caller=BOSS))
    assert woken is None
    assert world.wakes == []


def test_the_real_spawn_runs_the_follow_on_to_delivery(world, monkeypatch):
    monkeypatch.setattr(_WAKE, "_spawn_followon", _REAL_SPAWN)

    async def scenario():
        holder = await _hold_first_wake(world)
        late = await _park_one(world)
        world.gate["event"] = None
        await _finish(world, holder)
        assert _WAKE._FOLLOW_ONS, "the drain spawned no follow-on task"
        await asyncio.gather(*list(_WAKE._FOLLOW_ONS))
        await asyncio.sleep(0)
        assert not _WAKE._FOLLOW_ONS, "a finished follow-on is still referenced"
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]


@pytest.mark.parametrize("outcome", ["raises", "failed"])
def test_a_holder_that_fails_still_delivers_what_was_parked(world, monkeypatch, outcome):
    fixture_dispatch = _WAKE._dispatch_wake
    calls = []

    async def _dispatch(**kwargs):
        calls.append(1)
        result = await fixture_dispatch(**kwargs)
        if len(calls) == 1:
            if outcome == "raises":
                raise RuntimeError("dispatch blew up")
            return SimpleNamespace(execution_id=result.execution_id, status="failed",
                                   error="boom")
        return result

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch)

    async def scenario():
        holder = await _hold_first_wake(world)
        late = await _park_one(world)
        world.gate["event"] = None
        await _finish(world, holder)
        while world.spawned:
            await world.spawned.pop(0)
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]
    statuses = [a["status"] for a in world.audits]
    assert statuses == ["parked", "dispatch_error" if outcome == "raises" else "failed",
                        "completed"]


def _set_clock(monkeypatch, t):
    monkeypatch.setattr(_WAKE, "time", SimpleNamespace(time=lambda: t))


def test_a_recovered_run_back_dated_before_a_returned_wake_is_delivered(world, monkeypatch):
    """A retained result is written terminal minutes late with the agent's own
    finish instant as ``completed_at``. A wake for another run that ran and
    returned in between listed this run as running, so it must still wake."""
    async def scenario():
        t0 = datetime.now(timezone.utc)
        slow = _callee_run()
        other = _callee_run()
        _end(other)
        w = await _WAKE.maybe_wake_caller(other)
        assert w
        await _wake_turn_ends(world, w)
        _set_clock(monkeypatch, NOW + 30)
        _end(slow, t0 - timedelta(seconds=300))
        await _WAKE.maybe_wake_caller(slow)
        while world.spawned:
            await world.spawned.pop(0)
        return slow

    slow = asyncio.run(scenario())
    assert any(slow in w["message"] for w in world.wakes), "the recovered ending was dropped"


@pytest.mark.parametrize("other_wake", ["failed", "completed"])
def test_a_parked_ending_survives_another_wake_in_a_later_window(world, monkeypatch, other_wake):
    """Y parks on H's running wake. In the next window Z ends and its wake
    (failed before the agent ran, or completed) drains Y into a follow-on:
    no wake that ran after Y ended has listed it, so Y must be delivered."""
    fixture_dispatch = _WAKE._dispatch_wake
    calls = []

    async def _dispatch(**kw):
        calls.append(kw)
        if len(calls) == 2 and other_wake == "failed":
            return SimpleNamespace(execution_id=None, status="failed", error="Agent at capacity")
        return await fixture_dispatch(**kw)

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch)

    async def scenario():
        holder = await _hold_first_wake(world)
        y = await _park_one(world)
        world.gate["event"] = None
        _set_clock(monkeypatch, NOW + 15)
        z = _callee_run()
        _end(z)
        zw = await _WAKE.maybe_wake_caller(z)
        while world.spawned:
            await world.spawned.pop(0)
        if zw:
            await _wake_turn_ends(world, zw)
        await _wake_turn_ends(world, world.wakes[0]["id"])
        await _finish(world, holder)
        while world.spawned:
            await world.spawned.pop(0)
        return y

    y = asyncio.run(scenario())
    assert any(y in w["message"].splitlines()[0] for w in world.wakes), "the parked ending was dropped"


def test_a_holder_closed_mid_wake_leaves_the_parked_ending(world):
    """A holder coroutine closed while it awaits (GeneratorExit: a pending task
    destroyed with its loop) must not drain into a loop that is going away."""
    async def scenario():
        world.gate["event"] = asyncio.Event()
        first = _callee_run()
        _end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = _WAKE.maybe_wake_caller(first)
        holder.send(None)                        # runs until the wake turn awaits
        assert len(world.wakes) == 1
        late = await _park_one(world)
        holder.close()
        return late

    late = asyncio.run(scenario())
    assert world.redis.get(f"{MISSED}{BOSS}") == late, "a closed holder consumed the parked ending"
    assert world.spawned == []


def test_a_holder_that_raises_past_the_dispatch_still_delivers(world, monkeypatch):
    """An exception out of the claimed wake itself (past the dispatch's own
    handler) drains on its way out."""
    real = _WAKE._claimed_wake

    async def _boom(*a, **kw):
        await real(*a, **kw)
        raise RuntimeError("after the dispatch")

    monkeypatch.setattr(_WAKE, "_claimed_wake", _boom)

    async def scenario():
        holder = await _hold_first_wake(world)
        late = await _park_one(world)
        monkeypatch.setattr(_WAKE, "_claimed_wake", real)
        world.gate["event"] = None
        with pytest.raises(RuntimeError):
            await _finish(world, holder)
        while world.spawned:
            await world.spawned.pop(0)
        return late

    late = asyncio.run(scenario())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"]


def test_a_failed_wake_does_not_cover_a_later_window(world, monkeypatch):
    """Every wake comes back FAILED before the agent runs (no free slot). No
    turn listed anything, so each later window tries again."""
    attempts = []

    async def _capacity(**kw):
        attempts.append(1)
        return SimpleNamespace(execution_id=None, status="failed", error="Agent at capacity")

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _capacity)

    async def scenario():
        rows = [_callee_run() for _ in range(3)]
        earlier = datetime.now(timezone.utc) - timedelta(seconds=2)
        for r in rows:
            _end(r, earlier)
        for i, r in enumerate(rows):
            _set_clock(monkeypatch, NOW + 15 * i)
            await _WAKE.maybe_wake_caller(r)
            while world.spawned:
                await world.spawned.pop(0)

    asyncio.run(scenario())
    assert len(attempts) == 3
