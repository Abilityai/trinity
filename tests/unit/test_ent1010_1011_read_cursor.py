"""trinity-enterprise#1010 / #1011 — the Inbox read cursor covers what the
client held, and each room identity space keeps its own.

#1010. Opening a chat or a room wrote the viewer's read cursor at the SERVER's
current time. An arrival that landed after the client's last fetch but before
that write was older than the cursor, so it never counted as unread although the
person never saw it. The read write now takes ``through``: the newest instant the
client actually held (its last fetch's ``as_of`` or the newest message it
rendered). The cursor moves to ``through`` — clamped to now, and never backwards
— so a later arrival still counts. A write with no ``through`` keeps the old
meaning (now), so a client that predates this is unchanged.

#1011. A room seats a person as ``user`` + username (a platform session) or
``workspace_user`` + email (a Workspace session), and the cursor row was keyed
by email alone, so one email seated under both shared one cursor. A room's read
cursor now lives in a row per identity space (``room:<participant kind>``). The
legacy ``room`` row (which also carries the star) is read as the floor for both
spaces until that space writes its own, so no viewer's count moves on upgrade.

Runs against a throwaway sqlite carrying the real tables, so the SQL is the code
under test.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
WS = "workspace_user"
WS_READER = (WS, ALICE)
PLATFORM_READER = ("user", "alice")


@pytest.fixture()
def cursor_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent1010.db"
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
    from db.tables import metadata as oss_metadata

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
            schedule_executions,
            room_gate_holds,
        ],
    )
    yield get_engine()


# --- helpers ----------------------------------------------------------------


def _room(room_id, people=(WS_READER,), agents=("ada",), at="2026-10-01T09:00:00Z"):
    from shared_sessions import db as rdb

    participants = [(k, i, "moderator") for k, i in people] + [
        ("agent", a, "member") for a in agents
    ]
    rdb.create_room_with_participants(
        room_id, f"Room {room_id}", None, people[0][1], 200, None, None,
        participants, uuid.uuid4().hex, "Room created.", at,
    )


def _say(room_id, at, sender="ada"):
    from shared_sessions import db as rdb

    mid = uuid.uuid4().hex
    rdb.append_message(mid, room_id, "agent", sender, "hello", [], "message", None, at)
    return mid


def _thread(session_id, email=ALICE, agent="ada"):
    from sqlalchemy import insert

    from db.engine import get_engine
    from db.tables import enterprise_portal_sessions

    with get_engine().begin() as conn:
        conn.execute(insert(enterprise_portal_sessions).values(
            id=session_id, client_email=email, agent_name=agent,
            created_at="2026-10-01T09:00:00Z",
        ))


def _reply(session_id, at, email=ALICE, agent="ada"):
    from sqlalchemy import insert

    from db.engine import get_engine
    from db.tables import enterprise_portal_messages

    with get_engine().begin() as conn:
        conn.execute(insert(enterprise_portal_messages).values(
            id=uuid.uuid4().hex, session_id=session_id, client_email=email,
            agent_name=agent, role="assistant", content="reply", created_at=at,
        ))


def _state(reader=WS_READER):
    from client_portal import service

    return service.get_chat_state(ALICE, room_reader=reader)


def _entry(state, kind, cid):
    return next((c for c in state["chats"] if c["kind"] == kind and c["id"] == cid), None)


def _unread(kind, cid, reader=WS_READER):
    e = _entry(_state(reader), kind, cid)
    return None if e is None else e["unread"]


def _mark(kind, cid, *, through=None, reader=WS_READER, now=None, monkeypatch=None):
    from client_portal import service

    if now is not None:
        monkeypatch.setattr(service, "utc_now_iso", lambda: now)
    service.mark_chat_read(ALICE, kind, cid, room_reader=reader, through=through)


def _rows():
    from sqlalchemy import text

    from db.engine import get_engine

    with get_engine().connect() as conn:
        return {
            (r["chat_kind"], r["chat_id"]): dict(r)
            for r in conn.execute(text(
                "SELECT * FROM enterprise_portal_chat_state WHERE client_email = :e"
            ), {"e": ALICE}).mappings()
        }


NOW = "2026-10-01T12:00:00.000000Z"


# ---------------------------------------------------------------------------
# #1010 — the read write covers only what the client held
# ---------------------------------------------------------------------------


def test_a_thread_reply_landing_between_the_fetch_and_the_read_stays_unread(cursor_db, monkeypatch):
    _thread("s1")
    _reply("s1", "2026-10-01T10:00:00Z")           # the client fetched this one
    fetched_at = "2026-10-01T10:00:05Z"
    _reply("s1", "2026-10-01T10:00:30Z")           # lands after the fetch
    _mark("thread", "s1", through=fetched_at, now=NOW, monkeypatch=monkeypatch)

    assert _unread("thread", "s1") == 1


def test_a_room_message_landing_between_the_fetch_and_the_read_stays_unread(cursor_db, monkeypatch):
    _room("r1")
    _say("r1", "2026-10-01T10:00:00Z")
    fetched_at = "2026-10-01T10:00:05Z"
    _say("r1", "2026-10-01T10:00:30Z")
    _mark("room", "r1", through=fetched_at, now=NOW, monkeypatch=monkeypatch)

    assert _unread("room", "r1") == 1


def test_what_the_client_held_is_read(cursor_db, monkeypatch):
    _thread("s1")
    held = "2026-10-01T10:00:00Z"
    _reply("s1", held)
    _mark("thread", "s1", through=held, now=NOW, monkeypatch=monkeypatch)

    assert _unread("thread", "s1") == 0


def test_a_read_with_no_through_still_reads_up_to_now(cursor_db, monkeypatch):
    """A client that predates #1010 sends no body: the old meaning, unchanged."""
    _thread("s1")
    _reply("s1", "2026-10-01T10:00:30Z")
    _mark("thread", "s1", now=NOW, monkeypatch=monkeypatch)

    assert _unread("thread", "s1") == 0
    assert _rows()[("thread", "s1")]["last_read_at"] == NOW


def test_a_through_in_the_future_is_clamped_to_now(cursor_db, monkeypatch):
    """A client cannot pre-read arrivals that do not exist yet."""
    _thread("s1")
    _mark("thread", "s1", through="2999-01-01T00:00:00Z", now=NOW, monkeypatch=monkeypatch)
    _reply("s1", "2026-10-01T12:00:01Z")

    assert _rows()[("thread", "s1")]["last_read_at"] == NOW
    assert _unread("thread", "s1") == 1


def test_an_older_through_never_moves_the_cursor_back(cursor_db, monkeypatch):
    """Two tabs, or a slow request overtaken by a later one: the later read
    wins whatever order the writes land in."""
    _thread("s1")
    _reply("s1", "2026-10-01T10:00:30Z")
    _mark("thread", "s1", through="2026-10-01T11:00:00Z", now=NOW, monkeypatch=monkeypatch)
    _mark("thread", "s1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)

    assert _rows()[("thread", "s1")]["last_read_at"] == "2026-10-01T11:00:00Z"
    assert _unread("thread", "s1") == 0


def test_the_cursor_keeps_the_clients_own_spelling(cursor_db, monkeypatch):
    """Stored timestamps are compared as strings. A held message's own
    `created_at`, sent back verbatim, must read that message — re-serialising it
    to another precision ("…00Z" → "…00.000000Z") would sort it after itself."""
    _thread("s1")
    _reply("s1", "2026-10-01T10:00:00Z")
    _mark("thread", "s1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)

    assert _rows()[("thread", "s1")]["last_read_at"] == "2026-10-01T10:00:00Z"
    assert _unread("thread", "s1") == 0


@pytest.mark.parametrize("bad", ["yesterday", "2026-10-01", "2026-10-01T10:00:00+02:00",
                                 "2026-10-01 10:00:00Z", "", "x" * 64])
def test_a_through_that_is_not_an_instant_is_refused(cursor_db, bad):
    from client_portal import service
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as exc:
        service.mark_chat_read(ALICE, "thread", "s1", room_reader=WS_READER, through=bad)
    assert exc.value.status_code == 422
    assert _rows() == {}


def test_the_first_read_ever_sets_the_baseline_at_through(cursor_db, monkeypatch):
    """ent#557's baseline is "the instant from which a chat never opened counts".
    Written at the first read, it takes the same instant the read covers: an
    arrival in ANOTHER, never-opened chat after what the client held is unread."""
    _thread("s1")
    _thread("s2")
    _mark("thread", "s1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)
    _reply("s2", "2026-10-01T10:30:00Z")

    assert _rows()[("account", "baseline")]["last_read_at"] == "2026-10-01T10:00:00Z"
    assert _unread("thread", "s2") == 1


def test_an_existing_baseline_never_moves(cursor_db, monkeypatch):
    _thread("s1")
    _mark("thread", "s1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)
    _mark("thread", "s1", through="2026-10-01T11:00:00Z", now=NOW, monkeypatch=monkeypatch)

    assert _rows()[("account", "baseline")]["last_read_at"] == "2026-10-01T10:00:00Z"


def test_the_route_takes_through_from_the_body(cursor_db, monkeypatch):
    from client_portal import router, service
    from client_portal.models import PortalChatRead
    from client_portal.portal_auth import PortalPrincipal

    monkeypatch.setattr(service, "utc_now_iso", lambda: NOW)
    _thread("s1")
    _reply("s1", "2026-10-01T10:00:30Z")
    resp = router.portal_mark_chat_read(
        "thread", "s1", body=PortalChatRead(through="2026-10-01T10:00:05Z"),
        principal=PortalPrincipal(ALICE, False, True, False),
    )

    assert resp.status_code == 204
    assert _unread("thread", "s1") == 1


def test_the_route_answers_a_bad_through_with_422(cursor_db):
    from fastapi import HTTPException

    from client_portal import router
    from client_portal.models import PortalChatRead
    from client_portal.portal_auth import PortalPrincipal

    with pytest.raises(HTTPException) as exc:
        router.portal_mark_chat_read(
            "thread", "s1", body=PortalChatRead(through="yesterday"),
            principal=PortalPrincipal(ALICE, False, True, False),
        )
    assert exc.value.status_code == 422


def test_the_state_read_says_as_of_when(cursor_db, monkeypatch):
    """The watermark the client sends back as `through` when it has rendered
    nothing newer: the instant the state it holds was read."""
    from client_portal import router, service
    from client_portal.portal_auth import PortalPrincipal

    monkeypatch.setattr(service, "utc_now_iso", lambda: NOW)
    state = router.portal_chat_state(
        previews=False, principal=PortalPrincipal(ALICE, False, True, False))
    assert state["as_of"] == NOW


def test_the_previews_read_says_as_of_when_too(cursor_db, monkeypatch):
    from client_portal import router, service
    from client_portal.portal_auth import PortalPrincipal

    monkeypatch.setattr(service, "roster_agent_names", lambda email, include_owned: set())
    monkeypatch.setattr(service, "utc_now_iso", lambda: NOW)
    state = router.portal_chat_state(
        previews=True, principal=PortalPrincipal(ALICE, False, True, False))
    assert state["as_of"] == NOW


def test_as_of_is_read_before_the_counts(cursor_db, monkeypatch):
    """`as_of` must not be later than the snapshot it labels: stamped after the
    counts, an arrival committed between the two would be inside `as_of` but
    missing from what the client was shown, and its read would swallow it."""
    from client_portal import db, service

    order = []
    real = db.count_unread_by_session
    monkeypatch.setattr(service, "utc_now_iso", lambda: order.append("as_of") or NOW)
    monkeypatch.setattr(db, "count_unread_by_session",
                        lambda email: order.append("counts") or real(email))
    service.get_chat_state(ALICE, room_reader=WS_READER)

    assert order.index("as_of") < order.index("counts")


# ---------------------------------------------------------------------------
# #1011 — each identity space keeps its own room read state
# ---------------------------------------------------------------------------


def _both_seated(room_id="r1"):
    _room(room_id, people=(WS_READER, PLATFORM_READER))


def test_reading_as_the_workspace_identity_leaves_the_platform_identitys_count(cursor_db, monkeypatch):
    _both_seated()
    _mark("room", "r1", through="2026-10-01T09:30:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)
    _mark("room", "r1", through="2026-10-01T09:30:00Z", reader=PLATFORM_READER, now=NOW, monkeypatch=monkeypatch)
    _say("r1", "2026-10-01T10:00:00Z")
    assert _unread("room", "r1", WS_READER) == 1
    assert _unread("room", "r1", PLATFORM_READER) == 1

    _mark("room", "r1", through="2026-10-01T10:00:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)

    assert _unread("room", "r1", WS_READER) == 0
    assert _unread("room", "r1", PLATFORM_READER) == 1


def test_reading_as_the_platform_identity_leaves_the_workspace_identitys_count(cursor_db, monkeypatch):
    _both_seated()
    _mark("room", "r1", through="2026-10-01T09:30:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)
    _say("r1", "2026-10-01T10:00:00Z")

    _mark("room", "r1", through="2026-10-01T10:00:00Z", reader=PLATFORM_READER, now=NOW, monkeypatch=monkeypatch)

    assert _unread("room", "r1", PLATFORM_READER) == 0
    assert _unread("room", "r1", WS_READER) == 1


def test_a_cursor_row_from_before_the_split_still_counts_for_both_spaces(cursor_db):
    """Today's rows keep working: a legacy `room` cursor is the floor for each
    space until that space writes its own, so nobody's count moves on upgrade."""
    from client_portal import db as pdb

    _both_seated()
    pdb.mark_chat_read(ALICE, "room", "r1", "2026-10-01T10:00:00Z")   # pre-split write
    _say("r1", "2026-10-01T09:45:00Z")
    _say("r1", "2026-10-01T10:15:00Z")

    assert _unread("room", "r1", WS_READER) == 1
    assert _unread("room", "r1", PLATFORM_READER) == 1


def test_a_spaces_first_cursor_never_starts_below_the_legacy_one(cursor_db, monkeypatch):
    """Reading with an older `through` than the shared pre-split cursor must not
    resurrect messages that row already read."""
    from client_portal import db as pdb

    _both_seated()
    pdb.mark_chat_read(ALICE, "room", "r1", "2026-10-01T10:00:00Z")
    _say("r1", "2026-10-01T09:45:00Z")

    _mark("room", "r1", through="2026-10-01T09:30:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)

    assert _unread("room", "r1", WS_READER) == 0


def test_the_room_is_one_entry_and_keeps_its_star(cursor_db, monkeypatch):
    from client_portal import service

    _both_seated()
    service.set_chat_star(ALICE, "room", "r1", True)
    _mark("room", "r1", through="2026-10-01T10:00:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)
    _mark("room", "r1", through="2026-10-01T10:00:00Z", reader=PLATFORM_READER, now=NOW, monkeypatch=monkeypatch)

    for reader in (WS_READER, PLATFORM_READER):
        entries = [c for c in _state(reader)["chats"] if c["id"] == "r1"]
        assert entries == [{"kind": "room", "id": "r1", "starred": True, "unread": 0}]


def test_a_read_room_is_still_listed_with_no_star_and_nothing_unread(cursor_db, monkeypatch):
    """Before the split a read room had a `room` row and so an entry with
    `unread: 0`; reading now writes only the space row, which must list it the
    same way."""
    _room("r1")
    _mark("room", "r1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)

    assert _entry(_state(), "room", "r1") == {"kind": "room", "id": "r1", "starred": False, "unread": 0}


def test_another_spaces_cursor_row_never_lists_the_room(cursor_db, monkeypatch):
    """A Workspace identity's row must not hand a platform credential that is
    not in the room the room's id (the #959 security review #1 rule)."""
    _room("r1", people=(WS_READER,))
    _mark("room", "r1", through="2026-10-01T10:00:00Z", reader=WS_READER, now=NOW, monkeypatch=monkeypatch)

    assert _entry(_state(PLATFORM_READER), "room", "r1") is None


def test_the_space_row_is_never_a_chat_kind_the_api_takes(cursor_db):
    from client_portal import service
    from client_portal.service import ClientPortalError

    with pytest.raises(ClientPortalError) as exc:
        service.mark_chat_read(ALICE, "room:user", "r1", room_reader=PLATFORM_READER)
    assert exc.value.status_code == 400


def test_the_row_cap_applies_to_a_new_space_row(cursor_db, monkeypatch):
    from client_portal import db as pdb

    _room("r1")
    monkeypatch.setattr(pdb, "MAX_CHAT_STATE_ROWS", 0)
    _mark("room", "r1", through="2026-10-01T10:00:00Z", now=NOW, monkeypatch=monkeypatch)

    assert _rows() == {}
