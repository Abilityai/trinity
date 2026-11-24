"""abilityai/trinity#4538 — the loose ends behind a parked caller wake.

A wake that finds its caller at capacity is parked (#4733) and delivered when a
slot frees. Five ways that delivery could be missed or could spin, each pinned
against the real code:

* F1 — ``release_admission`` (a ``/chat`` whose setup raised) releases, then
  drains;
* F2 — the ended run's slot is still on the meter when its terminal drains the
  parked wake: admission refuses, and the ending must stay parked for the slot
  release, not end as a FAILED wake row;
* F3 — a meter that cannot be read must not turn park → drain → refused → park
  into a loop of FAILED wake rows;
* F4 — a follow-on cancelled before it claimed the hold must leave the marker;
* F5 — a terminate (an agent's running turn, and a turn parked in the backend
  queue) releases a slot without the terminal fan-out, and must drain too.

F2 and F5 are closed at the one place every slot release passes: the
``SlotService`` release callback ``CapacityManager`` already registers for the
backlog drain. So these tests release through a real ``CapacityManager`` over a
real ``SlotService`` (in-memory Redis).

Related flow: docs/memory/feature-flows/agent-to-agent-collaboration.md
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_4536_wake_window as T  # noqa: E402 — the #4536 harness
import test_4733_wake_hold as H  # noqa: E402 — the #4733 capacity fixtures
from test_4536_wake_window import world  # noqa: E402,F401
from test_4733_wake_hold import full, meter  # noqa: E402,F401
from db_harness import db_backend, count as _hcount  # noqa: E402,F401

import services.capacity_manager as _CM  # noqa: E402
import services.chat_execution_service as _CE  # noqa: E402
from database import db  # noqa: E402
from services.slot_service import SlotService  # noqa: E402

_WAKE = T._WAKE
BOSS, WORKER, MISSED = T.BOSS, T.WORKER, T.MISSED
pytestmark = pytest.mark.unit


async def _settle(world, rounds: int = 20) -> None:
    """Let release callbacks and follow-on wakes run to quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)
        while world.spawned:
            await world.spawned.pop(0)


@pytest.fixture
def slots(world, monkeypatch):
    """A real ``CapacityManager`` over a real ``SlotService``, as the process
    builds it: the slot-release callback it registers is the one under test."""
    redis = fakeredis.FakeRedis(decode_responses=True)
    slot_service = SlotService("redis://unused")
    slot_service.redis = redis
    backlog = MagicMock(drain_next=AsyncMock(return_value=None))
    manager = _CM.CapacityManager(
        redis_url="redis://unused", slot_service=slot_service, backlog_service=backlog)
    manager._redis = redis
    monkeypatch.setattr(_CM, "get_capacity_manager", lambda: manager)
    monkeypatch.setattr(_CE, "get_capacity_manager", lambda: manager)

    async def hold(agent: str, execution_id: str) -> None:
        assert await slot_service.acquire_slot(agent, execution_id, 3)

    return SimpleNamespace(manager=manager, backlog=backlog, redis=redis, hold=hold)


def _wake_rows(status: str | None = None) -> int:
    where = "agent_name=:a AND triggered_by='caller_wake'"
    if status:
        return _hcount("schedule_executions", where + " AND status=:s", a=BOSS, s=status)
    return _hcount("schedule_executions", where, a=BOSS)


async def _park_behind(world, busy: str) -> str:
    """An ending of a run BOSS delegated, parked because BOSS is at capacity."""
    ending = H._one_ending()
    assert await _WAKE.maybe_wake_caller(ending) is None
    assert world.redis.get(MISSED + BOSS) == ending
    return ending


# ---------------------------------------------------------------------------
# F1 — release_admission: release, then drain
# ---------------------------------------------------------------------------

def test_release_admission_drains_after_it_releases_the_slot(monkeypatch):
    """A ``/chat`` whose setup raised never reaches ``run_chat_turn``'s
    ``finally``; this is where its slot is given back."""
    order = []
    monkeypatch.setattr(_CE.caller_wake_service, "drain_after_slot_release",
                        lambda name: order.append(("drain", name)))
    monkeypatch.setattr(_CE, "idempotency_service", MagicMock())
    capacity = MagicMock()
    capacity.release = AsyncMock(side_effect=lambda name, eid: order.append(("release", name)))

    asyncio.run(_CE.release_admission("agent1", execution_id="e1", capacity=capacity, idem="I"))
    assert order == [("release", "agent1"), ("drain", "agent1")]


def test_release_admission_does_not_drain_for_a_slot_it_could_not_release(monkeypatch):
    drained = []
    monkeypatch.setattr(_CE.caller_wake_service, "drain_after_slot_release", drained.append)
    monkeypatch.setattr(_CE, "idempotency_service", MagicMock())
    capacity = MagicMock(release=AsyncMock(side_effect=RuntimeError("redis down")))

    asyncio.run(_CE.release_admission("agent1", execution_id="e1", capacity=capacity, idem="I"))
    asyncio.run(_CE.release_admission("agent1", execution_id="e1", capacity=None, idem="I"))
    assert drained == []


# ---------------------------------------------------------------------------
# the slot release drains a parked wake (F2, F5)
# ---------------------------------------------------------------------------

def test_any_slot_release_delivers_a_wake_parked_for_a_free_slot(world, full, slots):
    """The one place every release passes, whoever released and however the
    row ended."""
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        ending = await _park_behind(world, busy)
        world.free["slot"] = True
        T._end(busy)                                  # no terminal fan-out for it
        await slots.manager.release(BOSS, busy)
        await _settle(world)
        return ending

    ending = asyncio.run(go())
    assert len(world.wakes) == 1, "the slot was released and the parked wake stayed parked"
    assert ending in world.wakes[0]["message"].splitlines()[0]
    slots.backlog.drain_next.assert_awaited_once_with(BOSS)


def test_a_release_that_frees_nothing_drains_nothing(world, full, slots):
    """A late or repeated release of an id that holds no slot (#1083)."""
    busy = H._busy_run()

    async def go():
        await _park_behind(world, busy)
        world.free["slot"] = True
        await slots.manager.release(BOSS, "never-held")
        await _settle(world)

    asyncio.run(go())
    assert world.wakes == []
    assert world.redis.get(MISSED + BOSS)


def test_a_wake_drain_that_raises_does_not_fail_the_release_or_the_backlog_drain(
        world, slots, monkeypatch):
    """The wake drain is the second thing a release does: it must never cost
    the release, nor the backlog drain beside it."""
    async def _boom(_agent):
        raise RuntimeError("wake drain broke")

    monkeypatch.setattr(_WAKE, "drain_on_slot_release", _boom, raising=False)

    async def go():
        await slots.hold(BOSS, "e1")
        await slots.manager.release(BOSS, "e1")      # must not raise
        await _settle(world)
        held = slots.redis.zcard(f"agent:slots:{BOSS}")
        # The callback's own guard, not only SlotService's wrapper around it.
        await slots.manager._on_slot_released(BOSS)
        return held

    assert asyncio.run(go()) == 0, "the slot was not released"
    assert slots.backlog.drain_next.await_count == 2
    slots.backlog.drain_next.assert_awaited_with(BOSS)


def test_the_backlog_is_drained_before_the_parked_wake(world, slots, monkeypatch):
    """A queued run was waiting first; the wake takes what is left."""
    order = []

    async def _backlog(agent):
        order.append(("backlog", agent))

    async def _wake(agent):
        order.append(("wake", agent))

    slots.backlog.drain_next.side_effect = _backlog
    monkeypatch.setattr(_WAKE, "drain_on_slot_release", _wake)

    async def go():
        await slots.hold(BOSS, "e1")
        await slots.manager.release(BOSS, "e1")
        await _settle(world)

    asyncio.run(go())
    assert order == [("backlog", BOSS), ("wake", BOSS)]


def test_a_backlog_drain_that_raises_still_drains_the_parked_wake(world, full, slots):
    slots.backlog.drain_next.side_effect = RuntimeError("backlog broke")
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        await _park_behind(world, busy)
        world.free["slot"] = True
        T._end(busy)
        await slots.manager.release(BOSS, busy)
        await _settle(world)

    asyncio.run(go())
    assert len(world.wakes) == 1


def test_a_release_that_leaves_the_agent_full_does_not_churn_the_parked_wake(world, full, slots):
    """The backlog's sentinel swap releases a slot it takes straight back, so
    the callback fires with nothing freed. The drain must look at the meter
    before it takes the marker: otherwise every such release re-runs the whole
    hook and writes one more ``parked`` audit row."""
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        await slots.hold(BOSS, "sentinel")
        ending = await _park_behind(world, busy)
        await slots.manager.release(BOSS, "sentinel")     # world.free stays False
        await _settle(world)
        return ending

    ending = asyncio.run(go())
    assert world.redis.get(MISSED + BOSS) == ending
    assert len(H._parked(world)) == 1, "a release that freed nothing re-parked the ending"
    assert full.refused == []


def test_a_reclaimed_stale_slot_delivers_a_parked_wake(world, full, slots):
    """The cleanup sweep removes a leaked slot without the release callback;
    it is still a slot that freed."""
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        ending = await _park_behind(world, busy)
        T._end(busy)                                       # leaked: the row is long terminal
        slots.redis.zadd(f"agent:slots:{BOSS}", {busy: 1.0})   # acquired at the epoch
        slots.redis.delete(f"agent:slot:{BOSS}:{busy}")
        world.free["slot"] = True
        reclaimed = await slots.manager.reclaim_stale()
        await _settle(world)
        return ending, reclaimed

    ending, reclaimed = asyncio.run(go())
    assert reclaimed == {BOSS: [busy]}
    assert len(world.wakes) == 1, "a reclaimed slot left the parked wake parked"
    assert ending in world.wakes[0]["message"].splitlines()[0]


def test_a_force_release_delivers_a_parked_wake(world, full, slots):
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        ending = await _park_behind(world, busy)
        T._end(busy)
        world.free["slot"] = True
        await slots.manager.force_release(BOSS)
        await _settle(world)
        return ending

    ending = asyncio.run(go())
    assert len(world.wakes) == 1, "a force release left the parked wake parked"
    assert ending in world.wakes[0]["message"].splitlines()[0]


def test_a_reclaim_whose_wake_drain_raises_still_returns_what_it_reclaimed(
        world, slots, monkeypatch):
    async def _boom(_agent):
        raise RuntimeError("wake drain broke")

    monkeypatch.setattr(_WAKE, "drain_on_slot_release", _boom)

    async def go():
        await slots.hold(BOSS, "old")
        slots.redis.zadd(f"agent:slots:{BOSS}", {"old": 1.0})
        slots.redis.delete(f"agent:slot:{BOSS}:old")
        reclaimed = await slots.manager.reclaim_stale()
        await slots.hold(BOSS, "e2")
        cleared = await slots.manager.force_release(BOSS)
        return reclaimed, cleared.slots_cleared

    assert asyncio.run(go()) == ({BOSS: ["old"]}, 1)


# ---------------------------------------------------------------------------
# F5 — a terminate releases the slot without the terminal fan-out
# ---------------------------------------------------------------------------

class _AgentSaysTerminated:
    status_code = 200

    def json(self):
        return {"status": "terminated", "returncode": -15}


class _AgentClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, _url):
        return _AgentSaysTerminated()


@pytest.fixture
def terminate(monkeypatch):
    """Everything a terminate does besides the slot and the row is out of scope."""
    monkeypatch.setattr(_CE, "agent_httpx_client", lambda name, timeout=15.0: _AgentClient())
    monkeypatch.setattr(_CE, "_record_cancel_event", AsyncMock())
    monkeypatch.setattr(_CE, "_audit_terminate_once", AsyncMock())
    monkeypatch.setattr(_CE, "_close_dispatch_activity_cancelled", AsyncMock())
    monkeypatch.setattr(_CE.agent_call_limiter, "cancel_inflight",
                        lambda eid, agent_name=None: "parked")


@pytest.mark.parametrize("how", ["running_turn", "parked_in_the_backend_queue"])
def test_a_terminated_run_frees_its_slot_for_a_parked_wake(world, full, slots, terminate, how):
    """An operator stops the run that held the caller's last slot. The row is
    written CANCELLED with no terminal fan-out, so nothing else drains."""
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        ending = await _park_behind(world, busy)
        world.free["slot"] = True
        if how == "running_turn":
            await _CE._proxy_terminate_and_finalize(BOSS, busy, busy, None)
        else:
            assert await _CE._cancel_inflight_if_parked(BOSS, busy, busy, None)
        await _settle(world)
        return ending

    ending = asyncio.run(go())
    assert db.get_execution(busy).status == "cancelled"
    assert len(world.wakes) == 1, "the terminated run's slot freed and the parked wake was not delivered"
    assert ending in world.wakes[0]["message"].splitlines()[0]


# ---------------------------------------------------------------------------
# F2 — the ended run's slot is still on the meter when its terminal drains
# ---------------------------------------------------------------------------

def test_a_wake_refused_while_the_ended_runs_slot_is_still_held_is_delivered_at_the_release(
        world, full, slots, meter):
    """The run's terminal reaches the hook before its ``finally`` releases the
    slot. No run of the caller is running any more, the meter is still full,
    admission refuses — and the ending must wait for the release, not be lost
    behind a FAILED wake row.

    (``meter`` after ``slots``: the wake service reads the stubbed meter, the
    release goes through the real manager.)"""
    busy = H._busy_run()

    async def go():
        await slots.hold(BOSS, busy)
        ending = await _park_behind(world, busy)

        T._end(busy)                                  # the terminal lands...
        await _WAKE.maybe_wake_caller(busy)           # ...and its fan-out drains
        await _settle(world)
        refused_before_release = len(full.refused)
        parked = world.redis.get(MISSED + BOSS)

        meter["available"] = 1                        # ...then the finally releases
        world.free["slot"] = True
        await slots.manager.release(BOSS, busy)
        await _settle(world)
        return ending, parked, refused_before_release

    ending, parked, refused_before_release = asyncio.run(go())
    assert parked == ending, "the refused ending was dropped instead of parked again"
    assert refused_before_release <= 1, "refused wakes looped while the slot was held"
    assert len(world.wakes) == 1, "the slot was released and the ending was never delivered"
    assert ending in world.wakes[0]["message"].splitlines()[0]


def test_a_refusal_with_no_run_left_does_not_spin(world, full, meter):
    """Parked again with no running run and a meter that stays full: nothing
    may drain it back into a refusal — one FAILED row at most, then it waits."""
    busy = H._busy_run()

    async def go():
        ending = await _park_behind(world, busy)
        T._end(busy)
        await _WAKE.maybe_wake_caller(busy)
        await _settle(world, rounds=40)
        return ending

    ending = asyncio.run(go())
    assert len(full.refused) <= 1, f"{len(full.refused)} refused wakes for one ending"
    assert world.redis.get(MISSED + BOSS) == ending


# ---------------------------------------------------------------------------
# F3 — an unreadable meter must not loop
# ---------------------------------------------------------------------------

def test_an_unreadable_meter_does_not_loop_a_refused_wake(world, full, meter):
    """The first dispatch still fails open (admission decides). After that
    refusal the ending is parked, and "deliver if a slot freed" must not read
    an unreadable meter as a free slot: that would dispatch, be refused and
    park again for as long as the meter is down."""
    meter["boom"] = True
    H._busy_run()                                     # a run of the caller is running
    ending = H._one_ending()

    async def go():
        await _WAKE.maybe_wake_caller(ending)
        await _settle(world, rounds=40)

    asyncio.run(go())
    assert len(full.refused) == 1, f"{len(full.refused)} refused wakes for one ending"
    assert world.redis.get(MISSED + BOSS) == ending


# ---------------------------------------------------------------------------
# F4 — a follow-on cancelled before it claims leaves the marker
# ---------------------------------------------------------------------------

def test_a_follow_on_cancelled_before_it_runs_leaves_the_parked_ending(world, monkeypatch):
    """The drain takes the marker and spawns the delivery as its own task. A
    stopping backend cancels that task before its first step; the ending must
    still be there for the next run's end."""
    monkeypatch.setattr(_WAKE, "_spawn_followon", T._REAL_SPAWN)
    ending = H._one_ending()
    world.redis.set(MISSED + BOSS, ending, ex=3600)

    async def go():
        await _WAKE._drain_missed(BOSS)
        tasks = list(_WAKE._FOLLOW_ONS)
        assert len(tasks) == 1, "the drain spawned no follow-on"
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert world.wakes == []
    assert world.redis.get(MISSED + BOSS) == ending, "a cancelled delivery lost the parked ending"
    assert 0 < world.redis.ttl(MISSED + BOSS) <= _WAKE._MISSED_TTL_SECONDS


def test_a_follow_on_cancelled_between_the_claim_and_the_dispatch_leaves_the_parked_ending(
        world, monkeypatch):
    """It holds the caller but no wake row exists yet (the budget check and the
    reads before the dispatch are still ahead): cancelled there, nothing else
    would ever deliver the ending."""
    monkeypatch.setattr(_WAKE, "_spawn_followon", T._REAL_SPAWN)
    ending = H._one_ending()
    world.redis.set(MISSED + BOSS, ending, ex=3600)
    reached = asyncio.Event()

    async def _stuck_budget(_callee, _caller):
        reached.set()
        await asyncio.Event().wait()                      # never returns

    monkeypatch.setattr(_WAKE, "_within_wake_budget", _stuck_budget)

    async def go():
        await _WAKE._drain_missed(BOSS)
        (task,) = list(_WAKE._FOLLOW_ONS)
        await asyncio.wait_for(reached.wait(), 30)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert world.wakes == []
    assert world.redis.get(MISSED + BOSS) == ending, "cancelled before its wake row existed, and lost"


def test_a_cancelled_follow_on_never_overwrites_a_newer_parked_ending(world, monkeypatch):
    monkeypatch.setattr(_WAKE, "_spawn_followon", T._REAL_SPAWN)
    old, newer = H._one_ending(), H._one_ending()
    world.redis.set(MISSED + BOSS, old, ex=3600)

    async def go():
        await _WAKE._drain_missed(BOSS)
        (task,) = list(_WAKE._FOLLOW_ONS)
        world.redis.set(MISSED + BOSS, newer, ex=3600)     # parked meanwhile
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert world.redis.get(MISSED + BOSS) == newer


def test_a_follow_on_cancelled_after_it_claimed_does_not_put_the_ending_back(world, monkeypatch):
    """Once the delivery holds the caller and its wake is being dispatched, the
    wake row owns the outcome; putting the marker back would wake twice."""
    monkeypatch.setattr(_WAKE, "_spawn_followon", T._REAL_SPAWN)
    ending = H._one_ending()
    world.redis.set(MISSED + BOSS, ending, ex=3600)

    async def go():
        world.gate["event"] = asyncio.Event()             # the wake turn keeps running
        await _WAKE._drain_missed(BOSS)
        (task,) = list(_WAKE._FOLLOW_ONS)
        for _ in range(50):
            await asyncio.sleep(0)
            if world.wakes:
                break
        assert len(world.wakes) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert world.redis.get(MISSED + BOSS) is None
