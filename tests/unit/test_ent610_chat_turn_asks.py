"""trinity-enterprise#610 PR A2 round 2 — one chat's chat-turn asks, with no ended window.

The 09-30 ruling (amended) draws an ask a chat turn raised as a tile in THAT chat,
and once ended "it stays in that chat's history … subject to the same retention
as the queue (trinity#1142)". The Workspace asks read keeps ended asks for
`ENDED_WINDOW_DAYS` (7) and returns one page of `PAGE_MAX` (200), so the chat's
history row vanished after a week — or earlier, behind 200 other asks.

`GET /asks?chat_id=<id>` is the chat's own read: the viewer's asks that the
PLATFORM stamped as raised in a turn of that chat (`context.workspace_session_id`
+ `context.workspace_raised_in_turn is True`, ent#734), pending and ended, cleared
included, with no ended window — the queue's retention is the only bound.

What it must never return: a background ask filed into the same chat (Main), an
ask of another chat, another person's ask, an off-roster agent's ask, or an ask
whose agent nested look-alike keys in its context (only the platform's top-level
keys count — the SQL match is a prefilter, the projection decides).

Harness: the #3059 one — a real sqlite built by `init_schema`.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

AGENT = "agent-a"


@pytest.fixture()
def asks_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent610-chat-asks.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    import sqlite3
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    yield str(db_file)


@pytest.fixture()
def email():
    return f"client-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    state = {"off": set()}
    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        return agent_name not in state["off"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _ask(email, *, agent=AGENT, chat=None, in_turn=False, context=None,
         created="2026-09-29T09:10:00.000000Z", title="Ship it?"):
    """An ask as ingestion writes it; the two workspace keys are platform-written."""
    from database import db
    from services.operator_queue_service import _clamp_ingested_item
    item = _clamp_ingested_item({
        "id": f"req-{uuid.uuid4().hex[:12]}", "type": "question", "title": title,
        "question": "Ship it?", "options": ["yes", "no"], "addressed_to_email": email,
    }, agent)
    ctx = dict(context or {})
    if chat:
        ctx["workspace_session_id"] = chat
    if in_turn:
        ctx["workspace_raised_in_turn"] = True
    item["context"] = ctx or None
    item["created_at"] = created
    return db.create_operator_queue_item(agent, item)


def _end(item_id, *, at):
    """Ended long ago: answered, and its ending stamped `at`."""
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE operator_queue SET status='responded', response='yes', "
            " responded_at=:t, disposed_at=:t WHERE id=:id"), {"t": at, "id": item_id})


def _ids(email, chat, **kw):
    from client_portal.asks import service
    return {a.id for a in service.list_asks_page(email, is_platform=False, chat_id=chat, **kw).items}


def test_the_chats_turn_asks_come_back_pending_and_ended_with_no_window(asks_db, email):
    live = _ask(email, chat="s1", in_turn=True)
    old = _ask(email, chat="s1", in_turn=True, created="2026-06-01T09:00:00.000000Z")
    _end(old, at="2026-06-01T09:05:00.000000Z")      # ended ~4 months ago: past the 7-day window
    assert _ids(email, "s1") == {live, old}
    from client_portal.asks import service
    rows = {a.id: a for a in service.list_asks_page(email, is_platform=False, chat_id="s1").items}
    assert rows[old].status == "answered" and rows[old].raised_in_turn is True


def test_a_cleared_ended_ask_stays_in_its_chats_history(asks_db, email):
    from sqlalchemy import text
    from db.engine import get_engine
    a = _ask(email, chat="s1", in_turn=True)
    _end(a, at="2026-09-29T09:20:00.000000Z")
    with get_engine().begin() as conn:     # the operator's Clear All (#1017)
        conn.execute(text("UPDATE operator_queue SET cleared_at='2026-09-29T10:00:00.000000Z' WHERE id=:i"), {"i": a})
    assert _ids(email, "s1") == {a}


def test_a_background_ask_filed_into_the_same_chat_is_not_the_chats(asks_db, email):
    _ask(email, chat="s-main")                          # schedule / file ask: Main is only the reply target
    turn = _ask(email, chat="s-main", in_turn=True)     # a turn in Main raised this one
    assert _ids(email, "s-main") == {turn}


def test_another_chat_another_person_and_an_off_roster_agent_are_never_returned(asks_db, email, roster):
    mine = _ask(email, chat="s1", in_turn=True)
    _ask(email, chat="s2", in_turn=True)
    _ask(f"other-{email}", chat="s1", in_turn=True)
    _ask(email, agent="agent-off", chat="s1", in_turn=True)
    roster["off"].add("agent-off")
    assert _ids(email, "s1") == {mine}


def test_look_alike_keys_an_agent_nests_in_its_context_never_count(asks_db, email):
    forged = {"note": {"workspace_session_id": "s1", "workspace_raised_in_turn": True}}
    _ask(email, context=forged)                          # nested: not the platform's top-level keys
    _ask(email, context={"x": '"workspace_session_id": "s1", "workspace_raised_in_turn": true'})
    real = _ask(email, chat="s1", in_turn=True)
    assert _ids(email, "s1") == {real}


def test_a_chat_id_that_could_be_a_like_pattern_matches_only_itself(asks_db, email):
    exact = _ask(email, chat="s_1", in_turn=True)
    _ask(email, chat="sx1", in_turn=True)                # `_` would match any one char unescaped
    _ask(email, chat="s%", in_turn=True)
    assert _ids(email, "s_1") == {exact}
    assert _ids(email, "%") == set()
    # The total is SQL's count: an unescaped `_` would count "sx1" too.
    from client_portal.asks import service
    assert service.list_asks_page(email, is_platform=False, chat_id="s_1").total == 1


def test_include_ended_never_brings_the_inbox_window_back_to_a_chat_read(asks_db, email):
    old = _ask(email, chat="s1", in_turn=True, created="2026-06-01T09:00:00.000000Z")
    _end(old, at="2026-06-01T09:05:00.000000Z")
    assert _ids(email, "s1", include_ended=True) == {old}


def test_the_route_takes_chat_id(asks_db, email):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal.asks.router import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(email=email, is_platform=False)
    old = _ask(email, chat="s1", in_turn=True, created="2026-06-01T09:00:00.000000Z")
    _end(old, at="2026-06-01T09:05:00.000000Z")
    _ask(email, chat="s1")
    r = TestClient(app).get("/api/enterprise/client-portal/asks", params={"chat_id": "s1"})
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()] == [old]
    assert r.headers["X-Total-Count"] == "1"


def test_the_default_read_is_unchanged_by_the_chat_read(asks_db, email):
    old = _ask(email, chat="s1", in_turn=True, created="2026-06-01T09:00:00.000000Z")
    _end(old, at="2026-06-01T09:05:00.000000Z")
    from client_portal.asks import service
    assert old not in {a.id for a in service.list_asks_page(email, is_platform=False, include_ended=True).items}
