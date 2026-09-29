"""#3059 — the Workspace asks read is paged, not silently capped.

`GET /api/enterprise/client-portal/asks` stopped at 200 rows and said nothing,
so a viewer with more than 200 asks lost the oldest ended ones — and past 200
pending, pending ones too — with nothing on screen admitting the cut.

The read now carries `X-Total-Count` and, while more remain, `X-Next-Cursor`,
and a client can page past 200 with an opt-in `limit`/`cursor`. The body stays
a LIST, so no current caller changes.

Two properties are what make a total worth having, and both are pinned here:

  * **The total is the viewer's visible set, not the SQL window.** The visible
    kinds and the read-time roster re-check used to run in Python AFTER the SQL
    `limit`, so a page of 200 rows could render 150 asks and a count taken in
    SQL would include rows the viewer never sees. They now filter in SQL first.
  * **The order is stable across pages**, pending before ended, `id` as the
    tie-break — so walking the cursor visits every visible ask exactly once and
    never drops a pending ask while ended ones are shown.

Harness: the ent#428 one — a real sqlite built by `init_schema`, asks raised
through the real ingestion clamp, a controllable roster.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def asks_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-asks-3059.db"
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
def client_email():
    # `db.engine` caches the engine at first use — a unique addressee per test
    # keeps rows from different tests apart (the ent#428 harness note).
    return f"client-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Roster membership per agent, controllable. Default: every agent is on it."""
    state = {"off": set(), "calls": []}
    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        state["calls"].append(agent_name)
        return agent_name not in state["off"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _raise_ask(agent="agent-a", addressed=None, kind="question", priority=None):
    from database import db
    from services.operator_queue_service import _clamp_ingested_item
    item = {
        "id": f"req-{uuid.uuid4().hex[:12]}", "type": kind, "title": "Need a decision",
        "question": "Ship it?", "options": ["yes", "no"], "addressed_to_email": addressed,
    }
    if priority:
        item["priority"] = priority
    return db.create_operator_queue_item(agent, _clamp_ingested_item(item, agent))


def _end(item_id, email):
    from client_portal.asks import service
    service.answer_ask(item_id, email, is_platform=False, response="yes", response_text=None)


def _page(email, **kw):
    from client_portal.asks import service
    return service.list_asks_page(email, is_platform=False, **kw)


# --- the service --------------------------------------------------------------

def test_more_than_200_pending_are_all_reachable_and_counted(asks_db, client_email):
    mine = {_raise_ask(addressed=client_email) for _ in range(230)}
    first = _page(client_email)
    assert len(first.items) == 200
    assert first.total == 230
    assert first.next_cursor
    second = _page(client_email, cursor=first.next_cursor)
    assert len(second.items) == 30 and second.next_cursor is None and second.total == 230
    seen = [a.id for a in first.items + second.items]
    assert len(seen) == len(set(seen)), "an ask appeared on two pages"
    assert set(seen) == mine, "walking the cursor did not visit every ask exactly once"


def test_the_total_counts_only_what_the_viewer_can_see(asks_db, client_email, roster):
    """Invisible kinds, other people's asks and off-roster agents are filtered
    BEFORE the limit — so they neither consume page slots nor inflate the total."""
    visible = {_raise_ask(agent="agent-a", addressed=client_email) for _ in range(5)}
    _raise_ask(agent="agent-a", addressed=f"other-{client_email}")      # someone else's
    for _ in range(3):
        _raise_ask(agent="agent-gone", addressed=client_email)          # share revoked
    roster["off"].add("agent-gone")
    page = _page(client_email, limit=3)
    assert page.total == 5
    assert len(page.items) == 3 and page.next_cursor
    rest = _page(client_email, limit=3, cursor=page.next_cursor)
    assert {a.id for a in page.items + rest.items} == visible
    assert rest.next_cursor is None


def test_pending_is_never_dropped_while_ended_ones_are_shown(asks_db, client_email):
    ended = [_raise_ask(addressed=client_email) for _ in range(4)]
    for i in ended:
        _end(i, client_email)
    pending = {_raise_ask(addressed=client_email) for _ in range(3)}
    page = _page(client_email, include_ended=True, limit=3)
    assert {a.id for a in page.items} == pending, "an ended ask took a pending ask's slot"
    assert page.total == 7


def test_the_order_is_stable_when_timestamps_tie(asks_db, client_email):
    """Rows raised in the same instant must still page deterministically: `id`
    is the last sort key, so two identical reads agree and pages never overlap."""
    from database import db
    ids = [_raise_ask(addressed=client_email) for _ in range(12)]
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE operator_queue SET created_at = '2026-09-29T00:00:00Z' "
                          "WHERE addressed_to_email = :e"), {"e": client_email})
    walk = []
    cursor = None
    while True:
        p = _page(client_email, limit=5, cursor=cursor)
        walk += [a.id for a in p.items]
        cursor = p.next_cursor
        if not cursor:
            break
    assert walk == [a.id for a in _page(client_email, limit=200).items]
    assert sorted(walk) == sorted(ids) and len(walk) == len(set(walk))
    del db


def test_the_roster_is_still_asked_once_per_agent(asks_db, client_email, roster):
    """ent#428's property survives the move into SQL."""
    for _ in range(5):
        _raise_ask(agent="agent-a", addressed=client_email)
    for _ in range(3):
        _raise_ask(agent="agent-b", addressed=client_email)
    roster["calls"].clear()
    assert _page(client_email).total == 8
    assert sorted(roster["calls"]) == ["agent-a", "agent-b"]


def test_the_list_function_keeps_its_contract(asks_db, client_email):
    """`list_asks` (the pre-#3059 entry point) still returns a plain list."""
    from client_portal.asks import service
    _raise_ask(addressed=client_email)
    out = service.list_asks(client_email, is_platform=False)
    assert isinstance(out, list) and len(out) == 1


# --- the route ----------------------------------------------------------------

def _client(email):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal.asks.router import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(email=email, is_platform=False)
    return TestClient(app)


def test_the_route_stays_a_list_and_carries_the_total_and_cursor(asks_db, client_email):
    for _ in range(7):
        _raise_ask(addressed=client_email)
    c = _client(client_email)
    r = c.get("/api/enterprise/client-portal/asks", params={"limit": 4})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list) and len(r.json()) == 4
    assert r.headers["X-Total-Count"] == "7"
    nxt = r.headers["X-Next-Cursor"]
    r2 = c.get("/api/enterprise/client-portal/asks", params={"limit": 4, "cursor": nxt})
    assert len(r2.json()) == 3 and "X-Next-Cursor" not in r2.headers
    assert r2.headers["X-Total-Count"] == "7"


def test_a_default_read_is_unchanged_but_now_says_its_total(asks_db, client_email):
    for _ in range(3):
        _raise_ask(addressed=client_email)
    r = _client(client_email).get("/api/enterprise/client-portal/asks")
    assert r.status_code == 200 and len(r.json()) == 3
    assert r.headers["X-Total-Count"] == "3" and "X-Next-Cursor" not in r.headers


@pytest.mark.parametrize("bad", ["not-a-cursor", "-1", "o:abc", "99999999999999999999999"])
def test_an_unreadable_cursor_is_a_named_422(asks_db, client_email, bad):
    r = _client(client_email).get("/api/enterprise/client-portal/asks", params={"cursor": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", [0, 201])
def test_limit_is_bounded(asks_db, client_email, limit):
    r = _client(client_email).get("/api/enterprise/client-portal/asks", params={"limit": limit})
    assert r.status_code == 422
