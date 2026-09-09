"""ent#557 — a reply in a chat you have never opened is unread.

ent#359 defined "unread" relative to a read cursor, and gave a thread with NO
cursor a count of nothing. That was right for the reason it was written for:
inventing a cursor at the beginning of time would have badged every historical
conversation in every install on the day it shipped.

What it could not foresee is ent#523, which made **Main the landing place for
everything an agent starts** — agent-initiated messages, asks raised outside a
chat, scheduled briefs all resolve to it. A freshly minted Main has never been
read by anyone, so the one case an unread badge exists for produced no badge
anywhere: the agent replied, and nothing indicated it.

The rule is now: a thread with no cursor counts agent messages newer than the
viewer's **account baseline**, which is the earliest read cursor they hold
anywhere. Read as *anything an agent has said to you since the first time you
read anything here, in a chat you have never opened*.

Both halves of `MIN` are load-bearing and both are pinned below:

  * **MIN, not MAX.** "Since you were last here" needs no baseline row and reads
    better, and it MOVES — so reading one chat would silently mark another read.
    That case is `test_reading_one_chat_does_not_silently_clear_another`, and it
    is the reason this file exists rather than a one-line predicate change.

  * **NULL when the viewer holds no cursor at all.** A first-ever sign-in still
    counts nothing — ent#359's property preserved rather than traded away, and
    it falls out of SQL's NULL comparison rather than a second branch.

Runs against a throwaway sqlite carrying the real tables (the ent#359 harness),
so the SQL is the code under test rather than a mock of it.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent557.db"
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
# The reported case
# ---------------------------------------------------------------------------

def test_an_agent_started_chat_you_have_never_opened_is_unread(chat_db):
    """The ent#523 Main case, which produced no badge anywhere before this."""
    from client_portal import db as pdb

    # Alice has used the Workspace: she has read something, once.
    pdb.mark_chat_read(ALICE, "thread", "t-old", "2026-09-01T09:00:00Z")
    # An agent then starts a conversation of its own and speaks into it.
    _msg(chat_db, session_id="main-scribe", email=ALICE, role="assistant",
         at="2026-09-08T14:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {"main-scribe": 1}


def test_several_messages_in_a_never_opened_chat_all_count(chat_db):
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-old", "2026-09-01T09:00:00Z")
    for at in ("2026-09-08T14:00:00Z", "2026-09-08T14:05:00Z", "2026-09-08T14:09:00Z"):
        _msg(chat_db, session_id="main-scribe", email=ALICE, role="assistant", at=at)

    assert pdb.count_unread_by_session(ALICE) == {"main-scribe": 3}


def test_the_viewers_own_messages_still_never_count(chat_db):
    """The role filter is unchanged; the new arm must not widen it."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-old", "2026-09-01T09:00:00Z")
    _msg(chat_db, session_id="t-new", email=ALICE, role="user",
         at="2026-09-08T14:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


# ---------------------------------------------------------------------------
# ent#359's property, preserved rather than traded away
# ---------------------------------------------------------------------------

def test_a_first_ever_sign_in_still_counts_nothing(chat_db):
    """No cursor anywhere ⇒ no baseline ⇒ nothing is unread. The whole reason
    the null-cursor rule was written, and it still holds: this is what stops
    every historical conversation badging on someone's first visit."""
    from client_portal import db as pdb

    for at in ("2026-01-01T09:00:00Z", "2026-05-05T09:00:00Z", "2026-09-08T14:00:00Z"):
        _msg(chat_db, session_id="t-history", email=ALICE, role="assistant", at=at)

    assert pdb.count_unread_by_session(ALICE) == {}


def test_a_chat_that_predates_the_baseline_is_not_retroactively_unread(chat_db):
    """The conservative direction. A conversation that existed before the viewer
    ever read anything is history, not a notification."""
    from client_portal import db as pdb

    _msg(chat_db, session_id="t-ancient", email=ALICE, role="assistant",
         at="2026-01-01T09:00:00Z")
    pdb.mark_chat_read(ALICE, "thread", "t-other", "2026-09-01T09:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


# ---------------------------------------------------------------------------
# Why MIN and not MAX — the bug the obvious rule would have shipped
# ---------------------------------------------------------------------------

def test_reading_one_chat_does_not_silently_clear_another(chat_db):
    """A baseline of "your most recent cursor" needs no extra state and reads
    beautifully. It is also wrong, and this is the case that proves it.

    A reply lands in a chat Alice has never opened. She then reads a DIFFERENT
    chat. Under a moving baseline the new cursor would overtake the reply's
    timestamp and the badge would vanish — reading chat A would mark chat B
    read, silently, which is worse than the missing badge this issue is about.
    """
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-08T10:00:00Z")
    _msg(chat_db, session_id="t-b", email=ALICE, role="assistant",
         at="2026-09-08T10:05:00Z")
    assert pdb.count_unread_by_session(ALICE) == {"t-b": 1}

    # She reads t-a again, later than the unread reply in t-b.
    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-08T10:10:00Z")

    assert pdb.count_unread_by_session(ALICE) == {"t-b": 1}, (
        "reading one chat must not clear the badge on another"
    )


def test_the_baseline_is_the_FIRST_read_and_then_frozen(chat_db):
    """Stated directly, so the rule is pinned rather than inferred.

    The baseline is written on the first `mark_chat_read` and never moved
    afterwards — not recomputed from the cursors that exist now. A later read
    with an EARLIER timestamp (clock skew, a backfill, a test) does not move it
    either: whichever read came first is the viewer's arrival.
    """
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-05T09:00:00Z")   # baseline
    pdb.mark_chat_read(ALICE, "thread", "t-b", "2026-09-01T09:00:00Z")   # earlier, ignored

    # Before the baseline: history, not a notification.
    _msg(chat_db, session_id="t-never", email=ALICE, role="assistant",
         at="2026-09-03T09:00:00Z")
    assert pdb.count_unread_by_session(ALICE) == {}

    # After it: unread.
    _msg(chat_db, session_id="t-never", email=ALICE, role="assistant",
         at="2026-09-06T09:00:00Z")
    assert pdb.count_unread_by_session(ALICE) == {"t-never": 1}


def test_the_baseline_row_is_invisible_to_the_sidebar_and_the_caps(chat_db):
    """It lives in the chat-state table under a reserved kind, so every read of
    that table has to exclude it. A leak here would put a phantom `account`
    chat in the sidebar payload and spend one of the viewer's capped rows on
    bookkeeping they did not ask for."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-01T09:00:00Z")

    kinds = [r["chat_kind"] for r in pdb.get_chat_state(ALICE)]
    assert kinds == ["thread"], f"baseline leaked into the sidebar payload: {kinds}"
    assert pdb.count_chat_state_rows(ALICE) == 1
    assert pdb.count_starred_rows(ALICE) == 0


def test_the_baseline_survives_a_star_being_removed(chat_db):
    """`set_chat_star` deletes a row that has nothing left to remember. The
    baseline has a cursor, so it is not that row — but the case is pinned
    because losing the baseline would silently re-arm every never-opened chat
    back to counting nothing."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-01T09:00:00Z")
    pdb.set_chat_star(ALICE, "thread", "t-star", True, "2026-09-02T09:00:00Z")
    pdb.set_chat_star(ALICE, "thread", "t-star", False, "2026-09-02T10:00:00Z")

    _msg(chat_db, session_id="t-never", email=ALICE, role="assistant",
         at="2026-09-08T14:00:00Z")
    assert pdb.count_unread_by_session(ALICE) == {"t-never": 1}


# ---------------------------------------------------------------------------
# Isolation is unchanged by the new arm
# ---------------------------------------------------------------------------

def test_one_viewers_baseline_does_not_reach_anothers_chats(chat_db):
    """The baseline subquery is scoped to the same email as the outer query. A
    shared agent means two viewers see the same threads, so a baseline that
    leaked would badge Bob's sidebar from Alice's reading habits."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-a", "2026-09-01T09:00:00Z")
    _msg(chat_db, session_id="t-shared", email=BOB, role="assistant",
         at="2026-09-08T14:00:00Z")

    # Bob has never read anything: no baseline, nothing unread.
    assert pdb.count_unread_by_session(BOB) == {}
    # And Alice cannot see Bob's message at all — it is not her row.
    assert pdb.count_unread_by_session(ALICE) == {}


def test_email_case_does_not_fork_the_baseline(chat_db):
    from client_portal import db as pdb

    pdb.mark_chat_read("Alice@Example.com", "thread", "t-a", "2026-09-01T09:00:00Z")
    _msg(chat_db, session_id="t-new", email=ALICE, role="assistant",
         at="2026-09-08T14:00:00Z")

    assert pdb.count_unread_by_session("ALICE@EXAMPLE.COM") == {"t-new": 1}


def test_reading_the_never_opened_chat_clears_it(chat_db):
    """AC 5, at the layer that decides the number."""
    from client_portal import db as pdb

    pdb.mark_chat_read(ALICE, "thread", "t-old", "2026-09-01T09:00:00Z")
    _msg(chat_db, session_id="main-scribe", email=ALICE, role="assistant",
         at="2026-09-08T14:00:00Z")
    assert pdb.count_unread_by_session(ALICE) == {"main-scribe": 1}

    pdb.mark_chat_read(ALICE, "thread", "main-scribe", "2026-09-08T14:30:00Z")
    assert pdb.count_unread_by_session(ALICE) == {}
