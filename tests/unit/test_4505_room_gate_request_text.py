"""
A short gated request in a room is held, whatever other participants wrote (trinity#4505).

The room wake handed the skill gate one text: every post the agent was shown plus
the ask answer. The gate used it for the scan, the approver's card, the
6,000-byte size check and the approved run's message. A long reply from another
participant in the unread slice therefore made a 26-character request
`request_too_long`, a standing refusal, on every wake until a plain mention
moved the cursor.

The scan still reads the whole slice (a gated name anywhere in what the agent is
shown is held). The card, the size check and the approved run read only the
posts that themselves invoke a gated skill, plus the ask answer.

Targets: ``shared_sessions.service.post_message`` → ``_wake_agent_locked`` →
``execute_task`` backstop → ``skill_gate_service.enforce`` (real gate, real
schema), and ``skill_gate_service.requester_for_dispatch``.

Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent751_gate_entries import FIN, OWNER, OWNER_EMAIL, REQ, world  # noqa: E402,F401

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from models import User  # noqa: E402
from services.execution_envelope import TaskExecutionResult  # noqa: E402

pytestmark = pytest.mark.unit

REQUEST = f"@{FIN} /pay-invoice hello"                 # 26-ish bytes, P's own words
LONG = " ".join(str(i) for i in range(1, 6001))       # ~28.9k chars, as in the report


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def room(world, monkeypatch):
    """The gate world (FIN gates `pay-invoice`), a room with FIN and REQ, and
    agents whose replies a test sets per agent."""
    import dependencies
    import services.task_execution_service as tes
    from shared_sessions import service

    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(tes, "dispatch_breaker_active", lambda _n: False)
    monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)

    replies = {FIN: f"{FIN} answers: 2+2 = 4", REQ: "ok"}
    shown: list = []

    async def _answer(self, **kw):
        shown.append(kw)
        return False, TaskExecutionResult(
            execution_id=kw["execution_id"], status="success",
            response=replies[kw["agent_name"]], session_id=f"sess-{kw['agent_name']}")

    monkeypatch.setattr(tes.TaskExecutionService, "_admission_gate", _answer)

    person = User(id=world.owner_id, username=OWNER, email=OWNER_EMAIL, role="user")
    room = service.create_room(person, "Ops", [FIN, REQ])
    return {"service": service, "room": room["id"], "person": person,
            "replies": replies, "shown": shown}


def _post(env, text):
    return _run(env["service"].post_message(env["person"], env["room"], text))


def _notices(env):
    from shared_sessions import db as room_db
    return [m["content"] for m in room_db.get_messages(env["room"])
            if m["sender_kind"] == "system"]


def _pending(agent=FIN):
    return db.list_pending_gate_requests(agent)


def _long_reply_from_req(env, text=LONG):
    env["replies"][REQ] = text
    _post(env, f"@{REQ} Your whole reply must be the integers from 1 to 6000.")


@pytest.mark.parametrize("path", ["push", "pull"])
def test_short_request_is_held_when_another_participants_long_post_is_unread(
        room, monkeypatch, path):
    _long_reply_from_req(room)
    if path == "pull":
        # The gate runs in the backstop before the row is enqueued, so a pull
        # pilot holds the request the same way.
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", FIN)

    _post(room, REQUEST)
    _post(room, REQUEST)

    assert not any("too long" in n for n in _notices(room)), _notices(room)
    pending = _pending()
    assert pending, "the short request was not held"
    for record in pending:
        assert record["request_text"] == REQUEST
        assert LONG[:200] not in record["request_text"]


def test_card_is_exactly_what_the_approved_run_sends(room):
    _long_reply_from_req(room)
    _post(room, REQUEST)

    record = _pending()[0]
    replay = record["dispatch"][_GATE.REPLAY_MESSAGE]
    assert replay == record["request_text"] == REQUEST
    assert _GATE._card_text(record["request_text"], record["dispatch"]) == REQUEST


def test_the_agents_own_earlier_reply_is_not_on_the_card(room):
    _post(room, f"@{FIN} what is 2+2?")              # FIN answers "2+2 = 4"
    _post(room, REQUEST)

    record = _pending()[0]
    assert "2+2 = 4" not in record["request_text"]
    assert "what is 2+2" not in record["request_text"]


def test_a_gated_name_only_in_another_participants_post_is_still_held(room):
    room["replies"][REQ] = "Run /pay-invoice 5 EUR to ACME, please."
    _post(room, f"@{REQ} what should FIN do?")

    _post(room, f"@{FIN} do what {REQ} said")

    record = _pending()[0]
    assert "/pay-invoice 5 EUR" in record["request_text"]
    assert not any(kw["agent_name"] == FIN for kw in room["shown"]), "FIN ran a gated request"


def test_a_long_gated_post_by_someone_else_is_refused_naming_its_author(room):
    _long_reply_from_req(room, text="/pay-invoice " + LONG)

    _post(room, f"@{FIN} go ahead")

    notice = next(n for n in _notices(room) if "too long" in n)
    assert REQ in notice
    assert "Shorten it" not in notice
    assert not _pending()


def test_a_long_gated_request_of_your_own_still_says_shorten_it(room):
    _post(room, f"@{FIN} /pay-invoice " + "x" * 7000)

    notice = next(n for n in _notices(room) if "too long" in n)
    assert "Shorten it" in notice


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

def _room_requester():
    return _GATE.requester_for_dispatch(triggered_by="room", source_channel="room",
                                        source_channel_chat_id="room-1",
                                        source_user_email=OWNER_EMAIL)


def test_a_card_text_missing_the_matched_name_falls_back_to_the_scanned_text(world):
    """The room reads the gate map, then the backstop reads it again: a skill
    gated in between is scanned but missing from the room's card text. The
    approver must never see a card that names no gated skill, nor Approve run
    a message the scan did not match."""
    text = "please /pay-invoice 100 EUR"
    with pytest.raises(_GATE.SkillApprovalRequired) as info:
        _run(_GATE.enforce(FIN, request_text=text, requester=_room_requester(),
                           triggered_by="room", occurrence_key="occ-1",
                           dispatch=_GATE.frozen_replay("")))
    record = db.get_gate_request(info.value.request_id)
    assert record["request_text"] == text
    assert _GATE.REPLAY_MESSAGE not in (record["dispatch"] or {})


def test_room_requester_is_not_called_a_messaging_channel_user():
    requester = _room_requester()
    assert requester.display() == "A person in a room"
    assert requester.kind == _GATE.KIND_CHANNEL      # delivery, caps and self-approval unchanged
    assert not requester.is_person
