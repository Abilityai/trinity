"""trinity-enterprise#631 — tagging a person in a conversation reaches their Inbox.

A person tag is a POINTER, not a seat: naming a colleague writes ONE item into
the queue-item ledger (the store behind the Inbox's four doors, ent#610),
addressed to them, kind *Unread* — never *Action* — carrying who tagged them,
which conversation and which message. It grants no membership and no access.

Every test runs against a real SQLite file built by the platform's own
`init_schema`, because the properties under test live in the rows: one row per
(message, person), the row's type and status, what it is addressed to, and the
operator door's predicate that keeps it out of everyone else's queue.

1. a tag in a room creates exactly one Unread item for the person;
2. naming the same person twice in one message is one item, and a re-delivery
   of the same message is still one item;
3. an unknown name is refused BY NAME, with a reason, before anything is written;
4. an agent's wake list is unchanged when a person is named — people are never
   wake targets, and the message's stored `mentions` stay agent-only;
5. a reader who cannot see the conversation is told so (and who can let them
   in), never shown its content;
6. an agent cannot tag a person, on any seam;
7. the tagger sees delivered, then read;
8. the item never reaches the operator door (a pointer addressed to a person
   is not anyone else's queue).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"     # the tagger — owns the agent
BOB = "bob@example.com"         # shared the agent, not in the room
CAROL = "carol@example.com"     # an instance admin
DAVE = "dave@example.com"       # a platform user with no access to the agent
ERIN = "erin@example.com"       # shared the agent, but suspended
AGENT = "scout"
OTHER_AGENT = "scribe"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def mention_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent631.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.schema import init_schema

    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()

    from db.engine import get_engine
    from sqlalchemy import text
    eng = get_engine()
    with eng.begin() as conn:
        people = [
            (1, "alice", ALICE, "Alice Archer", "user", None),
            (2, "bob", BOB, "Bob Baker", "user", None),
            (3, "carol", CAROL, "Carol Chen", "admin", None),
            (4, "dave", DAVE, "Dave Dune", "user", None),
            (5, "erin", ERIN, "Erin Eagle", "user", "2026-09-01T00:00:00Z"),
        ]
        for uid, username, email, name, role, suspended in people:
            conn.execute(text(
                "INSERT INTO users (id, username, email, name, role, created_at, updated_at, suspended_at) "
                "VALUES (:id, :u, :e, :n, :r, 't', 't', :s)"
            ), {"id": uid, "u": username, "e": email, "n": name, "r": role, "s": suspended})
        for agent in (AGENT, OTHER_AGENT):
            conn.execute(text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) VALUES (:a, 1, 't')"
            ), {"a": agent})
        for email in (BOB, ERIN):
            conn.execute(text(
                "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
                "VALUES (:a, :e, 1, 't')"
            ), {"a": AGENT, "e": email})
    yield eng


@pytest.fixture()
def quiet_room(monkeypatch):
    """No WebSocket, no real agent turns: record who would have been woken."""
    from shared_sessions import service
    woke = []

    async def _wake(current_user, room_id, agent_name, chain_depth):
        woke.append(agent_name)

    monkeypatch.setattr(service, "_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(service, "_wake_agent", _wake)
    return woke


def _alice():
    from models import User
    return User(id=1, username="alice", role="user", email=ALICE)


def _agent_key(agent_name=AGENT):
    from models import User
    return User(id=1, username="alice", role="user", email=ALICE, agent_name=agent_name)


def _room(agents=(AGENT,), name="Pricing review"):
    from shared_sessions import service
    return service.create_room(_alice(), name, list(agents))


def _rows(engine):
    from sqlalchemy import text
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT * FROM operator_queue WHERE type = 'mention' ORDER BY created_at"
        )).mappings()]


def _post(room_id, content, tags=None, user=None):
    from shared_sessions import service
    return _run(service.post_message(user or _alice(), room_id, content, tags=tags))


def _reader(email, is_platform=True):
    from services import person_mention_service as pms
    return pms.Reader(email=email, is_platform=is_platform)


# ===========================================================================
# 1. One Unread item, carrying who, which conversation, which message
# ===========================================================================

def test_tagging_a_person_in_a_room_creates_exactly_one_unread_inbox_item(mention_db, quiet_room):
    room = _room()
    out = _post(room["id"], "@Bob Baker can you check the margin?", tags=[BOB])

    rows = _rows(mention_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "mention"
    assert row["addressed_to_email"] == BOB
    # Unread, never Action: the row is NOT pending, so nothing counts it as
    # something blocked on a person, and no ending machinery can reach it.
    assert row["status"] == "delivered"
    ctx = json.loads(row["context"])["mention"]
    assert ctx["tagged_by"]["label"] == "Alice Archer"
    assert ctx["conversation"] == {"kind": "room", "id": room["id"], "label": "Pricing review"}
    assert ctx["seq"] == out["seq"]
    assert ctx["message_id"]

    from services import person_mention_service as pms
    items = pms.list_for_reader(_reader(BOB))
    assert [i.state for i in items] == ["unread"]
    assert items[0].tagged_by == "Alice Archer"
    assert items[0].conversation.kind == "room"
    assert items[0].conversation.label == "Pricing review"
    assert out["tags"] == [{"label": "Bob Baker", "state": "delivered", "read_at": None}]


def test_the_item_is_not_an_ask_so_action_never_lists_it(mention_db, quiet_room):
    """The Workspace asks read (Action) shows only question/approval/alert —
    a tag asks for attention, not an answer."""
    from client_portal.asks import service as asks_service
    room = _room()
    _post(room["id"], "@Bob Baker fyi", tags=[BOB])
    assert "mention" not in asks_service._VISIBLE_KINDS
    page = asks_service.list_asks_page(BOB, True, None, include_ended=True)
    assert all(a.kind != "mention" for a in page.items)


# ===========================================================================
# 2. Bounded and idempotent
# ===========================================================================

def test_the_same_person_twice_in_one_message_is_one_item(mention_db, quiet_room):
    room = _room()
    out = _post(room["id"], "@Bob Baker and again @Bob Baker", tags=[BOB, BOB.upper(), f" {BOB} "])
    assert len(_rows(mention_db)) == 1
    assert len(out["tags"]) == 1


def test_redelivering_the_same_message_is_still_one_item(mention_db, quiet_room):
    from services import person_mention_service as pms
    room = _room()
    out = _post(room["id"], "@Bob Baker look", tags=[BOB])
    row = _rows(mention_db)[0]
    ctx = json.loads(row["context"])["mention"]
    conv = pms.Conversation(kind="room", id=room["id"], agent_name=AGENT, label="Pricing review")
    tagger = pms.tagger_for(_alice())
    people = pms.resolve_tags([BOB], [AGENT], tagger)
    again = pms.deliver(conv, ctx["message_id"], out["seq"], people, tagger)
    assert len(_rows(mention_db)) == 1
    assert again[0]["state"] == "delivered"


def test_too_many_people_in_one_message_is_refused_named(mention_db, quiet_room):
    from services import person_mention_service as pms
    from shared_sessions import service
    room = _room()
    tags = [f"p{i}@example.com" for i in range(pms.MAX_TAGS_PER_MESSAGE + 1)]
    with pytest.raises(service.RoomError) as ei:
        _post(room["id"], "everyone", tags=tags)
    assert ei.value.status_code == 422
    assert ei.value.code == "too_many_tags"


# ===========================================================================
# 3. An unknown name is refused by name, with a reason, before any write
# ===========================================================================

def test_an_unknown_person_is_refused_by_name_with_a_reason(mention_db, quiet_room):
    from shared_sessions import service, db as rdb
    room = _room()
    before = rdb.count_messages(room["id"])
    with pytest.raises(service.RoomError) as ei:
        _post(room["id"], "@Zed hello", tags=["zed@example.com"])
    err = ei.value
    assert err.status_code == 422
    assert err.code == "unknown_person"
    assert err.extra["name"] == "zed@example.com"
    assert "zed@example.com" in err.detail and "scout" in err.detail   # the reason names who can be tagged
    # Refused before anything was written — no message, no item.
    assert rdb.count_messages(room["id"]) == before
    assert _rows(mention_db) == []


def test_a_real_person_with_no_access_to_the_agents_is_refused_the_same_way(mention_db, quiet_room):
    """Dave exists but cannot reach the room's agents: the same refusal as a
    name that does not exist, so tagging is not an oracle for who has an account."""
    from shared_sessions import service
    room = _room()
    with pytest.raises(service.RoomError) as ei_dave:
        _post(room["id"], "hi", tags=[DAVE])
    with pytest.raises(service.RoomError) as ei_zed:
        _post(room["id"], "hi", tags=["zed@example.com"])
    assert (ei_dave.value.status_code, ei_dave.value.code) == (ei_zed.value.status_code, ei_zed.value.code)


def test_a_suspended_account_is_not_taggable(mention_db, quiet_room):
    from shared_sessions import service
    room = _room()
    with pytest.raises(service.RoomError) as ei:
        _post(room["id"], "hi", tags=[ERIN])
    assert ei.value.code == "unknown_person"


def test_the_picker_suggests_only_people_who_exist_and_can_reach_the_agents(mention_db, quiet_room):
    from shared_sessions import service
    room = _room()
    found = service.room_people(_alice(), room["id"], "b")
    assert [p["email"] for p in found] == [BOB]
    assert found[0]["label"] == "Bob Baker"
    everyone = {p["email"] for p in service.room_people(_alice(), room["id"], "e")}
    # bob (shared), carol (admin) — never dave (no access), erin (suspended),
    # or the tagger herself.
    assert DAVE not in everyone and ERIN not in everyone and ALICE not in everyone
    assert CAROL in service.room_people(_alice(), room["id"], "car")[0]["email"]


def test_the_picker_needs_a_query(mention_db, quiet_room):
    """Bounded (#450 pattern): no bare listing of every colleague."""
    from shared_sessions import service
    room = _room()
    assert service.room_people(_alice(), room["id"], "") == []


# ===========================================================================
# 4. The agent wake is unchanged; mentions are stored, distinguished
# ===========================================================================

def test_an_agents_wake_list_is_unchanged_when_a_person_is_named(mention_db, quiet_room):
    from shared_sessions import service, db as rdb
    room = _room(agents=(AGENT, OTHER_AGENT))
    out = _post(room["id"], f"@{AGENT} draft it, and @Bob Baker please review", tags=[BOB])
    participants = rdb.list_participants(room["id"])
    expected = service.resolve_mentions(f"@{AGENT} draft it, and @Bob Baker please review", participants)
    assert out["mentions"] == expected == [AGENT]
    assert quiet_room == [AGENT]               # bob is never a wake target
    stored = [m for m in rdb.get_messages(room["id"]) if m["seq"] == out["seq"]][0]
    assert stored["mentions"] == [AGENT]       # the agent wake, stored as before
    # …and the person pointer is stored apart from it, keyed to the message.
    tags = service.tags_by_message(room["id"], [stored["id"]])
    assert [t["label"] for t in tags[stored["id"]]] == ["Bob Baker"]


def test_a_person_named_without_a_tag_is_still_plain_text(mention_db, quiet_room):
    """No tag list → no item. Typing a name is not a tag; picking it is."""
    room = _room()
    out = _post(room["id"], "@Bob Baker hello")
    assert _rows(mention_db) == []
    assert out.get("tags") == []


# ===========================================================================
# 5. What the tagged person sees — nothing they are not entitled to
# ===========================================================================

def test_a_reader_who_cannot_see_the_room_is_told_so_not_shown_it(mention_db, quiet_room):
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "SECRET-MARGIN-42 — @Bob Baker take a look", tags=[BOB])
    item = pms.list_for_reader(_reader(BOB))[0]
    detail = pms.open_for_reader(_reader(BOB), item.id)
    assert detail.can_see is False
    assert detail.message is None
    assert detail.context == []
    assert detail.conversation.id is None            # no room id to a non-member
    assert detail.can_let_you_in == ["Alice Archer"]  # the room's moderator
    assert "SECRET-MARGIN-42" not in detail.model_dump_json()


def test_a_reader_who_can_see_the_room_gets_the_message_with_context(mention_db, quiet_room):
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "earlier line")
    _post(room["id"], "SECRET-MARGIN-42 — @Carol Chen take a look", tags=[CAROL])
    item = pms.list_for_reader(_reader(CAROL))[0]
    detail = pms.open_for_reader(_reader(CAROL), item.id)     # an admin sees every room
    assert detail.can_see is True
    assert "SECRET-MARGIN-42" in detail.message.content
    assert detail.message.sender_label == "Alice Archer"
    assert any(m.content == "earlier line" for m in detail.context)
    assert detail.conversation.id == room["id"]


def test_the_admin_bypass_is_the_platform_doors_only(mention_db, quiet_room):
    """#78: a portal session for the admin's address is not the admin."""
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "SECRET-MARGIN-42 @Carol Chen", tags=[CAROL])
    item = pms.list_for_reader(_reader(CAROL, is_platform=False))[0]
    detail = pms.open_for_reader(_reader(CAROL, is_platform=False), item.id)
    assert detail.can_see is False
    assert "SECRET-MARGIN-42" not in detail.model_dump_json()


def test_a_portal_session_does_not_carry_a_platform_members_room_access(mention_db, quiet_room):
    """Review: "could they ALREADY see it" is the room's own membership rule,
    per door. A platform member is a `user` participant; a Workspace session
    for the same address (including one a portal_delegate key minted) is a
    `workspace_user` and is a 404 on that room — so it is not shown it here
    either. The platform door still is."""
    from shared_sessions import db as rdb
    from services import person_mention_service as pms
    from utils.helpers import utc_now_iso
    room = _room()
    rdb.add_participant(room["id"], "user", "bob", "member", utc_now_iso())
    _post(room["id"], "SECRET-MARGIN-42 @Bob Baker", tags=[BOB])
    item = pms.list_for_reader(_reader(BOB))[0]
    portal = pms.open_for_reader(_reader(BOB, is_platform=False), item.id)
    assert portal.can_see is False
    assert "SECRET-MARGIN-42" not in portal.model_dump_json()
    assert pms.open_for_reader(_reader(BOB), item.id).can_see is True


def test_internal_only_facts_never_reach_the_reader(mention_db, quiet_room):
    """#78: no cost, no execution id — even to a reader who may see the room."""
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "@Carol Chen", tags=[CAROL])
    item = pms.list_for_reader(_reader(CAROL))[0]
    blob = pms.open_for_reader(_reader(CAROL), item.id).model_dump_json()
    for key in ("execution_id", "cost", "addressed_to_email", "request_id"):
        assert key not in blob


def test_someone_elses_item_is_a_uniform_404(mention_db, quiet_room):
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "@Bob Baker", tags=[BOB])
    item = pms.list_for_reader(_reader(BOB))[0]
    with pytest.raises(pms.TagError) as ei:
        pms.open_for_reader(_reader(DAVE), item.id)
    with pytest.raises(pms.TagError) as ei2:
        pms.open_for_reader(_reader(DAVE), "no-such-id")
    assert (ei.value.status_code, ei.value.code) == (ei2.value.status_code, ei2.value.code) == (404, "not_found")


def test_a_chat_tag_says_whose_private_chat_it_is(mention_db):
    """A 1:1 chat is its owner's alone: the tagged person is pointed at the
    person who can share it, never shown it."""
    from services import person_mention_service as pms
    tagger = pms.tagger_for_portal(pms.Reader(email=ALICE, is_platform=True), is_person=True)
    people = pms.resolve_tags([BOB], [AGENT], tagger)
    conv = pms.Conversation(kind="chat", id="sess-1", agent_name=AGENT, label=f"a chat with {AGENT}")
    pms.deliver(conv, "msg-1", None, people, tagger)
    item = pms.list_for_reader(_reader(BOB))[0]
    detail = pms.open_for_reader(_reader(BOB), item.id)
    assert detail.conversation.kind == "chat"
    assert detail.can_see is False and detail.message is None
    assert detail.can_let_you_in == ["Alice Archer"]


# ===========================================================================
# 6. Agents cannot tag people
# ===========================================================================

def test_an_agent_authored_message_cannot_create_a_person_tag(mention_db, quiet_room):
    from shared_sessions import service
    room = _room()
    with pytest.raises(service.RoomError) as ei:
        _post(room["id"], "@Bob Baker look", tags=[BOB], user=_agent_key())
    assert ei.value.status_code == 403
    assert ei.value.code == "agents_cannot_tag"
    assert _rows(mention_db) == []


def test_an_agent_key_cannot_list_people_to_tag(mention_db, quiet_room):
    """The room picker's gate (the route census lists it as DELEGATED here):
    an agent key — which resolves to its owner — never reads the people list."""
    from shared_sessions import service
    room = _room()
    with pytest.raises(service.RoomError) as ei:
        service.room_people(_agent_key(), room["id"], "b")
    assert (ei.value.status_code, ei.value.code) == (403, "agents_cannot_tag")


def test_an_agent_cannot_mint_a_mention_row_through_its_queue_file(mention_db):
    """The id prefix is platform-reserved, so an agent's operator-queue file
    cannot forge (or pre-suppress) a person tag."""
    from services import operator_queue_service as oqs
    from services import person_mention_service as pms
    assert oqs.is_platform_minted(f"{pms.MENTION_ID_PREFIX}abc")
    assert oqs.is_about_a_person({"request_id": f"{pms.MENTION_ID_PREFIX}abc"})


def test_an_external_workspace_client_cannot_tag(mention_db, quiet_room):
    from shared_sessions import service
    from shared_sessions.router import WorkspacePrincipal
    with pytest.raises(service.RoomError) as ei:
        service._tagger(WorkspacePrincipal(email="client@outside.example", is_platform=False))
    assert ei.value.status_code == 403
    assert ei.value.code == "tagging_unavailable"


# ===========================================================================
# 7. The tagger sees that it landed, and that it was read
# ===========================================================================

def test_the_tagger_sees_delivered_then_read(mention_db, quiet_room):
    from services import person_mention_service as pms
    from shared_sessions import service
    room = _room()
    out = _post(room["id"], "@Bob Baker", tags=[BOB])

    def _state():
        seen = service.get_room(_alice(), room["id"])
        msg = [m for m in seen["messages"] if m["seq"] == out["seq"]][0]
        return [(t["label"], t["state"]) for t in msg["tags"]]

    assert _state() == [("Bob Baker", "delivered")]
    item = pms.list_for_reader(_reader(BOB))[0]
    read = pms.mark_read(_reader(BOB), item.id)
    assert read.state == "read" and read.read_at
    assert _state() == [("Bob Baker", "read")]
    # Reading twice is a no-op, not an error.
    assert pms.mark_read(_reader(BOB), item.id).state == "read"


# ===========================================================================
# 8. The operator door never lists a person's pointer
# ===========================================================================

def test_the_operator_door_does_not_list_a_persons_pointer(mention_db, quiet_room):
    from database import db
    room = _room()
    _post(room["id"], "@Bob Baker", tags=[BOB])
    assert db.list_operator_queue_items(accessible_agent_names={AGENT}) == []
    assert db.count_operator_queue_items(accessible_agent_names={AGENT}) == 0
    stats = db.get_operator_queue_stats(accessible_agent_names={AGENT})
    assert stats["by_status"] == {}
    # Review: asking for the type by NAME must not reopen the door —
    # `GET /api/operator-queue?type=mention` is any sharee's query parameter.
    assert db.list_operator_queue_items(type="mention", accessible_agent_names={AGENT}) == []
    assert db.list_operator_queue_items(types=("mention",), accessible_agent_names={AGENT}) == []
    assert db.count_operator_queue_items(type="mention", accessible_agent_names={AGENT}) == 0


def test_the_operator_list_route_does_not_list_tags_by_type(mention_db, quiet_room):
    """Review: the route a sharee calls, with `?type=mention`, lists no tag."""
    from models import User
    from routers import operator_queue as oq_router
    room = _room()
    _post(room["id"], "@Bob Baker", tags=[BOB])
    owner = User(id=1, username="alice", role="user", email=ALICE)
    out = _run(oq_router.list_queue_items(
        status=None, type="mention", priority=None, agent_name=None, since=None,
        limit=100, offset=0, agent_names=None, cursor=None, current_user=owner))
    assert out["items"] == []


def test_an_agent_cannot_forge_a_tag_by_writing_the_type(mention_db):
    """Review: `type` is agent-authored (the queue file's ingest only bounds
    its length) and the addressee is any roster email, so an agent row typed
    `mention` must not become "Alice mentioned you" in Bob's Inbox — a tag is
    the platform's reserved id AND the type, never the type alone. And it must
    not hide the agent's ask from the operator either."""
    from database import db
    from services import person_mention_service as pms
    forged_ctx = {"mention": {
        "conversation": {"kind": "room", "id": "r-1", "label": "Board room"},
        "message_id": "m-1", "seq": 1,
        "tagged_by": {"username": "alice", "label": "Alice Archer"},
        "person": {"label": "Bob Baker"},
    }}
    row_id = db.create_operator_queue_item(AGENT, {
        "id": "agent-ask-1", "type": "mention", "title": "Alice Archer mentioned you",
        "question": "In Board room", "addressed_to_email": BOB, "context": forged_ctx,
    }, channel="file", raised_by="agent")
    assert pms.list_for_reader(_reader(BOB)) == []
    with pytest.raises(pms.TagError) as ei:
        pms.open_for_reader(_reader(BOB), row_id)
    assert ei.value.status_code == 404
    with pytest.raises(pms.TagError):
        pms.mark_read(_reader(BOB), row_id)
    # The agent's row is an ordinary ask to the operator, not a hidden one.
    listed = db.list_operator_queue_items(accessible_agent_names={AGENT})
    assert [r["request_id"] for r in listed] == ["agent-ask-1"]
    assert db.get_operator_queue_stats(accessible_agent_names={AGENT})["by_status"] == {"pending": 1}


def test_the_operator_door_answers_a_tag_id_as_not_found(mention_db, quiet_room):
    """Review: GET was a 404, but respond/cancel answered 400 "status
    'delivered'" — a different answer for an id the door says is not there."""
    from fastapi import HTTPException
    from models import OperatorResponse, User
    from routers import operator_queue as oq_router
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "@Bob Baker", tags=[BOB])
    item_id = pms.list_for_reader(_reader(BOB))[0].id
    owner = User(id=1, username="alice", role="user", email=ALICE)

    class _Req:
        headers = {}
        client = None

    with pytest.raises(HTTPException) as got:
        _run(oq_router.get_queue_item(item_id, current_user=owner))
    with pytest.raises(HTTPException) as responded:
        _run(oq_router.respond_to_queue_item(item_id, OperatorResponse(response="ok"), _Req(),
                                             current_user=owner))
    with pytest.raises(HTTPException) as cancelled:
        _run(oq_router.cancel_queue_item(item_id, _Req(), None, current_user=owner))
    assert got.value.status_code == responded.value.status_code == cancelled.value.status_code == 404


# ===========================================================================
# 9. The 1:1 chat seam and the Inbox door
# ===========================================================================

def _portal(email, *, is_platform=True, is_person=True):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(email=email, is_platform=is_platform, is_person=is_person)


def test_a_chat_tag_is_refused_by_name_before_the_turn_runs(mention_db):
    from client_portal import service
    with pytest.raises(service.ChatTagError) as ei:
        service.resolve_chat_tags(_portal(ALICE), AGENT, ["zed@example.com"])
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "unknown_person"
    assert ei.value.detail["name"] == "zed@example.com"
    # an external client may not tag at all; a system key is not a person
    with pytest.raises(service.ChatTagError) as ext:
        service.resolve_chat_tags(_portal("client@outside.example", is_platform=False), AGENT, [BOB])
    assert (ext.value.status_code, ext.value.detail["code"]) == (403, "tagging_unavailable")
    with pytest.raises(service.ChatTagError) as sys_key:
        service.resolve_chat_tags(_portal(ALICE, is_person=False), AGENT, [BOB])
    assert sys_key.value.detail["code"] == "agents_cannot_tag"
    assert service.resolve_chat_tags(_portal(ALICE), AGENT, None) is None


def test_a_chat_tag_lands_and_the_tagger_sees_it_on_their_message(mention_db):
    from client_portal import service
    tags = service.resolve_chat_tags(_portal(ALICE), AGENT, [BOB, BOB])
    service.deliver_chat_tags(AGENT, "sess-9", "msg-9", tags)
    service.deliver_chat_tags(AGENT, "sess-9", "msg-9", tags)       # a retried turn
    assert len(_rows(mention_db)) == 1
    messages = [{"id": "msg-9", "role": "user"}, {"id": "msg-10", "role": "assistant"}]
    service._attach_own_tags(messages, "sess-9", AGENT)
    assert messages[0]["tags"] == [{"label": "Bob Baker", "state": "delivered", "read_at": None}]
    assert messages[1]["tags"] is None


def test_the_chat_picker_is_a_platform_persons_and_needs_a_query(mention_db):
    from client_portal import service
    assert [p["email"] for p in service.chat_people(_portal(ALICE), AGENT, "bo")] == [BOB]
    assert service.chat_people(_portal(ALICE), AGENT, " ") == []
    with pytest.raises(service.ChatTagError):
        service.chat_people(_portal("client@outside.example", is_platform=False), AGENT, "bo")


def test_the_inbox_door_lists_opens_and_reads_only_the_callers_own(mention_db, quiet_room):
    from fastapi import HTTPException
    from client_portal.mentions import router as door
    room = _room()
    _post(room["id"], "@Bob Baker", tags=[BOB])
    listed = door.list_mentions(principal=_portal(BOB))
    assert [m.state for m in listed] == ["unread"]
    assert door.list_mentions(principal=_portal(DAVE)) == []
    opened = door.open_mention(listed[0].id, principal=_portal(BOB))
    assert opened.can_see is False
    with pytest.raises(HTTPException) as ei:
        door.open_mention(listed[0].id, principal=_portal(DAVE))
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as not_person:
        door.read_mention(listed[0].id, principal=_portal(BOB, is_person=False))
    assert not_person.value.status_code == 403
    assert door.read_mention(listed[0].id, principal=_portal(BOB)).state == "read"


def test_a_read_tag_is_swept_by_retention_and_an_unread_one_is_kept(mention_db, quiet_room):
    """A read tag is settled history (pruned with the ledger's terminal rows);
    an unread one is never swept, like a pending ask."""
    from sqlalchemy import text
    from database import db
    from services import person_mention_service as pms
    room = _room()
    _post(room["id"], "@Bob Baker @Carol Chen", tags=[BOB, CAROL])
    bob_item = pms.list_for_reader(_reader(BOB))[0]
    pms.mark_read(_reader(BOB), bob_item.id)
    with mention_db.begin() as conn:
        conn.execute(text("UPDATE operator_queue SET created_at = '2020-01-01T00:00:00Z' WHERE type = 'mention'"))
    assert db.prune_operator_queue_terminal_items(30, 30) == 1
    left = _rows(mention_db)
    assert [r["addressed_to_email"] for r in left] == [CAROL]
    assert left[0]["status"] == "delivered"
