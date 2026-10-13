"""trinity#3653 — the person who asked sees how a gated request ended.

Dana asks a companion agent in her Workspace chat; the companion calls a gated
skill on another agent; an approver decides. The platform wakes the companion
with the outcome and the companion writes a reply, but the wake-up run carried
no channel context, so the reply reached no conversation.

The fix: `enforce` records the chat of the requesting agent's turn while that
turn is still running, and every wake-up that reports an ENDING runs with that
context, so `channel_completion_report` posts the companion's reply into the
chat. The "approved, now running" wake-up carries none.

Driven over the real per-test database through `enforce`, `ask_service.answer`
and `resolve`. Stubbed: the gate map, the fingerprint read, the approved run,
and `execute_task` (recorded) under the real `_wake_requester_agent`.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401
from test_ent751_gate_dispatch import (  # noqa: E402,F401 — the shared world
    FIN, REQ, _answer, _raise_request, _resolve, gate,
)

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db.write_params import TaskExecutionFields  # noqa: E402
from models import TaskExecutionStatus  # noqa: E402

pytestmark = pytest.mark.unit

_REAL_WAKE = _GATE._wake_requester_agent   # the `gate` fixture replaces it

ORIGIN, SESSION, DANA = "exec-origin", "sess-dana", "dana@example.com"
CHAT = {"source_channel": "portal", "source_channel_chat_id": SESSION,
        "source_channel_client": DANA, "source_channel_agent": REQ}


def _origin(*, agent=REQ, status=TaskExecutionStatus.RUNNING, **fields):
    """The requesting agent's turn: Dana's message, in her Workspace chat."""
    fields = {"source_channel": "portal", "source_channel_chat_id": SESSION,
              "source_channel_client": DANA, **fields}
    db.create_task_execution(agent_name=agent, message="/chase-overdue INV-1001",
                             triggered_by="public", fields=TaskExecutionFields(**fields),
                             execution_id=ORIGIN)
    db.update_execution_status(execution_id=ORIGIN, status=status)


@pytest.fixture
def wake(gate, monkeypatch):
    """The real wake-up over a recorded `execute_task`."""
    calls = []

    class _Service:
        async def execute_task(self, **kw):
            calls.append(kw)

    # Imported here: the database harness drops the module between tests.
    import services.task_execution_service as tes
    monkeypatch.setattr(tes, "get_task_execution_service", lambda: _Service())

    async def _wake(record, text, **kw):
        await _REAL_WAKE(record, text, **kw)
        await asyncio.gather(*list(_GATE._inflight))

    monkeypatch.setattr(_GATE, "_wake_requester_agent", _wake)
    return SimpleNamespace(calls=calls, gate=gate)


def _chat_of(call):
    return {k: v for k, v in call.items() if k.startswith("source_channel") and v is not None}


# --- an ending reaches the chat ------------------------------------------------

def test_a_rejection_wakes_the_requester_in_the_chat_the_request_came_from(wake):
    _origin()
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    (call,) = wake.calls
    assert call["agent_name"] == REQ and call["triggered_by"] == _GATE.SKILL_GATE_TRIGGER
    assert _chat_of(call) == CHAT


def test_approved_and_running_stays_out_of_the_chat_and_the_ending_goes_in(wake):
    _origin()
    rid = _raise_request()
    _answer(rid, _GATE.APPROVE)
    assert _resolve(rid) == "dispatched"
    (approved,) = wake.calls
    assert "is running as execution" in approved["message"]
    assert _chat_of(approved) == {}

    record = db.get_gate_request(rid)
    asyncio.run(_GATE._notify_completion(record, "succeeded", execution_id="exec-run",
                                         detail="Sent."))
    assert _chat_of(wake.calls[-1]) == CHAT


def test_a_thread_and_an_inherited_binding_agent_ride_along(wake):
    _origin(source_channel="slack", source_channel_chat_id="C1", source_channel_thread="171.5",
            source_channel_agent="front-desk", source_channel_client=None)
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert _chat_of(wake.calls[-1]) == {
        "source_channel": "slack", "source_channel_chat_id": "C1",
        "source_channel_thread": "171.5", "source_channel_agent": "front-desk"}


# --- what is never taken as the chat -------------------------------------------

def test_a_turn_that_already_ended_gives_no_chat(wake):
    """An agent can cite any of its past turns as "its own". Only a turn still
    running when the request is made says which conversation is asking."""
    _origin(status=TaskExecutionStatus.SUCCESS)
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert _chat_of(wake.calls[-1]) == {}


def test_another_agents_turn_gives_no_chat(wake):
    _origin(agent=FIN)
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert _chat_of(wake.calls[-1]) == {}


def test_a_turn_with_no_chat_and_a_request_with_no_turn_give_no_chat(wake):
    _origin(source_channel=None, source_channel_chat_id=None, source_channel_client=None)
    rid = _raise_request()
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert _chat_of(wake.calls[-1]) == {}

    rid = _raise_request(_GATE.Requester(kind="agent", key=f"agent:{REQ}", agent_name=REQ),
                         occurrence="no-turn")
    _answer(rid, _GATE.REJECT)
    _resolve(rid)
    assert _chat_of(wake.calls[-1]) == {}


def test_the_chat_is_stored_under_its_own_key_beside_the_frozen_fields(wake):
    """The approved run replays the frozen `source_channel*` keys; the
    requester's chat is the wake-up's address and is none of them."""
    _origin()
    rid = _raise_request()
    frozen = db.get_gate_request(rid)["dispatch"]
    assert frozen[_GATE.REQUESTER_CHAT] == CHAT
    assert not [k for k in frozen if k.startswith("source_channel")]


# --- the row the report reads ----------------------------------------------------

def test_execute_task_writes_the_binding_agent_on_the_row(db_backend, monkeypatch):
    """`report_completion` and `turn_audience` read `source_channel_agent` off
    the row; a keyword `execute_task` accepted and did not persist would leave
    the wake-up reading as a direct client turn."""
    import services.task_execution_service as tes
    seen = {}

    def _create(**kw):
        seen.update(kw)
        raise RuntimeError("stop after the row")

    monkeypatch.setattr(db, "create_task_execution", _create)
    with pytest.raises(RuntimeError):
        asyncio.run(tes.TaskExecutionService().execute_task(
            agent_name=REQ, message="[Trinity] Rejected", triggered_by=_GATE.SKILL_GATE_TRIGGER,
            **CHAT))
    fields = seen["fields"]
    assert {k: getattr(fields, k) for k in CHAT} == CHAT
