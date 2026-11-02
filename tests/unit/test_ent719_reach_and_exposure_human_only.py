"""trinity-enterprise#719 — who may reach an agent, and who outside the platform
sees it, is a person's decision.

The routes here decide exactly that: sharing the agent and approving an access
request, revoking a share, the channel access policy, the instructions outside
audiences get, the agent-to-agent permission edges, its shared folders, its
outbound file sharing, the operator's control of its Workspace clients (log
out, block, unblock), its public chat links, which open it to anonymous
visitors, and its canvas share links and pin (the operator surface: who outside
the platform may read a canvas, and which canvas a whole roster sees first).
Each is a GRANT (Invariant #8), so each takes a
human-only rule as a decorator dependency, which FastAPI runs before the
route's ownership lookup:

* INTERACTIVE (`Depends(require_interactive)`, a signed-in session only) for
  sharing the agent and approving an access request. The share row they write
  IS a Workspace sign-in by emailed code, and a minter is stricter than the
  principal class it mints (#2996, the #717 whitelist rule);
* PERSON (`Depends(require_person)`, a signed-in session or the person's own
  `user` key) for the rest.

What this file pins, through each route's real router over a real schema:

1. every machine principal gets the rule's own 403: the agent's own key, an
   agent inside its reach (it spawned the agent with the key it holds now, and
   holds an edge to it), the `system` key, `connector`, `ops`,
   `portal_delegate`, an unknown scope, a principal with no `mcp_scope` at all,
   and a loopback JWT carrying a vouched source agent. The `user` key is refused
   too where the rule is INTERACTIVE;
2. the refusal is the same for an agent that does not exist (#186) and for a
   schema-invalid body, the handler never runs, and no database method is
   called;
3. a real, signed loopback JWT with no source agent is refused by the
   `get_current_user` entry fence on every route;
4. a person still succeeds on every route, and the person's `user` key does on
   every PERSON route; blocking and unblocking a client still need an admin;
5. the uses next to these grants are unchanged: the agent's own key still
   shares a file from its publish dir once a person has turned file sharing on,
   and still writes and clears its own canvas;
6. a fresh install (one agent, nothing shared, granted, requested, linked or
   pinned) refuses
   every machine principal the same way and gives the person empty lists.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""
from __future__ import annotations

import shutil
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
import routers.agent_files as agent_files_router
import routers.canvas as canvas_router
import routers.public_links as public_links_router
import routers.sharing as sharing_router
from client_portal import router as portal_router
from db.engine import dispose_engines
from db_harness import bootstrap_schema, run, seed_agent, seed_user
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from models import User
from services.agent_service import file_sharing as file_sharing_logic
from services.agent_service import folders as folders_logic
from services.agent_service import permissions as permissions_logic

pytestmark = pytest.mark.unit

ROUTERS = (sharing_router, agent_files_router, portal_router, public_links_router, canvas_router)
HELD_MODULES = (*ROUTERS, permissions_logic, folders_logic, file_sharing_logic)

# The authentication leaf, taken from a module that mounts it, so the override
# and the patches below hit the objects the app really uses.
_DEPS = sharing_router.get_current_user.__globals__  # the `dependencies` namespace
HUMAN_ONLY_DETAIL = _DEPS["HUMAN_ONLY_DETAIL"]
EVENT_LOOPBACK_SCOPE = _DEPS["EVENT_LOOPBACK_SCOPE"]
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

ADMIN, OLIVIA = "admin", "olivia"
A = "agent-a"            # the agent every route addresses
OLIVIAS = "agent-o"      # owned by a signed-in creator, not an admin
PARENT = "agent-parent"  # spawned A with the key it holds now, and holds an edge to A
TARGET = "agent-t"       # a permission-edge target
MISSING = "agent-gone"   # no such agent
GUEST = "guest@example.com"    # A is already shared with this person
ASKER = "asker@example.com"    # has a pending access request on A

PERSON_RULE, INTERACTIVE_RULE = "person", "interactive"


# --------------------------------------------------------------------------- #
# Principals: the real model, every scope field set
# --------------------------------------------------------------------------- #

def _principal(**kw) -> User:
    base = {"id": 1, "username": ADMIN, "email": "admin@example.com", "role": "admin"}
    base.update(kw)
    return User(**base)


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all. Both rules must
    fail closed on it (a sentinel, never `getattr(..., None)`)."""

    id = 1
    username = ADMIN
    email = "admin@example.com"
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


MACHINES: dict[str, Callable[[], Any]] = {
    "agent_own_key": lambda: _principal(
        mcp_scope="agent", agent_name=A, mcp_key_id="key-a", mcp_key_name=A),
    "agent_in_reach": lambda: _principal(
        mcp_scope="agent", agent_name=PARENT, mcp_key_id="key-parent", mcp_key_name=PARENT),
    "system_key": lambda: _principal(
        mcp_scope="system", mcp_key_id="key-system", mcp_key_name="trinity-system"),
    "connector_key": lambda: _principal(
        mcp_scope="connector", connector_agent=A, mcp_key_id="key-conn", mcp_key_name="connector"),
    "ops_key": lambda: _principal(mcp_scope="ops", mcp_key_id="key-ops", mcp_key_name="ops"),
    "portal_delegate_key": lambda: _principal(
        mcp_scope="portal_delegate", portal_delegate=True, mcp_key_id="key-pd", mcp_key_name="issuer"),
    "unknown_scope": lambda: _principal(
        mcp_scope="a-scope-no-one-has-shipped", mcp_key_id="key-x", mcp_key_name="x"),
    "no_scope_attribute": lambda: _NoScopeAttribute(),
    "vouched_loopback": lambda: _principal(
        mcp_scope=None, is_event_loopback=True, vouched_source_agent=PARENT),
}


def USER_KEY() -> User:
    return _principal(mcp_scope="user", mcp_key_id="key-user", mcp_key_name="laptop")


def PERSON() -> User:
    return _principal()  # a signed-in session: no key scope


def _rule_refusal(rule: str, principal) -> tuple[int, Any]:
    """The (status, detail) the rule's own primitive gives this principal.

    Taken from the primitive rather than retyped, so the assertion is "the
    route answers exactly what its rule answers"."""
    gate = _DEPS["require_interactive" if rule == INTERACTIVE_RULE else "require_person"]
    with pytest.raises(HTTPException) as exc:
        gate(principal)
    return exc.value.status_code, exc.value.detail


# --------------------------------------------------------------------------- #
# The routes, as a person would call them
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Route:
    key: str                  # the census key
    method: str
    path: str                 # `{agent}` and the world's ids are filled in per call
    rule: str
    body: Any = None
    invalid_body: Any = None  # a schema-invalid body; None when the route takes none
    ok: int = 200
    check: Callable[[Any, Any], None] | None = None  # (world, response) after a person's call

    def __str__(self) -> str:
        return self.key


def _shares_of(agent: str) -> set[str]:
    from database import db
    return {s.shared_with_email for s in db.get_agent_shares(agent)}


S4A_SHARING_AND_FILES = [
    Route("routers/sharing.py::share_agent_endpoint", "POST", "/api/agents/{agent}/share",
          INTERACTIVE_RULE, body={"email": "new@example.com"}, invalid_body={},
          check=lambda w, r: _shares_of(A) >= {"new@example.com"}),
    Route("routers/sharing.py::decide_access_request_endpoint", "POST",
          "/api/agents/{agent}/access-requests/{request_id}/decide",
          INTERACTIVE_RULE, body={"approve": True}, invalid_body={},
          check=lambda w, r: _shares_of(A) >= {ASKER}),
    Route("routers/sharing.py::unshare_agent_endpoint", "DELETE",
          "/api/agents/{agent}/share/" + GUEST, PERSON_RULE,
          check=lambda w, r: GUEST not in _shares_of(A)),
    Route("routers/sharing.py::update_access_policy_endpoint", "PUT",
          "/api/agents/{agent}/access-policy", PERSON_RULE,
          body={"require_email": True, "open_access": False, "group_auth_mode": "none"},
          invalid_body={},
          check=lambda w, r: r.json()["require_email"] is True),
    Route("routers/sharing.py::set_public_channel_prompt_endpoint", "PUT",
          "/api/agents/{agent}/public-prompt", PERSON_RULE,
          body={"public_channel_system_prompt": "Answer briefly."},
          invalid_body={"public_channel_system_prompt": ["not", "a", "string"]},
          check=lambda w, r: r.json()["public_channel_system_prompt"] == "Answer briefly."),
    Route("routers/agent_files.py::set_agent_permissions", "PUT",
          "/api/agents/{agent}/permissions", PERSON_RULE,
          body={"permitted_agents": [TARGET]}, invalid_body=["not", "an", "object"],
          check=lambda w, r: r.json()["permitted_count"] == 1),
    Route("routers/agent_files.py::add_agent_permission", "POST",
          "/api/agents/{agent}/permissions/" + PARENT, PERSON_RULE,
          check=lambda w, r: r.json()["status"] == "added"),
    Route("routers/agent_files.py::remove_agent_permission", "DELETE",
          "/api/agents/{agent}/permissions/" + TARGET, PERSON_RULE,
          check=lambda w, r: r.json()["status"] == "removed"),
    Route("routers/agent_files.py::update_agent_folders", "PUT",
          "/api/agents/{agent}/folders", PERSON_RULE,
          body={"expose_enabled": True}, invalid_body=["not", "an", "object"],
          check=lambda w, r: r.json()["expose_enabled"] is True),
    Route("routers/agent_files.py::set_agent_file_sharing", "PUT",
          "/api/agents/{agent}/file-sharing", PERSON_RULE,
          body={"enabled": True}, invalid_body=["not", "an", "object"],
          check=lambda w, r: r.json()["enabled"] is True),
]

def _client_url(verb: str) -> str:
    return "/api/enterprise/client-portal/agents/{agent}/clients/" + GUEST + "/" + verb


S4B_WORKSPACE_CLIENTS = [
    Route("client_portal/router.py::logout_agent_client", "POST", _client_url("logout"),
          PERSON_RULE, check=lambda w, r: r.json() == {"email": GUEST, "revoked": True}),
    Route("client_portal/router.py::block_agent_client", "POST", _client_url("block"),
          PERSON_RULE, body={"reason": "abuse"}, invalid_body={"reason": ["not", "a", "string"]},
          check=lambda w, r: r.json()["blocked"] is True),
    Route("client_portal/router.py::unblock_agent_client", "DELETE", _client_url("block"),
          PERSON_RULE, check=lambda w, r: r.json()["blocked"] is False),
]

def _link_ids(agent: str) -> list[str]:
    from database import db
    return [link["id"] for link in db.list_agent_public_links(agent)]


S4C_PUBLIC_LINKS = [
    Route("routers/public_links.py::create_public_link", "POST", "/api/agents/{agent}/public-links",
          PERSON_RULE, body={"name": "Front desk"}, invalid_body={"name": ["not", "a", "string"]},
          check=lambda w, r: r.json()["created_by"] == ADMIN and len(_link_ids(A)) == 2),
    Route("routers/public_links.py::list_public_links", "GET", "/api/agents/{agent}/public-links",
          PERSON_RULE, check=lambda w, r: [link["id"] for link in r.json()] == [w.ids["link_id"]]),
    Route("routers/public_links.py::get_public_link", "GET",
          "/api/agents/{agent}/public-links/{link_id}", PERSON_RULE,
          check=lambda w, r: r.json()["id"] == w.ids["link_id"]),
    Route("routers/public_links.py::update_public_link", "PUT",
          "/api/agents/{agent}/public-links/{link_id}", PERSON_RULE,
          body={"enabled": False}, invalid_body={"enabled": ["not", "a", "bool"]},
          check=lambda w, r: r.json()["enabled"] is False),
    Route("routers/public_links.py::delete_public_link", "DELETE",
          "/api/agents/{agent}/public-links/{link_id}", PERSON_RULE,
          check=lambda w, r: _link_ids(A) == []),
]

S4D_CANVAS_SHARES_AND_PIN = [
    Route("routers/canvas.py::create_canvas_share", "POST", "/api/agents/{agent}/canvas/main/share",
          PERSON_RULE, body={"scope": "authorized"}, invalid_body={"scope": "everyone"},
          check=lambda w, r: r.json()["scope"] == "authorized" and r.json()["id"] != w.ids["share_id"]),
    Route("routers/canvas.py::list_canvas_shares", "GET", "/api/agents/{agent}/canvas/shares",
          PERSON_RULE, check=lambda w, r: [share["id"] for share in r.json()] == [w.ids["share_id"]]),
    Route("routers/canvas.py::revoke_canvas_share", "DELETE",
          "/api/agents/{agent}/canvas/shares/{share_id}", PERSON_RULE,
          check=lambda w, r: r.json() == {"share_id": w.ids["share_id"], "revoked": True}),
    Route("routers/canvas.py::pin_canvas", "PUT", "/api/agents/{agent}/canvas/main/pin",
          PERSON_RULE, body={"pinned": True}, invalid_body={},
          check=lambda w, r: r.json()["pinned"] is True),
]

ROUTES = (S4A_SHARING_AND_FILES + S4B_WORKSPACE_CLIENTS + S4C_PUBLIC_LINKS
          + S4D_CANVAS_SHARES_AND_PIN)
PERSON_ROUTES = [r for r in ROUTES if r.rule == PERSON_RULE]
INTERACTIVE_ROUTES = [r for r in ROUTES if r.rule == INTERACTIVE_RULE]


# --------------------------------------------------------------------------- #
# The world: a real schema, real ownership, real reach
# --------------------------------------------------------------------------- #
#
# SQLite only, and built once per module: the rule under test does not depend on
# the dialect, and the full-schema bootstrap is the slow step (seconds per test
# on a loaded host). Each test gets its own copy of a seeded template file, so
# no test sees another's writes.

def _seed_people_and_agents() -> None:
    seed_user(1, ADMIN, role="admin")
    run("UPDATE users SET email = :e WHERE id = 1", e="admin@example.com")
    seed_agent(A, owner_id=1)


def _seed_world() -> dict:
    """An admin who owns A, PARENT and TARGET. PARENT spawned A with the key it
    holds now and holds an edge to A, so A is inside both its manage reach and
    its use reach. A is shared with GUEST, has a pending request from ASKER and
    an edge to TARGET."""
    from database import db

    _seed_people_and_agents()
    seed_user(2, OLIVIA, role="creator")
    run("UPDATE users SET email = :e WHERE id = 2", e="olivia@example.com")
    for name in (PARENT, TARGET):
        seed_agent(name, owner_id=1)
    seed_agent(OLIVIAS, owner_id=2)
    run("INSERT INTO mcp_api_keys (id, name, key_prefix, key_hash, created_at, is_active, "
        "user_id, agent_name, scope) VALUES ('key-parent', :p, 'trinity_mcp_p', 'hash-parent', "
        "'2026-01-01T00:00:00Z', 1, 1, :p, 'agent')", p=PARENT)
    run("UPDATE agent_ownership SET spawned_by_agent = :p, spawned_by_key_id = 'key-parent' "
        "WHERE agent_name = :a", p=PARENT, a=A)
    db.add_agent_permission(PARENT, A, created_by=ADMIN)
    db.add_agent_permission(A, TARGET, created_by=ADMIN)
    db.share_agent(A, ADMIN, GUEST)
    db.upsert_agent_canvas(A, "main", blocks=[
        {"id": "b1", "kind": "markdown", "payload": {"markdown": "Status"}}])
    return {
        "request_id": db.upsert_access_request(A, ASKER, "web")["id"],
        "link_id": db.create_public_link(A, ADMIN, name="Reception")["id"],
        "share_id": db.create_canvas_share(A, "main", scope="authorized", created_by="1")["id"],
    }


def _seed_fresh() -> dict:
    """A fresh install: one admin-owned agent, nothing shared, granted, requested."""
    _seed_people_and_agents()
    return {"request_id": "no-such-request", "link_id": "no-such-link", "share_id": "no-such-share"}


def _point_engine_at(monkeypatch, path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRINITY_DB_PATH", str(path))
    dispose_engines()


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    root = tmp_path_factory.mktemp("ent719-reach")
    built = {}
    with pytest.MonkeyPatch.context() as mp:
        empty = root / "empty.db"
        _point_engine_at(mp, empty)
        bootstrap_schema()
        dispose_engines()
        for name, seed in (("world", _seed_world), ("fresh", _seed_fresh)):
            path = root / f"{name}.db"
            shutil.copy(empty, path)
            _point_engine_at(mp, path)
            built[name] = (path, seed())
            dispose_engines()
    for path, _ids in built.values():
        assert not path.with_name(path.name + "-wal").exists(), "copy the WAL too"
    return built


class _Container:
    status = "running"
    attrs: ClassVar[dict[str, list]] = {"Mounts": []}


def _install_stubs(monkeypatch, live: set[str]) -> list:
    """Stand in for Docker, the WebSocket broadcast and the audit sink only.

    Everything the rule and the ownership checks read is the real thing."""
    container = _Container()

    def get_container(name):
        return container if name in live else None

    for mod in (sharing_router, permissions_logic, folders_logic, file_sharing_logic,
                public_links_router):
        monkeypatch.setattr(mod, "get_agent_container", get_container)
    for mod in (sharing_router, public_links_router):
        monkeypatch.setattr(mod, "manager", None)

    audit: list = []

    async def _log(**kw):
        audit.append(kw)

    # The routers' own bindings (see test_ent281's note on the #762 restore list).
    for mod in (sharing_router, agent_files_router, portal_router, canvas_router):
        monkeypatch.setattr(mod.platform_audit_service, "log", _log)

    # A real fakeredis behind the portal-session revocation cutoff (test_ent281).
    # Patched on the namespace the routers hold AND on the live module the
    # service's function-local `from dependencies import ...` resolves to.
    import dependencies as live_dependencies
    import fakeredis
    redis = fakeredis.FakeRedis()
    for namespace in {id(ns): ns for ns in (_DEPS, vars(live_dependencies))}.values():
        monkeypatch.setitem(namespace, "get_breaker_redis", lambda: redis)
    return audit


def _install(name, templates, tmp_path, monkeypatch, live):
    path, ids = templates[name]
    target = tmp_path / "trinity.db"
    shutil.copy(path, target)
    _point_engine_at(monkeypatch, target)
    audit = _install_stubs(monkeypatch, live)
    return types.SimpleNamespace(ids=dict(ids), audit=audit)


@pytest.fixture
def world(templates, tmp_path, monkeypatch):
    yield _install("world", templates, tmp_path, monkeypatch, live={A, PARENT, TARGET})
    dispose_engines()


@pytest.fixture
def fresh(templates, tmp_path, monkeypatch):
    yield _install("fresh", templates, tmp_path, monkeypatch, live={A})
    dispose_engines()


def _app() -> FastAPI:
    app = FastAPI()
    for mod in ROUTERS:
        app.include_router(mod.router)
    return app


class _Env:
    def __init__(self, state):
        self.state = state
        self.app = _app()
        self.holder: dict = {}
        leaves = {mod.get_current_user for mod in ROUTERS} | {_DEPS["get_current_user"]}
        for leaf in leaves:
            self.app.dependency_overrides[leaf] = lambda: self.holder["principal"]
        self.client = TestClient(self.app)

    def call(self, route: Route, *, agent: str = A, body: Any = None, principal=None):
        if principal is not None:
            self.holder["principal"] = principal
        url = route.path.format(agent=agent, **self.state.ids)
        payload = route.body if body is None else body
        return self.client.request(route.method, url, json=payload)


@pytest.fixture
def env(world):
    yield _Env(world)


@pytest.fixture
def fresh_env(fresh):
    yield _Env(fresh)


class _CallRecorder:
    """Stands in for one `db` singleton and records every method reached for."""

    def __init__(self, real, calls: list):
        self._real = real
        self._calls = calls

    def __getattr__(self, name):
        value = getattr(self._real, name)
        self._calls.append(name)
        return value


def _record_db(monkeypatch) -> list:
    """Point every `db` binding a route can reach at a recorder; return the call list.

    Every loaded module, plus the namespaces this file holds directly: the
    `dependencies` namespace the routers' gates and ownership checks read can be
    one that is no longer in `sys.modules` (the #762 restore list), and so can a
    router module. Matched by class name, not identity, for the same reason.
    `test_the_db_recorder_sees_an_ownership_lookup` is the positive control."""
    namespaces: dict[int, dict] = {}
    for mod in list(sys.modules.values()):
        namespace = getattr(mod, "__dict__", None)
        if isinstance(namespace, dict):
            namespaces[id(namespace)] = namespace
    for namespace in (_DEPS, *(vars(mod) for mod in HELD_MODULES)):
        namespaces[id(namespace)] = namespace

    calls: list = []
    recorders: dict[int, _CallRecorder] = {}
    for namespace in namespaces.values():
        real = namespace.get("db")
        if type(real).__name__ != "DatabaseManager":
            continue
        recorder = recorders.setdefault(id(real), _CallRecorder(real, calls))
        monkeypatch.setitem(namespace, "db", recorder)
    assert recorders, "no db singleton found to record"
    return calls


@pytest.fixture
def db_calls(env, monkeypatch):
    return _record_db(monkeypatch)


@pytest.fixture
def fresh_db_calls(fresh_env, monkeypatch):
    return _record_db(monkeypatch)


def _detail(response):
    body = response.json()
    return response.status_code, body.get("detail") if isinstance(body, dict) else body


# --------------------------------------------------------------------------- #
# 1-2. Every machine principal is refused, the same way, before any lookup
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("who", list(MACHINES))
@pytest.mark.parametrize("route", ROUTES, ids=str)
def test_a_machine_principal_gets_the_rules_refusal_before_any_lookup(env, db_calls, route, who):
    principal = MACHINES[who]()
    expected = _rule_refusal(route.rule, principal)

    assert _detail(env.call(route, principal=principal)) == expected
    # The same answer for an agent that does not exist (#186) ...
    assert _detail(env.call(route, agent=MISSING)) == expected
    # ... and for a body a person would get a validation error for.
    if route.invalid_body is not None:
        assert _detail(env.call(route, body=route.invalid_body)) == expected

    assert db_calls == [], f"the route reached the database before refusing: {db_calls}"
    assert env.state.audit == []


def _route(key: str) -> Route:
    return next(r for r in ROUTES if r.key == key)


def test_the_db_recorder_sees_an_ownership_lookup(env, db_calls):
    """Positive control for every `db_calls == []` above. A person's call is
    recorded at the route's ownership dependency (`OwnedAgentByName`) and at a
    service's own owner check, so an empty record means the route refused
    before either ran."""
    env.holder["principal"] = PERSON()

    assert env.call(_route("routers/sharing.py::update_access_policy_endpoint")).status_code == 200
    assert "get_agent_owner" in db_calls, db_calls

    db_calls.clear()
    assert env.call(_route("routers/agent_files.py::set_agent_permissions")).status_code == 200
    assert "can_user_share_agent" in db_calls, db_calls


@pytest.mark.parametrize("route", INTERACTIVE_ROUTES, ids=str)
def test_the_persons_own_key_cannot_write_a_workspace_sign_in(env, db_calls, route):
    """Sharing the agent and approving an access request take a signed-in
    session: the share row they write is a sign-in by emailed code."""
    principal = USER_KEY()
    expected = _rule_refusal(route.rule, principal)

    assert _detail(env.call(route, principal=principal)) == expected
    assert _detail(env.call(route, agent=MISSING)) == expected
    if route.invalid_body is not None:
        assert _detail(env.call(route, body=route.invalid_body)) == expected
    assert db_calls == []


def test_opening_an_agent_to_anonymous_visitors_takes_a_person(env):
    """A public link opens the agent to anyone holding its URL, so creating one
    is a grant. The agent's own key gets the PERSON rule's 403 and no link is
    written; the link a person made earlier is the only one left."""
    before = _link_ids(A)
    env.holder["principal"] = MACHINES["agent_own_key"]()

    response = env.client.post(f"/api/agents/{A}/public-links", json={"name": "Front desk"})

    assert _detail(response) == (403, HUMAN_ONLY_DETAIL), response.text
    assert _link_ids(A) == before == [env.state.ids["link_id"]]


# --------------------------------------------------------------------------- #
# 3. The entry fence refuses a real, unvouched loopback JWT
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("route", ROUTES, ids=str)
def test_an_unvouched_loopback_jwt_is_refused_at_the_entry_fence(world, monkeypatch, route):
    """A real, signed loopback JWT with NO source agent, through real
    authentication (no override). The route rule does not see this shape; the
    `get_current_user` entry fence refuses it on every route but the subscriber
    task dispatch. The admin it names exists and the token is not revoked, so a
    pass would be the route's success, not an incidental 401."""
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY

    monkeypatch.setitem(_DEPS, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {"sub": ADMIN, "scope": EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    client = TestClient(_app())  # no dependency override
    url = route.path.format(agent=A, **world.ids)
    response = client.request(route.method, url, json=route.body,
                              headers={"Authorization": f"Bearer {token}"})

    assert _detail(response) == (403, LOOPBACK_FENCE_DETAIL)
    assert world.audit == []


# --------------------------------------------------------------------------- #
# 4. A person still decides
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("route", ROUTES, ids=str)
def test_a_person_in_a_signed_in_session_still_can(env, route):
    response = env.call(route, principal=PERSON())
    assert response.status_code == route.ok, response.text
    if route.check:
        assert route.check(env.state, response) is not False


@pytest.mark.parametrize("route", PERSON_ROUTES, ids=str)
def test_the_persons_own_user_key_still_can(env, route):
    response = env.call(route, principal=USER_KEY())
    assert response.status_code == route.ok, response.text
    if route.check:
        assert route.check(env.state, response) is not False


def test_blocking_and_unblocking_a_client_still_take_an_admin(env, monkeypatch):
    """Block and unblock bar a person platform-wide, so they keep `assert_admin`
    behind the PERSON rule; logging a client out stays the owner's call.

    Olivia is a signed-in creator who owns her own agent: she passes the rule
    and the ownership check, and the admin check still refuses her, on her
    session and on her own `user` key alike."""
    olivia = {"id": 2, "username": OLIVIA, "email": "olivia@example.com", "role": "creator"}
    base = "/api/enterprise/client-portal/agents/" + OLIVIAS + "/clients/" + GUEST

    for principal in (User(**olivia), User(**olivia, mcp_scope="user", mcp_key_id="key-o",
                                           mcp_key_name="olivia-laptop")):
        env.holder["principal"] = principal
        blocked = env.client.post(base + "/block", json={"reason": "abuse"})
        assert _detail(blocked) == (403, "Blocking a client requires admin access")
        unblocked = env.client.delete(base + "/block")
        assert _detail(unblocked) == (403, "Unblocking a client requires admin access")
        logged_out = env.client.post(base + "/logout")
        assert logged_out.status_code == 200, logged_out.text

    assert [row["event_action"] for row in env.state.audit] == ["portal_client_logout"] * 2


# --------------------------------------------------------------------------- #
# 5. The use next to the grant is unchanged
# --------------------------------------------------------------------------- #

def test_the_agent_still_shares_its_own_file_once_a_person_turns_file_sharing_on(
        env, monkeypatch, tmp_path):
    """`share_file` is a USE of the file-sharing grant a person turns on.

    The agent's own key is refused while file sharing is off (the service's own
    flag check), and mints a download link once a person has turned it on
    through the PERSON-gated route. Only the container read and the disk are
    stood in for."""
    from services import agent_shared_files_service as svc

    async def _extract(agent_name, container_path):
        return b"quarterly numbers\n", "report.txt"

    monkeypatch.setattr(svc, "extract_from_agent", _extract)
    monkeypatch.setattr(svc, "STORAGE_ROOT", str(tmp_path / "agent-files"))
    monkeypatch.setattr(svc, "check_disk_space", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "get_public_chat_url", lambda: "")

    own_key = MACHINES["agent_own_key"]
    share = {"filename": "report.txt"}

    env.holder["principal"] = own_key()
    refused = env.client.post(f"/api/agents/{A}/shared-files", json=share)
    assert refused.status_code == 403, refused.text
    assert "FEATURE_DISABLED" in refused.json()["detail"]

    env.holder["principal"] = PERSON()
    turned_on = env.client.put(f"/api/agents/{A}/file-sharing", json={"enabled": True})
    assert turned_on.status_code == 200, turned_on.text

    env.holder["principal"] = own_key()
    minted = env.client.post(f"/api/agents/{A}/shared-files", json=share)
    assert minted.status_code == 201, minted.text
    assert minted.json()["url"].startswith("/api/files/")


def test_the_agent_still_writes_and_clears_its_own_canvas(env):
    """Rendering and tidying up its own canvas are USES (the #918 self-gate);
    only who outside the platform may read it, and the pin, are grants."""
    env.holder["principal"] = MACHINES["agent_own_key"]()
    body = {"blocks": [{"id": "b1", "kind": "markdown", "payload": {"markdown": "Done"}}]}

    written = env.client.put(f"/api/agents/{A}/canvas/notes", json=body)
    assert written.status_code == 200, written.text
    assert written.json()["canvas_id"] == "notes"

    cleared = env.client.delete(f"/api/agents/{A}/canvas/notes")
    assert cleared.status_code == 200, cleared.text
    from database import db
    assert db.get_agent_canvas(A, "notes") is None


# --------------------------------------------------------------------------- #
# 6. Fresh install
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("route", ROUTES, ids=str)
def test_on_a_fresh_install_every_machine_principal_is_refused_before_any_lookup(
        fresh_env, fresh_db_calls, route):
    principals = dict(MACHINES)
    if route.rule == INTERACTIVE_RULE:
        principals["user_key"] = USER_KEY
    for who, make in principals.items():
        principal = make()
        assert _detail(fresh_env.call(route, principal=principal)) == \
            _rule_refusal(route.rule, principal), who
    assert fresh_db_calls == []
    assert fresh_env.state.audit == []


FRESH_LISTS = [
    "/api/agents/{agent}/shares",
    "/api/agents/{agent}/access-requests",
    "/api/agents/{agent}/public-links",
    "/api/agents/{agent}/canvas/shares",
]


@pytest.mark.parametrize("path", FRESH_LISTS)
def test_on_a_fresh_install_the_persons_lists_are_empty(fresh_env, path):
    fresh_env.holder["principal"] = PERSON()
    response = fresh_env.client.get(path.format(agent=A))
    assert response.status_code == 200, response.text
    assert response.json() == []
