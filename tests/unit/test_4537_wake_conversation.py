"""abilityai/trinity#4537 — a wake turn is part of the conversation that
delegated: it knows what was asked, and its reply goes back to that chat.

A person asks an agent in chat to "ask X and tell me what it said once it's
done". The agent delegates asynchronously and replies that it will report back.
The wake turn then ran cut off at both ends: its frame carried only the callee's
ending (the agent had lost the request and its own plan), and its reply stayed
on the execution row (the person never saw it).

Pinned here, against a real database per backend:

* the frame quotes the delegating turn's request and reply, read off the turn
  the callee row links (``continues_execution_id``);
* a wake that delegates again still reads the person's request from the head
  of the chain, and the plan from the turn that delegated last;
* a turn of another agent is never quoted, whatever a row links to;
* the wake row's own terminal writes the reply into the person's chat thread,
  once, and announces it with identifiers only;
* nothing is written for a failed wake, a wake with no link, a turn an agent
  started, or a thread that is not that person's thread of that agent;
* a `/task` turn saved to a chat session names its thread on the row, which is
  what lets the Chat tab (it sends `/task`) receive the reply.

The stubs are the fixture's: the dispatch (it writes the wake row a real
dispatch would), the container probe, the policy and the audit.
"""

from __future__ import annotations

import asyncio
import json
import sys
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
import services.chat_persistence_service as _CHAT  # noqa: E402
from database import db  # noqa: E402
from models import ParallelTaskRequest, TaskExecutionStatus  # noqa: E402

pytestmark = pytest.mark.unit

BOSS, WORKER, OTHER = "zq4537-boss", "zq4537-worker", "zq4537-other"
USER_ID, USER_EMAIL = 7, "owner@example.com"
ASKED = "Ask the worker for the next number and tell me what it said once it's done."
PLAN = "I've asked the worker. When the answer arrives I'll append it to numbers.txt and report."
REPLY = "The worker said 47. I appended it to numbers.txt."


@pytest.fixture
def world(db_backend, monkeypatch):
    wakes, sent = [], []

    async def _dispatch_wake(**kwargs):
        row = db.create_task_execution(
            agent_name=kwargs["agent_name"],
            message=kwargs["message"],
            triggered_by=kwargs["triggered_by"],
            fields=TaskExecutionFields(
                chain_depth=kwargs.get("chain_depth"),
                continues_execution_id=kwargs.get("continues_execution_id"),
            ),
        )
        wakes.append({"id": row.id, "message": kwargs["message"]})
        return SimpleNamespace(execution_id=row.id, status="completed", error=None)

    async def _state(_agent):
        return "running"

    async def _audit(**_kw):
        return None

    def _policy(_agent):
        from services.wake_policy import WakeVerdict
        return WakeVerdict(enabled=True, source="instance")

    class _Manager:
        async def broadcast(self, payload):
            sent.append(json.loads(payload))

    monkeypatch.setattr(_WAKE, "_dispatch_wake", _dispatch_wake)
    monkeypatch.setattr(_WAKE, "_container_state", _state)
    monkeypatch.setattr(_WAKE, "_audit", _audit)
    monkeypatch.setattr(_WAKE, "_resolve_policy", _policy)
    monkeypatch.setattr(_WAKE, "_caller_subscribes_to", lambda *a: False)
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_WAKE, "_wake_budget_redis", lambda: redis)
    monkeypatch.setattr(_CHAT, "_websocket_manager", _Manager())
    return SimpleNamespace(wakes=wakes, sent=sent)


def _finish(execution_id: str, response: str = "", status: str = "success") -> None:
    _hrun(
        "UPDATE schedule_executions SET status=:s, response=:r, "
        "completed_at='2026-11-16T10:00:00.000000Z' WHERE id=:i",
        s=status, r=response, i=execution_id,
    )


def _person_turn(agent: str = BOSS, *, message: str = ASKED, reply: str = PLAN,
                 thread: bool = True, **fields) -> str:
    """The chat turn a person sent, finished, with its thread named on the row."""
    turn = db.create_task_execution(
        agent_name=agent, message=message, triggered_by="chat",
        fields=TaskExecutionFields(source_user_id=USER_ID, **fields),
    ).id
    _finish(turn, reply)
    if thread:
        session = db.get_or_create_chat_session(agent, USER_ID, USER_EMAIL)
        db.set_execution_chat_session(turn, session.id)
    return turn


def _delegated_run(continues: str | None, words: str = "47") -> str:
    run = db.create_task_execution(
        agent_name=WORKER, message="next number", triggered_by="agent",
        fields=TaskExecutionFields(source_agent_name=BOSS, continues_execution_id=continues),
    ).id
    _finish(run, words)
    return run


def _thread_messages(agent: str = BOSS):
    session = db.get_or_create_chat_session(agent, USER_ID, USER_EMAIL)
    return [(m.role, m.content) for m in db.get_chat_messages(session.id)]


async def _wake_for(run_id: str) -> str:
    wake_id = await _WAKE.maybe_wake_caller(run_id)
    assert wake_id, "the delegator was not woken"
    return wake_id


async def _wake_ends(wake_id: str, response: str = REPLY, status: str = "success") -> None:
    """The wake row's own terminal: what every CAS-won writer fans out."""
    _finish(wake_id, response, status)
    await _WAKE.maybe_wake_caller(wake_id)


# --- what the wake knows ----------------------------------------------------

def test_the_frame_carries_the_delegating_turn(world):
    async def scenario():
        await _wake_for(_delegated_run(_person_turn()))

    asyncio.run(scenario())
    frame = world.wakes[0]["message"]
    assert ASKED in frame
    assert PLAN in frame
    assert "posted, verbatim, as your next message" in frame
    # The ending is still what the frame is about.
    assert f"`{WORKER}` has ended" in frame


def test_a_wake_with_no_link_gets_the_frame_it_always_had(world):
    async def scenario():
        _person_turn()
        await _wake_for(_delegated_run(None))

    asyncio.run(scenario())
    frame = world.wakes[0]["message"]
    assert ASKED not in frame and PLAN not in frame
    assert "next message" not in frame


def test_a_second_round_reads_the_request_from_the_head_of_the_chain(world):
    async def scenario():
        first = await _wake_for(_delegated_run(_person_turn()))
        await _wake_ends(first, "47 is appended. Asking for the next one.")
        await _wake_for(_delegated_run(first, words="48"))

    asyncio.run(scenario())
    second = world.wakes[1]["message"]
    assert ASKED in second                                    # the person's request
    assert "47 is appended. Asking for the next one." in second  # the latest plan
    assert PLAN not in second                                 # not the stale one


def test_another_agents_turn_is_never_quoted(world):
    async def scenario():
        foreign = _person_turn(OTHER, message="OTHER'S PRIVATE REQUEST", reply="OTHER'S PLAN")
        await _wake_for(_delegated_run(foreign))

    asyncio.run(scenario())
    frame = world.wakes[0]["message"]
    assert "OTHER'S PRIVATE REQUEST" not in frame and "OTHER'S PLAN" not in frame


def test_a_secret_in_the_request_is_redacted_and_long_text_is_capped(world):
    secret = "sk-ant-api03-" + "A" * 80
    long_request = ("earlier context. " * 400) + f"Use {secret} and report back."

    async def scenario():
        await _wake_for(_delegated_run(_person_turn(message=long_request)))

    asyncio.run(scenario())
    frame = world.wakes[0]["message"]
    assert secret not in frame
    assert "report back." in frame            # the newest words survive the cap
    assert frame.count("earlier context.") < 120


def test_a_link_that_loops_is_bounded(world):
    async def scenario():
        a = db.create_task_execution(
            agent_name=BOSS, message="wake a", triggered_by=_WAKE.TRIGGERED_BY_CALLER_WAKE).id
        b = db.create_task_execution(
            agent_name=BOSS, message="wake b", triggered_by=_WAKE.TRIGGERED_BY_CALLER_WAKE,
            fields=TaskExecutionFields(continues_execution_id=a)).id
        _hrun("UPDATE schedule_executions SET continues_execution_id=:b WHERE id=:a", a=a, b=b)
        _finish(a, "a"); _finish(b, "b")
        await _wake_for(_delegated_run(b))

    asyncio.run(scenario())
    assert "next message" not in world.wakes[0]["message"]   # no person turn at the head


# --- where its reply goes ---------------------------------------------------

def test_the_reply_is_written_to_the_persons_thread_once(world):
    async def scenario():
        wake = await _wake_for(_delegated_run(_person_turn()))
        await _wake_ends(wake)
        await _WAKE.maybe_wake_caller(wake)       # a second terminal fan-out
        return wake

    wake = asyncio.run(scenario())
    assert _thread_messages() == [("assistant", REPLY)]
    assert world.sent == [{
        "type": "chat_response_ready", "execution_id": wake, "agent_name": BOSS,
        "chat_session_id": db.get_or_create_chat_session(BOSS, USER_ID, USER_EMAIL).id,
        "timestamp": world.sent[0]["timestamp"],
    }]
    assert REPLY not in json.dumps(world.sent)    # identifiers only (#918)


def test_a_second_rounds_reply_reaches_the_same_thread(world):
    async def scenario():
        first = await _wake_for(_delegated_run(_person_turn()))
        await _wake_ends(first, "47 is appended. Asking for the next one.")
        second = await _wake_for(_delegated_run(first, words="48"))
        await _wake_ends(second, "48 is appended too.")

    asyncio.run(scenario())
    assert _thread_messages() == [
        ("assistant", "47 is appended. Asking for the next one."),
        ("assistant", "48 is appended too."),
    ]


@pytest.mark.parametrize("status,response", [("failed", REPLY), ("success", "   ")])
def test_a_failed_or_empty_wake_writes_nothing(world, status, response):
    async def scenario():
        wake = await _wake_for(_delegated_run(_person_turn()))
        await _wake_ends(wake, response, status)

    asyncio.run(scenario())
    assert _thread_messages() == [] and world.sent == []


def test_a_wake_with_no_link_writes_nothing(world):
    async def scenario():
        _person_turn()
        wake = await _wake_for(_delegated_run(None))
        await _wake_ends(wake)

    asyncio.run(scenario())
    assert _thread_messages() == []


def test_a_turn_with_no_thread_writes_nothing(world):
    async def scenario():
        wake = await _wake_for(_delegated_run(_person_turn(thread=False)))
        await _wake_ends(wake)

    asyncio.run(scenario())
    assert _thread_messages() == []
    assert "next message" not in world.wakes[0]["message"]


def test_a_turn_an_agent_started_writes_nothing(world):
    async def scenario():
        turn = _person_turn(source_agent_name=OTHER)
        wake = await _wake_for(_delegated_run(turn))
        await _wake_ends(wake)

    asyncio.run(scenario())
    assert _thread_messages() == []


def test_a_thread_of_another_person_or_agent_is_never_written(world):
    async def scenario():
        turn = _person_turn(thread=False)
        theirs = db.get_or_create_chat_session(BOSS, 99, "someone@example.com")
        db.set_execution_chat_session(turn, theirs.id)
        wake = await _wake_for(_delegated_run(turn))
        await _wake_ends(wake)

        turn2 = _person_turn(thread=False)
        elsewhere = db.get_or_create_chat_session(OTHER, USER_ID, USER_EMAIL)
        db.set_execution_chat_session(turn2, elsewhere.id)
        wake2 = await _wake_for(_delegated_run(turn2))
        await _wake_ends(wake2)
        return theirs.id, elsewhere.id

    theirs, elsewhere = asyncio.run(scenario())
    assert db.get_chat_messages(theirs) == []
    assert db.get_chat_messages(elsewhere) == []
    assert world.sent == []


def test_a_write_that_fails_does_not_raise_and_can_be_retried(world, monkeypatch):
    calls = {"n": 0}
    real = db.add_chat_message

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        return real(*a, **kw)

    async def scenario():
        wake = await _wake_for(_delegated_run(_person_turn()))
        monkeypatch.setattr(db, "add_chat_message", _flaky)
        await _wake_ends(wake)                    # fails, swallowed
        assert _thread_messages() == []
        await _WAKE.maybe_wake_caller(wake)       # the claim was released

    asyncio.run(scenario())
    assert _thread_messages() == [("assistant", REPLY)]


# --- the Chat tab's turns name their thread ---------------------------------

def test_a_saved_task_turn_names_its_thread_on_the_row(db_backend):
    turn = db.create_task_execution(agent_name=BOSS, message=ASKED, triggered_by="chat").id
    request = ParallelTaskRequest(message=ASKED, save_to_session=True)
    result = SimpleNamespace(
        status=TaskExecutionStatus.SUCCESS, response=PLAN, execution_id=turn,
        cost=None, context_used=None, context_max=None,
    )
    session_id = asyncio.run(_CHAT.persist_chat_session(
        agent_name=BOSS, request=request, result=result,
        user_id=USER_ID, user_email=USER_EMAIL,
    ))
    assert session_id and db.get_execution_chat_session_id(turn) == session_id


def test_a_link_failure_does_not_lose_the_saved_turn(db_backend, monkeypatch):
    turn = db.create_task_execution(agent_name=BOSS, message=ASKED, triggered_by="chat").id

    def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "set_execution_chat_session", _boom)
    result = SimpleNamespace(
        status=TaskExecutionStatus.SUCCESS, response=PLAN, execution_id=turn,
        cost=None, context_used=None, context_max=None,
    )
    session_id = asyncio.run(_CHAT.persist_chat_session(
        agent_name=BOSS, request=ParallelTaskRequest(message=ASKED, save_to_session=True),
        result=result, user_id=USER_ID, user_email=USER_EMAIL,
    ))
    assert session_id
    assert [(m.role, m.content) for m in db.get_chat_messages(session_id)] == [
        ("user", ASKED), ("assistant", PLAN)]
