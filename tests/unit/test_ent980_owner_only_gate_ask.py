"""trinity-enterprise#980 — a `gate-owner-only-` ask is the agent owner's (and its addressee's).

A project steward's proposal is raised as a gate ask under the steward agent,
addressed to the project's creator. Its rows name people and tasks. The operator
queue lists, for a non-admin, every agent they own OR are shared on — so a person
the steward is merely shared with would read a change to a project they may not
be able to open. The prefix is owner-only, like a client's complaint (ent#866);
an ordinary skill-gate approval (`gate-`) stays visible to a shared operator.

Driven through the REAL router on a real (temp) database.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

from db_harness import db_backend, run as _hrun, seed_agent, seed_user  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "shop-agent"
OWNER, SHAREE, ADMIN = "owner", "client-b", "admin"

_APP = None
_PRINCIPAL: dict = {}


def _app():
    """ONE app per process over the real router (a second app over the same
    module router answered 401 in the #2915 harness), with every
    `get_current_user` it depends on overridden by the current stand-in."""
    global _APP
    if _APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP = app
    return _APP


def _as(username, role="user"):
    from fastapi.testclient import TestClient
    from models import User
    email = {OWNER: "owner@example.com", SHAREE: "client-b@example.com", ADMIN: "admin@example.com"}[username]
    uid = {OWNER: 1, SHAREE: 2, ADMIN: 3}[username]
    _PRINCIPAL["user"] = User(id=uid, username=username, email=email, role=role)
    return TestClient(_app(), raise_server_exceptions=True)


@pytest.fixture
def steward(db_backend, monkeypatch):
    seed_user(1, OWNER, role="user")
    seed_user(2, SHAREE, role="user")
    seed_user(3, ADMIN, role="admin")
    for uid, email in ((1, "owner@example.com"), (2, "client-b@example.com"), (3, "admin@example.com")):
        _hrun("UPDATE users SET email = :e WHERE id = :i", e=email, i=uid)
    seed_agent(AGENT, owner_id=1)
    _hrun("INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
          "VALUES (:a, 'client-b@example.com', 1, '2026-10-27T00:00:00Z')", a=AGENT)
    for rid in ("gate-owner-only-project-proposal-ppr_1", "gate-skill-1"):
        _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
              "question, created_at, raised_by, channel, addressed_to_email, resolved_to) VALUES "
              "(:i, :a, :i, 'approval', 'pending', 'medium', 'Approve the change?', 'q', "
              "'2026-10-27T00:00:00Z', 'gate', 'gate', 'owner@example.com', '[\"owner@example.com\"]')",
              i=rid, a=AGENT)
    _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
          "question, created_at, raised_by, channel) VALUES ('plain-1', :a, 'plain-1', 'approval', "
          "'pending', 'medium', 'Ship it?', 'q', '2026-10-27T00:00:00Z', 'agent', 'mcp')", a=AGENT)
    import routers.operator_queue as r
    monkeypatch.setattr(r, "_websocket_manager", None)


def _ids(client):
    r = client.get("/api/operator-queue", params={"agent_name": AGENT})
    assert r.status_code == 200, r.text
    return {i["request_id"] for i in r.json()["items"]}


def test_a_shared_non_owner_does_not_list_it(steward):
    ids = _ids(_as(SHAREE))
    assert "gate-owner-only-project-proposal-ppr_1" not in ids
    assert "gate-skill-1" in ids        # an ordinary gate approval is still theirs to see


@pytest.mark.parametrize("who,role", [(OWNER, "user"), (ADMIN, "admin")])
def test_the_owner_and_admins_still_list_it(steward, who, role):
    assert "gate-owner-only-project-proposal-ppr_1" in _ids(_as(who, role))


def test_the_prefix_is_owner_only_and_reserved():
    from services import operator_queue_service as oqs
    assert oqs.is_owner_only({"request_id": "gate-owner-only-project-proposal-x"})
    assert not oqs.is_owner_only({"request_id": "gate-project-agent-x"})
    assert oqs.is_about_a_person({"request_id": "gate-owner-only-project-proposal-x"})


# --- the card says whose decision it is (the row's `addressed`) ------------------

def _addressed(client, request_id):
    r = client.get("/api/operator-queue", params={"agent_name": AGENT})
    assert r.status_code == 200, r.text
    return {i["request_id"]: i for i in r.json()["items"]}[request_id].get("addressed")


def test_an_admin_reads_it_is_waiting_on_the_addressee_and_cannot_decide(steward):
    view = _addressed(_as(ADMIN, "admin"), "gate-owner-only-project-proposal-ppr_1")
    assert view == {"waiting_on": ["owner@example.com"], "can_decide": False}


def test_the_addressee_may_decide(steward):
    view = _addressed(_as(OWNER), "gate-owner-only-project-proposal-ppr_1")
    assert view == {"waiting_on": ["owner@example.com"], "can_decide": True}


def test_an_ordinary_agent_approval_carries_no_addressed(steward):
    assert _addressed(_as(OWNER), "plain-1") is None
