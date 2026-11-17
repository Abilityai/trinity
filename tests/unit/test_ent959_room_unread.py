"""trinity-enterprise#959 — room messages reach the Workspace Inbox.

Before this, `GET /client-portal/chat-state` reported every room with
`unread: 0` ("a room keeps its own seq cursor"), so the Inbox's Unread tab, the
sidebar's room rows and the pinned Inbox count never saw a room message.

A room's unread is now counted on the SAME per-viewer timestamp cursor a chat
uses (`enterprise_portal_chat_state`, `chat_kind = 'room'` — already written by
`POST /chat-state/room/{id}/read` when the person opens a room) and the same
ent#557 baseline for a room never opened. An arrival is an AGENT's message — the
chat rule (`role = 'assistant'`) in a room's terms: the person's own messages
and the room's system lines never count.

The access rule is the point of half this file: **membership is the grant**. A
count is only ever computed for a room the viewer is an active participant of,
in the identity space their credential acts in (`workspace_user` + email for a
Workspace session, `user` + username for a platform session). A non-member gets
NO entry for a room — not a count, not a zero, not the id (fix round 1: a
cursor row keyed by a shared email used to hand the id back) — and cannot write
a cursor for one; a participant who left is a non-member. The one exception is a
platform admin, who lists every room anyway: their own starred rows survive,
with no count.

Runs against a throwaway sqlite carrying the real tables, so the SQL is the
code under test.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
WS = "workspace_user"


@pytest.fixture()
def room_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent959.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        agent_reports,
        enterprise_portal_chat_state,
        enterprise_portal_messages,
        enterprise_portal_sessions,
        enterprise_room_messages,
        enterprise_room_participants,
        enterprise_rooms,
        room_gate_holds,
        schedule_executions,
    )
    from db.tables import (
        metadata as oss_metadata,
    )

    oss_metadata.create_all(
        get_engine(),
        tables=[
            agent_reports,
            enterprise_portal_chat_state,
            enterprise_portal_messages,
            enterprise_portal_sessions,
            enterprise_rooms,
            enterprise_room_participants,
            enterprise_room_messages,
            schedule_executions,  # `get_room`'s cost read joins it
            room_gate_holds,  # trinity#3402: an agent's room read leaves out its holds
        ],
    )
    yield get_engine()


def _room(
    room_id, *, kind=WS, identity=ALICE, agents=("ada", "bo"), at="2026-10-01T09:00:00Z"
):
    """A room through the real writer: the creator seated as moderator, its
    agents as members, and the opening system line."""
    from shared_sessions import db as rdb

    participants = [(kind, identity, "moderator")] + [
        ("agent", a, "member") for a in agents
    ]
    rdb.create_room_with_participants(
        room_id,
        f"Room {room_id}",
        None,
        identity,
        200,
        None,
        None,
        participants,
        uuid.uuid4().hex,
        "Room created.",
        at,
    )


def _say(
    room_id, at, *, sender_kind="agent", sender="ada", content="hello", kind="message"
):
    from shared_sessions import db as rdb

    mid = uuid.uuid4().hex
    rdb.append_message(mid, room_id, sender_kind, sender, content, [], kind, None, at)
    return mid


def _read(email, room_id, at):
    from client_portal import db as pdb

    pdb.mark_chat_read(email, "room", room_id, at)


def _state(email=ALICE, reader=(WS, ALICE)):
    from client_portal import service

    return service.get_chat_state(email, room_reader=reader)


def _room_entry(state, room_id):
    return next(
        (c for c in state["chats"] if c["kind"] == "room" and c["id"] == room_id), None
    )


# ---------------------------------------------------------------------------
# What counts
# ---------------------------------------------------------------------------


def test_agent_messages_after_my_room_cursor_are_unread(room_db):
    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T09:30:00Z")  # before the cursor: read
    _say("room_a", "2026-10-01T10:05:00Z")
    _say("room_a", "2026-10-01T10:06:00Z", sender="bo")

    assert _room_entry(_state(), "room_a")["unread"] == 2


def test_my_own_messages_and_the_rooms_system_lines_never_count(room_db):
    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z", sender_kind=WS, sender=ALICE)
    _say(
        "room_a",
        "2026-10-01T10:06:00Z",
        sender_kind="system",
        sender=None,
        kind="system",
        content="Room closed (budget).",
    )

    assert _room_entry(_state(), "room_a")["unread"] == 0


def test_opening_the_room_clears_it(room_db):
    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")
    assert _room_entry(_state(), "room_a")["unread"] == 1

    _read(ALICE, "room_a", "2026-10-01T10:10:00Z")

    assert _room_entry(_state(), "room_a")["unread"] == 0


def test_a_room_never_opened_counts_against_the_account_baseline(room_db):
    """The ent#557 rule, for a room: a cursorless room still reaches the payload
    once the viewer has read anything at all."""
    _room("room_a")
    _read(ALICE, "some-other-room", "2026-10-01T10:00:00Z")  # writes the baseline
    _say("room_a", "2026-10-01T10:05:00Z")

    entry = _room_entry(_state(), "room_a")
    assert entry == {"kind": "room", "id": "room_a", "starred": False, "unread": 1}


def test_a_first_ever_viewer_counts_nothing_in_a_room_either(room_db):
    _room("room_a")
    _say("room_a", "2026-10-01T10:05:00Z")

    assert _room_entry(_state(), "room_a") is None


def test_without_a_room_reader_no_room_row_is_returned(room_db):
    """A caller that names no participant identity cannot be shown to be in any
    room, so it gets no room ROW — fail closed, never a guessed identity. Its
    threads are untouched. (Replaces a test that asserted `unread == 0`, which
    held before the change as well as after it.)"""
    from client_portal import db as pdb
    from client_portal import service

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    pdb.mark_chat_read(ALICE, "thread", "t-1", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    state = service.get_chat_state(ALICE)
    assert _room_entry(state, "room_a") is None
    assert "room_a" not in repr(state)
    assert any(c["kind"] == "thread" and c["id"] == "t-1" for c in state["chats"])


# ---------------------------------------------------------------------------
# Membership is the grant (AC: a non-member never gets a room's row, id, name
# or count)
# ---------------------------------------------------------------------------


def test_a_non_member_holding_a_cursor_row_gets_no_entry_for_the_room(room_db):
    """A cursor row for a room the viewer is not in (one written before the
    write checked membership, or by the same email in the OTHER identity space)
    must not come back: no count, no zero, no id."""
    _room("room_a")
    _read(BOB, "room_a", "2026-10-01T08:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    state = _state(BOB, (WS, BOB))
    assert _room_entry(state, "room_a") is None
    assert "room_a" not in repr(state)


def test_a_non_member_is_never_handed_a_cursorless_room(room_db):
    _room("room_a")
    _read(BOB, "elsewhere", "2026-10-01T08:00:00Z")  # Bob has a baseline
    _say("room_a", "2026-10-01T10:05:00Z")

    state = _state(BOB, (WS, BOB))
    assert _room_entry(state, "room_a") is None
    assert all(c["id"] != "room_a" for c in state["chats"])


def test_the_count_is_scoped_to_the_identity_space_the_credential_acts_in(room_db):
    """A platform account whose email equals a Workspace member's email is NOT
    that member: room identities are `workspace_user`+email and `user`+username,
    two spaces (ent#362). Reading the room under the platform identity must not
    surface the Workspace member's room."""
    _room("room_a", kind=WS, identity=ALICE)
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    assert _room_entry(_state(ALICE, ("user", "alice")), "room_a") is None
    assert _room_entry(_state(ALICE, (WS, ALICE)), "room_a")["unread"] == 1


def test_a_platform_member_is_counted_under_its_username(room_db):
    _room("room_p", kind="user", identity="alice")
    _read(ALICE, "room_p", "2026-10-01T10:00:00Z")
    _say("room_p", "2026-10-01T10:05:00Z")

    assert _room_entry(_state(ALICE, ("user", "alice")), "room_p")["unread"] == 1


def test_a_participant_who_left_is_no_longer_counted(room_db):
    from shared_sessions import db as rdb

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    rdb.remove_participant("room_a", WS, ALICE, "2026-10-01T10:01:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    assert _room_entry(_state(), "room_a") is None


# ---------------------------------------------------------------------------
# The route resolves the reader from the credential
# ---------------------------------------------------------------------------


def _principal(email, *, is_platform, username=None, is_admin=False):
    from client_portal.portal_auth import PortalPrincipal

    return PortalPrincipal(email, is_platform, True, is_admin, username=username)


def test_the_route_counts_a_workspace_session_as_its_email(room_db):
    from client_portal import router

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    state = router.portal_chat_state(
        previews=False, principal=_principal(ALICE, is_platform=False)
    )
    assert _room_entry(state, "room_a")["unread"] == 1


def test_the_route_counts_a_platform_session_as_its_username(room_db):
    from client_portal import router

    _room("room_p", kind="user", identity="alice")
    _room("room_w", kind=WS, identity=ALICE)
    _read(ALICE, "room_p", "2026-10-01T10:00:00Z")
    _read(ALICE, "room_w", "2026-10-01T10:00:00Z")
    _say("room_p", "2026-10-01T10:05:00Z")
    _say("room_w", "2026-10-01T10:05:00Z")

    state = router.portal_chat_state(
        previews=False, principal=_principal(ALICE, is_platform=True, username="alice")
    )
    assert _room_entry(state, "room_p")["unread"] == 1
    # The same person's Workspace-identity room is not this credential's room:
    # not even its id comes back (security review #1).
    assert _room_entry(state, "room_w") is None


def test_a_platform_session_with_no_username_counts_no_room(room_db):
    """Fail closed: no identity, no room count — never a guessed one."""
    from client_portal import router

    _room("room_w", kind=WS, identity=ALICE)
    _read(ALICE, "room_w", "2026-10-01T10:00:00Z")
    _say("room_w", "2026-10-01T10:05:00Z")

    state = router.portal_chat_state(
        previews=False, principal=_principal(ALICE, is_platform=True)
    )
    assert _room_entry(state, "room_w") is None


# ---------------------------------------------------------------------------
# The Inbox's read (previews=true): the newest arrival and the first unread
# ---------------------------------------------------------------------------


@pytest.fixture()
def roster(monkeypatch):
    from client_portal import service as portal_service

    monkeypatch.setattr(
        portal_service, "roster_agent_names", lambda email, include_owned: set()
    )


def test_previews_carry_a_members_room_arrival(room_db, roster):
    from client_portal import router

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    first = _say("room_a", "2026-10-01T10:05:00Z", content="**first** reply")
    last = _say("room_a", "2026-10-01T10:07:00Z", sender="bo", content="second reply")

    state = router.portal_chat_state(
        previews=True, principal=_principal(ALICE, is_platform=False)
    )
    entry = _room_entry(state, "room_a")
    assert entry["unread"] == 2
    assert entry["first_unread_message_id"] == first
    assert entry["latest"] == {
        "kind": "message",
        "id": last,
        "at": "2026-10-01T10:07:00Z",
        "excerpt": "second reply",
        "outcome": None,
    }


def test_previews_never_carry_a_non_members_room(room_db, roster):
    from client_portal import router

    _room("room_a")
    _read(BOB, "room_a", "2026-10-01T08:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z", content="secret plans")

    state = router.portal_chat_state(
        previews=True, principal=_principal(BOB, is_platform=False)
    )
    assert _room_entry(state, "room_a") is None
    assert "room_a" not in repr(state)
    assert "secret plans" not in repr(state)


# ---------------------------------------------------------------------------
# The rooms list says which rooms the caller is IN (an admin lists them all)
# ---------------------------------------------------------------------------


class _User:
    def __init__(self, username, role="user"):
        self.username = username
        self.role = role
        self.agent_name = None


def test_the_rooms_list_marks_membership_for_an_admin_who_sees_every_room(room_db):
    from shared_sessions import service as rooms

    _room("room_mine", kind="user", identity="root")
    _room("room_theirs", kind=WS, identity=ALICE)

    listed = {
        r["id"]: r for r in rooms.list_rooms(_User("root", role="admin"))["rooms"]
    }
    assert listed["room_mine"]["member"] is True
    assert listed["room_theirs"]["member"] is False


def test_the_rooms_list_marks_a_members_rooms_as_member(room_db):
    from shared_sessions import service as rooms

    _room("room_mine", kind="user", identity="carol")

    listed = rooms.list_rooms(_User("carol"))["rooms"]
    assert [(r["id"], r["member"]) for r in listed] == [("room_mine", True)]


# ---------------------------------------------------------------------------
# An admin lists every room: their own starred rows stay, with no count
# ---------------------------------------------------------------------------


def test_an_admin_keeps_a_starred_room_they_are_not_in_but_gets_no_count(room_db, roster):
    from client_portal import db as pdb
    from client_portal import router

    _room("room_theirs", kind=WS, identity=BOB)
    pdb.mark_chat_read(ALICE, "thread", "t-1", "2026-10-01T09:30:00Z")  # baseline
    pdb.set_chat_star(ALICE, "room", "room_theirs", True, "2026-10-01T10:00:00Z")
    _say("room_theirs", "2026-10-01T10:05:00Z", content="not for the admin")

    admin = _principal(ALICE, is_platform=True, username="root", is_admin=True)
    for previews in (False, True):
        state = router.portal_chat_state(previews=previews, principal=admin)
        assert _room_entry(state, "room_theirs") == {
            "kind": "room", "id": "room_theirs", "starred": True, "unread": 0}
        assert "not for the admin" not in repr(state)

    # The same email as a NON-admin platform account gets nothing for it.
    plain = _principal(ALICE, is_platform=True, username="root")
    assert _room_entry(router.portal_chat_state(previews=False, principal=plain),
                       "room_theirs") is None


# ---------------------------------------------------------------------------
# The read write: only a member's credential moves a room cursor (review #3)
# ---------------------------------------------------------------------------


def _room_rows(email):
    from client_portal import db as pdb

    return {r["chat_id"]: r for r in pdb.get_chat_state(email) if r.get("chat_kind") == "room"}


def test_a_member_reads_the_room_through_the_route(room_db):
    from client_portal import router

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")
    assert _room_entry(_state(), "room_a")["unread"] == 1

    resp = router.portal_mark_chat_read("room", "room_a", principal=_principal(ALICE, is_platform=False))

    assert resp.status_code == 204
    assert _room_entry(_state(), "room_a")["unread"] == 0


def test_a_non_member_cannot_create_a_cursor_on_someone_elses_room(room_db):
    from client_portal import router

    _room("room_a")  # Alice's
    resp = router.portal_mark_chat_read("room", "room_a", principal=_principal(BOB, is_platform=False))

    assert resp.status_code == 204
    assert "room_a" not in _room_rows(BOB)


def test_a_platform_session_cannot_clear_the_same_emails_workspace_room(room_db):
    """Same email, other identity space: the platform account is not in the
    Workspace identity's room, so its read must not move the cursor both
    share (the row is keyed by email)."""
    from client_portal import router

    _room("room_w", kind=WS, identity=ALICE)
    _read(ALICE, "room_w", "2026-10-01T10:00:00Z")
    _say("room_w", "2026-10-01T10:05:00Z")

    router.portal_mark_chat_read(
        "room", "room_w", principal=_principal(ALICE, is_platform=True, username="alice"))

    assert _room_rows(ALICE)["room_w"]["last_read_at"] == "2026-10-01T10:00:00Z"
    assert _room_entry(_state(ALICE, (WS, ALICE)), "room_w")["unread"] == 1


def test_an_unknown_room_and_a_room_i_am_not_in_answer_alike(room_db):
    """Same status, same (absent) write — the write is no existence oracle."""
    from client_portal import router

    _room("room_a")
    bob = _principal(BOB, is_platform=False)
    a = router.portal_mark_chat_read("room", "room_a", principal=bob)
    b = router.portal_mark_chat_read("room", "room_nope", principal=bob)

    assert (a.status_code, a.body) == (b.status_code, b.body)
    assert _room_rows(BOB) == {}


def test_a_departed_participant_cannot_write_the_room_cursor(room_db):
    from client_portal import router
    from shared_sessions import db as rdb

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    rdb.remove_participant("room_a", WS, ALICE, "2026-10-01T10:01:00Z")

    router.portal_mark_chat_read("room", "room_a", principal=_principal(ALICE, is_platform=False))

    assert _room_rows(ALICE)["room_a"]["last_read_at"] == "2026-10-01T10:00:00Z"


# ---------------------------------------------------------------------------
# Rooms API: a participant who LEFT is not a member (security review #2)
# ---------------------------------------------------------------------------


class _AgentKey(_User):
    """An agent-scoped key: `shared_sessions.service._caller` reads it as the agent."""

    def __init__(self, name):
        super().__init__("owner")
        self.agent_name = name


def test_a_removed_agent_can_no_longer_fetch_the_room(room_db):
    from shared_sessions import db as rdb
    from shared_sessions import service as rooms

    _room("room_a")
    assert rooms.get_room(_AgentKey("ada"), "room_a")["id"] == "room_a"  # while in it
    rdb.remove_participant("room_a", "agent", "ada", "2026-10-01T10:01:00Z")
    _say("room_a", "2026-10-01T10:05:00Z", sender="bo", content="after ada left")

    with pytest.raises(rooms.RoomError) as gone:
        rooms.get_room(_AgentKey("ada"), "room_a")
    with pytest.raises(rooms.RoomError) as never:
        rooms.get_room(_AgentKey("ada"), "room_nope")
    # The uniform 404: a room you left reads exactly like one that never existed.
    assert (gone.value.status_code, gone.value.code) == (never.value.status_code, never.value.code) == (404, "room_not_found")
    # An agent still in the room reads on.
    assert rooms.get_room(_AgentKey("bo"), "room_a")["id"] == "room_a"


def test_a_removed_agent_no_longer_lists_the_room(room_db):
    from shared_sessions import db as rdb
    from shared_sessions import service as rooms

    _room("room_a")
    rdb.remove_participant("room_a", "agent", "ada", "2026-10-01T10:01:00Z")

    assert rooms.list_rooms(_AgentKey("ada"))["rooms"] == []
    assert [(r["id"], r["member"]) for r in rooms.list_rooms(_AgentKey("bo"))["rooms"]] == [("room_a", True)]


def test_a_removed_agent_cannot_post_into_the_room(room_db):
    import asyncio

    from shared_sessions import db as rdb
    from shared_sessions import service as rooms

    _room("room_a")
    rdb.remove_participant("room_a", "agent", "ada", "2026-10-01T10:01:00Z")
    before = len(rdb.get_messages("room_a"))

    with pytest.raises(rooms.RoomError) as ei:
        asyncio.run(rooms.post_message(_AgentKey("ada"), "room_a", "still here"))
    assert ei.value.status_code == 404
    assert len(rdb.get_messages("room_a")) == before


# ---------------------------------------------------------------------------
# The platform door hands its USERNAME to the room reader (validate W1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_platform_door_carries_the_username_to_the_room_reader(monkeypatch):
    """Executes `get_portal_principal`'s wiring, not a hand-built principal:
    every platform-session room count reads as `user` + this username."""
    from types import SimpleNamespace

    import database
    from client_portal import db as portal_db
    from client_portal import portal_auth as pa
    from client_portal import service

    user = SimpleNamespace(username="alice-ops", role="user", mcp_scope=None, agent_name=None)

    async def current_user(request, token):
        return user

    monkeypatch.setattr(pa, "decode_portal_session", lambda t: None)
    monkeypatch.setattr(pa, "resolve_platform_user_unfloored", current_user)
    monkeypatch.setattr(database.db, "get_user_by_username", lambda name: {"email": "Alice@Example.com"})
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)

    principal = await pa.get_portal_principal(SimpleNamespace(), SimpleNamespace(headers={}), token="jwt")

    assert service.room_reader_of(principal) == ("user", "alice-ops")
