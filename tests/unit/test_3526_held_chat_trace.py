"""
A gated request held from the Chat tab stays in the conversation (trinity#3526).

Targets, each through its own layer:
  * ``/task`` → ``chat_execution_service.dispatch_parallel_task`` — the held
    request and its notice are written to the chat session, once per request;
  * ``skill_gate_service.enforce`` — a person re-sending the same request while
    one is pending gets that request back;
  * ``chat_persistence_service.session_messages_view`` — the session read
    carries the request's current state;
  * ``routers.voice._build_context_summary`` — a held request stays out of the
    voice context.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import count as _count, db_backend  # noqa: E402,F401

# Sibling import (the unit dir is not implicitly importable) — reuse the gate
# entry-point harness rather than fork it.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent751_gate_entries import (  # noqa: E402,F401 — fixtures
    FIN, OWNER_EMAIL, REQ, _agent, world,
)

import services.chat_execution_service as _CE  # noqa: E402
import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import ParallelTaskRequest, User  # noqa: E402
from services import chat_persistence_service as _PERSIST  # noqa: E402

pytestmark = pytest.mark.unit

SENDER, SENDER_EMAIL = "gate-sender", "sender-3526@example.com"
TEXT = "/pay-invoice 100 EUR"


@pytest.fixture
def sender(world):
    """A signed-in person who can chat with FIN and is not its approver."""
    db.create_user(UserCreate(username=SENDER, role="user", email=SENDER_EMAIL))
    row = db.get_user_by_username(SENDER)
    return User(id=row["id"], username=SENDER, email=SENDER_EMAIL, role="user")


def _send(principal, message=TEXT, **kw):
    """One Chat-tab send: no Idempotency-Key, the session saved."""
    kw.setdefault("save_to_session", True)
    kw.setdefault("user_message", message)
    kw.setdefault("create_new_session", True)
    kw.setdefault("async_mode", True)
    return asyncio.run(_CE.dispatch_parallel_task(
        request=ParallelTaskRequest(message=message, **kw),
        name=FIN, current_user=principal, container=SimpleNamespace(status="running"),
        x_source_agent=None, x_via_mcp=None, idempotency_key=None,
        x_event_trigger=None, x_internal_secret=None))


def _held(principal, **kw):
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        _send(principal, **kw)
    return info.value


def _messages(session_id):
    return [(m.role, m.content, m.source) for m in db.get_chat_messages(session_id)]


# ---------------------------------------------------------------------------
# The held request is kept
# ---------------------------------------------------------------------------

def test_a_held_chat_tab_request_and_its_notice_are_saved_to_the_session(sender):
    held = _held(sender)
    sid = held.detail()["chat_session_id"]
    assert sid
    marker = f"gate:{held.request_id}"
    assert _messages(sid) == [("user", TEXT, marker), ("system", held.message, marker)]
    assert db.get_chat_session(sid).user_id == sender.id


def test_the_saved_request_is_the_users_own_words_not_the_context_prompt(sender):
    held = _held(sender, message=f"### Current message:\n\nUser: {TEXT}", user_message=TEXT)
    assert _messages(held.detail()["chat_session_id"])[0][:2] == ("user", TEXT)


def test_a_request_not_saved_to_a_session_writes_nothing(sender):
    held = _held(sender, save_to_session=False)
    assert "chat_session_id" not in held.detail()
    assert _count("chat_messages") == 0
    assert _count("chat_sessions") == 0


def test_a_foreign_chat_session_id_falls_back_to_the_senders_own_session(world, sender):
    other = db.create_new_chat_session(agent_name=FIN, user_id=world.owner_id, user_email=OWNER_EMAIL)
    held = _held(sender, create_new_session=False, chat_session_id=other.id)
    sid = held.detail()["chat_session_id"]
    assert sid != other.id
    assert db.get_chat_session(sid).user_id == sender.id
    assert _messages(other.id) == []


def test_a_failed_save_still_answers_pending(sender, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_chat_message", _boom)
    held = _held(sender)
    assert held.detail()["status"] == "pending_approval"
    assert "chat_session_id" not in held.detail()
    assert db.count_pending_gate_requests(FIN) == 1


# ---------------------------------------------------------------------------
# Re-sending while one is pending
# ---------------------------------------------------------------------------

def test_a_person_resending_the_same_request_gets_the_pending_one_back(sender):
    first = _held(sender)
    again = _held(sender)
    third = _held(sender)
    assert {first.request_id, again.request_id, third.request_id} == {first.request_id}
    assert db.count_pending_gate_requests(FIN) == 1
    assert _count("operator_queue") == 1
    # ...and the conversation holds the pair once, in the one session.
    sid = first.detail()["chat_session_id"]
    assert again.detail()["chat_session_id"] == sid == third.detail()["chat_session_id"]
    assert len(_messages(sid)) == 2
    assert _count("chat_sessions") == 1


def test_a_resend_spends_no_rate_token_and_passes_the_pending_cap(sender, monkeypatch):
    first = _held(sender)
    calls = []
    from services.rate_limiter import RateLimitResult

    def _check(*a, **k):
        calls.append(a)
        return RateLimitResult(False, 10, 0, 60)

    monkeypatch.setattr(_GATE.rate_limiter, "check", _check)
    monkeypatch.setattr(_GATE, "MAX_PENDING_PER_REQUESTER", 1)
    assert _held(sender).request_id == first.request_id
    assert calls == []


def test_a_different_request_from_the_same_person_is_its_own_approval(sender):
    first = _held(sender)
    other = _held(sender, message="/pay-invoice 250 EUR")
    assert other.request_id != first.request_id
    assert db.count_pending_gate_requests(FIN) == 2


def test_the_same_text_after_a_decision_raises_a_fresh_approval(sender):
    first = _held(sender)
    assert db.transition_gate_request(first.request_id, "denied")
    second = _held(sender)
    assert second.request_id != first.request_id
    assert db.get_gate_request(second.request_id)["state"] == "pending"


def test_another_person_sending_the_same_text_is_not_merged(world, sender):
    db.create_user(UserCreate(username="gate-sender-2", role="user", email="sender2-3526@example.com"))
    row = db.get_user_by_username("gate-sender-2")
    other = User(id=row["id"], username="gate-sender-2", email="sender2-3526@example.com", role="user")
    assert _held(sender).request_id != _held(other).request_id


def test_an_agent_repeating_a_keyless_request_still_raises_each_one(world):
    ids = {_held(_agent(world), save_to_session=False).request_id for _ in range(3)}
    assert len(ids) == 3


def test_the_same_text_with_a_different_model_is_its_own_approval(sender):
    """The approved run uses the frozen model, so a re-send that changed it is
    not the request already waiting."""
    first = _held(sender, model="sonnet")
    assert _held(sender, model="sonnet").request_id == first.request_id
    assert _held(sender, model="opus").request_id != first.request_id


def test_an_agent_keys_held_request_is_not_saved_to_its_owners_conversation(world):
    held = _held(_agent(world))          # save_to_session=True, an agent principal
    assert "chat_session_id" not in held.detail()
    assert _count("chat_messages") == 0


# ---------------------------------------------------------------------------
# The decision, read with the conversation
# ---------------------------------------------------------------------------

def _view(sid):
    return _PERSIST.session_messages_view(FIN, db.get_chat_messages(sid))


def test_the_session_read_marks_a_waiting_request_pending(sender):
    held = _held(sender)
    user_row, notice = _view(held.detail()["chat_session_id"])
    assert user_row["gate"] == notice["gate"] == {
        "request_id": held.request_id, "state": "pending", "text": None}


@pytest.mark.parametrize("state, says", [
    ("denied", "was rejected; nothing was run"),
    ("expired", "expired before anyone decided"),
    ("cancelled", "was cancelled; nothing was run"),
])
def test_the_session_read_carries_the_decision(sender, state, says):
    held = _held(sender)
    assert db.transition_gate_request(held.request_id, state)
    gate = _view(held.detail()["chat_session_id"])[1]["gate"]
    assert gate["state"] == state
    assert says in gate["text"] and held.request_id in gate["text"]
    assert OWNER_EMAIL not in gate["text"]          # never who decided


def test_an_approved_request_names_the_run_it_started(sender):
    held = _held(sender)
    assert db.claim_gate_request_for_dispatch(held.request_id, "exec-3526-run")
    assert _view(held.detail()["chat_session_id"])[1]["gate"]["state"] == "pending"   # not started yet
    assert db.transition_gate_request(held.request_id, "dispatched")
    gate = _view(held.detail()["chat_session_id"])[1]["gate"]
    assert gate["state"] == "approved"
    assert "is running as execution exec-3526-run" in gate["text"]


def test_an_ordinary_message_carries_no_gate_field(sender):
    session = db.create_new_chat_session(agent_name=FIN, user_id=sender.id, user_email=SENDER_EMAIL)
    db.add_chat_message(session.id, FIN, sender.id, SENDER_EMAIL, "user", "hello")
    (row,) = _view(session.id)
    assert "gate" not in row and row["content"] == "hello"


def test_a_marker_naming_another_agents_request_is_not_resolved(sender):
    held = _held(sender)
    sid = held.detail()["chat_session_id"]
    rows = _PERSIST.session_messages_view("some-other-agent", db.get_chat_messages(sid))
    assert rows[1]["gate"] == {"request_id": held.request_id, "state": "not_found", "text": None}


def test_the_session_route_returns_the_decision(sender, monkeypatch):
    from routers import chat as chat_router

    held = _held(sender)
    assert db.transition_gate_request(held.request_id, "denied")
    monkeypatch.setattr(chat_router, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    body = asyncio.run(chat_router.get_chat_session_detail(
        session_id=held.detail()["chat_session_id"], limit=100, name=FIN, current_user=sender))
    assert [m["gate"]["state"] for m in body["messages"]] == ["denied", "denied"]
    assert "was rejected" in body["messages"][1]["gate"]["text"]


def test_the_pull_pilot_history_leaves_a_held_request_out(sender, monkeypatch):
    from routers import chat as chat_router

    held = _held(sender)
    sid = held.detail()["chat_session_id"]
    db.add_chat_message(sid, FIN, sender.id, SENDER_EMAIL, "user", "what is overdue?")
    monkeypatch.setattr(chat_router, "get_agent_container", lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(chat_router, "is_pull_pilot_agent", lambda name: True)
    history = asyncio.run(chat_router.get_agent_chat_history(name=FIN, current_user=sender))
    assert [(m["role"], m["content"]) for m in history] == [("user", "what is overdue?")]


# ---------------------------------------------------------------------------
# Voice context
# ---------------------------------------------------------------------------

def test_the_voice_context_leaves_a_held_request_out(sender):
    from routers import voice

    held = _held(sender)
    sid = held.detail()["chat_session_id"]
    db.add_chat_message(sid, FIN, sender.id, SENDER_EMAIL, "user", "what is overdue?")
    summary = voice._build_context_summary(sid)
    assert summary == "User: what is overdue?"


def test_a_staged_secret_in_a_held_request_is_not_stored(sender, monkeypatch):
    monkeypatch.setattr(_PERSIST, "get_staged_values", lambda: ["s3cr3t-staged-value"])
    held = _held(sender, message=f"{TEXT} with s3cr3t-staged-value", user_message=None)
    stored = _messages(held.detail()["chat_session_id"])[0][1]
    assert "s3cr3t-staged-value" not in stored and TEXT in stored
