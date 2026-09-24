"""trinity-enterprise#596 — only designated agents may change an agent's skills.

The ruling (operator 2026-09-17, restated 09-18): changing an agent's skills —
another agent's OR ITS OWN — is its own permission. An instance admin grants it
to named agents; every other agent key is refused on both; neither this nor
permission to call an agent implies the other; humans and the system agent are
unchanged.

What these tests pin, and why each exists:

1. the scope matrix of the gate (`capability_refusal`) — an ALLOWLIST (#2323), so
   a scope nobody has thought of is refused, and a principal with no scope at
   all fails CLOSED;
2. the composed dependency — the capability check runs BEFORE the owner fence,
   so a non-holder gets one uniform 403 (never a 404/403 existence signal) and a
   holder gains no reach beyond its owner's agents;
3. the ROUTE TABLE — every route that changes an agent's skills depends on the
   fence (read off FastAPI's real dependant graph, not the source text), so the
   next route added cannot forget it;
4. a refusal changes NOTHING — driven through a real FastAPI app on all four
   routes, with every write and delivery wired to explode if reached;
5. the file-route bypass — `.claude/skills/**` written through `PUT /files`
   takes the same capability (both reviewers found it; verified);
6. grants over a real SQLite file — live-only, idempotent who/when, soft-delete;
7. attribution over a real SQLite file — the agent that made the change is
   recorded, and a replace does not re-stamp the names it keeps.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from db.capability_grants import CAPABILITY_SKILLS_MANAGE

HOLDER = "trinity-pm"


class _Deps:
    """The `dependencies` module the ROUTER actually holds.

    The unit conftest pops `dependencies` (and `services.platform_audit_service`)
    from `sys.modules` after every test (`_POP_PREFIXES`), so a module-level
    `import dependencies` here would be a different object from the one
    `routers.skills` bound at import — overrides and patches aimed at it would
    silently miss. Read everything through the router's own references instead.
    """

    def __getattr__(self, name):
        from routers import skills
        return skills.get_skill_managed_agent_by_name.__globals__[name]


deps = _Deps()
SIBLING = "sales-companion"
OUTSIDER = "someone-elses-agent"


def _principal(scope, agent=None, role="admin", username="owner"):
    return SimpleNamespace(id=1, username=username, email="o@example.com", role=role,
                           mcp_scope=scope, agent_name=agent, connector_agent=None,
                           mcp_key_id="key-1" if scope else None)


def _request(path="/api/agents/x/skills", method="PUT"):
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method=method,
                           url=SimpleNamespace(path=path), state=SimpleNamespace(request_id="r1"))


@pytest.fixture
def grants(monkeypatch):
    """`db.agent_has_capability` answered from a set the test owns."""
    held = {HOLDER}
    from database import db
    monkeypatch.setattr(db, "agent_has_capability",
                        lambda agent, cap: cap == CAPABILITY_SKILLS_MANAGE and agent in held)
    return held


@pytest.fixture
def audit(monkeypatch):
    """One recorder for every audit write this test can reach: the instance the
    router bound at import AND the one `enforce_agent_capability` imports lazily
    (the conftest may have re-imported the module since)."""
    import importlib
    from routers import skills
    lazily_imported = importlib.import_module("services.platform_audit_service")
    log = AsyncMock(return_value="evt")
    monkeypatch.setattr(skills.platform_audit_service, "log", log)
    monkeypatch.setattr(lazily_imported.platform_audit_service, "log", log)
    return log


# ---- 1. the scope matrix -----------------------------------------------------

@pytest.mark.parametrize("scope,agent,passes", [
    (None, None, True),              # JWT human — unchanged
    ("user", None, True),            # a human's own MCP key — unchanged
    ("system", None, True),          # trinity-system — unchanged
    ("agent", HOLDER, True),         # granted
    ("agent", SIBLING, False),       # not granted
    ("agent", None, False),          # agent scope with no agent identity
    ("connector", None, False),
    ("ops", None, False),
    ("portal_delegate", None, False),
    ("a-scope-invented-tomorrow", None, False),   # the #2323 property
])
def test_the_gate_is_an_allowlist(grants, scope, agent, passes):
    refusal = deps.capability_refusal(_principal(scope, agent), CAPABILITY_SKILLS_MANAGE)
    assert (refusal is None) is passes
    if not passes:
        code, message = refusal
        assert code == "skill_management_not_permitted"
        assert "Settings" in message   # names where it is granted, not just "no"


def test_a_principal_with_no_scope_attribute_fails_closed(grants):
    """`getattr(..., None)` would read an absent scope as the JWT human — the most
    privileged value in the set (#2323). The sentinel must refuse it."""
    stand_in = SimpleNamespace(username="owner", role="admin", agent_name=None)
    assert deps.capability_refusal(stand_in, CAPABILITY_SKILLS_MANAGE) is not None


def test_a_revoked_holder_is_refused_on_the_very_next_call(grants):
    p = _principal("agent", HOLDER)
    assert deps.capability_refusal(p, CAPABILITY_SKILLS_MANAGE) is None
    grants.discard(HOLDER)
    assert deps.capability_refusal(p, CAPABILITY_SKILLS_MANAGE) is not None


def test_call_permission_does_not_grant_skill_management(grants, monkeypatch):
    """AC4: an `agent_permissions` edge to the target buys nothing here. The gate
    must not consult the call graph at all — wire it to say yes and to explode."""
    from database import db
    for name in ("is_agent_permitted", "can_agent_call_agent", "get_permitted_agents"):
        if hasattr(db, name):
            monkeypatch.setattr(db, name, lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the skill gate read the call graph")))
    assert deps.capability_refusal(_principal("agent", SIBLING), CAPABILITY_SKILLS_MANAGE) is not None


@pytest.mark.parametrize("scope,agent,expected", [
    ("agent", HOLDER, HOLDER),
    ("system", None, "trinity-system"),   # would otherwise read as a human (R29)
    ("user", None, None),
    (None, None, None),
])
def test_acting_agent_name(scope, agent, expected):
    assert deps.acting_agent_name(_principal(scope, agent)) == expected


# ---- 2. the composed dependency ---------------------------------------------

@pytest.fixture
def owner_fence(monkeypatch):
    """The real owner fence, replaced by a recorder: which names reached it."""
    reached = []

    def fence(agent_name, current_user):
        reached.append(agent_name)
        if agent_name == OUTSIDER:
            raise HTTPException(status_code=404, detail="Agent not found")
        return agent_name

    from routers import skills
    monkeypatch.setitem(skills.get_skill_managed_agent_by_name.__globals__, "get_owned_agent_by_name", fence)
    return reached


def _run_fence(principal, target):
    return asyncio.run(deps.get_skill_managed_agent_by_name(
        request=_request(f"/api/agents/{target}/skills"), agent_name=target, current_user=principal))


@pytest.mark.parametrize("target", [SIBLING, "no-such-agent", OUTSIDER])
def test_a_non_holder_gets_one_uniform_403_before_the_owner_fence(grants, owner_fence, audit, target):
    """Existent, nonexistent and someone else's agent all answer the same 403,
    and the owner fence is never reached — the refusal is not an existence oracle."""
    with pytest.raises(HTTPException) as exc:
        _run_fence(_principal("agent", SIBLING), target)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "skill_management_not_permitted"
    assert owner_fence == []


def test_a_non_holder_is_refused_on_ITSELF(grants, owner_fence, audit):
    """The ruling that closed the build gate: `permitted`, not `{self} ∪ permitted`."""
    with pytest.raises(HTTPException) as exc:
        _run_fence(_principal("agent", SIBLING), SIBLING)
    assert exc.value.status_code == 403


def test_a_holder_manages_itself_and_siblings_but_never_beyond_its_owner(grants, owner_fence, audit):
    assert _run_fence(_principal("agent", HOLDER), HOLDER) == HOLDER
    assert _run_fence(_principal("agent", HOLDER), SIBLING) == SIBLING
    with pytest.raises(HTTPException) as exc:
        _run_fence(_principal("agent", HOLDER), OUTSIDER)
    assert exc.value.status_code == 404   # the grant widened nothing
    audit.assert_not_awaited()


@pytest.mark.parametrize("scope", [None, "user", "system"])
def test_humans_and_the_system_agent_are_unchanged(grants, owner_fence, audit, scope):
    assert _run_fence(_principal(scope), SIBLING) == SIBLING
    assert owner_fence == [SIBLING]
    audit.assert_not_awaited()


def test_every_refusal_is_an_audit_row(grants, owner_fence, audit):
    with pytest.raises(HTTPException):
        _run_fence(_principal("agent", SIBLING), HOLDER)
    audit.assert_awaited_once()
    kw = audit.await_args.kwargs
    assert kw["event_action"] == "capability_refused"
    assert kw["actor_agent_name"] == SIBLING and kw["target_id"] == HOLDER
    assert kw["details"]["capability"] == CAPABILITY_SKILLS_MANAGE
    # The refused actor is the AGENT. `platform_audit_service` ranks `actor_user`
    # above `actor_agent_name`, so passing the principal filed the attempt as the
    # owner's own act (actor_type=user) — found by the live demo, not by review.
    assert "actor_user" not in kw
    assert kw["actor_email"] == "o@example.com"      # the owner rides along
    assert kw["mcp_key_id"] == "key-1" and kw["mcp_scope"] == "agent"


def test_a_refused_agent_is_filed_as_an_AGENT_by_the_real_actor_resolver():
    """The resolver the audit service really uses: with `actor_user` present it
    returns ('user', owner id) whatever else is passed — so the gate must not
    pass it for an agent principal."""
    import importlib
    svc = importlib.import_module("services.platform_audit_service").PlatformAuditService
    assert svc._resolve_actor(None, SIBLING, "agent", "key-1")[:2] == ("agent", SIBLING)
    assert svc._resolve_actor(_principal("agent", SIBLING), SIBLING, "agent", "key-1")[0] == "user"


def test_an_audit_failure_never_turns_the_403_into_a_500(grants, owner_fence, monkeypatch):
    import importlib
    mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(mod.platform_audit_service, "log", AsyncMock(side_effect=RuntimeError("db down")))
    with pytest.raises(HTTPException) as exc:
        _run_fence(_principal("agent", SIBLING), SIBLING)
    assert exc.value.status_code == 403


# ---- 3. the route table ------------------------------------------------------

def _dependency_calls(route):
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        seen.add(getattr(d.call, "__name__", repr(d.call)))
        stack.extend(d.dependencies)
    return seen


def test_every_route_that_changes_an_agents_skills_takes_the_fence():
    """Read off FastAPI's own dependant graph — what actually runs — so a route
    that forgets the fence fails here, whatever its source looks like."""
    from routers import skills
    # ent#530 widened the filter from `/skills` to `/skill`, so the skill-SET
    # writes are held to the same fence. `/skill-manager` is the admin GRANT
    # route (grant-vs-use), gated by `require_admin`, not by this fence.
    writers = [r for r in skills.router.routes
               if "/agents/{agent_name}/skill" in r.path and "/skill-manager" not in r.path
               and r.methods & {"PUT", "POST", "DELETE"}]
    assert {(r.path, m) for r in writers for m in r.methods} == {
        ("/api/agents/{agent_name}/skills", "PUT"),
        ("/api/agents/{agent_name}/skills/inject", "POST"),
        ("/api/agents/{agent_name}/skills/{skill_name}", "POST"),
        ("/api/agents/{agent_name}/skills/{skill_name}", "DELETE"),
        ("/api/agents/{agent_name}/skill-sets/{set_name}", "POST"),
        ("/api/agents/{agent_name}/skill-sets/{set_name}", "DELETE"),
    }
    for r in writers:
        assert "get_skill_managed_agent_by_name" in _dependency_calls(r), r.path


def test_the_grant_routes_exist_and_are_not_shadowed():
    """Its own noun, and nothing earlier answers the same (path, method) — the
    #2984 trap, where a second declaration on a taken path is silently dead."""
    from routers import skills
    table = {}
    for r in skills.router.routes:
        for m in getattr(r, "methods", ()) or ():
            assert (r.path, m) not in table, f"duplicate {m} {r.path}"
            table[(r.path, m)] = r.endpoint.__name__
    assert table[("/api/skills/managers", "GET")] == "list_skill_managers"
    assert table[("/api/agents/{agent_name}/skill-manager", "PUT")] == "set_skill_manager"


# ---- 4. a refusal changes nothing (real app, real dependency chain) ----------

def test_a_refused_agent_key_changes_nothing_on_any_of_the_four_routes(grants, audit, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import skills

    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a refused call reached a write"))
    for name in ("assign_skill", "set_agent_skills", "unassign_skill", "get_agent_skill_names",
                 "assign_skill_set", "unassign_skill_set", "replace_skill_sets", "set_skill_individual"):
        monkeypatch.setattr(skills.db, name, boom)
    monkeypatch.setattr(skills.skill_service, "deliver_assigned", AsyncMock(side_effect=AssertionError("delivered")))
    monkeypatch.setattr(skills.skill_service, "inject_skills", AsyncMock(side_effect=AssertionError("injected")))
    monkeypatch.setattr(skills.skill_service, "get_skill", lambda n: {"name": n})

    app = FastAPI()
    app.include_router(skills.router)
    app.dependency_overrides[skills.get_current_user] = lambda: _principal("agent", SIBLING)
    client = TestClient(app)

    for method, path, body in [
        ("PUT", f"/api/agents/{HOLDER}/skills", {"skills": ["research"]}),
        ("POST", f"/api/agents/{HOLDER}/skills/inject", None),
        ("POST", f"/api/agents/{HOLDER}/skills/research", None),
        ("DELETE", f"/api/agents/{HOLDER}/skills/research", None),
        ("PUT", f"/api/agents/{SIBLING}/skills", {"skills": ["research"]}),   # itself
        ("POST", f"/api/agents/{HOLDER}/skill-sets/dev-backlog", None),       # ent#530
        ("DELETE", f"/api/agents/{HOLDER}/skill-sets/dev-backlog", None),
        ("PUT", f"/api/agents/{HOLDER}/skills", {"skills": [], "sets": ["dev-backlog"]}),
    ]:
        r = client.request(method, path, json=body)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "skill_management_not_permitted"


# ---- 5. the file-route bypass --------------------------------------------------

@pytest.mark.parametrize("path,ancestors,expected", [
    (".claude/skills/x/SKILL.md", False, True),
    ("/home/developer/.claude/skills", False, True),
    (".claude/skills/x/scripts/run.sh", False, True),
    ("docs/../.claude/skills/x/SKILL.md", False, True),   # normalised before matching
    (".claude/skillsX/SKILL.md", False, False),           # a prefix, not the dir
    (".claude/agents/x.md", False, False),
    ("notes.md", False, False),
    (".claude", False, False),                           # a write INTO .claude is not a skill write…
    (".claude", True, True),                             # …but DELETING it removes every skill
    ("/home/developer", True, True),
    ("/", True, True),
    ("notes", True, False),
])
def test_what_counts_as_touching_the_skills_dir(path, ancestors, expected):
    from services.agent_service.files import _touches_skills_dir
    assert _touches_skills_dir(path, include_ancestors=ancestors) is expected


@pytest.fixture
def files_mod(monkeypatch, grants, audit):
    from services.agent_service import files
    monkeypatch.setattr(files.db, "can_user_access_agent", lambda u, a: True)
    # Past the gate, the next thing each logic function does is look up the
    # container; "not found" is the observable proof the gate let it through.
    monkeypatch.setattr(files, "get_agent_container", lambda name: None)
    return files


@pytest.mark.parametrize("fn,args", [
    ("update_agent_file_logic", (".claude/skills/x/SKILL.md", "---\nname: x\n---")),
    ("create_agent_folder_logic", (".claude/skills/x",)),
    ("delete_agent_file_logic", (".claude",)),
])
def test_a_non_holder_cannot_reach_the_skills_dir_through_the_file_routes(files_mod, fn, args):
    call = getattr(files_mod, fn)
    path, *rest = args
    with pytest.raises(HTTPException) as exc:
        if fn == "update_agent_file_logic":
            asyncio.run(call(SIBLING, path, rest[0], _principal("agent", SIBLING), _request()))
        else:
            asyncio.run(call(SIBLING, path, _principal("agent", SIBLING), _request()))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "skill_management_not_permitted"


@pytest.mark.parametrize("principal", [_principal(None), _principal("agent", HOLDER)])
def test_humans_and_holders_still_edit_skills_through_the_files_tab(files_mod, principal):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(files_mod.update_agent_file_logic(
            SIBLING, ".claude/skills/x/SKILL.md", "body", principal, _request()))
    assert exc.value.status_code == 404   # reached the container lookup: past the gate


def test_ordinary_files_are_untouched_by_the_skill_gate(files_mod):
    """A non-holder agent editing notes.md behaves exactly as before this issue."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(files_mod.update_agent_file_logic(
            SIBLING, "notes.md", "body", _principal("agent", SIBLING), _request()))
    assert exc.value.status_code == 404


# ---- 6. grants over a real SQLite file ----------------------------------------

@pytest.fixture
def real_grants(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, insert
    import db.tables as tables
    import db.capability_grants as mod

    engine = create_engine(f"sqlite:///{tmp_path / 'grants.db'}")
    tables.agent_ownership.create(engine)
    tables.agent_capability_grants.create(engine)
    tables.agent_permissions.create(engine)
    with engine.begin() as conn:
        for name in (HOLDER, SIBLING):
            conn.execute(insert(tables.agent_ownership).values(
                agent_name=name, owner_id=1, created_at="2026-09-23T00:00:00Z"))
    monkeypatch.setattr(mod, "get_engine", lambda: engine)
    return SimpleNamespace(ops=mod.CapabilityGrantOperations(), engine=engine, tables=tables)


def test_grant_check_list_revoke(real_grants):
    ops = real_grants.ops
    assert ops.agent_has_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is False
    assert ops.grant_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE, "admin") is True
    assert ops.agent_has_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is True
    assert ops.agent_has_capability(SIBLING, CAPABILITY_SKILLS_MANAGE) is False
    assert [h["agent_name"] for h in ops.list_capability_holders(CAPABILITY_SKILLS_MANAGE)] == [HOLDER]
    assert ops.revoke_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is True
    assert ops.agent_has_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is False
    assert ops.revoke_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is False


def test_a_repeat_grant_keeps_the_original_who_and_when(real_grants):
    ops = real_grants.ops
    ops.grant_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE, "first-admin")
    first = ops.list_capability_holders(CAPABILITY_SKILLS_MANAGE)[0]
    assert ops.grant_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE, "second-admin") is False
    assert ops.list_capability_holders(CAPABILITY_SKILLS_MANAGE)[0] == first


def test_a_soft_deleted_holder_holds_nothing_and_recovery_restores_it(real_grants):
    from sqlalchemy import update
    t = real_grants.tables
    real_grants.ops.grant_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE, "admin")
    with real_grants.engine.begin() as conn:
        conn.execute(update(t.agent_ownership).where(t.agent_ownership.c.agent_name == HOLDER)
                     .values(deleted_at="2026-09-23T01:00:00Z"))
    assert real_grants.ops.agent_has_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is False
    assert real_grants.ops.list_capability_holders(CAPABILITY_SKILLS_MANAGE) == []
    with real_grants.engine.begin() as conn:
        conn.execute(update(t.agent_ownership).where(t.agent_ownership.c.agent_name == HOLDER)
                     .values(deleted_at=None))
    assert real_grants.ops.agent_has_capability(HOLDER, CAPABILITY_SKILLS_MANAGE) is True


def test_a_grant_row_for_an_agent_with_no_ownership_row_holds_nothing(real_grants):
    """A holder key whose agent row is gone must fail closed."""
    from sqlalchemy import insert
    with real_grants.engine.begin() as conn:
        conn.execute(insert(real_grants.tables.agent_capability_grants).values(
            agent_name="deleted-agent", capability=CAPABILITY_SKILLS_MANAGE,
            granted_by="admin", granted_at="2026-09-23T00:00:00Z"))
    assert real_grants.ops.agent_has_capability("deleted-agent", CAPABILITY_SKILLS_MANAGE) is False


def test_an_unknown_capability_is_refused_at_the_sink(real_grants):
    with pytest.raises(ValueError):
        real_grants.ops.grant_agent_capability(HOLDER, "skills.mange", "admin")
    assert real_grants.ops.agent_has_capability(HOLDER, "skills.mange") is False


def test_granting_writes_no_call_permission(real_grants):
    """AC4, the other direction: the grant must not imply calling."""
    from sqlalchemy import select, func
    real_grants.ops.grant_agent_capability(HOLDER, CAPABILITY_SKILLS_MANAGE, "admin")
    with real_grants.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(real_grants.tables.agent_permissions)).scalar() == 0


# ---- the grant service ---------------------------------------------------------

@pytest.fixture
def svc(monkeypatch):
    from services import capability_grant_service as mod
    state = {"granted": [], "revoked": []}
    owners = {HOLDER: {"is_system": False}, "trinity-system": {"is_system": True},
              "a-ghost": {"is_system": False}}
    monkeypatch.setattr(mod.db, "get_agent_owner", lambda n: owners.get(n))
    monkeypatch.setattr(mod.db, "get_agent_ephemeral_info",
                        lambda n: {"is_ephemeral": n == "a-ghost"})
    monkeypatch.setattr(mod.db, "grant_agent_capability",
                        lambda n, c, by: state["granted"].append((n, by)) or True)
    monkeypatch.setattr(mod.db, "revoke_agent_capability",
                        lambda n, c: state["revoked"].append(n) or True)
    return SimpleNamespace(mod=mod, state=state)


@pytest.mark.parametrize("target,status,code", [
    ("no-such-agent", 404, "agent_not_found"),      # nonexistent and soft-deleted look alike
    ("trinity-system", 422, "system_agent_not_grantable"),
    ("a-ghost", 422, "ephemeral_agent_not_grantable"),
])
def test_who_may_not_receive_the_grant(svc, target, status, code):
    with pytest.raises(svc.mod.CapabilityGrantRefused) as exc:
        svc.mod.set_grant(target, CAPABILITY_SKILLS_MANAGE, True, "admin")
    assert (exc.value.status_code, exc.value.code) == (status, code)
    assert svc.state["granted"] == []


def test_revoke_needs_no_target_validation(svc):
    """Revoking from a since-deleted agent must still work, or its row is an orphan."""
    svc.mod.set_grant("no-such-agent", CAPABILITY_SKILLS_MANAGE, False, "admin")
    assert svc.state["revoked"] == ["no-such-agent"]


# ---- the grant routes: an admin at a screen, never a key -----------------------

def test_the_grant_route_refuses_an_admins_own_mcp_key(monkeypatch, audit):
    from routers import skills
    with pytest.raises(HTTPException) as exc:
        asyncio.run(skills.set_skill_manager(
            HOLDER, skills.SkillManagerGrantRequest(granted=True), _request(),
            admin_user=_principal("user", role="admin")))
    assert exc.value.status_code == 403
    audit.assert_not_awaited()


def test_the_grant_route_grants_and_audits_only_real_changes(monkeypatch, audit):
    from routers import skills
    results = iter([{"agent_name": HOLDER, "capability": CAPABILITY_SKILLS_MANAGE, "granted": True, "changed": True},
                    {"agent_name": HOLDER, "capability": CAPABILITY_SKILLS_MANAGE, "granted": True, "changed": False}])
    monkeypatch.setattr(skills.capability_grant_service, "set_grant", lambda *a: next(results))
    admin = _principal(None, role="admin")
    first = asyncio.run(skills.set_skill_manager(HOLDER, skills.SkillManagerGrantRequest(granted=True),
                                                 _request(), admin_user=admin))
    again = asyncio.run(skills.set_skill_manager(HOLDER, skills.SkillManagerGrantRequest(granted=True),
                                                 _request(), admin_user=admin))
    assert first.changed is True and again.changed is False
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_action"] == "skill_manager_grant"


# ---- 7. attribution over a real SQLite file ------------------------------------

@pytest.fixture
def real_skills(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import db.tables as tables
    import db.skills as mod
    engine = create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    tables.agent_skills.create(engine)
    monkeypatch.setattr(mod, "get_engine", lambda: engine)
    return mod.SkillsOperations()


def test_the_agent_that_made_the_change_is_recorded(real_skills):
    real_skills.assign_skill(SIBLING, "research", "owner", assigned_by_agent=HOLDER)
    row = real_skills.get_agent_skills(SIBLING)[0]
    assert (row.assigned_by, row.assigned_by_agent) == ("owner", HOLDER)
    real_skills.assign_skill(SIBLING, "writing", "owner")          # a human, in the UI
    assert {s.skill_name: s.assigned_by_agent for s in real_skills.get_agent_skills(SIBLING)} == {
        "research": HOLDER, "writing": None}


def test_a_replace_does_not_restamp_the_names_it_keeps(real_skills):
    """delete-all + reinsert would make a human's skill read as the orchestrator's
    after its next replace. Only the names the replace ADDS belong to it."""
    real_skills.set_agent_skills(SIBLING, ["research"], "alice")                 # a human
    before = real_skills.get_agent_skills(SIBLING)[0]
    real_skills.set_agent_skills(SIBLING, ["research", "writing"], "owner", assigned_by_agent=HOLDER)
    rows = {s.skill_name: s for s in real_skills.get_agent_skills(SIBLING)}
    kept, added = rows["research"], rows["writing"]
    assert (kept.assigned_by, kept.assigned_by_agent, kept.assigned_at) == ("alice", None, before.assigned_at)
    assert (added.assigned_by, added.assigned_by_agent) == ("owner", HOLDER)


# ---- both tracks, the registry -------------------------------------------------

def test_the_sqlite_migration_builds_the_table_and_the_column(tmp_path):
    """Executed, not read: run the real migration against a pre-#596 shape."""
    import sqlite3
    from db.migrations import _migrate_agent_capability_grants, MIGRATIONS
    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE agent_skills (id INTEGER PRIMARY KEY, agent_name TEXT, skill_name TEXT, "
                "assigned_by TEXT, assigned_at TEXT, source_id TEXT, delivery_status TEXT)")
    _migrate_agent_capability_grants(cur, conn)
    _migrate_agent_capability_grants(cur, conn)           # idempotent
    cols = {r[1] for r in cur.execute("PRAGMA table_info(agent_skills)")}
    assert "assigned_by_agent" in cols
    assert {r[1] for r in cur.execute("PRAGMA table_info(agent_capability_grants)")} == {
        "agent_name", "capability", "granted_by", "granted_at"}
    assert ("agent_capability_grants", _migrate_agent_capability_grants) in MIGRATIONS


def test_the_alembic_revision_extends_the_single_head():
    import importlib.util, pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src/backend/migrations/versions"
    spec = importlib.util.spec_from_file_location("rev", root / "0072_agent_capability_grants.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == ("0072_agent_capability_grants", "0071_seat_decisions")


def test_the_grant_follows_the_agent_through_rename_and_delete():
    from db.agent_cleanup import AGENT_REFS, Policy
    refs = {(r.table, r.column): r.policy for r in AGENT_REFS}
    assert refs[("agent_capability_grants", "agent_name")] is Policy.CASCADE
    # audit-only initiator provenance: deliberately NOT re-keyed (source_agent_name precedent)
    assert ("agent_skills", "assigned_by_agent") not in refs
