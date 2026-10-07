"""
The per-agent skill gate map — the REST surface and who may use it
(trinity-enterprise#753).

Target: ``GET/PUT/DELETE /api/agents/{agent_name}/skill-gates[/{skill_name}]``
(``routers/skill_gate.py`` → ``services/skill_gate_map_service.py``), driven
through a TestClient over the REAL routes and dependencies on the real per-test
database. ``get_current_user`` is overridden by walking each route's own
dependant tree, so every principal goes through the real fences.

The rules (AC 4, AC 8; Invariant #8; the #3236 shape):

* WRITE — a person (a signed-in session or the person's own user-scoped key)
  who owns the agent or is an admin; or an agent key holding ``skills.manage``,
  only on an agent its owner OWNS (never the admin short-circuit) and NEVER on
  itself (the agent a gate constrains must not lift it). The system key, a
  connector key, any other scope and a principal with no scope are refused on
  the principal alone (403), before anything about the target is said.
* READ — anyone with access to the agent; an agent key reads its OWN gates, or
  (holding ``skills.manage``) an agent its owner owns. A sibling the key may not
  read and an agent that does not exist answer the same 404 body.

Stubbed: the container state (stopped, so no exec), the library, the audit sink.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # _route_census, as test_2996 does

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER, OTHER, ADMIN = "owner-753-rt", "other-753-rt", "admin-753-rt"
FIN = "fin-753-rt"          # owned by OWNER
SIB = "sib-753-rt"          # owned by OWNER, no grant
ORCH = "orch-753-rt"        # owned by OWNER, holds skills.manage
AORCH = "aorch-753-rt"      # owned by ADMIN, holds skills.manage
ELSE = "else-753-rt"        # owned by OTHER
GHOST = "ghost-753-rt"
MISSING = "missing-753-rt"

_PRINCIPAL = {"user": None}
_APP = {}


def _client():
    from fastapi.testclient import TestClient
    if "app" not in _APP:
        from fastapi import FastAPI
        from routers import skill_gate as r
        app = FastAPI()
        app.include_router(r.agent_router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.agent_router.routes:
            walk(route.dependant)
        assert found, "no get_current_user on the skill-gate routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP["app"] = app
    return TestClient(_APP["app"], raise_server_exceptions=True)


def _user(username, *, scope=None, agent=None, role="user", connector=None):
    from models import User
    return User(id=1, username=username, email=f"{username}@example.com", role=role,
                mcp_scope=scope, agent_name=agent, connector_agent=connector,
                mcp_key_id="key-1" if scope else None)


@pytest.fixture
def api(db_backend, monkeypatch):
    from database import db
    from db_models import UserCreate
    from db.capability_grants import CAPABILITY_SKILLS_MANAGE
    from services import docker_utils, assignment_provider
    import services.platform_audit_service as PAS
    from services.skill_service import skill_service

    for name, role in ((OWNER, "user"), (OTHER, "user"), (ADMIN, "admin")):
        db.create_user(UserCreate(username=name, role=role, email=f"{name}@example.com"))
    for agent, owner in ((FIN, OWNER), (SIB, OWNER), (ORCH, OWNER), (AORCH, ADMIN), (ELSE, OTHER)):
        db.register_agent_owner(agent, owner)
    db.register_agent_owner(GHOST, OWNER, is_ephemeral=True)
    db.grant_agent_capability(ORCH, CAPABILITY_SKILLS_MANAGE, ADMIN)
    db.grant_agent_capability(AORCH, CAPABILITY_SKILLS_MANAGE, ADMIN)

    async def _stopped(name):
        return "stopped"

    async def _audit(*a, **kw):
        return "evt"

    monkeypatch.setattr(docker_utils, "agent_container_state_async", _stopped)
    monkeypatch.setattr(skill_service, "list_skills", lambda: [])
    monkeypatch.setattr(PAS.platform_audit_service, "log", _audit)
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: None)
    return SimpleNamespace(db=db, client=_client())


def _as(principal):
    _PRINCIPAL["user"] = principal


def _put(api, agent, skill="pay-invoice", body=None):
    return api.client.put(f"/api/agents/{agent}/skill-gates/{skill}", json=body or {})


def _delete(api, agent, skill="pay-invoice"):
    return api.client.delete(f"/api/agents/{agent}/skill-gates/{skill}")


def _get(api, agent):
    return api.client.get(f"/api/agents/{agent}/skill-gates")


# ---- writes -------------------------------------------------------------------

@pytest.mark.parametrize("principal", [
    pytest.param(lambda: _user(OWNER), id="owner-session"),
    pytest.param(lambda: _user(OWNER, scope="user"), id="owner-user-key"),
    pytest.param(lambda: _user(ADMIN, role="admin"), id="admin-session"),
    pytest.param(lambda: _user(OWNER, scope="agent", agent=ORCH), id="holder-on-a-sibling"),
])
def test_who_may_set_and_clear(api, principal):
    _as(principal())
    r = _put(api, FIN, body={"deadline_hours": 12})
    assert r.status_code == 200, r.text
    assert r.json()["gate"]["deadline_hours"] == 12
    assert _delete(api, FIN).json()["changed"] is True


@pytest.mark.parametrize("principal,code", [
    pytest.param(lambda: _user(ADMIN, scope="system", role="admin"), "person_required", id="system-key"),
    pytest.param(lambda: _user(OWNER, scope="connector", connector=FIN), "person_required", id="connector"),
    pytest.param(lambda: _user(OWNER, scope="ops"), "person_required", id="ops-scope"),
    pytest.param(lambda: _user(OWNER, scope="agent", agent=SIB), "skill_management_not_permitted",
                 id="agent-without-the-grant"),
    pytest.param(lambda: _user(OWNER, scope="agent", agent=ORCH), "person_required", id="holder-on-itself"),
])
def test_who_may_not_write_is_refused_on_the_principal(api, principal, code):
    _as(principal())
    target = ORCH if principal().agent_name == ORCH else FIN
    for r in (_put(api, target), _delete(api, target)):
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == code
    assert api.db.list_agent_skill_gates(target) == []


def test_the_refusal_does_not_depend_on_whether_the_agent_exists(api):
    _as(_user(ADMIN, scope="system", role="admin"))
    assert _put(api, MISSING).json() == _put(api, FIN).json()


def test_a_principal_without_a_scope_is_refused(api):
    _as(SimpleNamespace(id=1, username=OWNER, email=None, role="admin", agent_name=None,
                        connector_agent=None, portal_delegate=False))
    assert _put(api, FIN).status_code == 403


def test_a_holder_reaches_only_agents_its_owner_owns(api):
    """An agent key carries its owner's role; an admin-owned holder must not
    reach another user's agent through `can_user_share_agent`'s admin
    short-circuit. Same 404 as an agent that does not exist."""
    _as(_user(ADMIN, scope="agent", agent=AORCH, role="admin"))
    other = _put(api, ELSE)
    missing = _put(api, MISSING)
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()
    assert api.db.list_agent_skill_gates(ELSE) == []


def test_a_person_who_does_not_own_the_agent_gets_the_uniform_404(api):
    _as(_user(OTHER))
    r, m = _put(api, FIN), _put(api, MISSING)
    assert r.status_code == m.status_code == 404 and r.json() == m.json()


def test_a_ghost_is_refused_after_the_access_check(api):
    _as(_user(OWNER))
    r = _put(api, GHOST)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ephemeral_agent"
    assert r.headers["X-Trinity-Error-Code"] == "ephemeral_agent"
    _as(_user(OTHER))
    assert _put(api, GHOST).status_code == 404


@pytest.mark.parametrize("body,code", [
    ({"deadline_hours": True}, "invalid_deadline"),
    ({"deadline_hours": "24"}, "invalid_deadline"),
    ({"deadline_hours": 500}, "invalid_deadline"),
    ({"approver": "approver"}, "approver_unavailable"),
    ({"approver": "owner"}, "invalid_approver"),
])
def test_bad_input_gets_its_named_code(api, body, code):
    _as(_user(OWNER))
    r = _put(api, FIN, body=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code
    assert r.headers["X-Trinity-Error-Code"] == code


def test_a_bad_skill_name_is_refused(api):
    _as(_user(OWNER))
    r = _put(api, FIN, skill="bad%20name")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_skill_name"


def test_put_then_get_round_trips_every_field(api):
    _as(_user(OWNER))
    _put(api, FIN, skill="Pay-Invoice", body={"deadline_hours": 48})
    _put(api, FIN, skill="Pay-Invoice", body={})                      # omitted: kept
    body = _get(api, FIN).json()
    assert body["agent_name"] == FIN
    assert body["approver_kinds"] == ["primary"] and body["default_deadline_hours"] == 24
    [gate] = body["gates"]
    assert {k: gate[k] for k in ("skill_name", "approver", "deadline_hours", "origin", "set_by",
                                 "set_by_agent", "approver_reachable")} == {
        "skill_name": "pay-invoice", "approver": "primary", "deadline_hours": 48, "origin": "set",
        "set_by": OWNER, "set_by_agent": None, "approver_reachable": True}
    assert gate["set_at"]
    _put(api, FIN, skill="pay-invoice", body={"deadline_hours": None})
    assert _get(api, FIN).json()["gates"][0]["deadline_hours"] is None


# ---- reads --------------------------------------------------------------------

def test_a_machine_key_never_reads_who_set_a_gate(api):
    """`set_by` is a username — an email for email-login users. A gate set by a
    non-owner admin must not hand that admin's address to the agent's key
    (cso-diff 2026-10-07 finding 1; #715 people stay with people)."""
    _as(_user(ADMIN, role="admin"))
    _put(api, FIN)
    _as(_user(OWNER))
    assert _get(api, FIN).json()["gates"][0]["set_by"] == ADMIN
    for machine in (_user(OWNER, scope="agent", agent=FIN),
                    _user(OWNER, scope="agent", agent=ORCH),
                    _user(ADMIN, scope="system", role="admin")):
        _as(machine)
        [gate] = _get(api, FIN).json()["gates"]
        assert gate["set_by"] is None and gate["skill_name"] == "pay-invoice"
    _as(_user(OWNER, scope="agent", agent=ORCH))
    assert _put(api, FIN, body={"deadline_hours": 2}).json()["gate"]["set_by"] is None


@pytest.mark.parametrize("principal,agent", [
    pytest.param(lambda: _user(OWNER), FIN, id="owner"),
    pytest.param(lambda: _user(OWNER, scope="user"), FIN, id="owner-user-key"),
    pytest.param(lambda: _user(ADMIN, role="admin"), ELSE, id="admin"),
    pytest.param(lambda: _user(ADMIN, scope="system", role="admin"), FIN, id="system-key"),
    pytest.param(lambda: _user(OWNER, scope="agent", agent=SIB), SIB, id="agent-reads-its-own"),
    pytest.param(lambda: _user(OWNER, scope="agent", agent=ORCH), FIN, id="holder-reads-a-sibling"),
])
def test_who_may_read(api, principal, agent):
    _as(principal())
    r = _get(api, agent)
    assert r.status_code == 200, r.text
    assert r.json()["agent_name"] == agent


@pytest.mark.parametrize("principal,agent", [
    pytest.param(lambda: _user(OWNER, scope="agent", agent=SIB), FIN, id="agent-reads-a-sibling"),
    pytest.param(lambda: _user(ADMIN, scope="agent", agent=AORCH, role="admin"), ELSE,
                 id="admin-owned-holder-reads-another-users-agent"),
    pytest.param(lambda: _user(OTHER), FIN, id="person-without-access"),
])
def test_a_read_that_may_not_happen_looks_like_a_missing_agent(api, principal, agent):
    _as(principal())
    r, m = _get(api, agent), _get(api, MISSING)
    assert r.status_code == m.status_code == 404
    assert r.content == m.content


@pytest.mark.parametrize("principal", [
    pytest.param(lambda: _user(OWNER, scope="connector", connector=FIN), id="connector-on-its-agent"),
    pytest.param(lambda: _user(OWNER, scope="ops"), id="unknown-scope"),
])
def test_a_scope_that_may_not_read_is_refused_on_the_principal(api, principal):
    _as(principal())
    assert _get(api, FIN).status_code == 403
    assert _get(api, MISSING).status_code == 403


# ---- the census ----------------------------------------------------------------

def test_every_route_is_classified_in_the_census():
    import _route_census as census
    for key in ("routers/skill_gate.py::list_agent_skill_gates",
                "routers/skill_gate.py::set_agent_skill_gate",
                "routers/skill_gate.py::clear_agent_skill_gate"):
        assert key in census.AGENT_CALLABLE, key
