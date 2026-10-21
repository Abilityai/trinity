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
Workspace session, `user` + username for a platform session) — so a non-member
holding a cursor row for a room id (the write deliberately does not check the id
exists, invariant #8) learns nothing about it, not even that it has messages.

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


def test_without_a_room_reader_the_payload_is_the_old_shape(room_db):
    """A caller that names no participant identity (every existing caller)
    gets rooms reported as before — the count never guesses an identity."""
    from client_portal import service

    _room("room_a")
    _read(ALICE, "room_a", "2026-10-01T10:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    assert _room_entry(service.get_chat_state(ALICE), "room_a")["unread"] == 0


# ---------------------------------------------------------------------------
# Membership is the grant (AC: a non-member never gets a room's row, id, name
# or count)
# ---------------------------------------------------------------------------


def test_a_non_member_with_a_cursor_on_the_room_id_learns_no_count(room_db):
    """The read write does not check the id (invariant #8), so Bob can hold a
    cursor row for Alice's room. Its count must stay 0 — a number there would
    be an oracle over a room he cannot open."""
    _room("room_a")
    _read(BOB, "room_a", "2026-10-01T08:00:00Z")
    _say("room_a", "2026-10-01T10:05:00Z")

    entry = _room_entry(_state(BOB, (WS, BOB)), "room_a")
    assert entry == {"kind": "room", "id": "room_a", "starred": False, "unread": 0}


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

    assert _room_entry(_state(ALICE, ("user", "alice")), "room_a")["unread"] == 0
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

    assert _room_entry(_state(), "room_a")["unread"] == 0


# ---------------------------------------------------------------------------
# The route resolves the reader from the credential
# ---------------------------------------------------------------------------


def _principal(email, *, is_platform, username=None):
    from client_portal.portal_auth import PortalPrincipal

    return PortalPrincipal(email, is_platform, True, False, username=username)


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
    # The same person's Workspace-identity room is not this credential's room.
    assert _room_entry(state, "room_w")["unread"] == 0


def test_a_platform_session_with_no_username_counts_no_room(room_db):
    """Fail closed: no identity, no room count — never a guessed one."""
    from client_portal import router

    _room("room_w", kind=WS, identity=ALICE)
    _read(ALICE, "room_w", "2026-10-01T10:00:00Z")
    _say("room_w", "2026-10-01T10:05:00Z")

    state = router.portal_chat_state(
        previews=False, principal=_principal(ALICE, is_platform=True)
    )
    assert _room_entry(state, "room_w")["unread"] == 0


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
    entry = _room_entry(state, "room_a")
    assert entry["unread"] == 0
    assert "latest" not in entry and "first_unread_message_id" not in entry
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
