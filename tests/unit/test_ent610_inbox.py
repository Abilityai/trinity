"""trinity-enterprise#610 — the Workspace Inbox's backend.

The Inbox owns no table, no router and no store. Everything here is a change to a
rule that already existed, so every test pins the rule at the seam it lives in,
against a real SQLite file built by the platform's own `init_schema` (the
partial unique index on Main and the report indexes are part of what is tested).

1. **The deliverable arm (D3).** `count_unread_by_session` is still the ONLY
   unread function. It gains a second arm: a report addressed to me, stamped to a
   session I own, created after that chat's cursor (or my account baseline when
   the chat has none). Same cursor, no second read model.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT = "scribe"


@pytest.fixture()
def inbox_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent610.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.schema import init_schema

    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()

    from db.engine import get_engine
    yield get_engine()


def _wipe(engine):
    from sqlalchemy import text
    with engine.begin() as conn:
        for t in ("agent_reports", "enterprise_portal_messages",
                  "enterprise_portal_sessions", "enterprise_portal_chat_state"):
            conn.execute(text(f"DELETE FROM {t}"))


def _session(engine, sid, email, *, agent=AGENT, is_main=0, last=None, count=0):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_portal_sessions "
            "(id, agent_name, client_email, created_at, last_message_at, message_count, is_main) "
            "VALUES (:id, :a, :e, :c, :l, :n, :m)"
        ), {"id": sid, "a": agent, "e": email.lower(), "c": "2026-09-01T00:00:00Z",
            "l": last, "n": count, "m": is_main})


def _msg(engine, *, session_id, email, at, role="assistant", agent=AGENT,
         content="hi", source=None, cost=None, msg_id=None):
    from sqlalchemy import text
    mid = msg_id or uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_portal_messages "
            "(id, agent_name, client_email, session_id, role, content, cost, created_at, source) "
            "VALUES (:id, :a, :e, :s, :r, :c, :cost, :at, :src)"
        ), {"id": mid, "a": agent, "e": email.lower(), "s": session_id, "r": role,
            "c": content, "cost": cost, "at": at, "src": source})
    return mid


def _report(engine, *, session_id, addressed, at, agent=AGENT, title="Leads",
            display_hint="table", rid=None):
    from sqlalchemy import text
    rid = rid or uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_reports "
            "(id, agent_name, report_type, title, payload, display_hint, created_at, "
            " addressed_to_email, portal_session_id) "
            "VALUES (:id, :a, 'recon.leads', :t, '{}', :h, :at, :to, :sid)"
        ), {"id": rid, "a": agent, "t": title, "h": display_hint, "at": at,
            "to": addressed, "sid": session_id})
    return rid


def _state(engine, email, kind, cid, at):
    """A chat-state row written directly (the property sets cursors and
    baselines at chosen instants; `mark_chat_read` would stamp the baseline too)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_portal_chat_state "
            "(client_email, chat_kind, chat_id, last_read_at, updated_at) "
            "VALUES (:e, :k, :c, :at, :at)"
        ), {"e": email, "k": kind, "c": cid, "at": at})


def _read(email, sid, at):
    from client_portal import db as pdb
    pdb.mark_chat_read(email, "thread", sid, at)


# ===========================================================================
# 1. The deliverable arm (D3)
# ===========================================================================

def test_a_report_addressed_to_me_in_my_chat_after_the_cursor_is_new(inbox_db):
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {"s-a": 1}


def test_a_deliverable_produced_inside_a_turn_is_its_own_arrival_beside_the_reply(inbox_db):
    """"2 new": the reply and the report are two things that came back."""
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T10:00:01Z")

    assert pdb.count_unread_by_session(ALICE) == {"s-a": 2}


def test_a_report_addressed_to_me_but_stamped_in_someone_elses_chat_does_not_count(inbox_db):
    """The arm joins session OWNERSHIP. A card in Bob's chat is not in my Inbox —
    and nobody's reader would show it there (D4 is what stops it being written)."""
    from client_portal import db as pdb
    _session(inbox_db, "s-bob", BOB)
    _read(ALICE, "s-other", "2026-09-10T09:00:00Z")   # Alice has a baseline
    _read(BOB, "s-bob", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-bob", addressed=ALICE, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}
    assert pdb.count_unread_by_session(BOB) == {}


def test_an_unaddressed_report_in_my_chat_does_not_count(inbox_db):
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=None, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_a_report_addressed_to_someone_else_in_my_chat_does_not_count(inbox_db):
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=BOB, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_a_report_at_or_before_the_cursor_is_read(inbox_db):
    """Strictly after: a report written at the instant the chat was read was seen."""
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T08:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_a_never_opened_main_counts_a_deliverable_against_the_baseline(inbox_db):
    """The D4 case: a report-only Main the viewer has never opened. No cursor, so
    the account baseline decides — the #557 rule, unchanged, for deliverables."""
    from client_portal import db as pdb
    _read(ALICE, "s-old", "2026-09-01T09:00:00Z")          # the baseline
    _session(inbox_db, "main-a", ALICE, is_main=1)
    _report(inbox_db, session_id="main-a", addressed=ALICE, at="2026-09-10T10:00:00Z")
    _report(inbox_db, session_id="main-a", addressed=ALICE, at="2026-08-01T10:00:00Z")  # before

    assert pdb.count_unread_by_session(ALICE) == {"main-a": 1}


def test_a_first_ever_reader_counts_nothing_including_a_first_deliverable(inbox_db):
    """The inherited #557 property, stated in §5.40: no baseline ⇒ nothing counts."""
    from client_portal import db as pdb
    _session(inbox_db, "main-a", ALICE, is_main=1)
    _report(inbox_db, session_id="main-a", addressed=ALICE, at="2026-09-10T10:00:00Z")
    _msg(inbox_db, session_id="main-a", email=ALICE, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {}


def test_the_audience_is_matched_on_a_lowercased_bind_not_lower_of_the_column(inbox_db):
    """The bind is lowercased Python-side; the column is normalised at the
    boundary (#2955). A mixed-case caller still matches."""
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T10:00:00Z")

    assert pdb.count_unread_by_session("Alice@Example.com") == {"s-a": 1}


def test_the_deliverable_arm_uses_the_audience_index_not_a_scan(inbox_db):
    """EXPLAIN QUERY PLAN for arm (ii): `agent_reports` is reached through an
    index. `lower(addressed_to_email)` would scan the fleet's reports every poll."""
    from sqlalchemy import text

    from client_portal import db as pdb
    with inbox_db.connect() as conn:
        plan = conn.execute(text("EXPLAIN QUERY PLAN SELECT * FROM (" + pdb._UNREAD_ARRIVALS + ") AS a"),
                            {"email": ALICE, "bkind": pdb.BASELINE_KIND, "bid": pdb.BASELINE_ID}).fetchall()
    details = [str(row[-1]) for row in plan]
    report_steps = [d for d in details if "agent_reports" in d]
    assert report_steps, details
    assert all("USING" in d and "INDEX" in d for d in report_steps), details
    assert any("idx_agent_reports_audience" in d for d in report_steps), details


# --- the property: the count IS the number of arrivals ------------------------

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

_T = [f"2026-09-10T{h:02d}:00:00Z" for h in range(8, 20)]
_EMAILS = [ALICE, BOB]
_SESSIONS = [("s1", ALICE), ("s2", ALICE), ("s3", BOB)]


@st.composite
def _world(draw):
    msgs = draw(st.lists(st.tuples(st.sampled_from(_SESSIONS), st.sampled_from(["assistant", "user"]),
                                   st.sampled_from(_T)), max_size=12))
    reps = draw(st.lists(st.tuples(st.sampled_from(_SESSIONS), st.sampled_from([ALICE, BOB, None]),
                                   st.sampled_from(_T)), max_size=12))
    cursors = draw(st.dictionaries(st.sampled_from(_SESSIONS), st.sampled_from(_T), max_size=3))
    baseline = draw(st.dictionaries(st.sampled_from(_EMAILS), st.sampled_from(_T), max_size=2))
    return msgs, reps, cursors, baseline


def _expected(email, msgs, reps, cursors, baseline):
    """The rule, written out longhand, per session."""
    out: dict = {}
    for (sid, owner), role, at in msgs:
        if owner != email or role != "assistant":
            continue
        cur = cursors.get((sid, owner))
        floor = cur if cur is not None else baseline.get(email)
        if floor is not None and at > floor:
            out[sid] = out.get(sid, 0) + 1
    for (sid, owner), to, at in reps:
        if owner != email or to != email:
            continue
        cur = cursors.get((sid, owner))
        floor = cur if cur is not None else baseline.get(email)
        if floor is not None and at > floor:
            out[sid] = out.get(sid, 0) + 1
    return out


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(world=_world())
def test_the_count_is_exactly_the_arrivals_per_session(inbox_db, world):
    from client_portal import db as pdb
    msgs, reps, cursors, baseline = world
    _wipe(inbox_db)
    for sid, owner in _SESSIONS:
        _session(inbox_db, sid, owner)
    for (sid, owner), role, at in msgs:
        _msg(inbox_db, session_id=sid, email=owner, role=role, at=at)
    for (sid, _owner), to, at in reps:
        _report(inbox_db, session_id=sid, addressed=to, at=at)
    for email, at in baseline.items():
        _state(inbox_db, email, "account", "baseline", at)
    for (sid, owner), at in cursors.items():
        _state(inbox_db, owner, "thread", sid, at)

    for email in _EMAILS:
        assert pdb.count_unread_by_session(email) == _expected(
            email, msgs, reps, cursors, baseline)
