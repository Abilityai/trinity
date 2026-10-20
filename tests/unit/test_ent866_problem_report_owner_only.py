"""trinity-enterprise#866 — a Workspace client's complaint is the agent owner's.

Every thumbs-down raises a `workspace_problem_report` alert that names the rater
(and, since trinity#3521, quotes their comment). The operator queue lists, for a
non-admin, every agent they own OR are shared on — and a person an agent is
shared with is often a client too. So client B, shared on the agent and signed
in to the platform, read client A's email (and words) through the queue, while
`GET /evaluations` withheld exactly those facts from the same caller.

Driven through the REAL router on a real (temp) database: two clients rate, then
the owner, an admin and a shared non-owner read the queue every way it can be
read — the list, the per-agent list, by id — and try to act on the row.
"""
from __future__ import annotations

import asyncio
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
OWNER_EMAIL, CLIENT_A, CLIENT_B = "owner@example.com", "client-a@example.com", "client-b@example.com"

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
    email = {OWNER: OWNER_EMAIL, SHAREE: CLIENT_B, ADMIN: "admin@example.com"}[username]
    uid = {OWNER: 1, SHAREE: 2, ADMIN: 3}[username]
    _PRINCIPAL["user"] = User(id=uid, username=username, email=email, role=role)
    return TestClient(_app(), raise_server_exceptions=True)


@pytest.fixture
def shop(db_backend, monkeypatch):
    """The owner's agent, shared with client B; clients A and B each rate a
    reply down — A with a comment."""
    seed_user(1, OWNER, role="user")
    seed_user(2, SHAREE, role="user")
    seed_user(3, ADMIN, role="admin")
    for uid, email in ((1, OWNER_EMAIL), (2, CLIENT_B), (3, "admin@example.com")):
        _hrun("UPDATE users SET email = :e WHERE id = :i", e=email, i=uid)
    seed_agent(AGENT, owner_id=1)
    _hrun("INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
          "VALUES (:a, :e, 1, '2026-10-12T00:00:00Z')", a=AGENT, e=CLIENT_B)

    from client_portal import service
    for email, comment in ((CLIENT_A, "A's private words"), (CLIENT_B, None)):
        assert asyncio.run(service.raise_problem_report(
            AGENT, email, target_kind="message", target_id=f"m-{email}", comment=comment))

    # An ordinary alert on the same agent: the sharee must keep seeing those.
    _hrun("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, "
          "title, question, created_at) VALUES ('plain-1', :a, 'plain-1', 'alert', 'pending', "
          "'medium', 'Disk is filling up', 'q', '2026-10-12T00:00:00Z')", a=AGENT)

    import routers.operator_queue as r
    monkeypatch.setattr(r, "_websocket_manager", None)


def _report_ids():
    from database import db
    return sorted(i["id"] for i in db.list_operator_queue_items(agent_name=AGENT)
                  if i["type"] == "workspace_problem_report")


def _listed(client, path="/api/operator-queue", **params):
    r = client.get(path, params={"agent_name": AGENT, **params})
    assert r.status_code == 200, r.text
    return r.json()


# --- reads ------------------------------------------------------------------

def test_a_shared_non_owner_does_not_list_anyones_rating_alert(shop):
    body = _listed(_as(SHAREE))
    types = [i["type"] for i in body["items"]]
    assert "workspace_problem_report" not in types
    assert "alert" in types                          # other rows are untouched
    assert body["total"] == len(body["items"])       # the exclusion runs in SQL
    assert CLIENT_A not in str(body) and "A's private words" not in str(body)


def test_naming_the_type_does_not_open_it(shop):
    body = _listed(_as(SHAREE), type="workspace_problem_report")
    assert body["items"] == [] and body["total"] == 0


def test_the_per_agent_list_hides_them_too(shop):
    body = _listed(_as(SHAREE), path=f"/api/operator-queue/agents/{AGENT}")
    assert all(i["type"] != "workspace_problem_report" for i in body["items"])
    assert CLIENT_A not in str(body)


def test_by_id_it_reads_like_a_row_that_is_not_there(shop):
    client = _as(SHAREE)
    for item_id in _report_ids():
        r = client.get(f"/api/operator-queue/{item_id}")
        assert r.status_code == 404
        assert r.json()["detail"] == "Queue item not found"


@pytest.mark.parametrize("who,role", [(OWNER, "user"), (ADMIN, "admin")])
def test_the_owner_and_admins_still_see_the_full_alert(shop, who, role):
    client = _as(who, role)
    body = _listed(client)
    reports = [i for i in body["items"] if i["type"] == "workspace_problem_report"]
    assert len(reports) == 2
    assert any(CLIENT_A in i["question"] and "A's private words" in i["question"] for i in reports)
    for item_id in _report_ids():
        assert client.get(f"/api/operator-queue/{item_id}").status_code == 200
    per_agent = _listed(client, path=f"/api/operator-queue/agents/{AGENT}")
    assert sum(i["type"] == "workspace_problem_report" for i in per_agent["items"]) == 2


# --- acts -------------------------------------------------------------------

def test_a_shared_non_owner_cannot_acknowledge_or_cancel_one(shop):
    client = _as(SHAREE)
    for item_id in _report_ids():
        assert client.post(f"/api/operator-queue/{item_id}/respond",
                           json={"response": "acknowledged"}).status_code == 404
        assert client.post(f"/api/operator-queue/{item_id}/cancel").status_code == 404
    from database import db
    assert all(db.get_operator_queue_item(i)["status"] == "pending" for i in _report_ids())


def test_bulk_cancel_skips_them_for_a_shared_non_owner(shop):
    ids = _report_ids()
    r = _as(SHAREE).post("/api/operator-queue/bulk-cancel", json={"ids": ids + ["plain-1"]})
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == 1 and r.json()["skipped"] == len(ids)
    from database import db
    assert all(db.get_operator_queue_item(i)["status"] == "pending" for i in ids)


def test_the_owner_can_still_acknowledge_one(shop):
    item_id = _report_ids()[0]
    r = _as(OWNER).post(f"/api/operator-queue/{item_id}/cancel")
    assert r.status_code == 200, r.text


# --- the rule itself ----------------------------------------------------------

def test_only_the_complaint_is_owner_only_not_every_row_about_a_person():
    """A shared operator must still see and answer a skill gate's approval."""
    from services import operator_queue_service as oqs
    assert oqs.is_owner_only({"request_id": "workspace-problem-abc"})
    assert oqs.is_owner_only({"request_id": "  WORKSPACE-PROBLEM-abc"})
    assert not oqs.is_owner_only({"request_id": "gate-123"})
    assert not oqs.is_owner_only({"request_id": "plain-1"})
    assert set(oqs.OWNER_ONLY_ID_PREFIXES) <= set(oqs.ABOUT_A_PERSON_ID_PREFIXES)
