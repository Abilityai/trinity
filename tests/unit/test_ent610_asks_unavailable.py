"""The Workspace asks read fails LOUD, never empty (trinity-enterprise#610, PR A0).

Before this, `client_portal/asks/service.py::list_asks` caught every error and
returned `[]`, and `_on_roster` turned an unreadable roster into "not on the
roster". Either one made the Workspace say "nothing needs you" during an outage
— the #2915 failure class, and the exact claim the Inbox's Action tab makes.

Now:
  * a queue-read fault raises `AsksUnavailable`, and the route answers
    **503 `{code: "asks_unavailable"}`**;
  * in LIST mode a roster that cannot be READ is the same outage (503), while an
    agent that is genuinely off the roster is still dropped silently (200);
  * `answer_ask` keeps its uniform 404 on a roster failure (Invariant #8);
  * the suggestions build, the only other backend caller, still degrades through
    its own `attempt(..., [])`.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def asks_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-asks.db"
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
    # `db.engine` caches the engine at first use, so every test addresses its own
    # email rather than relying on a fresh database (see test_ent428).
    return f"client-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Roster membership, controllable: `on` answers, `raises` is an outage."""
    state = {"on": True, "raises": False}

    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        if state["raises"]:
            raise RuntimeError("roster down")
        return state["on"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _raise_ask(addressed, agent="agent-a"):
    from database import db
    from services.operator_queue_service import _clamp_ingested_item

    item = _clamp_ingested_item(
        {
            "id": f"req-{uuid.uuid4().hex[:12]}",
            "type": "question",
            "title": "Need a decision",
            "question": "Ship it?",
            "options": ["yes", "no"],
            "addressed_to_email": addressed,
        },
        agent,
    )
    return db.create_operator_queue_item(agent, item)


_APP = None
_PRINCIPAL = {"p": None}


def _client(email):
    """One app over the asks router; `get_portal_principal` overridden by walking
    the routes' own dependant trees (the test_ent611 pattern, no reimport)."""
    global _APP
    from fastapi.testclient import TestClient

    from client_portal.portal_auth import PortalPrincipal

    if _APP is None:
        from fastapi import FastAPI

        from client_portal.asks import router as ar

        app = FastAPI()
        app.include_router(ar.router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_portal_principal":
                    found.add(sub.call)
                walk(sub)

        for route in ar.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_portal_principal dependency on the asks routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["p"]
        _APP = app
    _PRINCIPAL["p"] = PortalPrincipal(email=email, is_platform=False)
    return TestClient(_APP, raise_server_exceptions=False)


URL = "/api/enterprise/client-portal/asks"


# --- the service raises ------------------------------------------------------


def test_a_queue_read_fault_raises_rather_than_returning_empty(
    asks_db, client_email, monkeypatch
):
    from client_portal.asks import service

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(service.db, "list_operator_queue_items", boom)
    with pytest.raises(service.AsksUnavailable):
        service.list_asks(client_email, is_platform=False)


def test_an_unreadable_roster_raises_in_list_mode(asks_db, client_email, roster):
    from client_portal.asks import service

    _raise_ask(client_email)

    roster["raises"] = True
    with pytest.raises(service.AsksUnavailable):
        service.list_asks(client_email, is_platform=False)


def test_an_agent_genuinely_off_the_roster_is_still_dropped_silently(
    asks_db, client_email, roster
):
    """Fail-closed is unchanged for a CLEAN "no": that is an authorisation answer,
    not an outage, so it hides the ask and the list is still a success."""
    from client_portal.asks import service

    _raise_ask(client_email)

    roster["on"] = False
    assert service.list_asks(client_email, is_platform=False) == []


def test_answering_on_an_unreadable_roster_stays_a_uniform_404(
    asks_db, client_email, roster
):
    """Invariant #8: the answer path must not start distinguishing an outage from
    not-yours — that would be an existence oracle for ask ids."""
    from client_portal.asks import service

    ask_id = _raise_ask(client_email)

    roster["raises"] = True
    with pytest.raises(service.AskError) as exc:
        service.answer_ask(ask_id, client_email, False, "yes", None)
    assert exc.value.status_code == 404
    assert exc.value.code == "not_found"


# --- the route answers 503 -----------------------------------------------------


def test_the_route_answers_503_asks_unavailable_on_a_queue_fault(
    asks_db, client_email, monkeypatch
):
    from client_portal.asks import service

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(service.db, "list_operator_queue_items", boom)
    r = _client(client_email).get(URL, params={"include_ended": True})

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "asks_unavailable"
    assert r.json()["detail"]["message"]


def test_the_route_answers_503_on_an_unreadable_roster(asks_db, client_email, roster):
    _raise_ask(client_email)
    roster["raises"] = True

    r = _client(client_email).get(URL)

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "asks_unavailable"


def test_the_route_still_answers_200_for_a_clean_off_roster_agent(
    asks_db, client_email, roster
):
    _raise_ask(client_email)
    roster["on"] = False

    r = _client(client_email).get(URL)

    assert r.status_code == 200
    assert r.json() == []


def test_the_route_answers_200_with_the_ask_when_everything_reads(
    asks_db, client_email
):
    ask_id = _raise_ask(client_email)

    r = _client(client_email).get(URL)

    assert r.status_code == 200
    assert [a["id"] for a in r.json()] == [ask_id]


# --- the other caller still degrades --------------------------------------------


def test_the_suggestions_build_still_degrades_to_no_ask_ids(
    asks_db, client_email, monkeypatch
):
    """`suggestions/service._gather` wraps the call in `attempt(..., [])`; a raise
    here costs the suggestions their ask items, never the whole list."""
    from datetime import datetime, timezone

    from client_portal.asks import service
    from client_portal.suggestions import service as suggestions

    def boom(**_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(service.db, "list_operator_queue_items", boom)
    out = suggestions._gather(
        "agent-a", client_email, datetime.now(timezone.utc), False
    )

    assert out["ask_ids"] == []
