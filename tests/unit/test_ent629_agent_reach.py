"""trinity-enterprise#629 — an agent key reaches only what it is allowed to reach,
on the raw backend routes too.

An agent-scoped MCP key resolves to its OWNER carrying the owner's role
(Invariant #8), so every owner/access predicate answered "yes" for any sibling
the owner holds — on an admin-owned install, for every agent. The
`agent_permissions` edge the MCP layer checks (P-02) did not exist one hop down:
with its injected key and `TRINITY_BACKEND_URL`, agent A could chat with, task,
fan out to, loop, read the history and logs of, or replace the git token of any
sibling B.

The fix narrows the shared gate (`dependencies.agent_may_reach`), not the
endpoints:

* USE (the access tier): itself, an agent it holds an edge to, an agent it spawned;
* MANAGE (the owner tier): itself, an agent it spawned — an edge is never "manage";
* every other principal is unchanged.

Driven against a real (temp) schema via ``db_harness`` — the access predicates
and the permission edges are the real ones; only the spawn-provenance lookup is
stubbed. Part D is a static guard: an inline access check that forgets the
narrowing fails the build.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
while str(_BACKEND) in sys.path:
    sys.path.remove(str(_BACKEND))
sys.path.insert(0, str(_BACKEND))
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

import dependencies  # noqa: E402
from models import User  # noqa: E402

OWNER = "owner"
A, B, C, SPAWN = "a-alpha", "a-beta", "a-gamma", "a-child"


def _agent_key(agent: str = A, role: str = "admin") -> User:
    """Agent A's key: resolves to its owner, carrying the owner's role."""
    return User(id=1, username=OWNER, role=role, mcp_scope="agent", agent_name=agent)


def _human(role: str = "admin") -> User:
    return User(id=1, username=OWNER, role=role)


@pytest.fixture
def fleet(db_backend, monkeypatch):
    """One owner (admin — the worst case: every predicate short-circuits True),
    four agents. A holds an edge to B only; A spawned SPAWN."""
    from database import db

    seed_user(1, OWNER, role="admin")
    for name in (A, B, C, SPAWN):
        seed_agent(name, owner_id=1)
    db.add_agent_permission(A, B, created_by=OWNER)

    def ephemeral_info(target):
        return {"spawned_by_agent": A, "spawned_by_key_id": "key-a"} if target == SPAWN else None

    class _Key:
        id = "key-a"

    monkeypatch.setattr(dependencies.db, "get_agent_ephemeral_info", ephemeral_info)
    monkeypatch.setattr(dependencies.db, "get_agent_mcp_api_key",
                        lambda agent: _Key() if agent == A else None)
    return db


def _status(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return 200
    except HTTPException as e:
        return e.status_code


# ---------------------------------------------------------------------------
# Part A — the reach rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target,use,manage", [
    (A, True, True),          # itself
    (B, True, False),         # an edge is "may call", never "may manage"
    (C, False, False),        # a sibling with no edge
    (SPAWN, True, True),      # an agent it spawned, with the key it holds now
])
def test_the_reach_of_an_agent_key(fleet, target, use, manage):
    key = _agent_key()
    assert dependencies.agent_may_reach(key, target) is use
    assert dependencies.agent_may_reach(key, target, manage=True) is manage


def test_every_other_principal_is_unchanged(fleet):
    for who in (_human(), _human("user"),
                User(id=1, username=OWNER, role="admin", mcp_scope="user"),
                User(id=1, username=OWNER, role="admin", mcp_scope="system")):
        assert dependencies.agent_may_reach(who, C) is True
        assert dependencies.agent_may_reach(who, C, manage=True) is True


def test_a_spawn_record_under_a_replaced_key_is_not_reach(fleet, monkeypatch):
    class _NewKey:
        id = "key-rotated"

    monkeypatch.setattr(dependencies.db, "get_agent_mcp_api_key", lambda agent: _NewKey())
    assert dependencies.agent_may_reach(_agent_key(), SPAWN, manage=True) is False


# ---------------------------------------------------------------------------
# Part B — the shared gates
# ---------------------------------------------------------------------------

def test_access_tier_dependencies_refuse_a_sibling_without_an_edge(fleet):
    key = _agent_key()
    for dep, kw in ((dependencies.get_authorized_agent, "name"),
                    (dependencies.get_authorized_agent_by_name, "agent_name")):
        assert _status(dep, **{kw: C, "current_user": key}) == 404, dep.__name__
        assert _status(dep, **{kw: B, "current_user": key}) == 200, dep.__name__
        assert _status(dep, **{kw: A, "current_user": key}) == 200, dep.__name__
        # Outside reach reads exactly like "no such agent" (#186).
        try:
            dep(**{kw: C, "current_user": key})
        except HTTPException as e:
            assert e.detail == "Agent not found"


def test_owner_tier_dependencies_refuse_even_an_edge(fleet):
    key = _agent_key()
    for dep, kw in ((dependencies.get_owned_agent, "name"),
                    (dependencies.get_owned_agent_by_name, "agent_name")):
        assert _status(dep, **{kw: B, "current_user": key}) == 404, dep.__name__
        assert _status(dep, **{kw: C, "current_user": key}) == 404, dep.__name__
        assert _status(dep, **{kw: A, "current_user": key}) == 200, dep.__name__
        assert _status(dep, **{kw: SPAWN, "current_user": key}) == 200, dep.__name__


def test_imperative_helpers_narrow_the_same_way(fleet):
    key = _agent_key()
    assert _status(dependencies.assert_agent_access, key, C) == 403
    assert _status(dependencies.assert_agent_access, key, B) == 200
    assert _status(dependencies.assert_agent_owner, key, B) == 403
    assert _status(dependencies.assert_agent_owner, key, A) == 200


def test_a_human_owner_still_reaches_everything(fleet):
    for who in (_human(), _human("user")):
        assert _status(dependencies.get_authorized_agent, name=C, current_user=who) == 200
        assert _status(dependencies.get_owned_agent_by_name, agent_name=C, current_user=who) == 200


def test_the_skills_capability_keeps_its_owner_wide_reach(fleet, monkeypatch):
    """ent#596: `skills.manage` IS the grant to manage the owner's other agents —
    its dependency keeps the plain owner fence, not the narrowed one."""
    monkeypatch.setattr(dependencies.db, "agent_has_capability",
                        lambda agent, cap: agent == A and cap == "skills.manage")

    class _Req:
        method = "PUT"
        scope = {"path": "/x"}
        client = None
        state = type("S", (), {})()

    got = asyncio.run(dependencies.get_skill_managed_agent_by_name(
        request=_Req(), agent_name=C, current_user=_agent_key()))
    assert got == C


def test_a_skills_holder_reaches_a_siblings_skills_dir_and_nothing_else(fleet, monkeypatch):
    """files.py: `skills.manage` (ent#596) is the grant to change the owner's
    other agents' skills, so a holder reaches a sibling's skills path with no
    edge — and only that path."""
    from services.agent_service import files

    monkeypatch.setattr(dependencies.db, "agent_has_capability",
                        lambda agent, cap: agent == A and cap == "skills.manage")
    key = _agent_key()
    assert files._may_reach_for_path(key, C, ".claude/skills/x/SKILL.md") is True
    assert files._may_reach_for_path(key, C, "notes.md") is False
    monkeypatch.setattr(dependencies.db, "agent_has_capability", lambda agent, cap: False)
    assert files._may_reach_for_path(key, C, ".claude/skills/x/SKILL.md") is False


def test_a2a_inbound_is_narrowed_too(fleet, monkeypatch):
    import routers.a2a as a2a

    monkeypatch.setattr(a2a.db, "get_a2a_exposed", lambda name: True)
    monkeypatch.setattr(a2a.a2a_gate, "check_inbound_allowed", lambda name, who: True)
    assert _status(a2a._authorize_inbound, _agent_key(), C) == 404
    assert _status(a2a._authorize_inbound, _agent_key(), B) == 200


def test_the_operator_queue_narrows_an_agent_key_on_an_admin_owned_install(fleet):
    """The queue had its own predicate: admin role → no filter at all. An agent
    key carries its owner's role, so it read (and could cancel) every agent's
    asks. It now gets its reach, like every other route."""
    import routers.operator_queue as oq

    assert oq._accessible_set(_agent_key()) == {A, B, SPAWN}
    assert _status(oq._assert_agent_accessible, C, oq._accessible_set(_agent_key())) == 403
    assert oq._accessible_set(_human()) is None          # an admin person: unchanged


# ---------------------------------------------------------------------------
# Part C — the dispatch routes resolve their target through the narrowed gate
# ---------------------------------------------------------------------------

def _path_agent_dependency(router, method: str, path: str):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in route.methods:
            for dep in route.dependant.dependencies:
                # By qualified name: the suite may reload `dependencies`, so
                # identity with this module's import proves nothing.
                if (getattr(dep.call, "__module__", None) == "dependencies"
                        and dep.call.__name__ in ("get_authorized_agent",
                                                  "get_authorized_agent_by_name")):
                    return dep.call
            return None
    raise AssertionError(f"{method} {path} not registered")


@pytest.mark.parametrize("module,router_attr,path", [
    ("routers.chat", "router", "/api/agents/{name}/chat"),
    ("routers.chat", "router", "/api/agents/{name}/task"),
    ("routers.fan_out", "router", "/api/agents/{name}/fan-out"),
    ("routers.loops", "agent_router", "/api/agents/{name}/loops"),
])
def test_dispatch_routes_resolve_the_target_through_the_access_gate(module, router_attr, path):
    import importlib

    router = getattr(importlib.import_module(module), router_attr)
    assert _path_agent_dependency(router, "POST", path) is not None, (
        f"POST {path} must resolve its agent through get_authorized_agent — "
        f"that is where an agent key's reach is narrowed (ent#629)")


# ---------------------------------------------------------------------------
# Part D — static guard: every inline access check also narrows
# ---------------------------------------------------------------------------

#: A function calling the raw `db.can_user_access_agent` / `can_user_share_agent`
#: with a principal must also call `agent_may_reach`, or be listed here with
#: the reason its principal is never an agent key, or its reach is decided
#: otherwise.
_EXEMPT = {
    ("dependencies.py", "can_manage_agent_skills"):
        "the skills.manage capability is the explicit grant to the owner's agents (ent#596)",
    ("routers/agent_files.py", "_can_view"):
        "ent#727: the metric grant is the agent's explicit reach to the serving agent",
    ("services/chat_execution_service.py", "_inherited_channel_context"):
        "an agent principal is self-scoped in the branch above (ent#265)",
    ("services/agent_service/crud.py", "_fork_destination_in_use_message"):
        "takes a username, not a principal; names a repo binding in a create conflict",
    ("services/agent_service/terminal.py", "*"):
        "the terminal websocket authenticates a person, never an agent key",
    ("client_portal/service.py", "*"):
        "portal principals are people; agent keys never reach the client portal",
}
_PREDICATES = {"can_user_access_agent", "can_user_share_agent"}
#: `_may_reach_for_path` is files.py's reach plus the ent#596 skills-holder case.
_REACH_HELPERS = {"agent_may_reach", "_may_reach_for_path"}


def _offenders():
    out = []
    files = [_BACKEND / "dependencies.py"]
    for sub in ("routers", "services", "client_portal", "adapters"):
        files += sorted((_BACKEND / sub).rglob("*.py"))
    for path in files:
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/"):
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            names = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in calls}
            # Only the innermost function that makes the call is judged.
            inner = {id(n) for sub in ast.walk(fn) if sub is not fn
                     and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                     for n in ast.walk(sub)}
            own = [c for c in calls if id(c) not in inner
                   and getattr(c.func, "attr", None) in _PREDICATES]
            if not own or names & _REACH_HELPERS:
                continue
            if (rel, fn.name) in _EXEMPT or (rel, "*") in _EXEMPT:
                continue
            out.append(f"{rel}::{fn.name}")
    return out


def test_every_inline_access_check_narrows_an_agent_key():
    offenders = _offenders()
    assert not offenders, (
        "These call db.can_user_access_agent / can_user_share_agent without "
        "dependencies.agent_may_reach, so an agent key would reach any sibling its "
        "owner holds (ent#629). AND the reach in, or add the function to _EXEMPT "
        f"with the reason: {offenders}")


def test_every_exemption_still_exists():
    """A stale exemption would quietly re-open the door for a new function of
    the same name — so each listed function must still call a predicate."""
    for (rel, fn_name), _ in _EXEMPT.items():
        src = (_BACKEND / rel).read_text()
        if fn_name == "*":
            assert any(p in src for p in _PREDICATES), rel
            continue
        tree = ast.parse(src)
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        assert fns, f"{rel}::{fn_name} no longer exists — drop it from _EXEMPT"
