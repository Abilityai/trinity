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
* Redis trouble on the park does not raise, and the drop is audited.

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
