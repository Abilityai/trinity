"""
The Permissions panel's "not enforced in-container" line — its read
(abilityai/trinity-enterprise#925).

`GET /api/agents/{agent_name}/peer-skills/enforcement`, driven over the REAL
route and its access dependency on the real per-test database. Pins:

* the answer is only for a person who may manage the agent's skills (the
  ent#754 probe rule); an agent key, a shared viewer and a user without access
  get nulls or the uniform 404;
* "a peer could reach" = an agent with an edge to it, or one whose events it
  subscribes to, lacks a grant for one of its library skills — never itself,
  `trinity-system` or its spawner;
* the in-agent hook is probed (one `/health` read) only when reachable.
Related flow: docs/memory/feature-flows/agent-permissions.md
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import services.skill_gate_map_service as _MAP  # noqa: E402
from database import db  # noqa: E402
from db_models import EventSubscriptionCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402
from routers import agent_files as _AGENT_FILES  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "owner-925r", "owner-925r@example.com"
VIEWER, VIEWER_EMAIL = "viewer-925r", "viewer-925r@example.com"
STRANGER = "stranger-925r"
FIN, BILL, OPS = "fin-925r", "bill-925r", "ops-925r"
NULLS = {"reachable": None, "hook": None}

_PRINCIPAL = {"user": None}


def _app():
    app = FastAPI()
    app.include_router(_AGENT_FILES.router)
    found = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            if getattr(sub.call, "__name__", "") == "get_current_user":
                found.add(sub.call)
            walk(sub)

    for route in _AGENT_FILES.router.routes:
        if getattr(route, "path", "").endswith("/peer-skills/enforcement"):
            walk(route.dependant)
    assert found, "the enforcement route has no get_current_user dependency"
    for call in found:
        app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
    return app


@pytest.fixture
def world(db_backend, monkeypatch):
    for name, email in ((OWNER, OWNER_EMAIL), (VIEWER, VIEWER_EMAIL),
                        (STRANGER, "stranger-925r@example.com")):
        db.create_user(UserCreate(username=name, role="user", email=email))
    for agent in (FIN, BILL, OPS):
        db.register_agent_owner(agent, OWNER)
    db.share_agent(FIN, OWNER, VIEWER_EMAIL)
    keys = {agent: db.create_agent_mcp_api_key(agent, OWNER).id for agent in (FIN, BILL, OPS)}
    probes = []

    async def _hook(agent):
        probes.append(agent)
        return "missing"

    monkeypatch.setattr(_MAP, "hook_status", _hook)

    def as_(username, **extra):
        u = db.get_user_by_username(username)
        _PRINCIPAL["user"] = User(id=u["id"], username=username, email=u["email"], role="user",
                                  **extra)

    as_(OWNER)
    db.assign_skill(FIN, "pay-invoice", "owner")
    return SimpleNamespace(client=TestClient(_app()), probes=probes, keys=keys, as_=as_)


def _get(world, agent=FIN):
    return world.client.get(f"/api/agents/{agent}/peer-skills/enforcement")


def _ok(world, agent=FIN):
    res = _get(world, agent)
    assert res.status_code == 200, res.text
    return res.json()


def test_an_ungranted_caller_over_an_edge_makes_it_reachable_and_probes_once(world):
    db.add_agent_permission(BILL, FIN, OWNER)
    assert _ok(world) == {"reachable": True, "hook": "missing"}
    assert world.probes == [FIN]


def test_no_inbound_caller_is_not_reachable_and_nothing_is_probed(world):
    db.add_agent_permission(FIN, BILL, OWNER)          # outbound only
    assert _ok(world) == {"reachable": False, "hook": None}
    assert world.probes == []


def test_a_caller_holding_every_grant_does_not_count(world):
    db.add_agent_permission(BILL, FIN, OWNER)
    db.grant_peer_skill(BILL, FIN, "pay-invoice", OWNER)
    assert _ok(world)["reachable"] is False


def test_an_agent_with_no_library_skill_is_not_reachable(world):
    db.add_agent_permission(BILL, FIN, OWNER)
    db.delete_agent_skills(FIN)
    assert _ok(world)["reachable"] is False


def test_an_event_source_it_subscribes_to_counts_as_a_peer(world):
    db.create_event_subscription(FIN, EventSubscriptionCreate(
        source_agent=OPS, event_type="report.ready", target_message="publish it"), OWNER)
    assert _ok(world)["reachable"] is True


def test_its_spawner_and_the_system_agent_do_not_count(world, monkeypatch):
    db.register_agent_owner("trinity-system", OWNER)
    db.add_agent_permission(BILL, FIN, OWNER)
    db.add_agent_permission("trinity-system", FIN, OWNER)
    monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda a: (
        {"spawned_by_agent": BILL, "spawned_by_key_id": world.keys[BILL]} if a == FIN else {}))
    assert _ok(world)["reachable"] is False


@pytest.mark.parametrize("who", ["agent-key", "shared-viewer"])
def test_anyone_but_a_person_who_manages_its_skills_gets_nulls(world, who):
    db.add_agent_permission(BILL, FIN, OWNER)
    if who == "agent-key":
        world.as_(OWNER, mcp_scope="agent", agent_name=FIN, mcp_key_id=world.keys[FIN])
    else:
        world.as_(VIEWER)
    assert _ok(world) == NULLS
    assert world.probes == []


def test_a_user_without_access_gets_the_uniform_404(world):
    world.as_(STRANGER)
    assert _get(world).status_code == 404
