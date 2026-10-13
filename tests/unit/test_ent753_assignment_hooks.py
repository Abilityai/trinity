"""
The per-agent skill gate map — the library-assignment paths
(trinity-enterprise#753, AC 5 + AC 6, decision 1, the plan-gate rulings).

Two halves.

**The guard (static).** Every function in ``src/backend`` that writes
``agent_skills`` rows — directly through the facade or through
``skill_set_service``, called OR passed by reference (``asyncio.to_thread``
takes the function, never calls it) — must also reach the gate reconcile. The
writer set is every ``agent_skills`` writer, not the call names this change
happened to touch, and the discovered sites are pinned so a new one is a
visible edit. Mutation-checked: a planted writer without the hook, and a hook
named only in a string, both fail it.

**The behaviour (real routes, real database).** Driven through
``routers/skills.py`` with a TestClient:

* assigning a library skill whose metadata says ``approval: recommended`` gates
  it, and the response says so (``gates.gate_defaults``);
* a PERSON's unassign removes its explicit gate; an AGENT's unassign (a
  ``skills.manage`` holder — itself or a sibling) keeps it and says so
  (``gates_kept``) — so unassign-then-reassign cannot launder a gate away;
* a replace that drops a gated name follows the same rule, and a name whose
  library row is in ``conflict`` (the agent's own skill of that name runs)
  keeps its gate either way;
* the start path and the library sweep reconcile defaults too (backfill).

Stubbed: package delivery/removal, the container state, the library listing,
the audit sink, the change broadcast.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

# Every facade method that INSERTs into or DELETEs from `agent_skills`
# (db/skills.py, db/skill_sets.py) — `delete_agent_skills` excepted: an agent
# delete removes its gate rows through the AGENT_REFS cascade.
DB_WRITERS = frozenset({
    "assign_skill", "unassign_skill", "set_agent_skills",
    "assign_skill_set", "unassign_skill_set", "replace_skill_sets", "reconcile_skill_sets",
})
SET_SERVICE_WRITERS = frozenset({"assign", "unassign", "replace", "reconcile_agent"})
HOOKS = frozenset({"reconcile_library_gates", "_sync_gates"})

# Modules that ARE the writers (or delegate to them) — they cannot hook themselves.
EXEMPT_FILES = frozenset({"database.py", "services/skill_set_service.py"})
EXEMPT_DIRS = ("db/", "enterprise/", "tests/", "migrations/")

# The writing sites today. A new one must reach the reconcile AND be added here.
EXPECTED_SITES = frozenset({
    "routers/skills.py::update_agent_skills",
    "routers/skills.py::inject_skills",
    "routers/skills.py::assign_skill",
    "routers/skills.py::unassign_skill",
    "routers/skills.py::assign_skill_set",
    "routers/skills.py::unassign_skill_set",
    "services/agent_service/lifecycle.py::inject_assigned_skills",
    "services/skills_sync_service.py::SkillsLibrarySyncService._reinject_agent",
})


def _writer_refs(fn: ast.AST, set_aliases: set, fn_aliases: dict, db_aliases: set):
    """Every reference to an agent_skills writer inside `fn` — a call or a bare
    attribute load (passed to `to_thread`), on the facade under any name it is
    bound to (`db`, `core_db`, `database.db`), on the set service (any alias),
    or a from-imported set-service function."""
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            owner, attr = node.value, node.attr
            facade = ((isinstance(owner, ast.Name) and owner.id in db_aliases)
                      or (isinstance(owner, ast.Attribute) and owner.attr == "db"
                          and isinstance(owner.value, ast.Name) and owner.value.id == "database"))
            if facade and attr in DB_WRITERS:
                found.append(f"db.{attr}")
            elif isinstance(owner, ast.Name) and owner.id in set_aliases and attr in SET_SERVICE_WRITERS:
                found.append(f"skill_set_service.{attr}")
        elif isinstance(node, ast.Name) and node.id in fn_aliases:
            found.append(f"skill_set_service.{fn_aliases[node.id]}")
    return found


def _hook_refs(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in HOOKS:
            return True
        if isinstance(node, ast.Name) and node.id in HOOKS:
            return True
    return False


def _aliases(tree: ast.Module):
    """Names the set service and the DB facade are bound to in this module, and
    set-service functions imported by name (`from services.skill_set_service
    import x as y`)."""
    mods, fns, dbs = {"skill_set_service"}, {}, {"db", "_db"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "database":
                for a in node.names:
                    if a.name == "db":
                        dbs.add(a.asname or a.name)
            elif node.module == "services":
                for a in node.names:
                    if a.name == "skill_set_service":
                        mods.add(a.asname or a.name)
            elif node.module == "services.skill_set_service":
                for a in node.names:
                    if a.name in SET_SERVICE_WRITERS:
                        fns[a.asname or a.name] = a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "services.skill_set_service" and a.asname:
                    mods.add(a.asname)
    return mods, fns, dbs


def scan_source(relpath: str, source: str):
    """[(site, [writer refs], hooked?)] for every function in one module."""
    tree = ast.parse(source)
    mods, fns, dbs = _aliases(tree)
    out = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                refs = _writer_refs(child, mods, fns, dbs)
                if refs:
                    out.append((f"{relpath}::{prefix}{child.name}", refs, _hook_refs(child)))
    visit(tree)
    return out


def scan_backend():
    sites = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel in EXEMPT_FILES or rel.startswith(EXEMPT_DIRS) or "/__pycache__/" in rel:
            continue
        sites.extend(scan_source(rel, path.read_text()))
    return sites


def test_every_assignment_writer_reaches_the_gate_reconcile():
    sites = scan_backend()
    print("\n".join(f"{s}: {sorted(set(r))} hooked={h}" for s, r, h in sites))
    families = {ref.split(".")[0] + "." + ref.split(".")[1] for _, refs, _ in sites for ref in refs}
    for must in ("db.set_agent_skills", "db.assign_skill", "db.unassign_skill",
                 "skill_set_service.assign", "skill_set_service.unassign",
                 "skill_set_service.replace", "skill_set_service.reconcile_agent"):
        assert must in families, f"the guard no longer finds {must} — is it still looking?"
    unhooked = [s for s, _, hooked in sites if not hooked]
    assert unhooked == [], f"writes agent_skills without reconciling gates: {unhooked}"
    assert {s for s, _, _ in sites} == EXPECTED_SITES


def test_the_router_helper_itself_reaches_the_reconcile():
    tree = ast.parse((_BACKEND / "routers" / "skills.py").read_text())
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sync_gates")
    assert any(isinstance(n, ast.Attribute) and n.attr == "reconcile_library_gates"
               for n in ast.walk(helper))


@pytest.mark.parametrize("source,flagged", [
    pytest.param("async def f(a):\n    await asyncio.to_thread(skill_set_service.replace, a)\n",
                 True, id="by-reference-unhooked"),
    pytest.param("def f(a):\n    db.set_agent_skills(agent_name=a)\n", True, id="call-unhooked"),
    pytest.param("from services.skill_set_service import reconcile_agent as r\n"
                 "def f(a):\n    r(a)\n", True, id="aliased-import-unhooked"),
    pytest.param("from services import skill_set_service as sss\n"
                 "def f(a):\n    sss.unassign(a, 's', 'u')\n", True, id="aliased-module-unhooked"),
    pytest.param("def f(a):\n    db.unassign_skill(a, 's')\n    x = 'reconcile_library_gates'\n",
                 True, id="hook-named-only-in-a-string"),
    pytest.param("from database import db as core_db\n"
                 "def f(a):\n    core_db.assign_skill(a, 's', 'u')\n", True, id="aliased-facade-unhooked"),
    pytest.param("import database\n"
                 "def f(a):\n    database.db.set_agent_skills(agent_name=a)\n", True, id="chained-facade-unhooked"),
    pytest.param("async def f(a):\n    db.assign_skill(a, 's', 'u')\n"
                 "    await skill_gate_map_service.reconcile_library_gates(a, ctx=c)\n",
                 False, id="hooked"),
])
def test_the_guard_catches_what_it_should(source, flagged):
    [(site, refs, hooked)] = scan_source("x.py", source)
    assert refs and (not hooked) is flagged


# ---------------------------------------------------------------------------
# The behaviour
# ---------------------------------------------------------------------------

OWNER, ADMIN = "owner-753-hk", "admin-753-hk"
FIN, ORCH = "fin-753-hk", "orch-753-hk"


def _user(username, *, scope=None, agent=None, role="user"):
    from models import User
    return User(id=1, username=username, email=f"{username}@example.com", role=role,
                mcp_scope=scope, agent_name=agent, mcp_key_id="k" if scope else None)


@pytest.fixture
def skills_api(db_backend, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from database import db
    from db_models import UserCreate
    from db.capability_grants import CAPABILITY_SKILLS_MANAGE
    from routers import skills
    from services import docker_utils, assignment_provider, skill_set_service
    import importlib
    PAS = importlib.import_module("services.platform_audit_service")   # sys.modules, not the package attribute

    db.create_user(UserCreate(username=OWNER, role="user", email=f"{OWNER}@example.com"))
    db.create_user(UserCreate(username=ADMIN, role="admin", email=f"{ADMIN}@example.com"))
    db.register_agent_owner(FIN, OWNER)
    db.register_agent_owner(ORCH, OWNER)
    db.grant_agent_capability(ORCH, CAPABILITY_SKILLS_MANAGE, ADMIN)

    library = [{"name": "deploy", "approval": "recommended"},
               {"name": "pay-invoice", "approval": None},
               {"name": "notes", "approval": None}]

    async def _stopped(name):
        return "stopped"

    async def _audit(*a, **kw):
        return "evt"

    monkeypatch.setattr(docker_utils, "agent_container_state_async", _stopped)
    monkeypatch.setattr(PAS.platform_audit_service, "log", _audit)
    monkeypatch.setattr(assignment_provider, "get_provider", lambda: None)
    # The gate service reads the library through the LIVE module object; the
    # router may hold an older binding after the conftest's module resets.
    from services.skill_service import skill_service as live_skill_service
    for target in {id(skills.skill_service): skills.skill_service,
                   id(live_skill_service): live_skill_service}.values():
        monkeypatch.setattr(target, "list_skills", lambda: [dict(s) for s in library])
    monkeypatch.setattr(skills.skill_service, "get_skill",
                        lambda n: next((dict(s) for s in library if s["name"] == n), None))
    monkeypatch.setattr(skills.skill_service, "deliver_assigned",
                        AsyncMock(return_value={"status": "pending_start", "skills": {}}))
    removals = {"mode": "removed"}

    async def _remove(agent, names):
        if removals["mode"] == "busy":
            from services.skill_service import SkillInjectionBusy
            raise SkillInjectionBusy("injection in progress")
        return {"success": True, "skills_removed": len(names), "skills_failed": 0,
                "results": {n: {"success": True, "status": "removed"} for n in names}}

    monkeypatch.setattr(skills.skill_service, "remove_skills", _remove)
    monkeypatch.setattr(skills, "broadcast_skills_changed", AsyncMock())
    monkeypatch.setattr(skill_set_service, "via_sets_map", lambda *a, **k: {})
    monkeypatch.setattr(skill_set_service, "any_unresolved", lambda *a, **k: False)

    app = FastAPI()
    app.include_router(skills.router)
    principal = {"user": _user(OWNER)}
    app.dependency_overrides[skills.get_current_user] = lambda: principal["user"]
    client = TestClient(app)

    def gate(name, origin="set"):
        db.write_skill_gate(FIN, name, changes={}, origin=origin,
                            set_by=OWNER, set_by_agent=None)

    def gated():
        from services import skill_gate_service as sgs
        return set(sgs.list_skill_gates(FIN))

    return SimpleNamespace(db=db, client=client, principal=principal, gate=gate, gated=gated,
                           removals=removals, skills=skills)


def test_assigning_a_recommended_skill_gates_it_and_says_so(skills_api):
    r = skills_api.client.post(f"/api/agents/{FIN}/skills/deploy")
    assert r.status_code == 200, r.text
    assert r.json()["gates"]["gate_defaults"]["applied"] == ["deploy"]
    assert skills_api.gated() == {"deploy"}
    assert skills_api.client.post(f"/api/agents/{FIN}/skills/notes").json().get("gates") in (None, {})


def test_a_persons_unassign_removes_the_explicit_gate(skills_api):
    skills_api.client.post(f"/api/agents/{FIN}/skills/pay-invoice")
    skills_api.gate("pay-invoice")
    r = skills_api.client.delete(f"/api/agents/{FIN}/skills/pay-invoice")
    assert r.json()["gates"]["gates_removed"] == ["pay-invoice"]
    assert skills_api.gated() == set()


@pytest.mark.parametrize("target", [FIN, ORCH], ids=["sibling", "itself"])
def test_an_agents_unassign_keeps_the_explicit_gate_so_it_cannot_launder_it(skills_api, target):
    skills_api.client.post(f"/api/agents/{target}/skills/pay-invoice")
    skills_api.db.write_skill_gate(target, "pay-invoice", changes={},
                                   origin="set", set_by=OWNER, set_by_agent=None)
    skills_api.principal["user"] = _user(OWNER, scope="agent", agent=ORCH)
    r = skills_api.client.delete(f"/api/agents/{target}/skills/pay-invoice")
    assert r.status_code == 200, r.text
    assert r.json()["gates"]["gates_kept"] == ["pay-invoice"]
    skills_api.client.post(f"/api/agents/{target}/skills/pay-invoice")
    from services import skill_gate_service as sgs
    assert set(sgs.list_skill_gates(target)) == {"pay-invoice"}


def test_unassigning_a_recommended_skill_drops_its_default_for_anyone(skills_api):
    skills_api.client.post(f"/api/agents/{FIN}/skills/deploy")
    skills_api.principal["user"] = _user(OWNER, scope="agent", agent=ORCH)
    r = skills_api.client.delete(f"/api/agents/{FIN}/skills/deploy")
    assert r.json()["gates"]["gate_defaults"]["removed"] == ["deploy"]
    assert skills_api.gated() == set()
    skills_api.client.post(f"/api/agents/{FIN}/skills/deploy")          # re-adding restores it
    assert skills_api.gated() == {"deploy"}


def test_a_replace_follows_the_same_rules_and_spares_a_conflict_name(skills_api):
    skills_api.client.put(f"/api/agents/{FIN}/skills", json={"skills": ["pay-invoice", "notes", "deploy"]})
    assert skills_api.gated() == {"deploy"}
    skills_api.gate("pay-invoice")
    skills_api.gate("notes")
    skills_api.db.set_skill_delivery_status(FIN, ["notes"], [])   # the agent's own `notes` runs
    r = skills_api.client.put(f"/api/agents/{FIN}/skills", json={"skills": []})
    assert r.status_code == 200, r.text
    body = r.json()["gates"]
    assert body["gates_removed"] == ["pay-invoice"]
    assert body["gate_defaults"]["removed"] == ["deploy"]
    assert skills_api.gated() == {"notes"}


def test_a_replace_that_changes_nothing_touches_no_gate(skills_api):
    skills_api.client.put(f"/api/agents/{FIN}/skills", json={"skills": ["deploy", "notes"]})
    r = skills_api.client.put(f"/api/agents/{FIN}/skills", json={"skills": ["notes", "deploy"]})
    assert r.json().get("gates") in (None, {})


def test_unassigning_a_set_drops_its_members_explicit_gates_for_a_person(skills_api, monkeypatch):
    from services import skill_set_service

    def _unassign(agent, set_name, by):
        skills_api.db.unassign_skill(agent, "pay-invoice")
        return {"existed": True, "added": [], "removed": ["pay-invoice"]}

    monkeypatch.setattr(skill_set_service, "unassign", _unassign)
    skills_api.client.post(f"/api/agents/{FIN}/skills/pay-invoice")
    skills_api.gate("pay-invoice")
    r = skills_api.client.delete(f"/api/agents/{FIN}/skill-sets/finance")
    assert r.status_code == 200, r.text
    assert r.json()["gates"]["gates_removed"] == ["pay-invoice"]
    assert skills_api.gated() == set()


def test_the_start_path_backfills_defaults(skills_api, monkeypatch):
    from services.agent_service import lifecycle
    monkeypatch.setattr(lifecycle.skill_service, "inject_skills",
                        AsyncMock(return_value={"success": True, "results": {}}))
    prune = {"result": {"status": "skipped", "reason": "inventory_unavailable"}}
    monkeypatch.setattr(lifecycle.skill_service, "reconcile_agent_skills",
                        AsyncMock(side_effect=lambda *a, **k: prune["result"]))
    skills_api.db.assign_skill(FIN, "deploy", OWNER)              # held before this shipped
    skills_api.db.assign_skill(FIN, "notes", OWNER)
    assert skills_api.gated() == set()
    asyncio.run(lifecycle.inject_assigned_skills(FIN))
    assert skills_api.gated() == {"deploy"}
    # unassigned while its removal deferred: the default stays until a start's
    # prune has inventoried the agent — and goes then (with skills left, and
    # with none left: both branches of the start path)
    skills_api.db.unassign_skill(FIN, "deploy")
    asyncio.run(lifecycle.inject_assigned_skills(FIN))
    assert skills_api.gated() == {"deploy"}
    prune["result"] = {"status": "reconciled", "removed": 1, "results": {"deploy": {"status": "removed"}}}
    asyncio.run(lifecycle.inject_assigned_skills(FIN))
    assert skills_api.gated() == set()
    skills_api.db.assign_skill(FIN, "deploy", OWNER)
    asyncio.run(lifecycle.inject_assigned_skills(FIN))
    skills_api.db.unassign_skill(FIN, "deploy")
    skills_api.db.unassign_skill(FIN, "notes")
    prune["result"] = {"status": "clean", "removed": 0}
    asyncio.run(lifecycle.inject_assigned_skills(FIN))
    assert skills_api.gated() == set()


def test_the_library_sweep_backfills_defaults(skills_api, monkeypatch):
    from services import skills_sync_service as sss
    monkeypatch.setattr(sss.skill_service, "inject_skills",
                        AsyncMock(return_value={"success": True, "results": {}}))
    skills_api.db.assign_skill(FIN, "deploy", OWNER)
    asyncio.run(sss.SkillsLibrarySyncService._reinject_agent(FIN))
    assert skills_api.gated() == {"deploy"}


def test_a_default_is_in_place_before_the_package_is_delivered(skills_api, monkeypatch):
    seen = []

    async def _deliver(agent, names):
        seen.append(skills_api.gated())
        return {"status": "pending_start", "skills": {}}

    monkeypatch.setattr(skills_api.skills.skill_service, "deliver_assigned", _deliver)
    skills_api.client.post(f"/api/agents/{FIN}/skills/deploy")
    skills_api.client.put(f"/api/agents/{FIN}/skills", json={"skills": ["deploy", "notes"]})
    assert seen and all(s == {"deploy"} for s in seen)


def test_a_deferred_removal_keeps_every_gate_on_the_skill(skills_api):
    """The package may still be on the agent: neither its explicit gate nor its
    default goes before its files do."""
    skills_api.client.post(f"/api/agents/{FIN}/skills/deploy")       # default
    skills_api.client.post(f"/api/agents/{FIN}/skills/pay-invoice")
    skills_api.gate("pay-invoice")                                   # explicit
    skills_api.removals["mode"] = "busy"
    for name in ("deploy", "pay-invoice"):
        r = skills_api.client.delete(f"/api/agents/{FIN}/skills/{name}")
        assert r.json()["removal"]["status"] == "deferred"
    assert skills_api.gated() == {"deploy", "pay-invoice"}
    assert r.json()["gates"]["gates_kept"] == ["pay-invoice"]


@pytest.mark.parametrize("principal,dropped", [
    pytest.param(lambda: _user(OWNER, scope="user"), True, id="the-owners-user-key"),
    pytest.param(lambda: _user(ADMIN, scope="system", role="admin"), False, id="the-system-key"),
])
def test_who_counts_as_a_person_on_unassign(skills_api, principal, dropped):
    skills_api.client.post(f"/api/agents/{FIN}/skills/pay-invoice")
    skills_api.gate("pay-invoice")
    skills_api.principal["user"] = principal()
    r = skills_api.client.delete(f"/api/agents/{FIN}/skills/pay-invoice")
    assert r.status_code == 200, r.text
    assert (skills_api.gated() == set()) is dropped


def test_assigning_a_set_gates_its_recommended_member_before_delivery(skills_api, monkeypatch):
    from services import skill_set_service

    def _assign(agent, set_name, by, by_agent):
        skills_api.db.assign_skill(agent, "deploy", by)
        return {"created": True, "added": ["deploy"], "removed": [],
                "entry": {"schedules": []}}

    seen = []

    async def _deliver(agent, names):
        seen.append(skills_api.gated())
        return {"status": "pending_start", "skills": {}}

    async def _status(agent, probe=False):
        return []

    monkeypatch.setattr(skill_set_service, "assign", _assign)
    monkeypatch.setattr(skill_set_service, "agent_set_status", _status)
    monkeypatch.setattr(skills_api.skills.skill_service, "deliver_assigned", _deliver)
    r = skills_api.client.post(f"/api/agents/{FIN}/skill-sets/ops")
    assert r.status_code == 200, r.text
    assert r.json()["gates"]["gate_defaults"]["applied"] == ["deploy"]
    assert seen == [{"deploy"}]


def test_a_manual_sync_gates_before_injecting_and_drops_after_the_prune(skills_api, monkeypatch):
    from services import skill_set_service
    seen = []

    async def _inject(agent, force=False):
        seen.append(skills_api.gated())
        return {"success": True, "results": {}}

    monkeypatch.setattr(skill_set_service, "reconcile_agent", lambda *a, **k: ([], []))
    monkeypatch.setattr(skills_api.skills.skill_service, "inject_skills", _inject)
    monkeypatch.setattr(skills_api.skills.skill_service, "reconcile_agent_skills",
                        AsyncMock(return_value={"status": "clean", "removed": 0}))
    skills_api.db.assign_skill(FIN, "deploy", OWNER)
    skills_api.client.post(f"/api/agents/{FIN}/skills/inject")
    assert seen == [{"deploy"}]
    skills_api.db.unassign_skill(FIN, "deploy")
    r = skills_api.client.post(f"/api/agents/{FIN}/skills/inject")
    assert r.json()["gates"]["gate_defaults"]["removed"] == ["deploy"]
    assert skills_api.gated() == set()


def test_the_library_sweep_drops_a_default_only_after_its_prune(skills_api, monkeypatch):
    """A sync that dropped a set member re-injects and prunes; the member's
    default goes after the prune removed its package — not before."""
    from services import skills_sync_service as sss
    from services import skill_set_service
    seen = []

    async def _prune(agent, names):
        seen.append(skills_api.gated())
        return {"status": "reconciled", "removed": 1, "results": {"deploy": {"status": "removed"}}}

    monkeypatch.setattr(sss.skill_service, "inject_skills",
                        AsyncMock(return_value={"success": True, "results": {}}))
    monkeypatch.setattr(sss.skill_service, "reconcile_agent_skills", _prune)
    skills_api.db.assign_skill(FIN, "deploy", OWNER)
    skills_api.db.assign_skill(FIN, "notes", OWNER)
    asyncio.run(sss.SkillsLibrarySyncService._reinject_agent(FIN))
    assert skills_api.gated() == {"deploy"}

    def _drop_member(agent, lib=None):
        skills_api.db.unassign_skill(agent, "deploy")
        return [], ["deploy"]

    monkeypatch.setattr(skill_set_service, "reconcile_agent", _drop_member)
    asyncio.run(sss.SkillsLibrarySyncService._reinject_agent(FIN, sets_lib={}))
    assert seen == [{"deploy"}]                    # still gated while the prune ran
    assert skills_api.gated() == set()
