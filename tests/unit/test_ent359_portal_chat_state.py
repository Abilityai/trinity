"""Per-viewer star + unread state for the Workspace sidebar (ent#359).

The sidebar gains two things that are only meaningful relative to *who is
looking*: starred chats pinned above the date groups, and a per-agent "waiting
on you" badge. Everything worth testing here follows from that one word.

Three properties, in order of how badly they fail:

  * **Isolation.** A star is one person's bookmark and a room is shared between
    several people. If this state were reachable across users — or had been
    stored on the room row, which is the obvious-looking design — one
    participant's star would appear in every other participant's sidebar, and
    one client would learn which chats another client keeps.

  * **The unread definition.** "Unread" is relative to a read cursor. A thread
    with no cursor must report NOTHING rather than reporting its whole history,
    or the day this shipped every historical conversation in every install would
    have lit up with a badge — noise that trains people to ignore the badge,
    which is worse than not having one.

  * **The row cap.** Neither writer verifies the chat exists, deliberately: a
    404 for an unknown id would turn `star` into an existence oracle over every
    chat id in the install (OSS invariant #8). The cap is what stands in for
    that validation, so it has to bound NEW rows without freezing the ones a
    user legitimately owns.

Runs against a throwaway sqlite carrying the real tables, so the unread SQL —
the join and the `created_at > last_read_at` predicate — is the code under test
rather than a mock of it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
NOW = "2026-08-12T10:00:00Z"


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-chat-state.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_chat_state,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    oss_metadata.create_all(get_engine(), tables=[
        enterprise_portal_chat_state,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    ])
    yield get_engine()


def _msg(engine, *, session_id, email, role, at, agent="scribe"):
    from db.tables import enterprise_portal_messages as m
    import uuid
    with engine.begin() as conn:
        conn.execute(m.insert().values(
            id=uuid.uuid4().hex, agent_name=agent, client_email=email.lower(),
            session_id=session_id, role=role, content="hi", cost=None, created_at=at,
        ))


# ---------------------------------------------------------------------------
# Isolation — the property that a column on the chat row could not have given us
# ---------------------------------------------------------------------------

def test_a_star_is_visible_only_to_the_person_who_set_it(chat_db):
    """The reason this is a separate table and not a `starred` column.

    A room is shared. Had the star lived on the room row, Alice starring a room
    would have pinned it in Bob's sidebar too — and told him she cares about it.
    """
    from client_portal import db as pdb

    pdb.set_chat_star(ALICE, "room", "room-1", True, NOW)

    assert [c["chat_id"] for c in pdb.get_chat_state(ALICE)] == ["room-1"]
    assert pdb.get_chat_state(BOB) == []


def test_state_is_keyed_by_kind_so_the_two_id_spaces_cannot_collide(chat_db):
    """Thread ids and room ids are independent. Starring thread `x` must not
    star room `x`."""
    from client_portal import db as pdb

    pdb.set_chat_star(ALICE, "thread", "x", True, NOW)
    rows = {(r["chat_kind"], r["chat_id"]): r for r in pdb.get_chat_state(ALICE)}

    assert ("thread", "x") in rows
    assert ("room", "x") not in rows


def test_email_case_does_not_fork_a_users_state(chat_db):
    """Sign-in normalises to lower-case, but a caller reaching the service with
    the address as typed must not get a second, empty set of stars."""
    from client_portal import db as pdb

    pdb.set_chat_star("Alice@Example.com", "thread", "t1", True, NOW)
    assert [c["chat_id"] for c in pdb.get_chat_state(ALICE)] == ["t1"]


# ---------------------------------------------------------------------------
# Unread — defined relative to a cursor, and only relative to a cursor
# ---------------------------------------------------------------------------

def test_a_thread_with_no_read_cursor_reports_nothing_unread(chat_db):
    """The alternative — treating "never read" as "all unread" — would have
    badged every historical conversation on the day this shipped."""
    from client_portal import db as pdb

    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_only_agent_messages_after_the_cursor_count(chat_db):
    from client_portal import db as pdb

    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:00:00Z")
    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T09:30:00Z")
    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:40:00Z")
    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:50:00Z")

    assert pdb.count_unread_by_session(ALICE) == {"t1": 2}


def test_the_users_own_messages_are_never_unread(chat_db):
    """Sending marks the thread read, so a user's own turn arriving "after the
    cursor" must not badge the chat they are sitting in."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T09:00:00Z")
    _msg(chat_db, session_id="t1", email=ALICE, role="user", at="2026-08-12T09:10:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_reading_again_clears_the_count(chat_db):
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T09:00:00Z")
    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:10:00Z")
    assert pdb.count_unread_by_session(ALICE) == {"t1": 1}

    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T09:20:00Z")
    assert pdb.count_unread_by_session(ALICE) == {}


def test_one_users_unread_is_not_another_users_unread(chat_db):
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T09:00:00Z")
    pdb.mark_chat_read(BOB, "thread", "t2", "2026-08-12T09:00:00Z")
    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2026-08-12T09:10:00Z")

    assert pdb.count_unread_by_session(ALICE) == {"t1": 1}
    assert pdb.count_unread_by_session(BOB) == {}


def test_star_and_read_do_not_overwrite_each_other(chat_db):
    """Both write the same row. A partial upsert that wrote every column would
    make opening a chat silently unstar it."""
    from client_portal import db as pdb

    pdb.set_chat_star(ALICE, "thread", "t1", True, NOW)
    pdb.mark_chat_read(ALICE, "thread", "t1", "2026-08-12T11:00:00Z")

    row = pdb.get_chat_state(ALICE)[0]
    assert row["starred_at"] and row["last_read_at"] == "2026-08-12T11:00:00Z"

    pdb.set_chat_star(ALICE, "thread", "t1", False, "2026-08-12T12:00:00Z")
    row = pdb.get_chat_state(ALICE)[0]
    assert row["starred_at"] is None
    # Unstar keeps the cursor: dropping the row would mark the whole thread
    # unread again, so unstarring a chat would light up a badge for it.
    assert row["last_read_at"] == "2026-08-12T11:00:00Z"


# ---------------------------------------------------------------------------
# Service rules — validation, cap, and the shape the router returns
# ---------------------------------------------------------------------------

def test_unknown_chat_kind_is_rejected(chat_db):
    from client_portal import service as svc

    with pytest.raises(svc.ClientPortalError) as e:
        svc.set_chat_star(ALICE, "sqlmap", "t1", True)
    assert e.value.status_code == 400


@pytest.mark.parametrize("bad", ["", "   ", "x" * 129])
def test_out_of_bounds_chat_ids_are_rejected(chat_db, bad):
    """The id is attacker-chosen text that lands in a primary key, because the
    writers deliberately do not check that the chat exists."""
    from client_portal import service as svc

    with pytest.raises(svc.ClientPortalError) as e:
        svc.set_chat_star(ALICE, "thread", bad, True)
    assert e.value.status_code == 400


def test_starring_an_unknown_chat_succeeds_rather_than_leaking_existence(chat_db):
    """A 404 here would answer "does chat X exist?" for every id in the install.
    The write is confined to the caller's own rows, so it costs nothing."""
    from client_portal import service as svc

    svc.set_chat_star(ALICE, "thread", "no-such-chat", True)

    from client_portal import db as pdb
    assert [c["chat_id"] for c in pdb.get_chat_state(ALICE)] == ["no-such-chat"]


def test_the_row_cap_stops_new_rows(chat_db, monkeypatch):
    from client_portal import db as pdb
    from client_portal import service as svc

    monkeypatch.setattr(pdb, "MAX_CHAT_STATE_ROWS", 2)
    svc.set_chat_star(ALICE, "thread", "t1", True)
    svc.set_chat_star(ALICE, "thread", "t2", True)

    with pytest.raises(svc.ClientPortalError) as e:
        svc.set_chat_star(ALICE, "thread", "t3", True)
    assert e.value.status_code == 409


def test_the_cap_never_freezes_a_row_the_user_already_owns(chat_db, monkeypatch):
    """A cap that applied to updates would leave a user at the ceiling unable to
    unstar (the only action that gets them back under it) or to advance a read
    cursor they already have — punishing them for state they legitimately
    accumulated."""
    from client_portal import db as pdb
    from client_portal import service as svc

    monkeypatch.setattr(pdb, "MAX_CHAT_STATE_ROWS", 2)
    svc.set_chat_star(ALICE, "thread", "t1", True)
    svc.set_chat_star(ALICE, "thread", "t2", True)

    svc.mark_chat_read(ALICE, "thread", "t1")          # update, not insert
    svc.set_chat_star(ALICE, "thread", "t1", False)    # the way back under the cap

    rows = {r["chat_id"]: r for r in pdb.get_chat_state(ALICE)}
    assert rows["t1"]["last_read_at"] is not None
    assert rows["t1"]["starred_at"] is None


def test_mark_read_at_the_cap_is_a_no_op_not_an_error(chat_db, monkeypatch):
    """Opening a chat must not fail because a bookkeeping table is full."""
    from client_portal import db as pdb
    from client_portal import service as svc

    monkeypatch.setattr(pdb, "MAX_CHAT_STATE_ROWS", 1)
    svc.set_chat_star(ALICE, "thread", "t1", True)

    svc.mark_chat_read(ALICE, "thread", "brand-new")   # must not raise

    assert [c["chat_id"] for c in pdb.get_chat_state(ALICE)] == ["t1"]


def test_get_chat_state_reports_stars_and_unread_together(chat_db):
    from client_portal import service as svc

    svc.set_chat_star(ALICE, "room", "r1", True)
    svc.mark_chat_read(ALICE, "thread", "t1")
    _msg(chat_db, session_id="t1", email=ALICE, role="assistant", at="2099-01-01T00:00:00Z")

    by_ref = {(c["kind"], c["id"]): c for c in svc.get_chat_state(ALICE)["chats"]}

    assert by_ref[("room", "r1")]["starred"] is True
    assert by_ref[("room", "r1")]["unread"] == 0        # rooms carry their own cursor
    assert by_ref[("thread", "t1")]["unread"] == 1
    assert by_ref[("thread", "t1")]["starred"] is False
