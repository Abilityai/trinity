"""Scope fences must read the routed path, never the Host-rebuilt URL (#3102).

Starlette rebuilds `request.url` from the `Host` header, so a Host carrying
`/`, `?` or `#` makes `request.url.path` differ from the path the router
dispatched. Every fence here is driven with a REAL `starlette.requests.Request`
whose Host smuggles an allowlisted path while the routed path is forbidden.
The older fence tests use `SimpleNamespace` fakes and TestClient, neither of
which can express that desync, which is why none of them caught it.
"""
import ast
import asyncio
import pathlib
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"


def _crafted(method: str, routed: str, shown: str) -> Request:
    """A request routed to `routed` whose Host makes `request.url.path == shown`."""
    req = Request({
        "type": "http", "method": method, "path": routed, "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", f"backend{shown}?".encode())],
    })
    assert req.url.path == shown, "precondition: the Host header must desync url.path"
    return req


def _honest(method: str, path: str) -> Request:
    return Request({
        "type": "http", "method": method, "path": path, "root_path": "",
        "query_string": b"", "scheme": "http", "server": ("backend", 8000),
        "headers": [(b"host", b"backend:8000")],
    })


def _expect_403(fn, *args):
    with pytest.raises(HTTPException) as exc:
        fn(*args)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# The five fences
# --------------------------------------------------------------------------- #

def test_ephemeral_fence_reads_routed_path(monkeypatch):
    import dependencies as deps
    monkeypatch.setattr(deps.db, "get_agent_ephemeral_info", lambda n: {"is_ephemeral": True})
    fence = deps._enforce_ephemeral_key_fence
    fence(_honest("GET", "/api/agents/ghost-a/info"), "ghost-a")  # control: allowed
    _expect_403(fence, _crafted("GET", "/api/agents/sibling/files", "/api/agents/ghost-a/info"), "ghost-a")


def test_ops_fence_reads_routed_path():
    import dependencies as deps
    fence = deps._enforce_ops_key_fence
    fence(_honest("GET", "/api/version"))  # control: allowed
    _expect_403(fence, _crafted("GET", "/api/settings", "/api/version"))


@pytest.fixture
def mcp_key(monkeypatch):
    import dependencies as deps

    def use(scope, agent_name=None):
        monkeypatch.setattr(deps.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": scope, "agent_name": agent_name,
            "user_id": "owner", "user_email": "owner@example.com",
        })
        monkeypatch.setattr(deps.db, "get_user_by_email", lambda *_a, **_k: {
            "id": 1, "username": "owner", "email": "owner@example.com", "role": "admin",
        })
        return lambda req: asyncio.run(deps.get_current_user(req, token="trinity_mcp_fake"))

    return use


def test_connector_fence_reads_routed_path(mcp_key):
    call = mcp_key("connector", "agent-1")
    call(_honest("POST", "/api/agents/agent-1/chat"))  # control: allowed
    _expect_403(call, _crafted("POST", "/api/agents/agent-2/chat", "/api/agents/agent-1/chat"))


def test_portal_delegate_fence_reads_routed_path(mcp_key):
    from dependencies import PORTAL_DELEGATE_SCOPE
    call = mcp_key(PORTAL_DELEGATE_SCOPE)
    exchange = "/api/enterprise/client-portal/auth/exchange"
    call(_honest("POST", exchange))  # control: allowed
    _expect_403(call, _crafted("POST", "/api/agents", exchange))


def test_event_loopback_fence_reads_routed_path(monkeypatch):
    import dependencies as deps
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    monkeypatch.setattr(deps.db, "get_user_by_username", lambda *_a, **_k: {
        "id": 1, "username": "admin", "email": "admin@example.com", "role": "admin",
        "suspended_at": None,
    })
    monkeypatch.setattr(deps, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {"sub": "admin", "scope": deps.EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    call = lambda req: asyncio.run(deps.get_current_user(req, token=token))  # noqa: E731
    call(_honest("POST", "/api/agents/orch/task"))  # control: allowed
    _expect_403(call, _crafted("POST", "/api/users", "/api/agents/orch/task"))


# --------------------------------------------------------------------------- #
# Host header guard (defense in depth, covers HTTP and WebSocket)
# --------------------------------------------------------------------------- #

def _guarded_client():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route, WebSocketRoute
    from utils.host_header import HostHeaderGuard

    async def ok(_request):
        return PlainTextResponse("ok")

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    app = Starlette(routes=[Route("/x", ok), WebSocketRoute("/ws", ws)])
    app.add_middleware(HostHeaderGuard)
    return TestClient(app)


@pytest.mark.parametrize("host", [
    "backend/api/agents/a/chat?", "backend?x", "backend#x", "backend\\x",
    "user@backend", "back end", "backend\t", "backend\x7f",
])
def test_guard_rejects_path_shifting_host(host):
    resp = _guarded_client().get("/x", headers={"host": host})
    assert resp.status_code == 400


@pytest.mark.parametrize("host", [
    "backend:8000", "localhost", "127.0.0.1:8000", "[::1]:8000", "Your-Domain.com", "",
])
def test_guard_passes_real_hosts(host):
    resp = _guarded_client().get("/x", headers={"host": host})
    assert resp.status_code == 200


def test_guard_passes_missing_host():
    from utils.host_header import HostHeaderGuard
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(HostHeaderGuard(app)({"type": "http", "headers": []}, None, None))
    assert seen == ["http"]


def test_guard_closes_websocket_with_crafted_host():
    from starlette.websockets import WebSocketDisconnect
    client = _guarded_client()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"host": "backend/x?"}) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    with client.websocket_connect("/ws", headers={"host": "backend:8000"}) as ws:
        assert ws.receive_text() == "ok"


def test_main_installs_guard_outermost():
    """Starlette's `add_middleware` prepends, so the LAST call runs first."""
    tree = ast.parse((BACKEND / "main.py").read_text())
    added = [
        (n.args[0].id if isinstance(n.args[0], ast.Name) else None)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_middleware" and n.args
    ]
    assert added and added[-1] == "HostHeaderGuard", added
    decorators = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and any(getattr(getattr(d, "func", None), "attr", None) == "middleware"
                          for d in n.decorator_list)]
    last_add = max(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "add_middleware")
    assert all(d.lineno < last_add for d in decorators), \
        "an @app.middleware registered after the guard would run before it"


# --------------------------------------------------------------------------- #
# Static guard: nothing in the backend reads `<x>.url.path`
# --------------------------------------------------------------------------- #

def test_no_backend_code_reads_url_path():
    hits = []
    for f in BACKEND.rglob("*.py"):
        if "enterprise" in f.relative_to(BACKEND).parts:
            continue  # private submodule owns its twin guard
        for node in ast.walk(ast.parse(f.read_text(), str(f))):
            if (isinstance(node, ast.Attribute) and node.attr == "path"
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "url"):
                hits.append(f"{f.relative_to(BACKEND)}:{node.lineno}")
    assert not hits, (
        "request.url is rebuilt from the Host header; read request.scope['path'] "
        f"(the routed path) instead: {hits}"
    )
