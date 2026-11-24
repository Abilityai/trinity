"""abilityai/trinity#4538 — a caller that waits is not woken.

The caller wake exists for a caller that got a receipt. It also fired for a
caller that held the call open and already had the answer, and a caller that
polled in its own turn could not decline it. Two facts about one call now
stand the wake down, recorded when the callee row is created ("call terms",
an idempotency row keyed on the callee execution id) and read at its terminal:

* ``wake: false`` — declined per call, sync or async;
* ``caller_wait_ms`` — how long a sync caller waits; a run that ends inside
  that wait (less a margin) was answered inline.

Only positive evidence stands a wake down: no terms, unreadable terms, a
half-written row or a deadline that does not parse all wake, as before.

Driven through the REAL ``/task`` dispatch (``dispatch_parallel_task`` →
``create_task_execution_and_activities``) and the REAL hook
(``maybe_wake_caller``) over one real database, on the #4536 harness. Only the
agent call is a double: the sync applier and the async background task.

Related flow: docs/memory/feature-flows/agent-to-agent-collaboration.md
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_4536_wake_window as T  # noqa: E402 — the #4536 harness
from test_4536_wake_window import world  # noqa: E402,F401
from db_harness import db_backend, count as _hcount, run as _hrun  # noqa: E402,F401

import services.chat_execution_service as _CE  # noqa: E402
import services.dispatch_admission_service as _DISPATCH  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import ChatMessageRequest, ParallelTaskRequest, User  # noqa: E402

_WAKE = T._WAKE
BOSS, WORKER, MISSED = T.BOSS, T.WORKER, T.MISSED
OWNER = "zq4538-owner"
pytestmark = pytest.mark.unit

# Bound before `world` inlines `asyncio.to_thread` for the whole process.
_REAL_TO_THREAD = asyncio.to_thread


# ---------------------------------------------------------------------------
# the world: BOSS may call WORKER, through the real /task dispatch
# ---------------------------------------------------------------------------

@pytest.fixture
def calls(world, monkeypatch):
    db.create_user(UserCreate(username=OWNER, role="operator", email="owner@example.com"))
    db.register_agent_owner(BOSS, OWNER)
    db.register_agent_owner(WORKER, OWNER)
    db.add_agent_permission(BOSS, WORKER, created_by=OWNER)
    owner = db.get_user_by_username(OWNER)

    capacity = SimpleNamespace(
        acquire=AsyncMock(return_value=SimpleNamespace(state="admitted", queue_position=None)))
    monkeypatch.setattr(_DISPATCH, "get_capacity_manager", lambda: capacity)
    monkeypatch.setattr(_DISPATCH, "dispatch_breaker_active", lambda _n: False)
    # The agent call itself: the sync applier, and the async background task.
    monkeypatch.setattr(_CE, "_dispatch_sync",
                        AsyncMock(return_value={"status": "success", "response": "pong"}))

    async def _admitted(**_kw):
        return SimpleNamespace(state="admitted", pull=False), 900

    async def _background(**_kw):
        return None

    monkeypatch.setattr(_CE, "_acquire_task_capacity", _admitted)
    monkeypatch.setattr(_CE, "run_async_task", _background)
    return SimpleNamespace(owner_id=owner["id"], n=0)


def _key_of(calls, agent: str) -> User:
    return User(
        id=calls.owner_id, username=OWNER, email="owner@example.com", role="operator",
        agent_name=agent, mcp_scope="agent", mcp_key_id="k-" + agent,
        mcp_key_name=f"agent-{agent}-key",
    )


def _ids_on(agent: str) -> set:
    from db.engine import get_engine
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(
            text("SELECT id FROM schedule_executions WHERE agent_name = :a"), {"a": agent})}


async def _call(calls, *, source=BOSS, target=WORKER, **body):
    """One ``POST /task`` from ``source``'s own key. Returns (child id, answer)."""
    calls.n += 1
    before = _ids_on(target)
    answer = await _CE.dispatch_parallel_task(
        request=ParallelTaskRequest(message="ping", **body),
        name=target,
        current_user=_key_of(calls, source) if source else User(
            id=calls.owner_id, username=OWNER, email="owner@example.com", role="operator"),
        container=SimpleNamespace(status="running"),
        x_source_agent=source,
        x_via_mcp="true",
        idempotency_key=f"zq4538-{calls.n}",
        x_event_trigger=None,
        x_internal_secret=None,
    )
    (child,) = _ids_on(target) - before
    return child, answer


async def _ends(execution_id: str):
    """The callee row's terminal, as the fan-out reaches the hook."""
    T._end(execution_id)
    return await _WAKE.maybe_wake_caller(execution_id)


def _wake_rows() -> int:
    return _hcount("schedule_executions", "agent_name=:a AND triggered_by='caller_wake'", a=BOSS)


def _skips(world) -> list:
    return [(a["status"], a.get("reason")) for a in world.audits
            if a["status"] == "skipped_declined"]


def _later(monkeypatch, seconds: float) -> None:
    """The hook's clock runs ``seconds`` ahead."""
    monkeypatch.setattr(
        _WAKE, "_now", lambda: datetime.now(timezone.utc) + timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# (a) a sync call answered inline starts no wake
# ---------------------------------------------------------------------------

def test_a_sync_call_answered_inside_the_callers_wait_starts_no_wake(world, calls):
    """The issue's case: `parallel: true` without `async`, the callee answers in
    four seconds, the caller already holds the reply."""
    async def go():
        child, _answer = await _call(calls, async_mode=False, caller_wait_ms=25_000)
        return await _ends(child)

    assert asyncio.run(go()) is None
    assert _wake_rows() == 0, "a caller that held the call open was woken for its own answer"
    assert _skips(world) == [("skipped_declined", "answered_inline")]


# ---------------------------------------------------------------------------
# (c) a sync call that outlived the caller's wait keeps its wake
# ---------------------------------------------------------------------------

def test_a_sync_call_that_outlived_the_callers_wait_keeps_its_wake(world, calls, monkeypatch):
    """The MCP server stopped waiting at 25 s and handed back a receipt; the run
    ended later. That caller has no answer and is owed the wake."""
    async def go():
        child, _answer = await _call(calls, async_mode=False, caller_wait_ms=25_000)
        _later(monkeypatch, 40)
        return await _ends(child)

    assert asyncio.run(go())
    assert _wake_rows() == 1
    assert _skips(world) == []


@pytest.mark.parametrize("ends_before_deadline, woken", [(6.0, False), (4.0, True)])
def test_the_margin_decides_a_run_that_ends_close_to_the_deadline(
        world, calls, monkeypatch, ends_before_deadline, woken):
    """Inside the last ``INLINE_MARGIN_SECONDS`` the answer may not reach the
    caller in time, so the wake stands (a visible double, never a silence)."""
    assert _WAKE.INLINE_MARGIN_SECONDS == 5

    async def go():
        child, _answer = await _call(calls, async_mode=False, caller_wait_ms=25_000)
        _later(monkeypatch, 25 - ends_before_deadline)
        return await _ends(child)

    assert bool(asyncio.run(go())) is woken
    assert _wake_rows() == (1 if woken else 0)


# ---------------------------------------------------------------------------
# (b) wake: false on an async call starts no wake, and the receipt says so
# ---------------------------------------------------------------------------

def test_a_declined_async_call_starts_no_wake_and_its_receipt_says_so(world, calls):
    """The scheduled run that delegates async and polls in its own turn."""
    async def go():
        child, answer = await _call(calls, async_mode=True, wake=False)
        return answer, await _ends(child)

    answer, woken = asyncio.run(go())
    assert answer["status"] == "accepted"
    assert answer["wake_on_ending"] == {"effective": False, "source": "call"}
    assert woken is None
    assert _wake_rows() == 0, "a caller that declined the wake was woken"
    assert _skips(world) == [("skipped_declined", "caller_declined")]


def test_a_declined_sync_call_starts_no_wake_even_past_the_wait(world, calls, monkeypatch):
    async def go():
        child, _answer = await _call(calls, async_mode=False, wake=False, caller_wait_ms=25_000)
        _later(monkeypatch, 3600)
        return await _ends(child)

    assert asyncio.run(go()) is None
    assert _skips(world) == [("skipped_declined", "caller_declined")]


def test_the_decline_is_on_the_callers_collaboration_activity(world, calls, monkeypatch):
    seen = []

    async def _track(**kw):
        seen.append(kw)
        return "act-1"

    monkeypatch.setattr(_CE.activity_service, "track_activity", _track)
    asyncio.run(_call(calls, async_mode=True, wake=False))
    asyncio.run(_call(calls, async_mode=True))
    collab = [a["details"].get("wake") for a in seen
              if a["details"].get("action") == "parallel_task"]
    assert collab == ["declined", None]


# ---------------------------------------------------------------------------
# only positive evidence stands a wake down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"async_mode": True},                               # the default: a receipt, then a wake
    {"async_mode": True, "wake": True},                 # `true` is the default, not a force
    {"async_mode": False},                              # a sync caller that declared no wait
    {"async_mode": True, "caller_wait_ms": 25_000},     # a wait means nothing on a receipt
])
def test_a_call_that_declares_nothing_wakes_as_before(world, calls, body):
    async def go():
        child, answer = await _call(calls, **body)
        return child, answer, await _ends(child)

    child, answer, woken = asyncio.run(go())
    assert woken, "a call that neither declined nor declared a wait lost its wake"
    assert _WAKE._call_terms(WORKER, child) is None, "terms were written with nothing to record"
    assert answer.get("wake_on_ending", {}).get("source") != "call"


def test_wake_true_never_overrides_the_owners_switch(world, calls, monkeypatch):
    from services.wake_policy import WakeVerdict

    monkeypatch.setattr(_WAKE, "_resolve_policy",
                        lambda _a: WakeVerdict(enabled=False, source="agent"))

    async def go():
        child, _answer = await _call(calls, async_mode=True, wake=True)
        return await _ends(child)

    assert asyncio.run(go()) is None
    assert _wake_rows() == 0


def test_a_person_or_a_self_task_records_no_terms(world, calls):
    async def go():
        person, _a = await _call(calls, source=None, async_mode=True, wake=False)
        own, _b = await _call(calls, source=WORKER, async_mode=True, wake=False)
        return person, own

    person, own = asyncio.run(go())
    assert _WAKE._call_terms(WORKER, person) is None
    assert _WAKE._call_terms(WORKER, own) is None


def _run_with_terms(snapshot) -> str:
    run = T._callee_run()
    if snapshot is not None:
        _WAKE.record_call_terms(WORKER, run, snapshot)
    return run


@pytest.mark.parametrize("snapshot", [
    {"declined": False, "wait_until": None},
    {"declined": False, "wait_until": "not a timestamp"},
    {"declined": "yes", "wait_until": 12345},
    {"declined": None},
])
def test_terms_that_say_nothing_usable_wake(world, snapshot):
    """Unparsable never reads as "answered inline", and only a real `true`
    declines."""
    run = _run_with_terms(snapshot)
    assert asyncio.run(_ends(run))
    assert _skips(world) == []


def test_unreadable_terms_wake(world, monkeypatch):
    run = _run_with_terms({"declined": True, "wait_until": None})

    def _boom(scope, key):
        if key.startswith("caller_wake:terms:"):
            raise RuntimeError("pool timeout")
        return None

    monkeypatch.setattr(_WAKE, "_peek_idempotency", _boom)
    assert asyncio.run(_ends(run)), "a read error stood the wake down"


def test_a_half_written_terms_row_wakes(world):
    """The claim landed and the snapshot did not: nothing was recorded."""
    from services import idempotency_service

    run = T._callee_run()
    idempotency_service.begin(_WAKE._agent_scope(WORKER), f"caller_wake:terms:{run}")
    assert asyncio.run(_ends(run))


def test_a_terms_write_that_fails_does_not_fail_the_dispatch(world, calls, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(_WAKE, "_begin_idempotency", _boom)

    async def go():
        child, answer = await _call(calls, async_mode=True, wake=False)
        # The hold is claimed through the same seam; give it back for the hook.
        monkeypatch.undo()
        return child, answer

    child, answer = asyncio.run(go())
    assert answer["status"] == "accepted"
    assert child


# ---------------------------------------------------------------------------
# where the stand-down sits
# ---------------------------------------------------------------------------

def test_a_declined_ending_still_drains_a_wake_parked_for_the_callee(world):
    """Any run's end may free the slot a parked wake of THAT agent waits for;
    the stand-down must not come before that drain."""
    world.redis.set(MISSED + WORKER, "some-parked-ending")
    run = _run_with_terms({"declined": True, "wait_until": None})
    assert asyncio.run(_ends(run)) is None
    assert world.redis.get(MISSED + WORKER) is None, "the declined ending skipped the drain"
    assert len(world.spawned) == 1
    world.spawned.pop().close()


def test_a_declined_ending_is_never_parked_behind_a_running_wake(world):
    """Checked before the hold: parked, it would be delivered later by the
    drain — a wake the caller declined."""
    async def go():
        world.gate["event"] = asyncio.Event()
        first = T._callee_run()
        T._end(first, datetime.now(timezone.utc) - timedelta(seconds=1))
        holder = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        await asyncio.sleep(0)
        assert len(world.wakes) == 1

        declined = _run_with_terms({"declined": True, "wait_until": None})
        assert await _ends(declined) is None
        parked = world.redis.get(MISSED + BOSS)

        world.gate["event"] = None
        await T._wake_turn_ends(world, world.wakes[0]["id"])
        await T._finish(world, holder)
        return parked

    assert asyncio.run(go()) is None, "a declined ending was parked for later delivery"
    assert len(world.wakes) == 1
    assert [a["status"] for a in world.audits if a["status"] == "parked"] == []


def test_a_policy_off_caller_gets_no_audit_row(world, monkeypatch):
    """After the policy read: a caller that is never woken stays silent, so the
    audit volume does not grow."""
    from services.wake_policy import WakeVerdict

    monkeypatch.setattr(_WAKE, "_resolve_policy",
                        lambda _a: WakeVerdict(enabled=False, source="agent"))
    run = _run_with_terms({"declined": True, "wait_until": None})
    assert asyncio.run(_ends(run)) is None
    assert world.audits == []


def test_the_skip_audit_names_the_layer_and_the_instants(world, calls):
    async def go():
        child, _answer = await _call(calls, async_mode=False, caller_wait_ms=25_000)
        await _ends(child)
        return child

    child = asyncio.run(go())
    (row,) = [a for a in world.audits if a["status"] == "skipped_declined"]
    assert row["caller"] == BOSS and row["callee"] == WORKER
    assert row["callee_execution_id"] == child and row["execution_id"] is None
    assert row["policy_source"] == "instance"
    assert T._WAKE._as_utc(row["wait_until"]) > T._WAKE._as_utc(row["read_at"])


# ---------------------------------------------------------------------------
# the backend's own give-up re-opens the wake
# ---------------------------------------------------------------------------

def test_a_sync_long_poll_that_gives_up_moves_the_deadline_before_it_reads_the_row(
        world, monkeypatch):
    """Flip, then read: a terminal that lands in between is at worst a wake
    beside an inline answer, never a lost wake."""
    from services import idempotency_service
    from services.chat_signals import ChatDispatchError

    run = _run_with_terms({
        "declined": False,
        "wait_until": T._iso(datetime.now(timezone.utc) + timedelta(hours=1)),
    })
    order = []
    real_flip, real_read = _WAKE.caller_stopped_waiting, db.get_execution

    def _flip(callee, execution_id):
        order.append("flip")
        return real_flip(callee, execution_id)

    def _read(execution_id):
        order.append("read")
        return real_read(execution_id)

    async def _never(execution_id, timeout):
        raise asyncio.TimeoutError

    monkeypatch.setattr(_CE.caller_wake_service, "caller_stopped_waiting", _flip)
    monkeypatch.setattr(_CE.db, "get_execution", _read)
    monkeypatch.setattr(_CE, "wait_for_sync_terminal", _never)

    async def go():
        with pytest.raises(ChatDispatchError) as refused:
            await _CE._dispatch_sync_backlog(
                name=WORKER, execution_id=run, sync_effective_timeout=1,
                idem=idempotency_service.IdempotencyDecision(
                    enabled=False, replay=False, in_flight=False))
        assert refused.value.status_code == 504
        monkeypatch.setattr(_CE.db, "get_execution", real_read)
        return await _ends(run)

    woken = asyncio.run(go())
    assert order[:2] == ["flip", "read"]
    assert woken, "the handler gave its caller a receipt and the wake was still stood down"
    assert _wake_rows() == 1


def test_the_flip_keeps_a_decline_and_ignores_a_call_without_terms(world):
    declined = _run_with_terms({"declined": True, "wait_until": "2999-01-01T00:00:00.000000Z"})
    bare = T._callee_run()
    _WAKE.caller_stopped_waiting(WORKER, declined)
    _WAKE.caller_stopped_waiting(WORKER, bare)
    terms = _WAKE._call_terms(WORKER, declined)
    assert terms["declined"] is True
    assert _WAKE._as_utc(terms["wait_until"]) <= datetime.now(timezone.utc)
    assert _WAKE._call_terms(WORKER, bare) is None


# ---------------------------------------------------------------------------
# the request models
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [ParallelTaskRequest, ChatMessageRequest])
def test_the_request_models_carry_both_fields(model):
    from pydantic import ValidationError

    plain = model(message="hi")
    assert plain.wake is None and plain.caller_wait_ms is None
    sent = model(message="hi", wake=False, caller_wait_ms=25_000)
    assert sent.wake is False and sent.caller_wait_ms == 25_000
    for bad in (0, -1, 86_400_001):
        with pytest.raises(ValidationError):
            model(message="hi", caller_wait_ms=bad)


# ---------------------------------------------------------------------------
# on real threads (the harness above runs `asyncio.to_thread` inline)
# ---------------------------------------------------------------------------

@pytest.fixture
def threads(world, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", _REAL_TO_THREAD)
    return world


def test_on_a_real_thread_a_declined_call_starts_no_wake(threads, calls):
    async def go():
        child, _answer = await _call(calls, async_mode=True, wake=False)
        return await _ends(child)

    assert asyncio.run(go()) is None
    assert _wake_rows() == 0
    assert _skips(threads) == [("skipped_declined", "caller_declined")]


def test_on_a_real_thread_an_inline_answer_starts_no_wake(threads, calls):
    async def go():
        child, _answer = await _call(calls, async_mode=False, caller_wait_ms=25_000)
        return await _ends(child)

    assert asyncio.run(go()) is None
    assert _skips(threads) == [("skipped_declined", "answered_inline")]


def test_on_real_threads_two_endings_at_once_are_one_wake_and_one_parked(threads):
    """The hold is one database claim; the reads around it now cross threads."""
    async def go():
        threads.gate["event"] = asyncio.Event()
        first, second = T._callee_run(), T._callee_run()
        T._end(first)
        T._end(second)
        a = asyncio.create_task(_WAKE.maybe_wake_caller(first))
        b = asyncio.create_task(_WAKE.maybe_wake_caller(second))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if threads.wakes and (b.done() or a.done()):
                break
        running = len(threads.wakes)
        threads.gate["event"] = None
        for ev in threads.held:
            ev.set()
        await asyncio.gather(a, b)
        while threads.spawned:
            await threads.spawned.pop(0)
        return running

    assert asyncio.run(go()) == 1, "two wakes of one caller ran at the same time"
    assert len(threads.wakes) <= 2
