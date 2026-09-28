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


# ===========================================================================
# 2. An addressed report always has a chat of its addressee (D4)
# ===========================================================================
#
# `services/report_service.resolve_report_session(execution_id, agent, audience,
# allow_main=...)`: the publishing turn's in-flight chat only if the ADDRESSEE
# owns it, else — for the AGENT'S OWN publish only (`allow_main=True`) — the
# addressee's Main (minted if absent) touched with `added=0`; no audience → None;
# fail-soft None + WARNING.

@pytest.fixture()
def inflight(monkeypatch):
    """Control what the publishing turn resolves to: `valid` = the execution is
    this agent's; `session` = the ent#286 in-flight marker's answer."""
    import services.idempotency_service as idem
    from client_portal import service as portal_service

    state = {"valid": True, "session": None}
    monkeypatch.setattr(idem, "resolve_and_validate_execution",
                        lambda eid, agent: object() if state["valid"] else None)
    monkeypatch.setattr(portal_service, "get_inflight_session_for_execution",
                        lambda eid: state["session"])
    return state


def _main_of(engine, email, agent=AGENT):
    from sqlalchemy import text
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT id, last_message_at, message_count FROM enterprise_portal_sessions "
            "WHERE client_email = :e AND agent_name = :a AND is_main = 1 AND archived_at IS NULL"
        ), {"e": email, "a": agent}).mappings()]


def test_no_execution_stamps_the_addressees_main_minting_and_touching_it(inbox_db, inflight):
    from services import report_service
    sid = report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True)

    mains = _main_of(inbox_db, ALICE)
    assert [m["id"] for m in mains] == [sid]
    # Touched so the sidebar lists it (`is_main && !last_message_at` is hidden)…
    assert mains[0]["last_message_at"]
    # …with added=0: a report is not a message, so Reset's "untouched" test holds.
    assert int(mains[0]["message_count"] or 0) == 0


def test_an_existing_main_is_reused_not_duplicated(inbox_db, inflight):
    from services import report_service
    _session(inbox_db, "main-a", ALICE, is_main=1, last="2026-09-01T00:00:00Z", count=4)

    assert report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True) == "main-a"
    mains = _main_of(inbox_db, ALICE)
    assert len(mains) == 1
    assert mains[0]["last_message_at"] > "2026-09-01T00:00:00Z"
    assert int(mains[0]["message_count"]) == 4


def test_the_addressees_own_inflight_chat_wins(inbox_db, inflight):
    from services import report_service
    _session(inbox_db, "s-alice-turn", ALICE)
    inflight["session"] = "s-alice-turn"

    assert report_service.resolve_report_session("exec-1", AGENT, ALICE, allow_main=True) == "s-alice-turn"
    assert _main_of(inbox_db, ALICE) == []          # no Main minted for it


def test_a_report_for_alice_during_bobs_turn_goes_to_alices_main(inbox_db, inflight):
    """The Stage 2 C3 case: stamped into Bob's chat, Alice's arm never counted it
    and Bob's reader filters on audience = Bob — the card was in nobody's chat."""
    from services import report_service
    _session(inbox_db, "s-bob-turn", BOB)
    inflight["session"] = "s-bob-turn"

    sid = report_service.resolve_report_session("exec-1", AGENT, ALICE, allow_main=True)

    assert sid != "s-bob-turn"
    assert [m["id"] for m in _main_of(inbox_db, ALICE)] == [sid]


def test_a_foreign_execution_or_a_non_portal_turn_lands_in_main(inbox_db, inflight):
    from services import report_service
    inflight["valid"] = False
    first = report_service.resolve_report_session("exec-foreign", AGENT, ALICE, allow_main=True)
    inflight["valid"], inflight["session"] = True, None
    second = report_service.resolve_report_session("exec-cron", AGENT, ALICE, allow_main=True)

    assert first == second == _main_of(inbox_db, ALICE)[0]["id"]


def test_no_audience_is_no_chat_and_mints_nothing(inbox_db, inflight):
    from services import report_service
    inflight["session"] = "s-anything"

    assert report_service.resolve_report_session("exec-1", AGENT, None, allow_main=True) is None
    assert report_service.resolve_report_session(None, AGENT, "", allow_main=True) is None
    assert _main_of(inbox_db, ALICE) == []


def test_a_main_that_cannot_be_opened_fails_soft_with_a_warning(inbox_db, inflight, monkeypatch, caplog):
    import logging

    from client_portal import service as portal_service
    from services import report_service

    def boom(agent, email):
        raise RuntimeError("db down")

    monkeypatch.setattr(portal_service, "ensure_main_session", boom)
    with caplog.at_level(logging.WARNING):
        assert report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_concurrent_publishes_make_one_main(inbox_db, inflight):
    """Race-safe by the partial unique index (`idx_portal_sessions_main`), which
    this fixture carries because it is built by `init_schema`."""
    from concurrent.futures import ThreadPoolExecutor

    from services import report_service

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True), range(8)))

    assert len(set(ids)) == 1 and ids[0]
    assert len(_main_of(inbox_db, ALICE)) == 1


def test_resetting_a_report_only_main_is_still_the_already_fresh_no_op(inbox_db, inflight, monkeypatch):
    from client_portal import service as portal_service
    from services import report_service

    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(portal_service, "get_turn_inflight", lambda sid: None)
    main = report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True)

    out = portal_service.reset_main_session(AGENT, ALICE)

    assert out["main_session_id"] == main
    assert out["archived_session_id"] is None


def test_a_stamped_report_only_main_counts_and_survives_the_sidebar_filter(inbox_db, inflight):
    """End to end over the two halves: the stamp gives the report a chat the
    viewer owns, the arm counts it, and the touch keeps the Main listable."""
    from client_portal import db as pdb
    from services import report_service

    _read(ALICE, "s-old", "2026-09-01T09:00:00Z")
    sid = report_service.resolve_report_session(None, AGENT, ALICE, allow_main=True)
    _report(inbox_db, session_id=sid, addressed=ALICE, at="2099-01-01T00:00:00Z")

    assert pdb.count_unread_by_session(ALICE) == {sid: 1}
    assert _main_of(inbox_db, ALICE)[0]["last_message_at"]


# --- the router calls it -------------------------------------------------------

@pytest.mark.asyncio
async def test_the_publish_route_stamps_through_the_service_with_or_without_an_execution(monkeypatch):
    from client_portal import service as cps
    from models import ReportCreate, User
    from routers import reports as mod

    stored, calls = {}, []

    async def fake_create(**kwargs):
        stored.update(kwargs)
        return {"id": "r1", "agent_name": AGENT, "report_type": "recon.leads", "title": "Leads",
                "payload": {}, "created_at": "2026-09-10T00:00:00Z", "user_id": 1}

    monkeypatch.setattr(cps, "agent_on_roster", lambda agent, email, include_owned=False: True)
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(mod.report_service, "create_report", fake_create)
    monkeypatch.setattr(
        mod.report_service, "resolve_report_session",
        lambda eid, agent, audience, *, allow_main:
            calls.append((eid, agent, audience, allow_main)) or "main-x")

    body = ReportCreate(report_type="recon.leads", title="Leads", payload={"rows": []},
                        audience_email=ALICE)
    agent_key = User(id=1, username="admin", role="admin", email="admin@example.com",
                     agent_name=AGENT)
    await mod.create_report(body, AGENT, request=None, current_user=agent_key)

    assert calls == [(None, AGENT, ALICE, True)]
    assert stored["portal_session_id"] == "main-x"


@pytest.mark.asyncio
async def test_only_the_agents_own_publish_may_fall_back_to_the_addressees_main(monkeypatch):
    """/cso finding. `create_report` is gated by `AuthorizedAgent`, so every
    human the agent is shared with can publish as it. Before this rule, one such
    person could address a report to another person on the roster and D4 minted
    and touched THAT person's Main — a badge, an excerpt and a deliverable
    card in their Inbox that the agent never produced. A human publish keeps
    the addressee-owned in-flight chat (if any) and nothing else."""
    from client_portal import service as cps
    from models import ReportCreate, User
    from routers import reports as mod

    calls = []

    async def fake_create(**kwargs):
        return {"id": "r1", "agent_name": AGENT, "report_type": "recon.leads", "title": "Leads",
                "payload": {}, "created_at": "2026-09-10T00:00:00Z", "user_id": 1}

    monkeypatch.setattr(cps, "agent_on_roster", lambda agent, email, include_owned=False: True)
    monkeypatch.setattr(mod.rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(mod.report_service, "create_report", fake_create)
    monkeypatch.setattr(
        mod.report_service, "resolve_report_session",
        lambda eid, agent, audience, *, allow_main: calls.append(allow_main) or None)

    body = ReportCreate(report_type="recon.leads", title="Leads", payload={"rows": []},
                        audience_email=ALICE)
    human = User(id=2, username="bob", role="user", email=BOB)
    other_agent_key = User(id=1, username="admin", role="admin", email="admin@example.com",
                           agent_name="someone-else")
    await mod.create_report(body, AGENT, request=None, current_user=human)

    assert calls == [False]
    # An agent key for a DIFFERENT agent is refused outright (403) before any stamp.
    with pytest.raises(Exception):
        await mod.create_report(body, AGENT, request=None, current_user=other_agent_key)
    assert calls == [False]


def test_a_human_publish_never_mints_or_stamps_the_addressees_main(inbox_db, inflight):
    from services import report_service
    _read(ALICE, "s-old", "2026-09-01T09:00:00Z")
    before = __import__("client_portal.db", fromlist=["x"]).count_unread_by_session(ALICE)

    assert report_service.resolve_report_session(None, AGENT, ALICE, allow_main=False) is None
    inflight["session"] = None
    assert report_service.resolve_report_session("exec-1", AGENT, ALICE, allow_main=False) is None

    assert _main_of(inbox_db, ALICE) == []
    from client_portal import db as pdb
    assert pdb.count_unread_by_session(ALICE) == before


def test_a_human_publish_still_lands_in_the_addressees_own_inflight_chat(inbox_db, inflight):
    from services import report_service
    _session(inbox_db, "s-alice-turn", ALICE)
    inflight["session"] = "s-alice-turn"

    assert report_service.resolve_report_session(
        "exec-1", AGENT, ALICE, allow_main=False) == "s-alice-turn"


def test_a_human_publish_during_someone_elses_turn_is_not_stamped_anywhere(inbox_db, inflight):
    from services import report_service
    _session(inbox_db, "s-bob-turn", BOB)
    inflight["session"] = "s-bob-turn"

    assert report_service.resolve_report_session(
        "exec-1", AGENT, ALICE, allow_main=False) is None
    assert _main_of(inbox_db, ALICE) == []


# ===========================================================================
# 3. The run outcome is a platform-written marker, never the body (D5)
# ===========================================================================

async def _deliver_completion(status, email=ALICE, sid="s-a"):
    """Drive the REAL completion writer (`_resolve_portal` → `deliver`)."""
    from services import channel_completion_report as ccr

    deliver = ccr._resolve_portal(
        binding_agent=AGENT, executing_agent=AGENT, chat_id=sid,
        context_client=email, thread=None, status=status,
        summary_or_error="The leads are in.", execution_id="exec-1",
    )
    assert deliver is not None
    assert await deliver() is True


def _messages(engine, sid):
    from sqlalchemy import text
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT content, source FROM enterprise_portal_messages WHERE session_id = :s"
        ), {"s": sid}).mappings()]


@pytest.mark.asyncio
@pytest.mark.parametrize("status, marker", [
    ("success", "completion:done"),
    ("failed", "completion:failed"),
    ("timeout", "completion:failed"),
])
async def test_the_completion_writer_stamps_its_outcome_marker(inbox_db, monkeypatch, status, marker):
    from client_portal import service as portal_service
    monkeypatch.setattr(portal_service, "get_turn_inflight", lambda sid: None)
    _session(inbox_db, "s-a", ALICE)

    await _deliver_completion(status)

    rows = _messages(inbox_db, "s-a")
    assert [r["source"] for r in rows] == [marker]
    # The wording is decided by the SAME status, so the two cannot disagree.
    assert rows[0]["content"].startswith("**Finished**" if status == "success" else "**Didn't finish**")


# ===========================================================================
# 4. Previews ride the chat-state read (D5): one statement, no cost, roster-scoped
# ===========================================================================
#
# `db.unread_arrivals_with_latest` windows the SAME `_UNREAD_ARRIVALS` fragment
# the count groups; `client_portal/chat_previews.get_chat_state_with_previews`
# hands its counts to `service.get_chat_state(unread=...)` and attaches the newest
# arrival to roster-scoped threads. `GET /chat-state?previews=true` routes there.

@pytest.fixture()
def roster(monkeypatch):
    """The caller's roster, controlled per test (default: only AGENT)."""
    from client_portal import service as portal_service
    names = {"v": {AGENT}}
    monkeypatch.setattr(portal_service, "roster_agent_names",
                        lambda email, include_owned: set(names["v"]))
    return names


def _previews(email=ALICE, is_platform=False):
    from client_portal import chat_previews
    return chat_previews.get_chat_state_with_previews(email, is_platform)


def _entry(state, sid):
    return next(c for c in state["chats"] if c["kind"] == "thread" and c["id"] == sid)


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def test_the_latest_arrival_is_the_newest_and_the_first_unread_is_the_earliest_message(inbox_db, roster):
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    m1 = _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z", content="first")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T11:00:00Z", content="second")
    rid = _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T12:00:00Z",
                  title="Weekly **leads**", display_hint="table")

    e = _entry(_previews(), "s-a")
    assert e["unread"] == 3
    assert e["first_unread_message_id"] == m1
    assert e["latest"] == {"kind": "deliverable", "id": rid, "at": "2026-09-10T12:00:00Z",
                           "excerpt": "Weekly leads", "outcome": None,
                           "title": "Weekly **leads**", "display_hint": "table"}


def test_a_chat_whose_only_arrivals_are_deliverables_has_no_first_unread_message(inbox_db, roster):
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T08:00:00Z")   # read
    _report(inbox_db, session_id="s-a", addressed=ALICE, at="2026-09-10T10:00:00Z")

    e = _entry(_previews(), "s-a")
    assert e["unread"] == 1 and e["latest"]["kind"] == "deliverable"
    assert e["first_unread_message_id"] is None


def test_the_excerpt_is_markdown_stripped_single_line_and_bounded(inbox_db, roster):
    from client_portal.chat_previews import EXCERPT_MAX_CHARS
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    body = "# Heading\n\n**Bold** and [a link](http://x.test) `code`\n> quoted\n- item " + "word " * 80
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z", content=body)

    ex = _entry(_previews(), "s-a")["latest"]["excerpt"]
    assert ex.startswith("Heading Bold and a link code quoted item word")
    assert len(ex) <= EXCERPT_MAX_CHARS and ex.endswith("…")
    assert not any(ch in ex for ch in "*#`[]>\n")


@pytest.mark.parametrize("text, expected", [
    (None, None), ("", None), ("```\ncode only\n```", None),
    ("<b>hi</b>\x00there", "hi there"), ("![alt](img.png) ok", "alt ok"),
    ("see file_name and *this*", "see file_name and this"),
])
def test_the_excerpt_helper_edges(text, expected):
    from client_portal.chat_previews import _arrival_excerpt
    assert _arrival_excerpt(text) == expected


def test_the_excerpt_redacts_credentials_before_stripping_markdown():
    from client_portal.chat_previews import _arrival_excerpt
    key = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    pat = "github_pat_" + "A" * 30
    ex = _arrival_excerpt(f"Use **{key}** or {pat} with API_KEY=hunter2hunter2")
    assert key not in ex and pat not in ex and "hunter2" not in ex
    assert "githubpat" not in ex.lower()   # never mangled into a shape the patterns miss
    assert "REDACTED" in ex


def test_no_cost_key_anywhere_in_the_preview_payload(inbox_db, roster):
    """AC 7: a portal message carries a `cost` column; no projection #610 adds
    may emit it (nor an execution id)."""
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z", cost=4.2)

    state = _previews()
    assert _entry(state, "s-a")["latest"]["kind"] == "message"
    keys = set(_walk_keys(state))
    assert "cost" not in keys and "execution_id" not in keys, keys


def test_an_off_roster_agents_thread_keeps_its_count_but_carries_no_preview(inbox_db, roster):
    _session(inbox_db, "s-a", ALICE)
    _session(inbox_db, "s-gone", ALICE, agent="departed")
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _read(ALICE, "s-gone", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z")
    _msg(inbox_db, session_id="s-gone", email=ALICE, agent="departed", at="2026-09-10T10:00:00Z")

    state = _previews()
    assert "latest" in _entry(state, "s-a")
    gone = _entry(state, "s-gone")
    assert gone["unread"] == 1                       # the known limitation: counted
    assert "latest" not in gone and "first_unread_message_id" not in gone


def test_the_roster_is_read_with_the_principal_kind(inbox_db, monkeypatch):
    from client_portal import service as portal_service
    seen = []
    monkeypatch.setattr(portal_service, "roster_agent_names",
                        lambda email, include_owned: seen.append(include_owned) or {AGENT})
    _previews(is_platform=True)
    _previews(is_platform=False)
    assert seen == [True, False]


def test_previews_are_bounded_to_the_most_recent_threads(inbox_db, roster, monkeypatch):
    from client_portal import chat_previews
    monkeypatch.setattr(chat_previews, "MAX_PREVIEWS", 2)
    _state(inbox_db, ALICE, "account", "baseline", "2026-09-10T00:00:00Z")
    for i in range(4):
        _session(inbox_db, f"s{i}", ALICE)
        _msg(inbox_db, session_id=f"s{i}", email=ALICE, at=f"2026-09-10T1{i}:00:00Z")

    state = _previews()
    with_preview = sorted(c["id"] for c in state["chats"] if "latest" in c)
    assert with_preview == ["s2", "s3"]
    assert all(c["unread"] == 1 for c in state["chats"] if c["kind"] == "thread")


def test_the_counts_come_from_the_same_statement_not_a_second_read(inbox_db, roster, monkeypatch):
    """The emitted `unread` and the preview are one snapshot: the previews path
    must hand its counts to `get_chat_state`, never let it re-count."""
    from client_portal import db as pdb
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z")

    def _second_read(email):
        raise AssertionError("count_unread_by_session must not run on the previews path")
    monkeypatch.setattr(pdb, "count_unread_by_session", _second_read)
    assert _entry(_previews(), "s-a")["unread"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status, outcome", [
    ("success", "done"), ("failed", "failed"), ("timeout", "failed"),
])
async def test_the_outcome_comes_from_the_real_completion_writers_marker(
        inbox_db, roster, monkeypatch, status, outcome):
    from client_portal import service as portal_service
    monkeypatch.setattr(portal_service, "get_turn_inflight", lambda sid: None)
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")

    await _deliver_completion(status)

    latest = _entry(_previews(), "s-a")["latest"]
    assert latest["kind"] == "message" and latest["outcome"] == outcome


@pytest.mark.parametrize("source", [None, "voice", "completion:bogus"])
def test_a_message_that_merely_says_finished_has_no_outcome(inbox_db, roster, source):
    """The pill is platform-written: an agent reply typing "**Finished**" does not
    make a finished run, and no source other than the two markers maps."""
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z",
         content="**Finished** — all done", source=source)

    latest = _entry(_previews(), "s-a")["latest"]
    assert latest["kind"] == "message" and latest["outcome"] is None


# --- the route ---------------------------------------------------------------

def _chat_state_client(email=ALICE, is_platform=False):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from client_portal import router as pr
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal

    app = FastAPI()
    app.include_router(pr.router)
    app.dependency_overrides[get_portal_principal] = (
        lambda: PortalPrincipal(email=email, is_platform=is_platform))
    return TestClient(app, raise_server_exceptions=False)


def test_without_previews_the_route_answers_the_ent359_shape_unchanged(inbox_db, roster):
    from client_portal import service as portal_service
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z")

    r = _chat_state_client().get("/api/enterprise/client-portal/chat-state")
    assert r.status_code == 200, r.text
    assert r.json() == portal_service.get_chat_state(ALICE)
    assert all(set(c) == {"kind", "id", "starred", "unread"} for c in r.json()["chats"])


def test_with_previews_the_route_attaches_the_latest_arrival_and_no_cost(inbox_db, roster):
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    mid = _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z",
               content="Here you go", cost=1.5)

    r = _chat_state_client().get("/api/enterprise/client-portal/chat-state?previews=true")
    assert r.status_code == 200, r.text
    e = _entry(r.json(), "s-a")
    assert e["unread"] == 1 and e["first_unread_message_id"] == mid
    assert e["latest"] == {"kind": "message", "id": mid, "at": "2026-09-10T10:00:00Z",
                           "excerpt": "Here you go"}
    assert "cost" not in set(_walk_keys(r.json()))


@pytest.mark.parametrize("is_platform", [True, False])
def test_the_route_reads_the_roster_with_the_callers_principal_kind(inbox_db, monkeypatch, is_platform):
    """The door: a platform user's previews cover the agents they own, a portal
    token's only its shared roster. The route must forward the principal kind,
    never a constant (a hard-coded kind widens or narrows the preview set)."""
    from client_portal import service as portal_service
    seen = []
    monkeypatch.setattr(portal_service, "roster_agent_names",
                        lambda email, include_owned: seen.append(include_owned) or {AGENT})
    r = _chat_state_client(is_platform=is_platform).get(
        "/api/enterprise/client-portal/chat-state?previews=true")
    assert r.status_code == 200, r.text
    assert seen == [is_platform]


def test_an_unreadable_roster_fails_the_previews_read_loudly(inbox_db, monkeypatch):
    """Fail loud (the D7 twin): a roster outage is a 5xx the shell reports as a
    stale read, never a 200 whose threads silently lost their previews."""
    from client_portal import service as portal_service
    _session(inbox_db, "s-a", ALICE)
    _read(ALICE, "s-a", "2026-09-10T09:00:00Z")
    _msg(inbox_db, session_id="s-a", email=ALICE, at="2026-09-10T10:00:00Z")

    def boom(email, include_owned):
        raise RuntimeError("roster store down")
    monkeypatch.setattr(portal_service, "roster_agent_names", boom)
    r = _chat_state_client().get("/api/enterprise/client-portal/chat-state?previews=true")
    assert r.status_code >= 500


# --- the property: n == the count, latest is the max, first is the min message -

@st.composite
def _world_ids(draw):
    msgs = draw(st.lists(st.tuples(st.sampled_from(_SESSIONS), st.sampled_from(["assistant", "user"]),
                                   st.sampled_from(_T)), max_size=10))
    reps = draw(st.lists(st.tuples(st.sampled_from(_SESSIONS), st.sampled_from([ALICE, BOB, None]),
                                   st.sampled_from(_T)), max_size=10))
    cursors = draw(st.dictionaries(st.sampled_from(_SESSIONS), st.sampled_from(_T), max_size=3))
    baseline = draw(st.dictionaries(st.sampled_from(_EMAILS), st.sampled_from(_T), max_size=2))
    return msgs, reps, cursors, baseline


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(world=_world_ids())
def test_the_windowed_read_agrees_with_the_count_per_session(inbox_db, world):
    from client_portal import db as pdb
    msgs, reps, cursors, baseline = world
    _wipe(inbox_db)
    for sid, owner in _SESSIONS:
        _session(inbox_db, sid, owner)
    arrivals = []   # (owner, sid, at, id, kind) — every row, filtered below
    for i, ((sid, owner), role, at) in enumerate(msgs):
        mid = _msg(inbox_db, session_id=sid, email=owner, role=role, at=at, msg_id=f"m{i:02d}")
        if role == "assistant":
            arrivals.append((owner, sid, at, mid, "message", owner))
    for i, ((sid, owner), to, at) in enumerate(reps):
        rid = _report(inbox_db, session_id=sid, addressed=to, at=at, rid=f"r{i:02d}")
        arrivals.append((owner, sid, at, rid, "deliverable", to))
    for email, at in baseline.items():
        _state(inbox_db, email, "account", "baseline", at)
    for (sid, owner), at in cursors.items():
        _state(inbox_db, owner, "thread", sid, at)

    for email in _EMAILS:
        counts = pdb.count_unread_by_session(email)
        windowed = pdb.unread_arrivals_with_latest(email)
        assert {s: w["n"] for s, w in windowed.items()} == counts
        for sid, w in windowed.items():
            owner = dict(_SESSIONS)[sid]
            floor = cursors.get((sid, owner), baseline.get(email))
            mine = [a for a in arrivals
                    if a[1] == sid and a[0] == email and a[5] == email
                    and floor is not None and a[2] > floor]
            assert w["n"] == len(mine) > 0
            newest = max(mine, key=lambda a: (a[2], a[3]))
            assert (w["latest"]["at"], w["latest"]["id"]) == (newest[2], newest[3])
            assert w["latest"]["at"] == max(a[2] for a in mine)
            msgs_only = [a for a in mine if a[4] == "message"]
            expected_first = min(msgs_only, key=lambda a: (a[2], a[3]))[3] if msgs_only else None
            assert w["first_unread_message_id"] == expected_first
