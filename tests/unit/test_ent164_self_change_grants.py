"""trinity-enterprise#164 — an agent changes its own shape only with a grant.

Re-scoped 2026-10-02: no per-action approval, no pause-and-resume. Three
capabilities join `skills.manage` (ent#596) in the closed set; without the grant
an agent's call is REFUSED, the refusal names the missing permission and how to
ask for it, and it is audited as `capability_refused`.

  | capability            | covers                                                     |
  |-----------------------|------------------------------------------------------------|
  | schedules.manage      | create/update/delete/enable/disable schedules + webhooks   |
  | instructions.manage   | CLAUDE.md, AGENTS.md, .claude/** (not skills), git reset   |
  | agents.manage         | create (non-ephemeral)/delete/deploy, reconfigure, rename  |

Decisions recorded (2026-10-05): ephemeral ("ghost") spawning stays governed by
ent#69 and needs no grant; every reconfigure route is in agents.manage; schedule
webhooks are in schedules.manage ("trigger now" is not); `.claude/skills/**`
stays `skills.manage` alone. Autonomy is not a capability (person-only).

The fences are read off FastAPI's own dependant graph — what actually runs — so
a route that forgets one fails here whatever its source looks like.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from db.capability_grants import (
    CAPABILITIES, CAPABILITY_AGENTS_MANAGE, CAPABILITY_INSTRUCTIONS_MANAGE,
    CAPABILITY_SCHEDULES_MANAGE, CAPABILITY_SKILLS_MANAGE,
)

pytestmark = pytest.mark.unit

HOLDER, SIBLING = "fleet-orchestrator", "sales-companion"


def _principal(scope, agent=None):
    return SimpleNamespace(id=1, username="owner", email="o@example.com", role="admin",
                           mcp_scope=scope, agent_name=agent, connector_agent=None,
                           mcp_key_id="key-1" if scope else None)


def _request(path="/api/agents/x", method="PUT", params=None):
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method=method,
                           scope={"path": path}, state=SimpleNamespace(request_id="r1"),
                           path_params=params or {"agent_name": SIBLING})


@pytest.fixture
def held(monkeypatch):
    """`db.agent_has_capability` answered from a {agent: {caps}} map the test owns."""
    grants = {HOLDER: set(CAPABILITIES)}
    from database import db
    monkeypatch.setattr(db, "agent_has_capability",
                        lambda agent, cap: cap in grants.get(agent, set()))
    return grants


@pytest.fixture
def audit(monkeypatch):
    import importlib
    mod = importlib.import_module("services.platform_audit_service")
    log = AsyncMock(return_value="evt")
    monkeypatch.setattr(mod.platform_audit_service, "log", log)
    return log


def _calls(route) -> set:
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        seen.add(getattr(d.call, "__name__", repr(d.call)))
        stack.extend(d.dependencies)
    return seen


def _routes(module):
    return {(r.path, m): r for r in module.router.routes for m in (getattr(r, "methods", ()) or ())}


# ---------------------------------------------------------------------------
# 1. The closed set and the refusal text
# ---------------------------------------------------------------------------

def test_the_three_capabilities_join_the_closed_set():
    assert CAPABILITIES == {CAPABILITY_SKILLS_MANAGE, CAPABILITY_SCHEDULES_MANAGE,
                            CAPABILITY_INSTRUCTIONS_MANAGE, CAPABILITY_AGENTS_MANAGE}


@pytest.mark.parametrize("cap,code", [
    (CAPABILITY_SCHEDULES_MANAGE, "schedule_management_not_permitted"),
    (CAPABILITY_INSTRUCTIONS_MANAGE, "instruction_management_not_permitted"),
    (CAPABILITY_AGENTS_MANAGE, "agent_management_not_permitted"),
    (CAPABILITY_SKILLS_MANAGE, "skill_management_not_permitted"),
])
def test_a_non_holder_gets_a_named_refusal_that_says_how_to_ask(held, cap, code):
    from routers import schedules
    capability_refusal = schedules.router.routes[0].endpoint.__globals__.get("capability_refusal")
    if capability_refusal is None:
        import dependencies
        capability_refusal = dependencies.capability_refusal
    got_code, message = capability_refusal(_principal("agent", SIBLING), cap)
    assert got_code == code
    assert "permission-request" in message and "admin grants it" in message
    assert capability_refusal(_principal("agent", HOLDER), cap) is None
    for human in (_principal(None), _principal("user"), _principal("system")):
        assert capability_refusal(human, cap) is None        # humans are never fenced


# ---------------------------------------------------------------------------
# 2. Every route in each class takes its fence (FastAPI's dependant graph)
# ---------------------------------------------------------------------------

def test_every_schedule_write_takes_the_schedules_fence_and_trigger_does_not():
    from routers import schedules
    table = _routes(schedules)
    fenced = [
        ("/api/agents/{name}/schedules", "POST"),
        ("/api/agents/{name}/schedules/{schedule_id}", "PUT"),
        ("/api/agents/{name}/schedules/{schedule_id}", "DELETE"),
        ("/api/agents/{name}/schedules/{schedule_id}/enable", "POST"),
        ("/api/agents/{name}/schedules/{schedule_id}/disable", "POST"),
        ("/api/agents/{name}/schedules/{schedule_id}/webhook", "POST"),
        ("/api/agents/{name}/schedules/{schedule_id}/webhook", "DELETE"),
        ("/api/agents/{name}/schedules/{schedule_id}/webhook/secret", "POST"),
        ("/api/agents/{name}/schedules/{schedule_id}/webhook/secret", "DELETE"),
    ]
    for key in fenced:
        assert "capability_fence_schedules_manage" in _calls(table[key]), key
    trigger = table[("/api/agents/{name}/schedules/{schedule_id}/trigger", "POST")]
    assert "capability_fence_schedules_manage" not in _calls(trigger)


def test_every_reconfigure_route_admits_only_a_person_or_a_holder():
    from routers import agent_config
    table = _routes(agent_config)
    for path in ("/read-only", "/resources", "/timeout", "/public-channel-model", "/guardrails"):
        route = table[(f"/api/agents/{{agent_name}}{path}", "PUT")]
        calls = _calls(route)
        assert "require_person_or_agents_manage" in calls, path
        assert "require_person" not in calls, path
    autonomy = table[("/api/agents/{agent_name}/autonomy", "PUT")]
    assert "require_person" in _calls(autonomy)           # never grantable


@pytest.mark.parametrize("module,key", [
    ("chat", ("/api/agents/{name}/model", "PUT")),
    ("agents", ("/api/agents/{agent_name}", "DELETE")),
    ("agents", ("/api/agents/deploy-local", "POST")),
    ("systems", ("/api/systems/deploy", "POST")),
])
def test_the_other_agent_management_routes_take_the_fence(module, key):
    import importlib
    mod = importlib.import_module(f"routers.{module}")
    assert "capability_fence_agents_manage" in _calls(_routes(mod)[key]), key


def test_rename_stays_person_only_and_is_not_a_grant():
    """ent#69 Part 2 ruled rename human-only; ent#164 does not reopen it (like
    autonomy, it is not something an agent can be granted)."""
    from routers import agent_rename
    src = agent_rename.rename_agent_endpoint.__globals__["reject_agent_principal"]
    assert src is not None
    route = _routes(agent_rename)[("/api/agents/{agent_name}/rename", "PUT")]
    assert "capability_fence_agents_manage" not in _calls(route)


def test_git_reset_takes_the_instructions_fence():
    from routers import git
    route = _routes(git)[("/api/agents/{agent_name}/git/reset-to-main-preserve-state", "POST")]
    assert "capability_fence_instructions_manage" in _calls(route)


# ---------------------------------------------------------------------------
# 3. The dependencies behave
# ---------------------------------------------------------------------------

def _dep(module, key, name):
    import importlib
    route = _routes(importlib.import_module(f"routers.{module}"))[key]
    stack = list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        if getattr(d.call, "__name__", "") == name:
            return d.call
        stack.extend(d.dependencies)
    raise AssertionError(f"{name} not on {key}")


def test_the_fence_refuses_a_non_holder_audits_it_and_passes_everyone_else(held, audit):
    fence = _dep("schedules", ("/api/agents/{name}/schedules", "POST"), "capability_fence_schedules_manage")
    req = _request(params={"name": "someone-else"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(fence(req, _principal("agent", SIBLING)))
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "schedule_management_not_permitted"
    assert audit.await_args.kwargs["event_action"] == "capability_refused"
    for ok in (_principal("agent", HOLDER), _principal(None), _principal("user")):
        assert asyncio.run(fence(req, ok)) is None


def test_an_agent_manages_its_own_schedules_without_a_grant(held, audit):
    """Decided 2026-10-05 (#2996 kept): an agent's own schedules are its own use;
    schedules.manage is what it needs for ANOTHER agent's."""
    fence = _dep("schedules", ("/api/agents/{name}/schedules", "POST"), "capability_fence_schedules_manage")
    own = _request(params={"name": SIBLING})
    assert asyncio.run(fence(own, _principal("agent", SIBLING))) is None
    other = _request(params={"name": "someone-else"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(fence(other, _principal("agent", SIBLING)))
    assert e.value.detail["code"] == "schedule_management_not_permitted"


def test_the_own_agent_exemption_is_schedules_only(held, audit):
    fence = _dep("agents", ("/api/agents/{agent_name}", "DELETE"), "capability_fence_agents_manage")
    with pytest.raises(HTTPException):
        asyncio.run(fence(_request(params={"agent_name": SIBLING}), _principal("agent", SIBLING)))


def test_person_or_holder_keeps_the_person_rule_for_every_non_agent(held, audit):
    dep = _dep("agent_config", ("/api/agents/{agent_name}/resources", "PUT"),
               "require_person_or_agents_manage")
    req = _request()
    assert asyncio.run(dep(req, _principal("agent", HOLDER))).agent_name == HOLDER
    with pytest.raises(HTTPException) as e:
        asyncio.run(dep(req, _principal("agent", SIBLING)))
    assert e.value.detail["code"] == "agent_management_not_permitted"
    assert asyncio.run(dep(req, _principal(None))) is not None            # a signed-in person
    for not_a_person in (_principal("connector"), _principal("portal_delegate")):
        with pytest.raises(HTTPException) as e:
            asyncio.run(dep(req, not_a_person))
        assert e.value.status_code == 403
        assert "code" not in (e.value.detail if isinstance(e.value.detail, dict) else {}) or \
            e.value.detail.get("code") != "agent_management_not_permitted"


# ---------------------------------------------------------------------------
# 4. Agent creation: ghosts are exempt, everything else needs the grant
# ---------------------------------------------------------------------------

def test_an_agent_creating_a_durable_agent_needs_the_grant(held, audit):
    from routers import agents as agents_router
    gate = agents_router.create_agent_endpoint.__globals__["_require_create_capability"]
    req = _request(path="/api/agents", method="POST", params={})
    with pytest.raises(HTTPException) as e:
        asyncio.run(gate(req, _principal("agent", SIBLING), SimpleNamespace(ephemeral=False, name="new-one")))
    assert e.value.detail["code"] == "agent_management_not_permitted"
    asyncio.run(gate(req, _principal("agent", HOLDER), SimpleNamespace(ephemeral=False, name="new-one")))


def test_spawning_a_ghost_needs_no_grant(held, audit):
    from routers import agents as agents_router
    gate = agents_router.create_agent_endpoint.__globals__["_require_create_capability"]
    req = _request(path="/api/agents", method="POST", params={})
    asyncio.run(gate(req, _principal("agent", SIBLING), SimpleNamespace(ephemeral=True, name="ghost-1")))
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Instruction files through the file routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,ancestors,expected", [
    ("CLAUDE.md", False, True),
    ("/home/developer/AGENTS.md", False, True),
    (".claude/settings.json", False, True),
    (".claude/agents/reviewer.md", False, True),
    (".claude/commands/x.md", False, True),
    (".claude/skills/x/SKILL.md", False, False),        # skills.manage alone (decided)
    ("docs/CLAUDE.md", False, False),                    # only the agent's own top-level file
    ("notes.md", False, False),
    (".claude", True, True),                             # deleting .claude deletes instructions
    ("/home/developer", True, True),
    ("notes", True, False),
])
def test_what_counts_as_an_instruction_file(path, ancestors, expected):
    from services.agent_service.files import _touches_instructions
    assert _touches_instructions(path, include_ancestors=ancestors) is expected


@pytest.fixture
def files_mod(monkeypatch, held, audit):
    from services.agent_service import files
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    monkeypatch.setattr(files, "get_agent_container", lambda name: None)
    return files


@pytest.mark.parametrize("fn,args", [
    ("update_agent_file_logic", ("CLAUDE.md", "be nice")),
    ("update_agent_file_logic", (".claude/settings.json", "{}")),
    ("create_agent_folder_logic", (".claude/agents",)),
    ("delete_agent_file_logic", ("AGENTS.md",)),
])
def test_a_non_holder_cannot_rewrite_instructions_through_the_file_routes(files_mod, fn, args):
    call = getattr(files_mod, fn)
    path, *rest = args
    with pytest.raises(HTTPException) as exc:
        if fn == "update_agent_file_logic":
            asyncio.run(call(SIBLING, path, rest[0], _principal("agent", SIBLING), _request()))
        else:
            asyncio.run(call(SIBLING, path, _principal("agent", SIBLING), _request()))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "instruction_management_not_permitted"


def test_a_skills_write_still_asks_only_for_skills_manage(files_mod, held):
    held[SIBLING] = {CAPABILITY_SKILLS_MANAGE}             # skills yes, instructions no
    with pytest.raises(HTTPException) as exc:
        asyncio.run(files_mod.update_agent_file_logic(
            SIBLING, ".claude/skills/x/SKILL.md", "body", _principal("agent", SIBLING), _request()))
    assert exc.value.status_code == 404                     # past every gate: container lookup


def test_ordinary_files_need_no_grant(files_mod):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(files_mod.update_agent_file_logic(
            SIBLING, "notes.md", "x", _principal("agent", SIBLING), _request()))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 6. Granting: who may receive instructions.manage, and the generic grant route
# ---------------------------------------------------------------------------

def test_a_calibrating_companion_cannot_be_granted_instructions(monkeypatch):
    from services import capability_grant_service as svc
    monkeypatch.setattr(svc.db, "get_agent_owner", lambda agent: {"username": "o", "is_system": False})
    monkeypatch.setattr(svc.db, "get_agent_role_readiness", lambda agent: {"status": "calibrating"})
    monkeypatch.setattr(svc.db, "grant_capability", lambda *a, **k: True, raising=False)
    with pytest.raises(svc.CapabilityGrantRefused) as e:
        svc.set_grant(SIBLING, CAPABILITY_INSTRUCTIONS_MANAGE, True, "admin")
    assert e.value.code == "calibrating_agent"
    # The other three are still grantable to a calibrating companion.
    monkeypatch.setattr(svc.db, "get_agent_ephemeral_info", lambda agent: None)
    assert svc.set_grant(SIBLING, CAPABILITY_SCHEDULES_MANAGE, True, "admin")["granted"] is True


def test_the_generic_grant_route_is_admin_only_and_not_shadowed():
    from routers import agent_config
    table = _routes(agent_config)
    put = table[("/api/agents/{agent_name}/capability-grants/{capability}", "PUT")]
    assert "require_admin" in _calls(put)
    get = table[("/api/agents/{agent_name}/capability-grants", "GET")]
    assert get.endpoint.__name__ == "list_agent_capability_grants"


def test_discarding_a_ghost_needs_no_grant_but_deleting_a_durable_agent_does(held, audit, monkeypatch):
    from database import db
    ghosts = {"ghost-1"}
    monkeypatch.setattr(db, "get_agent_ephemeral_info",
                        lambda name: {"is_ephemeral": name in ghosts})
    fence = _dep("agents", ("/api/agents/{agent_name}", "DELETE"), "capability_fence_agents_manage")
    assert asyncio.run(fence(_request(params={"agent_name": "ghost-1"}), _principal("agent", SIBLING))) is None
    with pytest.raises(HTTPException) as e:
        asyncio.run(fence(_request(params={"agent_name": "durable"}), _principal("agent", SIBLING)))
    assert e.value.detail["code"] == "agent_management_not_permitted"


def test_the_create_endpoint_refuses_before_anything_is_created(held, audit, monkeypatch):
    """The gate is on the endpoint itself, ahead of the idempotency claim and
    `create_agent_internal` — a refused create touches nothing."""
    from routers import agents as agents_router
    created = []

    async def fake_create(*a, **k):
        created.append(a)
        return {"name": "new-one"}
    monkeypatch.setattr(agents_router, "create_agent_internal", fake_create)
    config = SimpleNamespace(ephemeral=None, name="new-one")
    req = _request(path="/api/agents", method="POST", params={})
    with pytest.raises(HTTPException) as e:
        asyncio.run(agents_router.create_agent_endpoint(config, req, _principal("agent", SIBLING), None))
    assert e.value.detail["code"] == "agent_management_not_permitted"
    assert created == []
    asyncio.run(agents_router.create_agent_endpoint(config, req, _principal("agent", HOLDER), None))
    assert len(created) == 1
