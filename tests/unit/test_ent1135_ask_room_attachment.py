"""An ask raised during a ROOM turn records the room it came from, and the
room shows one line while it is open (abilityai/trinity-enterprise#1135).

ent#734 attaches an addressed ask to the Workspace chat whose turn raised it;
a room turn fell through to the pair's Main, so the room — where the agent
asked, in front of everyone — never learned that an ask was open, let alone
how it ended. The platform knows the raising turn (`X-Trinity-Execution-Id`,
validated as this agent's own) and every room turn's execution row is stamped
`triggered_by="room"`, `source_channel="room"`, `source_channel_chat_id=<room>`
(#2792). So `raise_ask` now also writes `context.workspace_room_id` — a
platform key beside the Main attachment, which is kept — when that execution
is a still-RUNNING room turn of this agent in an open room it is a current
participant of, and posts one system line into that room.

Harness: the ent#611 `ask` fixture over the real per-process SQLite (the
rooms tables are OSS core, ent#443), the REAL `room_for_execution` (the fixture
stubs the Main attachment only), a room created through the real engine, and
execution rows shaped like the ones the room engine stamps. Agent names are
unique per test — the database outlives the test.

Requirement: docs/memory/requirements/security.md §26.12
Related flow: docs/memory/feature-flows/operating-room.md
"""
from __future__ import annotations

import uuid

import pytest

from unit.test_ent611_native_ask import ask, real_db, _body, OWNER  # noqa: F401 — fixtures

import services.operator_queue_service as oqs

pytestmark = pytest.mark.unit

OWNER_USERNAME = "owner-1135"


@pytest.fixture()
def world(ask, real_db, monkeypatch):
    """The `ask` fixture plus a room the agent is a participant of."""
    import dependencies
    from db_models import UserCreate
    from models import User
    from shared_sessions import service as rooms

    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)
    if not real_db.get_user_by_username(OWNER_USERNAME):
        real_db.create_user(UserCreate(username=OWNER_USERNAME, role="user", email=OWNER))
    owner = real_db.get_user_by_username(OWNER_USERNAME)
    person = User(id=owner["id"], username=OWNER_USERNAME, email=OWNER, role="user")

    ask.agent = f"agent-1135-{uuid.uuid4().hex[:8]}"
    ask.other = f"agent-1135-o-{uuid.uuid4().hex[:8]}"
    for name in (ask.agent, ask.other):
        real_db.register_agent_owner(name, OWNER_USERNAME)
    ask.person = person
    ask.rooms = rooms
    ask.room = rooms.create_room(person, "Ops", [ask.agent, ask.other])["id"]
    return ask


def _execution(real_db, agent, *, triggered_by="room", channel="room", chat=None):
    """An execution row shaped like the one the room engine stamps (#2792)."""
    from db.write_params import TaskExecutionFields

    fields = TaskExecutionFields(
        source_channel=channel if chat else None,
        source_channel_chat_id=chat,
        source_channel_client=None,
    )
    return real_db.create_task_execution(agent, "hi", triggered_by=triggered_by, fields=fields).id


def _raise(world, rid, *, agent=None, body=None, **kw):
    return world.svc.raise_ask(agent or world.agent, _body(rid, **(body or {})),
                               raised_by="agent", channel="mcp", **kw)


def _context(world, receipt):
    return world.db.get_operator_queue_item(receipt["id"])["context"] or {}


def _lines(world):
    from shared_sessions import db as room_db
    return [m["content"] for m in room_db.get_messages(world.room) if m["kind"] == "system"]


# ---------------------------------------------------------------------------
# The attachment
# ---------------------------------------------------------------------------

def test_an_ask_from_a_running_room_turn_records_that_room(world, real_db):
    turn = _execution(real_db, world.agent, chat=world.room)
    receipt = _raise(world, "r-1", platform_execution_id=turn)
    ctx = _context(world, receipt)
    assert ctx["workspace_room_id"] == world.room
    # the Main attachment is kept — the Inbox and the pair's Main keep working
    assert ctx["workspace_session_id"] == f"thread-{OWNER}"


def test_a_finished_room_turn_records_no_room(world, real_db):
    turn = _execution(real_db, world.agent, chat=world.room)
    real_db.update_execution_status(turn, "success")
    receipt = _raise(world, "r-2", platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_another_agents_room_turn_records_no_room(world, real_db):
    """The trust bound: the header is the agent's own process's to send, so a
    turn that is not THIS agent's changes nothing (ent#734's bound)."""
    turn = _execution(real_db, world.other, chat=world.room)
    receipt = _raise(world, "r-3", platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_a_one_to_one_portal_turn_records_no_room(world, real_db):
    turn = _execution(real_db, world.agent, triggered_by="public", channel="portal",
                      chat="chat-1135")
    receipt = _raise(world, "r-4", platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_an_agent_that_left_the_room_records_no_room(world, real_db):
    turn = _execution(real_db, world.agent, chat=world.room)
    world.rooms.remove_participant(world.person, world.room, world.agent)
    receipt = _raise(world, "r-5", platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_a_closed_room_records_no_room(world, real_db):
    turn = _execution(real_db, world.agent, chat=world.room)
    world.rooms.close_room(world.person, world.room)
    receipt = _raise(world, "r-5b", platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_a_gate_raise_records_no_room(world, real_db):
    """A gate's ask is a background ask (the ent#610 amendment): it belongs to
    the Inbox only, whatever turn is running."""
    turn = _execution(real_db, world.agent, chat=world.room)
    receipt = world.svc.raise_ask(
        world.agent, _body("gate-r-6", title="Run /pay-invoice?"),
        raised_by="gate", channel="gate", addressee=OWNER, platform_execution_id=turn)
    assert "workspace_room_id" not in _context(world, receipt)


def test_a_credential_ask_records_no_room(world, real_db):
    """A secret has exactly one reader (ent#1066): it never attaches to a room."""
    turn = _execution(real_db, world.agent, chat=world.room)
    receipt = _raise(world, "r-7", platform_execution_id=turn,
                     body={"type": "credential", "options": None, "proposal": None,
                           "context": {"credential_name": "STRIPE_KEY"}})
    assert "workspace_room_id" not in _context(world, receipt)


def test_an_agent_written_room_key_is_stripped(world, real_db):
    """The room is platform-known only: an agent cannot name one (ent#429 rule)."""
    receipt = _raise(world, "r-8", body={"context": {"workspace_room_id": world.room,
                                                     "note": "kept"}})
    ctx = _context(world, receipt)
    assert "workspace_room_id" not in ctx
    assert ctx["note"] == "kept"


def test_a_lookup_failure_never_refuses_the_ask(world, real_db, monkeypatch):
    """Fail-soft, the #1632 clamp contract: the ask stands, on Main."""
    turn = _execution(real_db, world.agent, chat=world.room)

    def _boom(agent, execution_id):
        raise RuntimeError("db gone")

    monkeypatch.setattr(world.rooms, "room_for_execution", _boom)
    receipt = _raise(world, "r-9", platform_execution_id=turn)
    assert receipt["status"] == "created"
    assert "workspace_room_id" not in _context(world, receipt)


# ---------------------------------------------------------------------------
# The raised line
# ---------------------------------------------------------------------------

def test_a_successful_raise_posts_one_line_in_the_room(world, real_db):
    # the addressee is NOT seated in this room: the line names the role only
    world.state["owner"] = "primary-elsewhere-1135@example.com"
    before = len(_lines(world))
    turn = _execution(real_db, world.agent, chat=world.room)
    _raise(world, "r-10", platform_execution_id=turn,
           body={"title": "  Pay   the\nvendor  "})
    new = _lines(world)[before:]
    assert len(new) == 1
    line = new[0]
    assert line.startswith(f"{world.agent} asked: “Pay the vendor” — waiting on ")
    # the role word, never the resolved person's email (privacy rule §4.5)
    assert "primary-elsewhere-1135@example.com" not in line
    assert line.endswith("its primary.")


def test_a_long_title_is_capped_in_the_line(world, real_db, monkeypatch):
    # an operator may raise the agent title cap (env); the line keeps its own
    monkeypatch.setattr(oqs, "OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS", 300)
    turn = _execution(real_db, world.agent, chat=world.room)
    before = len(_lines(world))
    _raise(world, "r-11", platform_execution_id=turn, body={"title": "x" * 119 + "yz"})
    line = _lines(world)[before:][0]
    assert "“" + "x" * 119 + "…”" in line
    assert "yz" not in line


def test_a_seated_person_is_named_in_the_line(world, real_db):
    """The addressee IS a participant of this room: say who, by participant
    identity — that is what every reader of the room already sees."""
    turn = _execution(real_db, world.agent, chat=world.room)
    before = len(_lines(world))
    # OWNER is the room's creator (seated as a `user` participant, identity = username)
    _raise(world, "r-12", platform_execution_id=turn)
    line = _lines(world)[before:][0]
    assert line.endswith(f"waiting on {OWNER_USERNAME}.")


def test_a_refused_raise_leaves_no_trace_in_the_room(world, real_db):
    turn = _execution(real_db, world.agent, chat=world.room)
    before = len(_lines(world))
    world.state["rate_ok"] = False
    with pytest.raises(world.svc.AskRejected) as ei:
        _raise(world, "r-13", platform_execution_id=turn)
    assert ei.value.status_code == 429
    assert _lines(world)[before:] == []


def test_a_raise_with_no_room_posts_no_line(world, real_db):
    before = len(_lines(world))
    _raise(world, "r-14")
    assert _lines(world)[before:] == []


def test_a_failed_line_never_fails_the_ask(world, real_db, monkeypatch):
    turn = _execution(real_db, world.agent, chat=world.room)

    def _boom(*a, **k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(world.rooms, "_post_system", _boom)
    receipt = _raise(world, "r-15", platform_execution_id=turn)
    assert receipt["status"] == "created"
    assert _context(world, receipt)["workspace_room_id"] == world.room


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

def test_the_room_key_is_a_platform_key():
    """Stripped at both ingestion boundaries and ignored by replay `differs`,
    like every key the platform writes and no agent may author."""
    assert oqs._WORKSPACE_ROOM_KEY == "workspace_room_id"
    assert oqs._WORKSPACE_ROOM_KEY in oqs._PLATFORM_CONTEXT_KEYS


def test_room_for_execution_requires_a_running_room_turn_of_this_agent(world, real_db):
    """The helper at its own layer: each condition alone denies."""
    rooms = world.rooms
    turn = _execution(real_db, world.agent, chat=world.room)
    assert rooms.room_for_execution(world.agent, turn) == world.room
    assert rooms.room_for_execution(world.other, turn) is None
    assert rooms.room_for_execution(world.agent, "no-such-execution") is None
    # a room turn whose stamped room the agent is not in
    foreign = _execution(real_db, world.agent, chat="room-not-mine")
    assert rooms.room_for_execution(world.agent, foreign) is None
    # a `room` trigger under another channel is not a room turn
    odd = _execution(real_db, world.agent, channel="portal", chat=world.room)
    assert rooms.room_for_execution(world.agent, odd) is None
