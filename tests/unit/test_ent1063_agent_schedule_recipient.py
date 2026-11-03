"""An agent's own key never addresses, edits or fires a schedule delivered to
someone other than its owner (trinity-enterprise#1063).

A schedule addressed to a person (`deliver_to_workspace_email`, ent#498) makes
its runs that person's seat runs. The owner may address anyone; an agent key
resolves to its owner, so without this an agent could create or retarget a
schedule to a colleague, rewrite one the owner made for them, or fire it on
demand, minting a live turn in the colleague's seat. Addressing the owner stays
open; the owner's own session keeps every control.

Real routes, the real `get_current_user`, a real `trinity_mcp_…` agent key and a
real JWT on the unit conftest's SQLite. Only the scheduler hop behind `trigger`
is stubbed.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db
from db_models import UserCreate
from dependencies import create_access_token
from routers import schedules as schedules_router

pytestmark = pytest.mark.unit

CODE = "agent_cannot_address_another_person"
COLLEAGUE = "colleague@example.com"
BODY = {"name": "brief", "cron_expression": "0 9 * * 1", "message": "Weekly brief", "timezone": "UTC"}


class _SchedulerOK:
    def __init__(self, *a, **k): ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k):
        return SimpleNamespace(status_code=200, json=lambda: {"execution_id": "e1", "status": "started"},
                               text="{}")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(schedules_router.httpx, "AsyncClient", _SchedulerOK)
    app = FastAPI()
    app.include_router(schedules_router.router)
    return TestClient(app)


@pytest.fixture
def agent():
    suffix = uuid.uuid4().hex[:8]
    owner = f"owner-{suffix}@example.com"
    db.create_user(UserCreate(username=owner, email=owner, role="creator", password="x"))
    name = f"sched-{suffix}"
    db.register_agent_owner(name, owner)
    db.share_agent(name, owner, COLLEAGUE)   # the colleague can be addressed at all
    return SimpleNamespace(name=name, owner=owner, key=db.create_agent_mcp_api_key(name, owner).api_key,
                           jwt=create_access_token({"sub": owner}))


def call(client, agent, method, path="", *, as_owner=False, **kw):
    token = agent.jwt if as_owner else agent.key
    return client.request(method, f"/api/agents/{agent.name}/schedules{path}",
                          headers={"Authorization": f"Bearer {token}"}, **kw)


def refused(resp):
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == CODE, resp.text


def owners_colleague_schedule(client, agent):
    resp = call(client, agent, "POST", json={**BODY, "deliver_to_workspace_email": COLLEAGUE}, as_owner=True)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestCreate:
    def test_the_agent_cannot_address_a_colleague(self, client, agent):
        refused(call(client, agent, "POST", json={**BODY, "deliver_to_workspace_email": COLLEAGUE}))
        assert db.list_agent_schedules(agent.name) == []

    def test_the_agent_can_address_its_owner(self, client, agent):
        resp = call(client, agent, "POST", json={**BODY, "deliver_to_workspace_email": agent.owner})
        assert resp.status_code == 201, resp.text

    def test_the_agent_can_create_an_undelivered_schedule(self, client, agent):
        assert call(client, agent, "POST", json=BODY).status_code == 201

    def test_the_owner_can_address_a_colleague(self, client, agent):
        owners_colleague_schedule(client, agent)


class TestAnExistingThirdPartySchedule:
    def test_the_agent_cannot_retarget_its_own_schedule_to_a_colleague(self, client, agent):
        sid = call(client, agent, "POST", json=BODY).json()["id"]
        refused(call(client, agent, "PUT", f"/{sid}", json={"deliver_to_workspace_email": COLLEAGUE}))
        assert not db.get_schedule(sid).deliver_to_workspace_email

    def test_the_agent_cannot_rewrite_the_owners_colleague_schedule(self, client, agent):
        sid = owners_colleague_schedule(client, agent)
        refused(call(client, agent, "PUT", f"/{sid}", json={"message": "Injected"}))
        assert db.get_schedule(sid).message == "Weekly brief"

    @pytest.mark.parametrize("method, path", [
        ("POST", "/{sid}/trigger"), ("POST", "/{sid}/enable"), ("POST", "/{sid}/disable"), ("DELETE", "/{sid}"),
    ])
    def test_the_agent_cannot_fire_toggle_or_delete_it(self, client, agent, method, path):
        sid = owners_colleague_schedule(client, agent)
        refused(call(client, agent, method, path.format(sid=sid)))
        assert db.get_schedule(sid) is not None

    def test_a_webhook_cannot_be_minted_for_it_even_with_the_capability(self, client, agent):
        sid = owners_colleague_schedule(client, agent)
        db.grant_agent_capability(agent.name, "schedules.manage", agent.owner)
        refused(call(client, agent, "POST", f"/{sid}/webhook"))

    def test_the_owner_keeps_every_control(self, client, agent):
        sid = owners_colleague_schedule(client, agent)
        assert call(client, agent, "PUT", f"/{sid}", json={"message": "New brief"}, as_owner=True).status_code == 200
        assert call(client, agent, "POST", f"/{sid}/trigger", as_owner=True).status_code != 403
        assert call(client, agent, "DELETE", f"/{sid}", as_owner=True).status_code == 204


class TestTheAgentsOwnSchedules:
    def test_it_still_manages_and_fires_a_schedule_for_its_owner(self, client, agent):
        sid = call(client, agent, "POST", json={**BODY, "deliver_to_workspace_email": agent.owner}).json()["id"]
        assert call(client, agent, "PUT", f"/{sid}", json={"message": "Updated"}).status_code == 200
        assert call(client, agent, "POST", f"/{sid}/trigger").status_code != 403
        assert call(client, agent, "DELETE", f"/{sid}").status_code == 204

    def test_a_nulling_retarget_stays_open(self, client, agent):
        sid = call(client, agent, "POST", json={**BODY, "deliver_to_workspace_email": agent.owner}).json()["id"]
        assert call(client, agent, "PUT", f"/{sid}", json={"deliver_to_workspace_email": None}).status_code == 200
