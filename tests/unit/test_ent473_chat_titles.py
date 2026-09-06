"""ent#473 — rename a chat or room, and make generated titles trustworthy.

Three hands write `enterprise_portal_sessions.title`: the derived fallback
(first-message prefix), the ent#186 generated title, and — from this issue — a
person. The properties worth pinning are the ones where the obvious code is
subtly wrong:

  * **A person's title stands.** Generation runs off the reply path, so a
    rename typed inside the first turn's 15 s window races the model's guess.
    The guard is in the UPDATE (`title_source != 'user'`), not a read-then-write
    in the caller, so there is no window between the check and the write.

  * **One more attempt, not one more forever.** ent#186 titled a thread once,
    from its opener; a greeting-shaped opener or a failed first attempt left the
    thread mis-titled for good. `_title_plan` earns the retry on the exchange
    after the opener and nothing past it — `message_count <= 2` is the bound.

  * **Failure is observable once.** Every path in the generator is fail-soft
    for the client (right) and was silent for the operator (wrong). The health
    record warns on the transition INTO a bad state, stays quiet in it, and
    warns again after a recovery.

  * **The boundary refuses with a name.** An empty, multi-line or over-long
    title is a 400 carrying `invalid_title` and a sentence with an example —
    the same rule for a thread and a room, from one leaf.

Runs the db-layer tests against a throwaway sqlite carrying the real tables, so
the guard's WHERE clause is the code under test rather than a mock of it.
"""
from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit

ALICE = "alice@example.com"
BOB = "bob@example.com"
NOW = "2026-09-06T10:00:00Z"


# ---------------------------------------------------------------------------
# The shared validator leaf
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("  Q3   invoices  ", "Q3 invoices"),          # outer trim, inner collapse
    ("tab\tseparated", "tab separated"),          # a tab is whitespace
    ("bell\x07char", "bell char"),                # control chars → whitespace
    ("trailing newline\n", "trailing newline"),   # an OUTER line break is trimmed
    ("Q3 invoices?", "Q3 invoices?"),             # a person's punctuation is kept
    ("x" * 100, "x" * 100),                       # exactly the cap is fine
])
def test_normalize_accepts_and_normalises(raw, expected):
    from services.chat_title import normalize_chat_title
    assert normalize_chat_title(raw) == (expected, None)


@pytest.mark.parametrize("raw, reason", [
    ("", "empty"),
    ("   ", "empty"),
    (None, "empty"),
    (42, "empty"),
    ("\x00\x01", "empty"),                        # nothing survives
    ("two\nlines", "multiline"),                  # an INNER line break is a refusal
    ("two\r\nlines", "multiline"),
    ("x" * 101, "too_long"),
])
def test_normalize_refuses_with_a_reason(raw, reason):
    from services.chat_title import normalize_chat_title
    assert normalize_chat_title(raw) == (None, reason)


def test_the_refusal_sentence_names_the_rule_the_fix_and_an_example():
    from services.chat_title import CHAT_TITLE_MAX_CHARS, chat_title_problem
    long = "y" * 130
    msg = chat_title_problem("too_long", long)
    assert str(CHAT_TITLE_MAX_CHARS) in msg and "130" in msg and "Example:" in msg
    assert "line" in chat_title_problem("multiline") and "Example:" in chat_title_problem("multiline")
    assert "empty" in chat_title_problem("empty")


@pytest.mark.parametrize("text, greeting", [
    ("hi", True),
    ("Hello there!", True),
    ("hey, are you there?", True),
    ("test", True),
    ("Good morning", True),
    ("Hi, can you pull the Q3 invoices for Acme and summarise them", False),  # long: has a topic
    ("Invoice 4471 is wrong", False),
    ("", False),
    (None, False),
])
def test_greeting_shape(text, greeting):
    from services.chat_title import is_greeting
    assert is_greeting(text) is greeting


# ---------------------------------------------------------------------------
# Which attempt a turn earns
# ---------------------------------------------------------------------------

def _hist(*openers):
    return [{"role": "user", "content": openers[0]}, {"role": "assistant", "content": "…"}] if openers else []


@pytest.mark.parametrize("row, history, expected", [
    (None, [], None),                                                          # unreadable row: nothing
    ({"title": None, "message_count": 0}, [], "first"),                        # the ent#186 rule
    ({"title": "", "title_source": None, "message_count": 0}, [], "first"),
    ({"title": "hi", "title_source": None, "message_count": 2}, _hist("hi"), "retry"),           # first never landed
    ({"title": "hi", "title_source": None, "message_count": 1}, _hist("hi"), "retry"),           # failed first turn
    ({"title": "Greeting", "title_source": "generated", "message_count": 2}, _hist("hi"), "retry"),  # landed, but on a greeting
    ({"title": "Q3 invoices", "title_source": "generated", "message_count": 2},
     _hist("Where is the Q3 invoice?"), None),                                 # landed on a topic: done
    ({"title": "hi", "title_source": "user", "message_count": 2}, _hist("hi"), None),            # a person's title: never
    ({"title": "hi", "title_source": None, "message_count": 4}, _hist("hi"), None),              # past the window
])
def test_title_plan(row, history, expected):
    from client_portal.service import _title_plan
    assert _title_plan(row, history) == expected


# ---------------------------------------------------------------------------
# The db guard — a person's title stands
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-titles.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata,
        enterprise_portal_messages,
        enterprise_portal_sessions,
    )
    oss_metadata.create_all(get_engine(), tables=[
        enterprise_portal_messages,
        enterprise_portal_sessions,
    ])
    yield get_engine()


def _thread(pdb, sid="ps1", agent="scribe", email=ALICE, opener="hi"):
    pdb.create_portal_session(sid, agent, email, NOW)
    pdb.touch_portal_session(sid, NOW, added=1, title_if_empty=opener)
    return sid


def test_generated_title_lands_over_the_fallback_and_records_its_hand(portal_db):
    from client_portal import db as pdb
    sid = _thread(pdb)
    assert pdb.set_portal_session_title(sid, "Generated") is True
    row = pdb.get_portal_session(sid, "scribe", ALICE)
    assert (row["title"], row["title_source"]) == ("Generated", "generated")


def test_a_persons_title_is_never_overwritten_by_generation(portal_db):
    """The race this issue closes: generation runs off the reply path, so the
    person's rename can land FIRST and the model's guess must then stand down."""
    from client_portal import db as pdb
    sid = _thread(pdb)
    assert pdb.rename_portal_session(sid, "scribe", ALICE, "Mine") is True
    assert pdb.set_portal_session_title(sid, "A model's guess") is False
    row = pdb.get_portal_session(sid, "scribe", ALICE)
    assert (row["title"], row["title_source"]) == ("Mine", "user")


def test_rename_is_scoped_to_the_thread_owner_in_the_update_itself(portal_db):
    """Another client, or another agent, with the right id: no row moves.
    Uniform with `get_portal_session`, so the router's 404 discloses nothing."""
    from client_portal import db as pdb
    sid = _thread(pdb)
    assert pdb.rename_portal_session(sid, "scribe", BOB, "Not mine") is False
    assert pdb.rename_portal_session(sid, "other-agent", ALICE, "Wrong agent") is False
    assert pdb.rename_portal_session("no-such", "scribe", ALICE, "Ghost") is False
    assert pdb.get_portal_session(sid, "scribe", ALICE)["title"] == "hi"


def test_a_pre_473_row_keeps_its_title_and_reads_as_unattributed(portal_db):
    """AC: existing threads keep their titles; no migration. NULL is the honest
    hand for a row nobody can attribute — and it still lets generation land."""
    from client_portal import db as pdb
    sid = _thread(pdb, opener="Legacy title")
    row = pdb.get_portal_session(sid, "scribe", ALICE)
    assert (row["title"], row["title_source"]) == ("Legacy title", None)


def test_sidebar_search_matches_a_user_set_title(portal_db):
    """AC 5 (ent#402 merged the search): the rename writes the same column the
    search reads, so a person finds the chat by the name they gave it."""
    from client_portal import db as pdb
    sid = _thread(pdb, opener="hello")
    pdb.rename_portal_session(sid, "scribe", ALICE, "Acme renewal")
    hits = pdb.search_portal_sessions(ALICE, "%acme%", ["scribe"])
    assert [h["id"] for h in hits] == [sid]
    assert hits[0]["title"] == "Acme renewal"


# ---------------------------------------------------------------------------
# The service — validation lives here, named 400 at the router
# ---------------------------------------------------------------------------

def test_service_rename_validates_and_marks_the_hand(portal_db, monkeypatch):
    from client_portal import db as pdb, service
    from client_portal.service import ClientPortalError, InvalidChatTitle
    sid = _thread(pdb)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: True)

    with pytest.raises(InvalidChatTitle) as e:
        service.rename_session("scribe", ALICE, sid, "a\nb")
    assert (e.value.status_code, e.value.reason) == (400, "multiline")

    out = service.rename_session("scribe", ALICE, sid, "  Acme   renewal ")
    assert out["id"] == sid and out["title"] == "Acme renewal" and out["message_count"] == 1
    assert pdb.get_portal_session(sid, "scribe", ALICE)["title_source"] == "user"

    with pytest.raises(ClientPortalError) as e:
        service.rename_session("scribe", BOB, sid, "Not mine")
    assert e.value.status_code == 404


def test_service_rename_is_roster_gated_first(portal_db, monkeypatch):
    from client_portal import db as pdb, service
    from client_portal.service import ClientPortalError
    sid = _thread(pdb)
    monkeypatch.setattr(service, "agent_on_roster", lambda *a, **k: False)
    with pytest.raises(ClientPortalError) as e:
        service.rename_session("scribe", ALICE, sid, "Fine title")
    assert e.value.status_code == 404
    assert pdb.get_portal_session(sid, "scribe", ALICE)["title"] == "hi"


def test_router_turns_a_refused_title_into_a_named_400(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from client_portal import router as r, service
    from client_portal.service import InvalidChatTitle
    from services import rate_limiter

    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(service, "rename_session",
                        lambda *a, **k: (_ for _ in ()).throw(InvalidChatTitle("too_long", "x" * 150)))
    principal = SimpleNamespace(email=ALICE, is_platform=False)
    with pytest.raises(HTTPException) as e:
        r.portal_rename_session("scribe", "ps1", r.PortalSessionRename(title="x" * 150), principal)
    assert e.value.status_code == 400
    assert e.value.detail["code"] == "invalid_title"
    assert e.value.detail["reason"] == "too_long"
    assert "150" in e.value.detail["message"]


def test_rename_routes_are_registered():
    from client_portal.router import router as portal_router
    from shared_sessions.router import router as rooms_router
    portal = {(tuple(sorted(rt.methods)), rt.path) for rt in portal_router.routes if hasattr(rt, "methods")}
    rooms = {(tuple(sorted(rt.methods)), rt.path) for rt in rooms_router.routes if hasattr(rt, "methods")}
    assert (("PATCH",), "/api/enterprise/client-portal/agents/{agent_name}/sessions/{session_id}") in portal
    assert (("PATCH",), "/api/rooms/{room_id}") in rooms


# ---------------------------------------------------------------------------
# Rooms — the same rule, the same 400, and a person-only verb
# ---------------------------------------------------------------------------

def _room_service(monkeypatch, caller):
    from shared_sessions import service as rs
    renamed, broadcast = [], []
    monkeypatch.setattr(rs, "_require_membership", lambda room_id, cu: {"id": room_id, "name": "old"})
    monkeypatch.setattr(rs, "_caller", lambda cu: caller)
    monkeypatch.setattr(rs.db, "rename_room", lambda room_id, name: renamed.append((room_id, name)) or True)
    monkeypatch.setattr(rs, "_broadcast", lambda event, payload: broadcast.append((event, payload)))
    return rs, renamed, broadcast


def test_room_rename_by_a_person_lands_and_broadcasts_a_thin_trigger(monkeypatch):
    rs, renamed, broadcast = _room_service(monkeypatch, ("user", "alice"))
    out = rs.rename_room(object(), "room-1", "  Budget   review ")
    assert out == {"room_id": "room-1", "name": "Budget review"}
    assert renamed == [("room-1", "Budget review")]
    # #918: identifiers only on /ws — the name never rides the broadcast.
    assert broadcast == [("room_renamed", {"room_id": "room-1"})]


def test_room_rename_by_a_workspace_client_is_a_person_too(monkeypatch):
    rs, renamed, _ = _room_service(monkeypatch, (rs_kind := "workspace_user", ALICE))
    assert rs.WORKSPACE_KIND == rs_kind
    rs.rename_room(object(), "room-1", "Fine")
    assert renamed == [("room-1", "Fine")]


def test_an_agent_member_may_talk_but_not_rename(monkeypatch):
    """The ent#220 line: a member agent is reachable via its own MCP key and is
    a prompt-injection surface; a room's name is what every participant reads
    it by. Membership is checked FIRST, so this 403 discloses nothing a member
    cannot already see."""
    rs, renamed, _ = _room_service(monkeypatch, ("agent", "scribe"))
    with pytest.raises(rs.RoomError) as e:
        rs.rename_room(object(), "room-1", "Renamed by a bot")
    assert (e.value.status_code, e.value.code) == (403, "not_a_person")
    assert renamed == []


def test_room_rename_refuses_with_the_same_named_400(monkeypatch):
    rs, renamed, _ = _room_service(monkeypatch, ("user", "alice"))
    with pytest.raises(rs.RoomError) as e:
        rs.rename_room(object(), "room-1", "   ")
    assert (e.value.status_code, e.value.code, e.value.extra.get("reason")) == (400, "invalid_title", "empty")
    assert renamed == []


# ---------------------------------------------------------------------------
# Generator health — observable once, not silently forever
# ---------------------------------------------------------------------------

@pytest.fixture()
def fresh_health(monkeypatch):
    from client_portal import service
    monkeypatch.setattr(service, "_title_health", {
        "state": service.TITLE_HEALTH_UNKNOWN, "consecutive_failures": 0,
        "last_ok_at": None, "last_failure_at": None, "last_failure": None,
    })
    return service


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "portal thread titles" in r.getMessage()]


def test_no_credential_warns_once_and_recovers(fresh_health, caplog):
    svc = fresh_health
    caplog.set_level(logging.INFO, logger=svc.logger.name)
    svc._record_title_outcome("no_credential", "no ANTHROPIC_API_KEY and no subscription token for scribe")
    svc._record_title_outcome("no_credential", "no ANTHROPIC_API_KEY and no subscription token for scribe")
    svc._record_title_outcome("no_credential", "no ANTHROPIC_API_KEY and no subscription token for scribe")
    assert svc.title_generation_health()["state"] == "no_credential"
    assert len(_warnings(caplog)) == 1                       # once per episode, not per attempt
    svc._record_title_outcome("ok")
    h = svc.title_generation_health()
    assert h["state"] == "ok" and h["consecutive_failures"] == 0 and h["last_failure"] is None
    # A NEW episode warns again — recovery reset it.
    svc._record_title_outcome("no_credential", "gone again")
    assert len(_warnings(caplog)) == 2


def test_transport_failures_need_the_threshold_before_they_are_an_episode(fresh_health, caplog):
    svc = fresh_health
    caplog.set_level(logging.WARNING, logger=svc.logger.name)
    svc._record_title_outcome("ok")
    svc._record_title_outcome("failed", "HTTP 529")
    svc._record_title_outcome("failed", "HTTP 529")
    assert svc.title_generation_health()["state"] == "ok"    # a blip is not an episode
    assert _warnings(caplog) == []
    svc._record_title_outcome("failed", "HTTP 529")
    h = svc.title_generation_health()
    assert h["state"] == "failing" and h["consecutive_failures"] == 3 and h["last_failure"] == "HTTP 529"
    assert len(_warnings(caplog)) == 1
    svc._record_title_outcome("failed", "HTTP 529")
    assert len(_warnings(caplog)) == 1                       # steady state is quiet


def test_health_carries_no_credential_material_and_names_the_model(fresh_health):
    svc = fresh_health
    h = svc.title_generation_health()
    assert set(h) == {"state", "consecutive_failures", "last_ok_at", "last_failure_at", "last_failure", "model"}
    assert h["model"] == svc._TITLE_MODEL


def test_a_missing_credential_is_recorded_by_the_generator(fresh_health, monkeypatch):
    import asyncio
    svc = fresh_health
    monkeypatch.setattr(svc, "_resolve_title_auth", lambda agent: None)
    assert asyncio.run(svc._generate_thread_title("scribe", "hi", "hello")) is None
    h = svc.title_generation_health()
    assert h["state"] == "no_credential" and "scribe" in h["last_failure"]


def test_the_settings_read_carries_the_health(monkeypatch):
    """The one Workspace settings payload every edition renders — the notice
    reads `title_generation` from it, so it has to be there."""
    import asyncio
    from types import SimpleNamespace
    from routers import settings as settings_router
    from services.entitlement_service import entitlement_service
    monkeypatch.setattr(entitlement_service, "list_entitled_features", lambda: [])
    # The route's admin gate is the real one (#2323's allowlist); this test is
    # about the payload, so the gate is stubbed rather than a principal forged.
    monkeypatch.setattr(settings_router, "assert_admin", lambda *a, **k: None)
    out = asyncio.run(settings_router.get_portal_session_policy_status(SimpleNamespace(role="admin")))
    assert "title_generation" in out and "state" in out["title_generation"]


# ---------------------------------------------------------------------------
# Both migration tracks carry the column
# ---------------------------------------------------------------------------

def test_title_source_is_on_both_tracks_and_the_ddl():
    from pathlib import Path
    from db import migrations, schema, tables
    assert "title_source" in tables.enterprise_portal_sessions.c
    assert "title_source TEXT" in schema.SCHEMA_SQL["enterprise_portal_sessions"] if hasattr(schema, "SCHEMA_SQL") \
        else "title_source TEXT" in Path(schema.__file__).read_text()
    assert any(name == "portal_session_title_source" for name, _fn in migrations.MIGRATIONS)
    rev = Path(migrations.__file__).resolve().parents[1] / "migrations" / "versions" / "0052_portal_session_title_source.py"
    text = rev.read_text()
    assert 'down_revision = "0051_agent_loops_terminal_driven"' in text
    assert "ADD COLUMN IF NOT EXISTS title_source" in text


def test_search_is_scoped_by_the_same_roster_the_gate_enforces(monkeypatch):
    """AC 5 on the platform door: `search_chats` read only the SHARED roster,
    so an owner searching chats with their own agents always got nothing (the
    ent#358 class, on the search route). It now resolves the same set
    `agent_on_roster` enforces, owned agents included for a platform session."""
    from client_portal import service
    seen = {}
    monkeypatch.setattr(service, "roster_agent_names",
                        lambda email, include_owned: {"shared-1"} | ({"owned-1"} if include_owned else set()))
    monkeypatch.setattr(service.db, "search_portal_sessions",
                        lambda email, pattern, names, limit=30: seen.setdefault("names", list(names)) and [])
    service.search_chats(ALICE, "acme", include_owned=True)
    assert seen["names"] == ["owned-1", "shared-1"]
    seen.clear()
    service.search_chats(ALICE, "acme")
    assert seen["names"] == ["shared-1"]
