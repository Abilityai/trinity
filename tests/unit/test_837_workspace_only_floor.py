"""The `user` rung is Workspace-only (trinity-enterprise#837, PR 1).

`get_current_user` is the door every operator route authenticates through. It
refuses a Workspace-only principal (role `user`, or a role outside the ladder)
with one named 403, except on a route marked `@workspace_route` (a person) or
on an agent's own runtime surface (an agent whose owner is Workspace-only). The
Workspace doors resolve the credential through `resolve_platform_user_unfloored`.

Every request here is a REAL Starlette request — routed by FastAPI, so
`scope["route"]` is what production sees, or built from an ASGI scope (#3102).
Modules are imported inside each test: the unit conftest evicts `dependencies`
between tests, so a module-level import would be a stale copy (learnings
2026-09-23).
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"

WORKSPACE_ONLY = {
    "code": "workspace_only",
    "message": "This account works in the Workspace. Open /workspace.",
}


def _row(role, username="member"):
    return {
        "id": 7, "username": username, "email": f"{username}@example.com",
        "role": role, "suspended_at": None,
    }


@pytest.fixture
def jwt_as(monkeypatch):
    """A real platform JWT whose `sub` resolves to a user holding `role`."""
    import database
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    def make(role, username="member"):
        monkeypatch.setattr(
            database.db, "get_user_by_username", lambda name, *_a, **_k: _row(role, name)
        )
        return jwt.encode(
            {"sub": username, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM
        )

    return make


@pytest.fixture
def agent_key_as(monkeypatch):
    """An agent-scoped MCP key for `agent`, owned by a user holding `owner_role`."""
    import database

    def make(owner_role, agent="atlas", scope="agent"):
        monkeypatch.setattr(database.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": scope, "agent_name": agent if scope == "agent" else None,
            "key_id": "k1", "key_name": scope,
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(
            database.db, "get_user_by_email", lambda *_a, **_k: _row(owner_role, "owner")
        )
        monkeypatch.setattr(
            database.db, "get_agent_ephemeral_info", lambda *_a, **_k: {"is_ephemeral": False}
        )
        return "trinity_mcp_fake_agent_key"

    return make


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _users_app(monkeypatch):
    """The REAL users router, its handlers' storage stubbed."""
    from fastapi import FastAPI
    import routers.users as users_mod
    import database

    monkeypatch.setattr(database.db, "has_user_github_pat", lambda *_a, **_k: False)
    monkeypatch.setattr(database.db, "get_user_github_pat", lambda *_a, **_k: None)
    monkeypatch.setattr(users_mod.user_preferences_service, "get_all", lambda *_a, **_k: {})
    monkeypatch.setattr(database.db, "list_users", lambda *_a, **_k: [])
    app = FastAPI()
    app.include_router(users_mod.router)
    return TestClient(app)


def _plain_request(method="GET", path="/api/anything"):
    """An ASGI-scope request that matched NO route (no `scope["route"]`)."""
    return Request({
        "type": "http", "method": method, "path": path, "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", b"backend:8000")],
    })


# --------------------------------------------------------------------------- #
# The floor, on real routes
# --------------------------------------------------------------------------- #

class TestTheFloorOnRealRoutes:
    def test_a_user_is_refused_on_an_operator_route_with_the_named_refusal(self, monkeypatch, jwt_as):
        client = _users_app(monkeypatch)
        r = client.get("/api/users/me/github-pat", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403
        assert r.json()["detail"] == WORKSPACE_ONLY

    def test_an_admin_gate_answers_a_user_with_the_same_refusal(self, monkeypatch, jwt_as):
        client = _users_app(monkeypatch)
        r = client.get("/api/users", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403
        assert r.json()["detail"] == WORKSPACE_ONLY

    @pytest.mark.parametrize("role", ["operator", "creator", "admin"])
    def test_operator_and_above_pass_the_floor(self, monkeypatch, jwt_as, role):
        client = _users_app(monkeypatch)
        r = client.get("/api/users/me/github-pat", headers=_bearer(jwt_as(role)))
        assert r.status_code == 200, r.text

    def test_a_user_keeps_the_workspace_routes_on_this_router(self, monkeypatch, jwt_as):
        client = _users_app(monkeypatch)
        r = client.get("/api/users/me/preferences", headers=_bearer(jwt_as("user")))
        assert r.status_code == 200, r.text

    def test_a_role_outside_the_ladder_is_workspace_only(self, monkeypatch, jwt_as):
        client = _users_app(monkeypatch)
        r = client.get("/api/users/me/github-pat", headers=_bearer(jwt_as("viewer")))
        assert r.status_code == 403
        assert r.json()["detail"] == WORKSPACE_ONLY

    def test_the_refusal_comes_before_any_agent_lookup(self, monkeypatch, jwt_as):
        """Invariant #8: the 403 depends on the principal only, so an existing and
        a missing agent answer identically and no agent row is read."""
        from fastapi import APIRouter, Depends, FastAPI
        import database
        import dependencies as deps

        reads = []
        for name in ("get_agent_owner", "can_user_access_agent", "get_agent_by_name"):
            monkeypatch.setattr(
                database.db, name, lambda *a, _n=name, **k: reads.append(_n) or True, raising=False
            )
        router = APIRouter(prefix="/api/agents")

        @router.get("/{name}/thing")
        async def thing(name: str = Depends(deps.get_authorized_agent)):
            return {"name": name}

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        token = jwt_as("user")
        existing = client.get("/api/agents/atlas/thing", headers=_bearer(token))
        missing = client.get("/api/agents/no-such-agent/thing", headers=_bearer(token))
        assert existing.status_code == missing.status_code == 403
        assert existing.json() == missing.json() == {"detail": WORKSPACE_ONLY}
        assert reads == []


# --------------------------------------------------------------------------- #
# The marker
# --------------------------------------------------------------------------- #

def _marked_app(deps):
    from fastapi import APIRouter, Depends, FastAPI

    router = APIRouter(prefix="/api/probe")

    @router.get("/marked")
    @deps.workspace_route("probe: the Workspace calls it")
    async def marked(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    @router.get("/plain")
    async def plain(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestMarkedForWhatMembersUse:
    """trinity-enterprise#837 review: the marks follow what a member's own pages
    call — the public chat page's history, yes; the Dashboard's preference reset, no."""

    def test_a_user_reads_their_own_public_chat_history(self, monkeypatch, jwt_as):
        from fastapi import FastAPI
        import database
        import routers.public as public

        monkeypatch.setattr(public, "_validate_public_link", lambda token: {"agent_name": "agent-a"})
        monkeypatch.setattr(database.db, "get_agent_chat_sessions", lambda **_k: [])
        app = FastAPI()
        app.include_router(public.router)
        path = next(r.path for r in app.routes if r.path.endswith("/sessions/{token}"))
        r = TestClient(app).get(path.replace("{token}", "tok"), headers=_bearer(jwt_as("user")))
        assert r.status_code == 200, r.text

    def test_a_user_cannot_reset_a_preference(self, monkeypatch, jwt_as):
        client = _users_app(monkeypatch)
        r = client.delete("/api/users/me/preferences/fleet.grid", headers=_bearer(jwt_as("user")))
        assert r.status_code == 403 and r.json()["detail"] == WORKSPACE_ONLY


class TestTheMarker:
    def test_the_marker_admits_a_user_through_a_prefixed_router(self, jwt_as):
        import dependencies as deps

        client = _marked_app(deps)
        token = jwt_as("user")
        assert client.get("/api/probe/marked", headers=_bearer(token)).status_code == 200
        plain = client.get("/api/probe/plain", headers=_bearer(token))
        assert plain.status_code == 403 and plain.json()["detail"] == WORKSPACE_ONLY

    def test_the_marker_keeps_its_reason_and_returns_the_same_function(self):
        import dependencies as deps

        async def endpoint():
            return None

        assert deps.workspace_route("why")(endpoint) is endpoint
        assert getattr(endpoint, deps.WORKSPACE_ROUTE_ATTR) == "why"
        with pytest.raises(ValueError):
            deps.workspace_route("  ")

    def test_the_marker_does_not_admit_an_agent_key(self, agent_key_as):
        """The marked routes are the Workspace's; the Workspace is human-only (#2198)."""
        import dependencies as deps

        client = _marked_app(deps)
        r = client.get("/api/probe/marked", headers=_bearer(agent_key_as("user")))
        assert r.status_code == 403 and r.json()["detail"] == WORKSPACE_ONLY

    def test_the_marker_does_not_admit_a_key_even_the_persons_own(self, agent_key_as):
        """A `user`-scoped key minted before the upgrade cannot be listed or revoked
        by its owner any more; it must not keep the operator-side routes either."""
        import dependencies as deps

        client = _marked_app(deps)
        r = client.get("/api/probe/marked", headers=_bearer(agent_key_as("user", scope="user")))
        assert r.status_code == 403 and r.json()["detail"] == WORKSPACE_ONLY

    def test_with_no_matched_route_the_floor_refuses_a_user(self, jwt_as):
        import dependencies as deps
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            asyncio.run(deps.get_current_user(_plain_request(), token=jwt_as("user")))
        assert exc.value.status_code == 403 and exc.value.detail == WORKSPACE_ONLY

    def test_with_no_matched_route_an_operator_still_resolves(self, jwt_as):
        import dependencies as deps

        user = asyncio.run(deps.get_current_user(_plain_request(), token=jwt_as("operator")))
        assert user.role == "operator"

    def test_the_refusal_detail_is_byte_identical(self):
        import dependencies as deps

        assert deps.WORKSPACE_ONLY_DETAIL == WORKSPACE_ONLY

    @pytest.mark.parametrize("role,expected", [
        ("user", True), ("viewer", True), (None, True), ("", True),
        ("operator", False), ("creator", False), ("admin", False),
    ])
    def test_the_rung_predicate(self, role, expected):
        import dependencies as deps

        assert deps.is_workspace_only_role(role) is expected


# --------------------------------------------------------------------------- #
# An agent never exceeds its owner (Decision 2)
# --------------------------------------------------------------------------- #

_AGENT_SURFACE = [
    ("POST", "/api/agents/{name}/reports"),
    ("POST", "/api/notifications"),
    ("GET", "/api/agents/{name}"),
    ("GET", "/api/agents/{name}/info"),
    ("POST", "/api/agents/{name}/operator-queue"),
    ("GET", "/api/agents/{name}/operator-queue/req-1"),
    ("POST", "/api/skill-gate/check"),
]


def _agent_app(deps):
    """Probe routes at the agent runtime paths, plus fleet routes."""
    from fastapi import Depends, FastAPI

    app = FastAPI()

    async def handler(user=Depends(deps.get_current_user)):
        return {"who": user.username, "agent": user.agent_name}

    for method, path in _AGENT_SURFACE + [("GET", "/api/agents"), ("POST", "/api/agents/{name}/chat")]:
        app.add_api_route(path.replace("req-1", "{request_id}"), handler, methods=[method])
    return TestClient(app)


class TestAgentNeverExceedsItsOwner:
    @pytest.mark.parametrize("method,path", _AGENT_SURFACE)
    def test_an_agent_of_a_user_owner_keeps_its_own_runtime_surface(self, agent_key_as, method, path):
        import dependencies as deps

        client = _agent_app(deps)
        r = client.request(method, path.format(name="atlas"), headers=_bearer(agent_key_as("user")))
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/agents/sibling/reports"),
        ("GET", "/api/agents/sibling/info"),
        ("POST", "/api/agents/sibling/operator-queue"),
        ("GET", "/api/agents"),
        ("POST", "/api/agents/atlas/chat"),
    ])
    def test_everything_else_is_refused(self, agent_key_as, method, path):
        import dependencies as deps

        client = _agent_app(deps)
        r = client.request(method, path, headers=_bearer(agent_key_as("user")))
        assert r.status_code == 403 and r.json()["detail"] == WORKSPACE_ONLY

    def test_an_agent_of_an_operator_owner_is_untouched(self, agent_key_as):
        import dependencies as deps

        client = _agent_app(deps)
        r = client.get("/api/agents", headers=_bearer(agent_key_as("operator")))
        assert r.status_code == 200

    def test_a_connector_of_a_user_owner_keeps_its_two_routes(self, monkeypatch):
        """A connector key serves the external consumers of one agent, as a public
        link does, and the ent#46 fence confines it to that agent's chat and
        playbook list. The floor lets it through there (trinity-enterprise#837
        review); every other route is still the fence's refusal."""
        import database
        import dependencies as deps
        from fastapi import Depends, FastAPI

        monkeypatch.setattr(database.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": "connector", "agent_name": "atlas", "key_id": "k2", "key_name": "connector",
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(database.db, "get_user_by_email", lambda *_a, **_k: _row("user", "owner"))
        app = FastAPI()

        async def handler(user=Depends(deps.get_current_user)):
            return {"who": user.username}

        app.add_api_route("/api/agents/{name}/chat", handler, methods=["POST"])
        app.add_api_route("/api/agents/{name}/connector/playbooks", handler, methods=["GET"])
        app.add_api_route("/api/agents/{name}/info", handler, methods=["GET"])
        client = TestClient(app)
        key = _bearer("trinity_mcp_fake_connector_key")

        assert client.post("/api/agents/atlas/chat", headers=key).status_code == 200
        assert client.get("/api/agents/atlas/connector/playbooks", headers=key).status_code == 200
        fenced = client.get("/api/agents/atlas/info", headers=key)
        assert fenced.status_code == 403 and fenced.json()["detail"] != WORKSPACE_ONLY

    def test_a_static_route_that_spells_the_agents_name_is_not_its_own(self, agent_key_as):
        """`GET /api/agents/slots` is a static fleet route registered before
        `/api/agents/{agent_name}`, so its raw path matches the self-route pattern
        with `name == "slots"`. Only a route whose own path parameter carries the
        agent's name is its own."""
        import dependencies as deps
        from fastapi import Depends, FastAPI

        app = FastAPI()

        async def handler(user=Depends(deps.get_current_user)):
            return {"who": user.username}

        app.add_api_route("/api/agents/slots", handler, methods=["GET"])
        app.add_api_route("/api/agents/{agent_name}", handler, methods=["GET"])
        app.add_api_route("/api/agents/{agent_name}/info", handler, methods=["GET"])
        client = TestClient(app)
        key = agent_key_as("user", agent="slots")

        refused = client.get("/api/agents/slots", headers=_bearer(key))
        assert refused.status_code == 403 and refused.json()["detail"] == WORKSPACE_ONLY
        assert client.get("/api/agents/slots/info", headers=_bearer(key)).status_code == 200


# --------------------------------------------------------------------------- #
# The Workspace doors
# --------------------------------------------------------------------------- #

class TestTheWorkspaceDoors:
    def test_the_portal_principal_admits_a_user(self, monkeypatch, jwt_as):
        import client_portal.db as portal_db
        from client_portal.portal_auth import get_portal_principal
        from starlette.responses import Response

        monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
        principal = asyncio.run(
            get_portal_principal(_plain_request(), Response(), token=jwt_as("user"))
        )
        assert principal.email == "member@example.com"
        assert principal.is_platform is True

    def test_the_room_principal_keeps_a_user_a_platform_user(self, jwt_as):
        """Not the portal fallback: a `user` resolves to its own `User`, as before."""
        from shared_sessions.router import get_room_principal
        from starlette.responses import Response

        principal = asyncio.run(
            get_room_principal(_plain_request(), Response(), token=jwt_as("user"))
        )
        assert getattr(principal, "username", None) == "member"
        assert principal.role == "user"

    def test_the_room_principal_refuses_an_agent_of_a_user_owner(self, agent_key_as):
        """An agent of a Workspace-only owner keeps only its own runtime routes. The
        rooms door resolves past the floor, so without this refusal the agent could
        room with — and wake — any agent its owner reaches."""
        from fastapi import HTTPException
        from shared_sessions.router import get_room_principal
        from starlette.responses import Response

        with pytest.raises(HTTPException) as refused:
            asyncio.run(get_room_principal(_plain_request(), Response(), token=agent_key_as("user")))
        assert refused.value.status_code == 403
        assert refused.value.detail == WORKSPACE_ONLY

    def test_the_room_principal_keeps_an_agent_of_an_operator_owner(self, agent_key_as):
        from shared_sessions.router import get_room_principal
        from starlette.responses import Response

        principal = asyncio.run(
            get_room_principal(_plain_request(), Response(), token=agent_key_as("operator"))
        )
        assert principal.agent_name == "atlas"

    def test_the_unfloored_resolver_has_exactly_the_pinned_callers(self):
        """The resolver that skips the floor is the bypass; a new caller must be a
        reviewed edit to this set (the ent#162 writer-set pattern). EVERY reference
        counts — a `Depends(...)`, a router-level `dependencies=[...]`, a
        module-level use, an alias from `import ... as` — and each must be a direct
        call inside one of the four doors."""
        name = "resolve_platform_user_unfloored"
        uses = []
        for path in sorted(BACKEND.rglob("*.py")):
            rel = path.relative_to(BACKEND).as_posix()
            if rel.split("/")[0] in {"enterprise", "tests"} or "__pycache__" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = {name}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        if a.name == name and a.asname:
                            aliases.add(a.asname)
            parents = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent

            def enclosing(node):
                while node in parents:
                    node = parents[node]
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return node.name
                return "<module>"

            for node in ast.walk(tree):
                ident = node.id if isinstance(node, ast.Name) else (
                    node.attr if isinstance(node, ast.Attribute) else None)
                if ident not in aliases:
                    continue
                parent = parents.get(node)
                called = isinstance(parent, ast.Call) and parent.func is node
                uses.append((f"{rel}::{enclosing(node)}", called))
        assert all(called for _, called in uses), [u for u in uses if not u[1]]
        assert {where for where, _ in uses} == {
            "dependencies.py::get_current_user",
            "client_portal/portal_auth.py::get_portal_principal",
            "shared_sessions/router.py::get_room_principal",
            "client_portal/router.py::portal_voice_start",
        }


# --------------------------------------------------------------------------- #
# WebSocket doors (they authenticate outside get_current_user)
# --------------------------------------------------------------------------- #

class _FakeSocket:
    def __init__(self, token):
        self._inbox = [json.dumps({"type": "auth", "token": token})]
        self.sent = []
        self.closed = None

    async def accept(self):
        return None

    async def receive_text(self):
        return self._inbox.pop(0)

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class TestWebSocketDoors:
    def test_the_agent_terminal_refuses_a_workspace_only_owner(self, monkeypatch):
        """A demoted owner must not keep a shell into the agent (`.env`, tokens)."""
        import services.agent_service.terminal as terminal

        monkeypatch.setattr(terminal.db, "can_user_share_agent", lambda *_a, **_k: True)
        reached = []
        monkeypatch.setattr(terminal, "get_agent_container", lambda *_a, **_k: reached.append(1))
        sock = _FakeSocket("tok")
        manager = terminal.TerminalSessionManager()
        decode = lambda _t: {"sub": "member", "email": "member@example.com", "role": "user"}  # noqa: E731
        asyncio.run(manager.handle_terminal_session(sock, "atlas", "bash", decode))
        assert sock.closed and sock.closed[0] == 4003
        assert any(m.get("code") == "workspace_only" for m in sock.sent)
        assert reached == []
        assert manager._active_sessions == {}

    def test_the_agent_terminal_still_admits_an_owner_at_operator(self, monkeypatch):
        import services.agent_service.terminal as terminal

        monkeypatch.setattr(terminal.db, "can_user_share_agent", lambda *_a, **_k: True)
        monkeypatch.setattr(terminal, "get_agent_container", lambda *_a, **_k: None)
        sock = _FakeSocket("tok")
        decode = lambda _t: {"sub": "op", "email": "op@example.com", "role": "operator"}  # noqa: E731
        asyncio.run(terminal.TerminalSessionManager().handle_terminal_session(sock, "atlas", "bash", decode))
        # Past the role gate: the next refusal is the missing container, not the floor.
        assert not any(m.get("code") == "workspace_only" for m in sock.sent)

    @pytest.mark.parametrize("row,refused", [
        ({"role": "user"}, True), ({"role": "viewer"}, True), (None, True), ({}, True),
        ({"role": "operator"}, False), ({"role": "creator"}, False), ({"role": "admin"}, False),
    ])
    def test_the_event_stream_owner_check(self, row, refused):
        import dependencies as deps

        assert deps.owner_is_workspace_only(row) is refused

    def test_the_event_stream_refuses_a_workspace_only_owner_before_accepting(self):
        """`/ws/events` validates the key itself; the refusal must sit before accept
        and read the owner ROW it just resolved. Shape check on top of the
        executed owner check above (learnings 2026-09-25): main.py is not
        importable in the unit island."""
        tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_events_endpoint"
        )
        accept_line = min(
            n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "accept"
        )
        # `user_data` must be the owner row the handler looks up for this key...
        owner_lookup = next(
            (n for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "user_data" for t in n.targets)),
            None,
        )
        assert owner_lookup is not None and "get_user_by_username" in ast.unparse(owner_lookup.value)
        # ...and the guard must read exactly that row, close 4003 and return before accept().
        guards = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            tests_rung = any(
                isinstance(c, ast.Call)
                and getattr(c.func, "id", getattr(c.func, "attr", None)) == "owner_is_workspace_only"
                and len(c.args) == 1 and isinstance(c.args[0], ast.Name) and c.args[0].id == "user_data"
                for c in ast.walk(node.test)
            )
            closes = any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "close"
                and any(k.arg == "code" and getattr(k.value, "value", None) == 4003 for k in c.keywords)
                for c in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            )
            returns = any(isinstance(s, ast.Return) for s in node.body)
            if tests_rung and closes and returns and node.lineno < accept_line:
                guards.append(node.lineno)
        assert guards, "websocket_events_endpoint must refuse a Workspace-only owner before accept()"

    def test_every_websocket_route_has_a_workspace_only_disposition(self):
        """A new WebSocket route must say what it does with the `user` rung."""
        import sys
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import _route_census as rc

        dispositions = {
            "main.py::websocket_endpoint": "ticket comes from POST /api/ws/ticket (marked); events scoped to accessible agents (ent#467)",
            "main.py::websocket_events_endpoint": "refuses a Workspace-only owner (ent#837)",
            "routers/agents.py::agent_terminal": "refuses a Workspace-only role (ent#837)",
            "routers/system_agent.py::system_agent_terminal": "admin only",
            "routers/voice.py::voice_websocket": "bound to the voice session's owner (#600)",
            "routers/voip.py::voip_media_stream": "provider media stream, authenticated by the call binding",
        }
        assert set(dispositions) == set(rc.WEBSOCKET_ROUTES)


# --------------------------------------------------------------------------- #
# Sharing stops creating platform accounts
# --------------------------------------------------------------------------- #

class _SharingDb:
    def __init__(self):
        self.whitelisted = []

    def get_user_by_username(self, *_a, **_k):
        return {"id": 1, "username": "owner", "email": "owner@example.com"}

    def share_agent(self, agent_name, owner, email):
        return {"id": 1, "agent_name": agent_name, "shared_with_email": email}

    def get_setting_value(self, key, default=None):
        return "true" if key == "email_auth_enabled" else default

    def add_to_whitelist(self, email, *args, **kwargs):
        self.whitelisted.append(email)

    def get_access_request(self, request_id):
        return {"id": request_id, "agent_name": "atlas", "email": "guest@example.com", "channel": "web"}

    def decide_access_request(self, request_id, approve, by):
        return {"id": request_id, "agent_name": "atlas", "email": "guest@example.com",
                "channel": "web", "requested_at": "2026-10-09T00:00:00Z", "status": "approved"}


def _sharing(monkeypatch):
    import routers.sharing as sharing

    fake = _SharingDb()
    monkeypatch.setattr(sharing, "db", fake)
    monkeypatch.setattr(sharing, "get_agent_container", lambda *_a, **_k: object())
    monkeypatch.setattr(sharing, "manager", None)

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(sharing.platform_audit_service, "log", _no_audit)
    request = SimpleNamespace(client=None, scope={"path": "/api/agents/atlas/share"}, state=SimpleNamespace())
    owner = SimpleNamespace(username="owner", agent_name=None, connector_agent=None)
    return sharing, fake, request, owner


class TestSharingWritesNoLogin:
    def test_sharing_an_agent_writes_no_whitelist_row(self, monkeypatch):
        sharing, fake, request, owner = _sharing(monkeypatch)
        from database import AgentShareRequest

        asyncio.run(sharing.share_agent_endpoint(
            "atlas", AgentShareRequest(email="client@example.com"), request, owner
        ))
        assert fake.whitelisted == []

    def test_approving_an_access_request_writes_no_whitelist_row(self, monkeypatch):
        sharing, fake, request, owner = _sharing(monkeypatch)
        from models import AccessRequestDecision

        asyncio.run(sharing.decide_access_request_endpoint(
            "atlas", "req-1", AccessRequestDecision(approve=True), request, owner
        ))
        assert fake.whitelisted == []
