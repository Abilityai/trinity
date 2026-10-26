"""trinity-enterprise#701 — the edition-agnostic half of seating a second person.

The seat itself (who may be seated, by whom) is the entitled module's. What
every edition gets, and what this file pins over a real SQLite built from the
platform's own DDL:

* a person's READ FLOOR (`visible_from_seq`): seated mid-conversation, they read
  from their join point unless the floor is moved (a backscroll grant);
* LEAVING: a person who left, or was removed, no longer sees the room — the
  same uniform 404 as never having been in it — and can be seated again;
  a moderator cannot strand others; an agent cannot "leave";
* the room payload names who is reading (`you`) and lists the people, and only
  OFFERS seating to a platform moderator of an open room on an entitled install;
* the agent is told when more than one person is present, who, and what each
  may decide; a seated colleague keeps the room internal (#363);
* an @mention of a seated person reaches them as a tag, never as a wake.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent701.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import metadata as m, agent_ownership, users, schedule_executions
    m.create_all(get_engine(), tables=[agent_ownership, users, schedule_executions])
    from conftest import ensure_schema_tables
    ensure_schema_tables("enterprise_rooms", "enterprise_room_participants", "enterprise_room_messages")

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        for uid, name in ((1, "alice"), (2, "bob"), (3, "carol")):
            conn.execute(insert(users).values(id=uid, username=name, role="operator",
                                              email=f"{name}@example.com",
                                              created_at="t", updated_at="t"))
        for name in ("agent-a", "agent-b"):
            conn.execute(insert(agent_ownership).values(agent_name=name, owner_id=1, created_at="t"))

    import dependencies
    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)
    from shared_sessions import service
    monkeypatch.setattr(service, "_broadcast", lambda *a, **k: None)
    # Who is mid-turn lives in Redis; there is none here, and the client would
    # wait out its connect timeout on every read.
    monkeypatch.setattr(service, "working_agents", lambda room_id, participants: [])
    yield service


def _person(username, uid=None, role="operator"):
    from models import User
    return User(id=uid or {"alice": 1, "bob": 2, "carol": 3}[username], username=username, role=role)


ALICE, BOB, CAROL = _person("alice"), _person("bob"), _person("carol")


def _room(rooms):
    return rooms.create_room(ALICE, "Ops", ["agent-a", "agent-b"])


def _say(rooms, user, room_id, text):
    from utils.helpers import utc_now_iso
    import uuid
    kind, ident = rooms._caller(user)
    return rooms.db.append_message(uuid.uuid4().hex, room_id, kind, ident, text, [], "message",
                                   None, utc_now_iso())


def _seat(rooms, room_id, username, *, floor=None):
    from utils.helpers import utc_now_iso
    if floor is None:
        floor = rooms.db.max_seq(room_id) + 1
    rooms.db.seat_person(room_id, "user", username, rooms.COLLABORATOR_ROLE, floor, utc_now_iso())


def _entitled(monkeypatch, on=True):
    from services.entitlement_service import entitlement_service
    monkeypatch.setattr(entitlement_service, "is_entitled",
                        lambda fid: on and fid == "room_seats", raising=False)


# --- the read floor ---------------------------------------------------------

def test_a_person_seated_mid_conversation_reads_from_their_join_point(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    before = _say(rooms, ALICE, room["id"], "before bob")
    _seat(rooms, room["id"], "bob")
    after = _say(rooms, ALICE, room["id"], "after bob")

    seen = [m["seq"] for m in rooms.get_room(BOB, room["id"])["messages"]]
    assert before not in seen and after in seen
    assert rooms.get_room(BOB, room["id"])["history_from_seq"] == before + 1
    # The creator still reads everything.
    assert before in [m["seq"] for m in rooms.get_room(ALICE, room["id"])["messages"]]


def test_a_backscroll_grant_opens_the_earlier_transcript(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    before = _say(rooms, ALICE, room["id"], "before bob")
    _seat(rooms, room["id"], "bob")
    assert rooms.db.set_visible_from(room["id"], "user", "bob", 0) is True
    data = rooms.get_room(BOB, room["id"])
    assert before in [m["seq"] for m in data["messages"]]
    assert data["history_from_seq"] == 0
    bob = next(p for p in data["people"] if p["identity"] == "bob")
    assert bob["sees_history"] is True


def test_an_incremental_read_respects_the_floor(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _say(rooms, ALICE, room["id"], "one")
    _say(rooms, ALICE, room["id"], "two")
    _seat(rooms, room["id"], "bob")
    later = _say(rooms, ALICE, room["id"], "three")
    seen = [m["seq"] for m in rooms.get_room(BOB, room["id"], since_seq=1)["messages"]]
    assert seen == [later]


def test_seat_person_refuses_an_agent(rooms_db):
    with pytest.raises(ValueError):
        rooms_db.db.seat_person("r", "agent", "agent-a", "member", 0, "t")


# --- leaving ----------------------------------------------------------------

def test_a_person_who_left_no_longer_sees_the_room(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    out = rooms.leave_room(BOB, room["id"])
    assert out["left"] is True

    with pytest.raises(rooms.RoomError) as e:
        rooms.get_room(BOB, room["id"])
    assert e.value.status_code == 404 and e.value.code == "room_not_found"
    with pytest.raises(rooms.RoomError) as e:
        _run(rooms.post_message(BOB, room["id"], "still here?"))
    assert e.value.status_code == 404
    assert room["id"] not in [r["id"] for r in rooms.list_rooms(BOB)["rooms"]]


def test_the_roster_keeps_who_left_and_the_room_says_so(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    rooms.leave_room(BOB, room["id"])
    data = rooms.get_room(ALICE, room["id"])
    bob = next(p for p in data["people"] if p["identity"] == "bob")
    assert bob["left_at"]
    assert [p["identity"] for p in data["people"]] == ["alice", "bob"]   # present first
    assert any(m["content"] == "bob left the room." for m in data["messages"])


def test_a_person_who_left_can_be_seated_again(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    rooms.leave_room(BOB, room["id"])
    _seat(rooms, room["id"], "bob")
    assert rooms.get_room(BOB, room["id"])["id"] == room["id"]
    assert not rooms.db.get_participant(room["id"], "user", "bob")["left_at"]


def test_a_moderator_cannot_strand_the_others(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    with pytest.raises(rooms.RoomError) as e:
        rooms.leave_room(ALICE, room["id"])
    assert e.value.status_code == 409 and e.value.code == "last_moderator"
    rooms.leave_room(BOB, room["id"])
    assert rooms.leave_room(ALICE, room["id"])["left"] is True


def test_an_agent_cannot_leave(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    agent = _person("alice")
    agent = type(agent)(id=1, username="alice", role="operator", agent_name="agent-a")
    with pytest.raises(rooms.RoomError) as e:
        rooms.leave_room(agent, room["id"])
    assert e.value.status_code == 403 and e.value.code == "not_a_person"


def test_a_non_member_gets_the_uniform_404_not_a_leave_answer(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    with pytest.raises(rooms.RoomError) as e:
        rooms.leave_room(CAROL, room["id"])
    assert e.value.status_code == 404


# --- the payload ------------------------------------------------------------

def test_the_payload_names_the_reader(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    assert rooms.get_room(BOB, room["id"])["you"] == {"kind": "user", "identity": "bob"}


def test_seating_is_offered_only_to_a_platform_moderator_of_an_open_entitled_room(rooms_db, monkeypatch):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    _entitled(monkeypatch, on=False)
    assert rooms.get_room(ALICE, room["id"])["can_seat_people"] is False      # not entitled
    _entitled(monkeypatch, on=True)
    assert rooms.get_room(ALICE, room["id"])["can_seat_people"] is True       # moderator
    assert rooms.get_room(BOB, room["id"])["can_seat_people"] is False        # collaborator
    rooms.close_room(ALICE, room["id"])
    assert rooms.get_room(ALICE, room["id"])["can_seat_people"] is False      # closed


def test_an_outside_client_moderator_is_not_offered_seating(rooms_db, monkeypatch):
    rooms = rooms_db
    _entitled(monkeypatch, on=True)
    client = SimpleNamespace(email="client@example.com", is_portal=True, is_platform=False,
                             agent_name=None, role=None)
    import client_portal.service as portal
    monkeypatch.setattr(portal, "agent_on_roster", lambda *a, **k: True)
    room = rooms.create_room(client, "Client room", ["agent-a"])
    assert room["can_seat_people"] is False


# --- what the agent is told ---------------------------------------------------

def _prompt(rooms, participants):
    return rooms._build_turn_prompt({"name": "Ops"}, "agent-a",
                                    [{"sender_kind": "user", "sender_identity": "bob",
                                      "content": "hi", "kind": "message"}],
                                    cold=False, user_facing=False, participants=participants)


def test_the_agent_is_told_when_more_than_one_person_is_present(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    text = _prompt(rooms, rooms.db.list_participants(room["id"]))
    assert "2 people are in this room" in text
    assert "alice (started this room)" in text
    assert "bob (collaborator: can read and talk here, but cannot approve, spend or reassign)" in text
    assert "Approvals and spending still go to your primary" in text
    assert "bob (human): hi" in text                                    # the speaker, per line


def test_one_person_gets_no_people_line(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    assert "people are in this room" not in _prompt(rooms, rooms.db.list_participants(room["id"]))


def test_a_departed_person_is_not_counted(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    rooms.leave_room(BOB, room["id"])
    assert "people are in this room" not in _prompt(rooms, rooms.db.list_participants(room["id"]))


def test_a_seated_colleague_keeps_the_room_internal(rooms_db):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    assert rooms.room_is_user_facing(rooms.db.list_participants(room["id"])) is False


# --- mentions ---------------------------------------------------------------

def test_seated_people_mentioned_picks_only_present_platform_people(rooms_db):
    rooms = rooms_db
    parts = [
        {"kind": "user", "identity": "alice"}, {"kind": "user", "identity": "bob"},
        {"kind": "user", "identity": "dave", "left_at": "t"},
        {"kind": "agent", "identity": "agent-a"},
        {"kind": "workspace_user", "identity": "x@example.com"},
    ]
    text = "@bob and @dave, @agent-a and @alice — @bob again"
    assert rooms.seated_people_mentioned(text, parts, sender_identity="alice") == ["bob"]


def test_mentioning_a_seated_person_tags_them_and_wakes_no_one_new(rooms_db, monkeypatch):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    from services import person_mention_service as pms
    delivered = {}

    def _resolve(raw, agents, tagger):
        return [pms.Person(email=e, username=e.split("@")[0], label=e) for e in raw]

    def _deliver(conv, message_id, seq, people, tagger):
        delivered["people"] = [p.email for p in people]
        return [{"label": p.label, "state": "delivered", "read_at": None} for p in people]

    async def _no_wakes(*a, **k):
        return []

    monkeypatch.setattr(pms, "resolve_tags", _resolve)
    monkeypatch.setattr(pms, "deliver", _deliver)
    monkeypatch.setattr(rooms, "_after_landing", _no_wakes)

    out = _run(rooms.post_message(ALICE, room["id"], "@bob can you look?"))
    assert out["mentions"] == []                          # a person is never a wake target
    assert delivered["people"] == ["bob@example.com"]
    assert [t["state"] for t in out["tags"]] == ["delivered"]


def test_a_mention_that_cannot_be_tagged_never_fails_the_message(rooms_db, monkeypatch):
    rooms = rooms_db
    room = _room(rooms)
    _seat(rooms, room["id"], "bob")
    from services import person_mention_service as pms

    def _refuse(raw, agents, tagger):
        raise pms.TagError(422, "unknown_person", "no")

    async def _no_wakes(*a, **k):
        return []

    monkeypatch.setattr(pms, "resolve_tags", _refuse)
    monkeypatch.setattr(rooms, "_after_landing", _no_wakes)
    out = _run(rooms.post_message(ALICE, room["id"], "@bob ping"))
    assert out["seq"] and out["tags"] == []
