"""trinity-enterprise#980 — a `gate-addressee-only-` ask is its addressee's (and admins').

A project steward's proposal is raised as a gate ask under the steward agent,
addressed to the project's creator. Its rows name people and tasks of a project
that not everyone who can use the agent may open. The creator need not OWN the
steward — it may only be shared with them — so the rule is keyed on the
ADDRESSEE, not on who owns the agent: the addressee lists and opens it, an admin
does, and nobody else — not a sharee, and not the agent's owner either.
An ordinary skill-gate approval (`gate-`) stays visible to a shared operator.

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
PROPOSAL = "gate-addressee-only-project-proposal-ppr_1"
OWNERS_PROPOSAL = "gate-addressee-only-project-proposal-ppr_2"

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
    # PROPOSAL is addressed to the SHAREE — a creator the steward is only shared
    # with (the reviewed shape); OWNERS_PROPOSAL to the agent's owner.
    for rid, to in ((PROPOSAL, "client-b@example.com"), (OWNERS_PROPOSAL, "owner@example.com"),
                    ("gate-skill-1", "owner@example.com")):
        _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
              "question, created_at, raised_by, channel, addressed_to_email, resolved_to) VALUES "
              "(:i, :a, :i, 'approval', 'pending', 'medium', 'Approve the change?', 'q', "
              "'2026-10-27T00:00:00Z', 'gate', 'gate', :t, :r)",
              i=rid, a=AGENT, t=to, r=f'["{to}"]')
    _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
          "question, created_at, raised_by, channel) VALUES ('plain-1', :a, 'plain-1', 'approval', "
          "'pending', 'medium', 'Ship it?', 'q', '2026-10-27T00:00:00Z', 'agent', 'mcp')", a=AGENT)
    import routers.operator_queue as r
    monkeypatch.setattr(r, "_websocket_manager", None)


def _list(client, path="/api/operator-queue", **params):
    r = client.get(path, params=dict({"agent_name": AGENT} if path == "/api/operator-queue" else {},
                                     **params))
    assert r.status_code == 200, r.text
    return r.json()


def _ids(client):
    return {i["request_id"] for i in _list(client)["items"]}


def _agent_ids(client):
    return {i["request_id"] for i in _list(client, f"/api/operator-queue/agents/{AGENT}")["items"]}


def _opens(client, request_id):
    return client.get(f"/api/operator-queue/{request_id}").status_code == 200


def test_a_shared_addressee_lists_and_opens_it(steward):
    c = _as(SHAREE)
    assert PROPOSAL in _ids(c) and PROPOSAL in _agent_ids(c)
    assert _opens(c, PROPOSAL)
    # Someone else's proposal on the same agent is not theirs.
    assert OWNERS_PROPOSAL not in _ids(c) and OWNERS_PROPOSAL not in _agent_ids(c)
    assert not _opens(c, OWNERS_PROPOSAL)
    assert "gate-skill-1" in _ids(c)   # an ordinary gate approval is still theirs to see


def test_the_agents_owner_who_is_not_the_addressee_neither_lists_nor_opens_it(steward):
    c = _as(OWNER)
    assert PROPOSAL not in _ids(c) and PROPOSAL not in _agent_ids(c)
    assert not _opens(c, PROPOSAL)
    assert OWNERS_PROPOSAL in _ids(c) and _opens(c, OWNERS_PROPOSAL)


def test_an_admin_lists_and_opens_it(steward):
    c = _as(ADMIN, "admin")
    assert {PROPOSAL, OWNERS_PROPOSAL} <= _ids(c)
    assert {PROPOSAL, OWNERS_PROPOSAL} <= _agent_ids(c)
    assert _opens(c, PROPOSAL)


@pytest.mark.parametrize("who,role,expected", [
    (SHAREE, "user", {PROPOSAL, "gate-skill-1", "plain-1"}),
    (OWNER, "user", {OWNERS_PROPOSAL, "gate-skill-1", "plain-1"}),
    (ADMIN, "admin", {PROPOSAL, OWNERS_PROPOSAL, "gate-skill-1", "plain-1"}),
])
def test_total_is_exact(steward, who, role, expected):
    # The rule is a SQL condition before the limit, so `total` counts what is listed.
    body = _list(_as(who, role))
    assert {i["request_id"] for i in body["items"]} == expected
    assert body["total"] == len(expected)
    page = _list(_as(who, role), limit=1)
    assert page["total"] == len(expected) and len(page["items"]) == 1


def test_the_addressee_is_matched_case_insensitively_and_through_resolved_to(steward):
    _hrun("UPDATE operator_queue SET addressed_to_email = 'Client-B@Example.com' WHERE id = :i", i=PROPOSAL)
    assert PROPOSAL in _ids(_as(SHAREE))
    # An ask to a role several people fill: no single addressee, each holder lists it.
    _hrun("UPDATE operator_queue SET addressed_to_email = NULL, "
          "resolved_to = '[\"someone@example.com\", \"client-b@example.com\"]' WHERE id = :i", i=PROPOSAL)
    assert PROPOSAL in _ids(_as(SHAREE))
    assert _opens(_as(SHAREE), PROPOSAL)
    assert PROPOSAL not in _ids(_as(OWNER))


def test_the_prefix_is_addressee_only_and_reserved():
    from services import operator_queue_service as oqs
    row = {"request_id": "  GATE-ADDRESSEE-ONLY-project-proposal-x",
           "addressed_to_email": "a@example.com", "resolved_to": '["b@example.com"]'}
    assert oqs.is_addressee_only(row)
    assert not oqs.is_owner_only(row)          # not keyed on who owns the agent
    assert oqs.is_about_a_person(row)          # under `gate-`: never a machine key's, never agent-minted
    assert oqs.is_addressed_to(row, "A@example.com")
    assert oqs.is_addressed_to(row, "b@example.com")
    assert not oqs.is_addressed_to(row, "c@example.com")
    assert not oqs.is_addressed_to(row, "")
    assert not oqs.is_addressee_only({"request_id": "gate-project-agent-x"})
    assert oqs.is_owner_only({"request_id": "workspace-problem-1"})   # the complaint stays owner-only


# --- the card says whose decision it is (the row's `addressed`) ------------------

def _addressed(client, request_id):
    r = client.get("/api/operator-queue", params={"agent_name": AGENT})
    assert r.status_code == 200, r.text
    return {i["request_id"]: i for i in r.json()["items"]}[request_id].get("addressed")


def test_an_admin_reads_it_is_waiting_on_the_addressee_and_cannot_decide(steward):
    view = _addressed(_as(ADMIN, "admin"), PROPOSAL)
    assert view == {"waiting_on": ["client-b@example.com"], "can_decide": False}


def test_the_addressee_may_decide(steward):
    view = _addressed(_as(SHAREE), PROPOSAL)
    assert view == {"waiting_on": ["client-b@example.com"], "can_decide": True}


def test_an_ordinary_agent_approval_carries_no_addressed(steward):
    assert _addressed(_as(OWNER), "plain-1") is None
