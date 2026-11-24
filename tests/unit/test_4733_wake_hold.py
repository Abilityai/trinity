"""abilityai/trinity#4733 — one wake per caller at a time, whatever window it
claimed; a wake refused at capacity is parked, never a failed row.

The wake was keyed ``caller_wake:{caller}:{now // 15}``, and the #4536 park ran
only when that key replayed in flight. An ending in a LATER 15 s window computed
a new key, claimed it fresh and started a second wake while the first still ran
— reproduced as three wake turns running at once, and seen live. The bound the
docs promised, one running wake plus one pending per caller, held only inside
one window.

Pinned here, on the #4536 harness (the real ``maybe_wake_caller``, the real
idempotency claim on a real DB, an in-memory Redis):

* endings in later windows wait for the running wake, and the latest of them is
  delivered once when it ends — for a wake that outlives several windows and
  for a callee the wake itself started;
* an ending from before the running wake's claim folds into it in any window;
  one written late with a ``completed_at`` back-dated before the claim parks;
* the caller's hold is a DB fact: a backend stop that cancels the holder and
  writes the wake row FAILED with no terminal fan-out does not hold the caller
  past the claim's grace, and a later run's end delivers what was parked; a
  wake row's own terminal frees the hold its dispatch took and no other; a
  holder whose wait gave up while its row is still live does not let a second
  wake in;
* at capacity (the product ruling on #4733): no wake row is written, the ending
  is parked (``parked``, ``reason="at_capacity"``) and delivered when any run of
  the caller ends; not while a wake still runs; a pilot and a stale slot meter
  dispatch; a refusal that races the check parks too (its FAILED row is the
  stated limit), except on a pilot; a 300-row sweep at capacity writes no row;
* the two DB reads the hold rests on, ``peek`` and ``has_execution_with_status``.

The clock moves on the old window key (``time``) and on the service's own
``_now`` together, so every test crosses the boundaries the old key had.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_4536_wake_window as T  # noqa: E402 — the #4536 harness
from test_4536_wake_window import world  # noqa: E402,F401
from db_harness import db_backend, count as _hcount, run as _hrun  # noqa: E402,F401

from database import db  # noqa: E402
from db.write_params import TaskExecutionFields  # noqa: E402
from services.execution_envelope import TaskExecutionErrorCode  # noqa: E402

_WAKE = T._WAKE
BOSS, WORKER, MISSED = T.BOSS, T.WORKER, T.MISSED
pytestmark = pytest.mark.unit

# Mid-window on the old 15 s key, so +11 s and +26 s land in the next two.
_OLD_NOW = 1_800_000_000 - (1_800_000_000 % 15) + 5
_REAL_HAS_FREE_SLOT = _WAKE.__dict__.get("_has_free_slot")


class _Clock:
    """Moves the old window key and the service's own clock together.

    Both run ``offset`` seconds ahead of the real clock, which still stamps the
    claims and the rows, so "now" stays after every claim already taken."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        monkeypatch.setattr(_WAKE, "time",
                            SimpleNamespace(time=lambda: _OLD_NOW + self.offset), raising=False)
        monkeypatch.setattr(_WAKE, "_now", self.at, raising=False)

    def move(self, seconds: float) -> None:
        self.offset = float(seconds)

    def at(self, delta: float = 0.0) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset + delta)


def _wake_rows() -> int:
    return _hcount("schedule_executions", "agent_name=:a AND triggered_by='caller_wake'", a=BOSS)


def _hold():
    """The caller's hold as the DB has it (None when free)."""
    from services import idempotency_service
    return idempotency_service.peek(_WAKE._agent_scope(BOSS), _WAKE._hold_key(BOSS))


def _parked(world) -> list:
    return [a for a in world.audits if a["status"] == "parked"]


async def _release_all(world, *holders):
    world.gate["event"] = None
    for ev in world.held:
        ev.set()
    results = await asyncio.gather(*holders, return_exceptions=True)
    while world.spawned:
        await world.spawned.pop(0)
    return results


async def _hold_first_wake(world, clock):
    world.gate["event"] = asyncio.Event()                  # wake 1 keeps running
    first = T._callee_run()
    T._end(first, clock.at(-1))
    holder = asyncio.ensure_future(_WAKE.maybe_wake_caller(first))
    await asyncio.sleep(0.05)
    assert len(world.held) == 1
    return holder


# --- one wake at a time, across windows --------------------------------------

def test_endings_in_later_windows_wait_for_the_running_wake(world, monkeypatch):
    """The reported defect: three endings in three consecutive windows while
    wake 1 runs used to be three wake turns running at once."""
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        late = []
        for offset in (11, 26):                              # the next two windows
            clock.move(offset)
            run = T._callee_run()
            T._end(run, clock.at())
            late.append(asyncio.ensure_future(_WAKE.maybe_wake_caller(run)))
            await asyncio.sleep(0.05)
        running = len(world.held)
        marker = world.redis.get(MISSED + BOSS)
        await _release_all(world, holder, *late)
        return running, marker, run

    running, marker, last = asyncio.run(go())
    assert running == 1, f"{running} wake turns were running at once for one caller"
    assert marker == last, "the latest ending was not parked for the running wake"
    assert len(_parked(world)) == 2
    assert len(world.wakes) == 2, "the parked ending must be delivered exactly once"
    assert last in world.wakes[1]["message"].splitlines()[0]


def test_a_wake_that_delegates_again_gets_its_callees_ending_after_it_ends(world, monkeypatch):
    """The wake turn itself delegates; its callee ends several windows later
    while the wake still runs. No second wake; one follow-on names it."""
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        own = T._callee_run()                               # started by the running wake
        clock.move(50)                                       # three windows on
        T._end(own, clock.at())
        pending = asyncio.ensure_future(_WAKE.maybe_wake_caller(own))
        await asyncio.sleep(0.05)
        running = len(world.held)
        await _release_all(world, holder, pending)
        return running, own

    running, own = asyncio.run(go())
    assert running == 1
    assert len(world.wakes) == 2
    assert own in world.wakes[1]["message"].splitlines()[0]


def test_an_ending_from_before_the_claim_folds_in_a_later_window(world, monkeypatch):
    """Rows that ended before wake 1 claimed are in its listing: processed in
    the next window, they fold instead of waking again."""
    clock = _Clock(monkeypatch)

    async def go():
        world.gate["event"] = asyncio.Event()
        rows = [T._callee_run() for _ in range(5)]
        for r in rows:
            T._end(r, clock.at(-1))
        holder = asyncio.ensure_future(_WAKE.maybe_wake_caller(rows[0]))
        await asyncio.sleep(0.05)
        clock.move(11)                                       # the next window
        rest = [asyncio.ensure_future(_WAKE.maybe_wake_caller(r)) for r in rows[1:]]
        await asyncio.sleep(0.05)
        await _release_all(world, holder, *rest)

    asyncio.run(go())
    assert len(world.wakes) == 1, "a covered ending woke the caller again"
    assert _parked(world) == []
    assert world.redis.get(MISSED + BOSS) is None


def test_a_late_written_ending_back_dated_before_the_claim_is_parked(world, monkeypatch):
    """A retained result written minutes late carries the agent's own finish
    instant. Wake 1 listed it as running, so it must not fold into wake 1."""
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        late = T._callee_run()
        T._end(late, clock.at(-120))                         # back-dated before the claim
        assert await _WAKE.maybe_wake_caller(late) is None
        marker = world.redis.get(MISSED + BOSS)
        await _release_all(world, holder)
        return marker, late

    marker, late = asyncio.run(go())
    assert marker == late
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"].splitlines()[0]


# --- the hold is a DB fact ---------------------------------------------------

def _stop_backend_mid_wake(world):
    """A clean stop: the holder is cancelled, and `execute_task`'s cancel branch
    writes the wake row FAILED WITHOUT the terminal fan-out — so no
    `maybe_wake_caller(wake_row)` ever runs for it."""
    _hrun("UPDATE schedule_executions SET status='failed', error='Execution cancelled "
          "(backend shutdown)' WHERE id=:i", i=world.wakes[0]["id"])


def test_a_backend_stop_mid_wake_does_not_hold_the_caller(world, monkeypatch):
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        parked = T._callee_run()
        T._end(parked, clock.at())
        assert await _WAKE.maybe_wake_caller(parked) is None
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        _stop_backend_mid_wake(world)
        world.gate["event"] = None

        clock.move(130)                                      # the restarted backend, later
        fresh = T._callee_run()
        T._end(fresh, clock.at())
        woken = await _WAKE.maybe_wake_caller(fresh)
        assert woken, "the stopped wake still held the caller"
        assert _hold() is None, "the stopped wake's claim was never reclaimed"
        await T._wake_turn_ends(world, woken)
        return parked

    parked = asyncio.run(go())
    assert len(world.wakes) == 3
    assert parked in world.wakes[2]["message"].splitlines()[0]


def test_after_a_stop_an_ending_waits_out_the_grace_and_a_run_end_delivers_it(world, monkeypatch):
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        _stop_backend_mid_wake(world)
        world.gate["event"] = None

        clock.move(30)                                       # inside the claim's grace
        ending = T._callee_run()
        T._end(ending, clock.at())
        assert await _WAKE.maybe_wake_caller(ending) is None
        assert world.redis.get(MISSED + BOSS) == ending

        clock.move(150)                                      # a run of the caller ends
        run = db.create_task_execution(agent_name=BOSS, message="cron", triggered_by="schedule").id
        T._end(run, clock.at())
        await _WAKE.maybe_wake_caller(run)
        while world.spawned:
            await world.spawned.pop(0)
        return ending

    ending = asyncio.run(go())
    assert len(world.wakes) == 2
    assert ending in world.wakes[1]["message"].splitlines()[0]


def test_a_wake_rows_terminal_frees_the_hold_of_a_holder_that_is_gone(world, monkeypatch):
    """A crashed holder never releases; the recovery sweep's terminal for its
    wake row does, so the next ending wakes at once, not after the grace."""
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        world.gate["event"] = None
        await T._wake_turn_ends(world, world.wakes[0]["id"])
        clock.move(5)
        nxt = T._callee_run()
        T._end(nxt, clock.at())
        return await _WAKE.maybe_wake_caller(nxt)

    assert asyncio.run(go()), "the ended wake's hold still blocked the next ending"
    assert len(world.wakes) == 2


def test_a_late_wake_terminal_leaves_the_next_wakes_hold(world, monkeypatch):
    """Wake 1 returned and wake 2 holds the caller; wake 1's terminal side
    effects (a spawned task) run only now. Wake 2's hold must survive."""
    clock = _Clock(monkeypatch)

    async def go():
        first = T._callee_run()
        T._end(first, clock.at(-1))
        w1 = await _WAKE.maybe_wake_caller(first)
        assert w1
        clock.move(3)
        holder = await _hold_first_wake(world, clock)        # wake 2, held
        held_by_two = _hold()
        await _WAKE.maybe_wake_caller(w1)                     # wake 1's terminal, late
        while world.spawned:
            await world.spawned.pop(0)
        after = _hold()
        assert after is not None and after.claim_token == held_by_two.claim_token, \
            "wake 1's terminal freed wake 2's hold"
        third = T._callee_run()
        T._end(third, clock.at())
        pending = asyncio.ensure_future(_WAKE.maybe_wake_caller(third))
        await asyncio.sleep(0.05)
        running = len([e for e in world.held if not e.is_set()])
        await _release_all(world, holder, pending)
        return running

    assert asyncio.run(go()) == 1


def test_a_wake_terminal_reads_the_admission_instant_not_a_restamped_start(world):
    """A pull claim and the limiter park re-stamp ``started_at``; ``queued_at``
    keeps the admission. A wake row admitted before the hold was claimed is not
    that hold's wake, whatever its ``started_at`` says — here the hold is a
    claim whose own row does not exist yet, so no live row masks the rule."""
    from services import idempotency_service

    scope, key = _WAKE._agent_scope(BOSS), _WAKE._hold_key(BOSS)
    claim = idempotency_service.begin(scope, key)            # a holder, before its row
    older = db.create_task_execution(agent_name=BOSS, message="an older wake",
                                     triggered_by="caller_wake").id
    now = datetime.now(timezone.utc)
    _hrun("UPDATE schedule_executions SET status='success', queued_at=:q, started_at=:s, "
          "completed_at=:s WHERE id=:i", q=T._iso(now - timedelta(seconds=60)),
          s=T._iso(now + timedelta(seconds=60)), i=older)
    asyncio.run(_WAKE.maybe_wake_caller(older))              # that row's terminal
    after = _hold()
    assert after is not None and after.claim_token == claim.claim_token, \
        "a row admitted before the hold was claimed freed it"


def test_a_stale_hold_that_cannot_be_reclaimed_wakes_without_looping(world, monkeypatch):
    """The reclaim fails (a DB error on the delete): the wake runs without the
    hold rather than parking an ending the next read calls free."""
    from services import idempotency_service

    clock = _Clock(monkeypatch)
    idempotency_service.begin(_WAKE._agent_scope(BOSS), _WAKE._hold_key(BOSS))
    monkeypatch.setattr(_WAKE, "_reclaim_idempotency", lambda decision: False)
    clock.move(130)
    ending = T._callee_run()
    T._end(ending, clock.at())

    async def go():
        woken = await _WAKE.maybe_wake_caller(ending)
        for _ in range(5):                                    # a loop would refill this
            if not world.spawned:
                break
            await world.spawned.pop(0)
        return woken

    assert asyncio.run(go())
    assert world.spawned == [] and len(world.wakes) == 1


def test_a_holder_whose_wait_gave_up_does_not_let_a_second_wake_in(world, monkeypatch):
    """On a pilot the holder's wait can time out while its wake row is still
    queued or running. The row, not the holder, says a wake is running."""
    clock = _Clock(monkeypatch)
    fixture_dispatch = _WAKE._dispatch_wake
    calls = []

    async def _dispatch(**kw):
        calls.append(kw)
        if len(calls) == 1:
            row = db.create_task_execution(agent_name=kw["agent_name"], message=kw["message"],
                                           triggered_by=kw["triggered_by"])
            world.wakes.append({"id": row.id, "message": kw["message"]})
            return SimpleNamespace(execution_id=row.id, status="failed",
                                   error="Execution timed out", error_code=TaskExecutionErrorCode.TIMEOUT)
        return await fixture_dispatch(**kw)

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch)

    async def go():
        first = T._callee_run()
        T._end(first, clock.at(-1))
        w1 = await _WAKE.maybe_wake_caller(first)             # the waiter gave up; W1 still runs
        clock.move(20)
        nxt = T._callee_run()
        T._end(nxt, clock.at())
        assert await _WAKE.maybe_wake_caller(nxt) is None, "a second wake started beside W1"
        await T._wake_turn_ends(world, w1)                    # W1 really ends
        return nxt

    nxt = asyncio.run(go())
    assert len(calls) == 2
    assert nxt in world.wakes[1]["message"].splitlines()[0]


# --- at capacity (the product ruling on #4733) --------------------------------

@pytest.fixture
def full(world, monkeypatch):
    """Every slot of the caller is held by a running run: admission refuses a
    wake the way ``execute_task`` does (a FAILED row, error_code CAPACITY)."""
    world.free["slot"] = False
    fixture_dispatch = _WAKE._dispatch_wake
    refused = []

    async def _dispatch(**kw):
        if not world.free["slot"]:
            row = db.create_task_execution(agent_name=kw["agent_name"], message=kw["message"],
                                           triggered_by=kw["triggered_by"])
            _hrun("UPDATE schedule_executions SET status='failed', error=:e WHERE id=:i",
                  e="Agent at capacity (3/3 parallel tasks running)", i=row.id)
            refused.append(row.id)
            return SimpleNamespace(execution_id=row.id, status="failed",
                                   error="Agent at capacity (3/3 parallel tasks running)",
                                   error_code=TaskExecutionErrorCode.CAPACITY)
        return await fixture_dispatch(**kw)

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch)
    return SimpleNamespace(refused=refused)


def _one_ending() -> str:
    ending = T._callee_run()
    T._end(ending)
    return ending


def _busy_run(trigger: str = "schedule") -> str:
    return db.create_task_execution(agent_name=BOSS, message="holds a slot",
                                    triggered_by=trigger).id


def test_a_wake_refused_at_capacity_is_parked_not_recorded_as_failed(world, full):
    _busy_run()
    ending = T._callee_run()
    T._end(ending)
    assert asyncio.run(_WAKE.maybe_wake_caller(ending)) is None
    assert _wake_rows() == 0, "a wake refused at capacity left a caller_wake row"
    assert world.redis.get(MISSED + BOSS) == ending
    assert [a.get("reason") for a in _parked(world)] == ["at_capacity"]


@pytest.mark.parametrize("trigger", ["schedule", "chat", "operator_response"])
def test_a_parked_wake_is_delivered_when_a_run_of_the_caller_ends(world, full, trigger):
    """E1: any run's end can free the slot — a person's chat and a platform
    turn included, though neither has a delegating agent of its own."""
    busy = _busy_run(trigger)
    ending = T._callee_run()
    T._end(ending)

    async def go():
        assert await _WAKE.maybe_wake_caller(ending) is None
        world.free["slot"] = True
        T._end(busy)
        await _WAKE.maybe_wake_caller(busy)                   # that run's terminal
        while world.spawned:
            await world.spawned.pop(0)

    asyncio.run(go())
    assert full.refused == []
    assert len(world.wakes) == 1
    assert ending in world.wakes[0]["message"].splitlines()[0]


def test_a_run_end_while_a_wake_runs_leaves_the_parked_ending_for_that_wake(world, monkeypatch):
    clock = _Clock(monkeypatch)

    async def go():
        holder = await _hold_first_wake(world, clock)
        late = T._callee_run()
        T._end(late, clock.at())
        assert await _WAKE.maybe_wake_caller(late) is None
        run = _busy_run()
        T._end(run, clock.at())
        await _WAKE.maybe_wake_caller(run)
        assert world.spawned == [], "a run's end drained the ending while a wake still ran"
        await _release_all(world, holder)
        return late

    late = asyncio.run(go())
    assert len(world.wakes) == 2
    assert late in world.wakes[1]["message"].splitlines()[0]


def _refuse_after_the_check(world, monkeypatch):
    """The slot check passes, then the last slot goes before admission (and
    stays taken: that is the race)."""
    world.free["slot"] = True
    refusing = _WAKE._dispatch_wake

    async def _refuse(**kw):
        world.free["slot"] = False
        return await refusing(**kw)

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _refuse)


def test_a_refusal_that_races_the_slot_check_is_parked_too(world, full, monkeypatch):
    """The check found a slot; admission did not. That FAILED row is the stated
    limit; the ending is still delivered, and not drained into a retry at once."""
    _refuse_after_the_check(world, monkeypatch)
    _busy_run()
    ending = _one_ending()
    assert asyncio.run(_WAKE.maybe_wake_caller(ending)) is None
    assert len(full.refused) == 1
    assert world.redis.get(MISSED + BOSS) == ending
    assert world.spawned == [], "the holder drained the refused ending into a retry at once"


def test_a_refused_ending_is_delivered_if_the_busy_run_ended_before_the_marker(world, full, monkeypatch):
    """The run holding the slot ends between the refusal and the marker's SET:
    its own drain found nothing parked, so the park itself takes the freed slot."""
    admitting = _WAKE._dispatch_wake
    _refuse_after_the_check(world, monkeypatch)
    busy = _busy_run()
    real_set = world.redis.set

    def _set(name, value, *a, **k):
        if name.startswith(MISSED):
            T._end(busy)                                    # the slot frees right here
            world.free["slot"] = True
            monkeypatch.setattr(_WAKE, "_dispatch_wake", admitting)   # the race is over
        return real_set(name, value, *a, **k)

    monkeypatch.setattr(world.redis, "set", _set)
    ending = _one_ending()

    async def go():
        assert await _WAKE.maybe_wake_caller(ending) is None
        assert world.spawned, "the freed slot was not taken: the ending is stranded"
        while world.spawned:
            await world.spawned.pop(0)

    asyncio.run(go())
    assert len(full.refused) == 1
    assert len(world.wakes) == 1 and ending in world.wakes[0]["message"].splitlines()[0]


def test_a_drained_ending_never_folds_into_a_claim_that_may_not_dispatch(world):
    """A parked ending reaches its follow-on while a fresh claim holds the
    caller, a claim taken just after the ending ended. Folding would leave it
    with that claim, which a budget cap or an error can still drop; it parks."""
    from services import idempotency_service

    ending = T._callee_run()
    T._end(ending, datetime.now(timezone.utc) - timedelta(seconds=1))
    idempotency_service.begin(_WAKE._agent_scope(BOSS), _WAKE._hold_key(BOSS))   # the new claim
    assert asyncio.run(_WAKE.maybe_wake_caller(ending, _expect_caller=BOSS)) is None
    assert world.redis.get(MISSED + BOSS) == ending, "the drained ending folded"


def test_a_push_chat_slot_release_delivers_a_parked_wake(world, full):
    """A push /chat writes its terminal without the fan-out, so no hook runs
    for it; its slot release drains the parked wake instead."""
    busy = db.create_task_execution(agent_name=BOSS, message="a /chat turn", triggered_by="chat").id
    ending = _one_ending()

    async def go():
        assert await _WAKE.maybe_wake_caller(ending) is None
        world.free["slot"] = True
        T._end(busy)                                       # no maybe_wake_caller(busy)
        _WAKE.drain_after_slot_release(BOSS)
        while world.spawned:
            await world.spawned.pop(0)

    asyncio.run(go())
    assert full.refused == []
    assert len(world.wakes) == 1 and ending in world.wakes[0]["message"].splitlines()[0]


def test_run_chat_turn_drains_after_it_releases_its_slot(monkeypatch):
    """The production hand-off: ``run_chat_turn``'s ``finally`` releases the
    /chat slot, then asks the wake service to drain — on a failed turn too."""
    import httpx
    from unittest.mock import AsyncMock, MagicMock, patch

    import services.chat_execution_service as _CE
    from models import ChatMessageRequest
    from services.chat_signals import ChatDispatchError
    from test_chat_admission import _env, _idem, _user

    order = []
    monkeypatch.setattr(_CE.caller_wake_service, "drain_after_slot_release",
                        lambda name: order.append(("drain", name)))
    with _env(_idem(replay=False)) as m, \
         patch.object(_CE, "activity_service", MagicMock(complete_activity=AsyncMock())), \
         patch.object(_CE, "agent_post_with_retry", AsyncMock(side_effect=httpx.ConnectError("boom"))), \
         patch.object(_CE, "compose_system_prompt", return_value="sys"), \
         patch.object(_CE, "is_execution_context_enabled", return_value=False), \
         patch("services.subscription_auto_switch.is_auth_failure", return_value=False), \
         patch("services.subscription_auto_switch.handle_subscription_failure",
               AsyncMock(return_value=None)):
        m["cap"].release.side_effect = lambda name, eid: order.append(("release", name))
        with pytest.raises(ChatDispatchError):
            asyncio.run(_CE.run_chat_turn(
                name="agent1", request=ChatMessageRequest(message="hi"),
                current_user=_user(), x_source_agent=None,
                triggered_by="chat", task_execution_id="te1", _chat_subscription_id=None,
                chat_activity_id="ca1", collaboration_activity_id=None,
                session=MagicMock(id="s1"), execution=MagicMock(id="cex1"),
                queue_result="running", is_queued=False, chat_timeout=3600,
                idem=m["isvc"].begin.return_value, capacity=m["cap"],
            ))
    assert order == [("release", "agent1"), ("drain", "agent1")]


def test_a_300_row_sweep_at_capacity_writes_no_wake_row(world, full):
    busy = _busy_run()
    rows = [T._callee_run() for _ in range(300)]
    earlier = datetime.now(timezone.utc) - timedelta(seconds=2)
    for r in rows:
        T._end(r, earlier)

    async def go():
        for r in rows:
            assert await _WAKE.maybe_wake_caller(r) is None
        world.free["slot"] = True
        T._end(busy)
        await _WAKE.maybe_wake_caller(busy)
        while world.spawned:
            await world.spawned.pop(0)

    asyncio.run(go())
    assert full.refused == []
    assert len(world.wakes) == 1


# --- the real slot check ------------------------------------------------------

@pytest.fixture
def meter(world, monkeypatch):
    """The real ``_has_free_slot`` over a stubbed capacity meter and pilot flag."""
    if _REAL_HAS_FREE_SLOT is not None:
        monkeypatch.setattr(_WAKE, "_has_free_slot", _REAL_HAS_FREE_SLOT)
    else:
        monkeypatch.delattr(_WAKE, "_has_free_slot", raising=False)
    state = {"available": 0, "pilot": False, "boom": False}

    class _Capacity:
        async def get_slot_state(self, agent_name, max_concurrent):
            if state["boom"]:
                raise ConnectionError("redis down")
            return SimpleNamespace(available_slots=state["available"])

    import services.capacity_manager as cm
    import services.pull_pilot as pp
    monkeypatch.setattr(cm, "get_capacity_manager", lambda: _Capacity())
    monkeypatch.setattr(pp, "pull_owns_dispatch", lambda agent, trigger: state["pilot"])
    return state


def test_a_full_meter_with_a_running_run_parks(world, meter):
    _busy_run()
    assert asyncio.run(_WAKE.maybe_wake_caller(_one_ending())) is None
    assert world.wakes == []


@pytest.mark.parametrize("case", ["pilot", "stale_slots", "meter_down", "free"])
def test_the_slot_check_dispatches_whenever_admission_would_decide(world, meter, case):
    """A pilot queues the wake (never refused for a slot); a full meter with no
    running run is expired slots admission cleans first; an unreadable meter
    fails open; a free slot dispatches."""
    if case == "pilot":
        meter["pilot"] = True
        _busy_run()
    elif case == "meter_down":
        meter["boom"] = True
        _busy_run()
    elif case == "free":
        meter["available"] = 1
        _busy_run()
    assert asyncio.run(_WAKE.maybe_wake_caller(_one_ending()))
    assert len(world.wakes) == 1


def test_a_pilot_refusal_is_not_parked(world, full, monkeypatch):
    """On a pilot a CAPACITY result is a full backlog, which repeats: parking it
    would loop a FAILED row per round until the hourly budget ran out."""
    import services.pull_pilot as pp
    monkeypatch.setattr(pp, "pull_owns_dispatch", lambda agent, trigger: True)
    _refuse_after_the_check(world, monkeypatch)
    _busy_run()
    asyncio.run(_WAKE.maybe_wake_caller(_one_ending()))
    while world.spawned:
        asyncio.run(world.spawned.pop(0))
    assert len(full.refused) == 1
    assert world.redis.get(MISSED + BOSS) is None


# --- the DB reads the hold rests on ------------------------------------------

def test_peek_reads_a_claim_without_taking_it(db_backend):
    from services import idempotency_service as idem

    assert idem.peek("agent:zq4733", "k") is None
    claim = idem.begin("agent:zq4733", "k")
    seen = idem.peek("agent:zq4733", "k")
    assert seen is not None and seen.in_flight
    assert seen.claim_token == claim.claim_token and seen.claimed_at == claim.claimed_at
    assert idem.reclaim_in_flight(seen) is True               # usable as a CAS
    assert idem.peek("agent:zq4733", "k") is None

    assert idem.peek("agent:zq4733", "never") is None
    assert idem.begin("agent:zq4733", "never").replay is False, "a peek took the claim"

    idem.begin("agent:zq4733", "old")
    _hrun("UPDATE idempotency_keys SET created_at=:t WHERE scope='agent:zq4733' "
          "AND idempotency_key='old'", t=T._iso(datetime.now(timezone.utc) - timedelta(hours=25)))
    assert idem.peek("agent:zq4733", "old") is None, "an expired claim read as held"


def test_has_execution_with_status(db_backend):
    agent = "zq4733-reads"
    row = db.create_task_execution(agent_name=agent, message="m", triggered_by="caller_wake").id
    assert db.has_execution_with_status(agent, ["running"]) is True
    assert db.has_execution_with_status(agent, ["running"], triggered_by="caller_wake") is True
    assert db.has_execution_with_status(agent, ["running"], triggered_by="schedule") is False
    assert db.has_execution_with_status("zq4733-other", ["running"]) is False
    T._end(row)
    assert db.has_execution_with_status(agent, ["queued", "running", "pending_retry"]) is False
