"""Unit tests for the agent-server auth middleware (#1159).

The middleware enforces the per-agent ``X-Trinity-Agent-Token`` on every route —
HTTP **and** WebSocket — except the Docker ``/health`` probe and CORS
``OPTIONS`` preflight, with a grace path (empty token env → allow) so a fleet
still on the old base image keeps working until the operator-timed recreate
flips enforcement on.

WebSocket coverage is load-bearing: ``/ws/chat`` runs ``runtime.execute`` (i.e.
arbitrary Claude), and the original ``BaseHTTPMiddleware`` form could not see
WebSocket scopes — leaving that route unauthenticated. The pure-ASGI middleware
gates it; these tests are the regression guard for that bypass.

Loaded by file path (not ``import agent_server``) so the test stays hermetic —
the package ``__init__`` pulls in the whole agent-server import graph.
"""
import importlib.util
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect


_AUTH_FILE = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "agent_server"
    / "middleware"
    / "auth.py"
)


def _load_middleware():
    spec = importlib.util.spec_from_file_location(
        "agent_auth_middleware_under_test", str(_AUTH_FILE)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AgentAuthMiddleware


AgentAuthMiddleware = _load_middleware()

TOKEN = "deadbeef" * 8  # a derived-token-shaped value


def _build_app():
    async def info(request):
        return JSONResponse({"ok": True})

    async def health(request):
        return JSONResponse({"status": "healthy"})

    async def ws_chat(websocket: WebSocket):
        # Mirrors the agent server's /ws/chat: accept, echo, close.
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/api/agent/info", info, methods=["GET", "OPTIONS"]),
            Route("/health", health, methods=["GET"]),
            WebSocketRoute("/ws/chat", ws_chat),
        ]
    )
    app.add_middleware(AgentAuthMiddleware)
    return app


@pytest.fixture
def client():
    return TestClient(_build_app())


def test_grace_no_env_allows(monkeypatch, client):
    """Empty token env → allow (old image / pre-injection rollout grace)."""
    monkeypatch.delenv("TRINITY_AGENT_AUTH_TOKEN", raising=False)
    assert client.get("/api/agent/info").status_code == 200


def test_grace_empty_env_allows(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", "")
    assert client.get("/api/agent/info").status_code == 200


def test_enforced_missing_header_401(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    assert client.get("/api/agent/info").status_code == 401


def test_enforced_wrong_header_401(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    resp = client.get(
        "/api/agent/info", headers={"X-Trinity-Agent-Token": "wrong-token"}
    )
    assert resp.status_code == 401


def test_enforced_correct_header_200(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    resp = client.get(
        "/api/agent/info", headers={"X-Trinity-Agent-Token": TOKEN}
    )
    assert resp.status_code == 200


def test_health_exempt_even_when_enforced(monkeypatch, client):
    """Docker healthcheck has no token; /health must stay reachable."""
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    assert client.get("/health").status_code == 200


def test_options_preflight_bypasses(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    resp = client.options("/api/agent/info")
    assert resp.status_code != 401


# --- WebSocket gating (the BaseHTTPMiddleware-bypass regression guard) --------


def test_ws_grace_no_env_allows(monkeypatch, client):
    """Empty token env → WS allowed (old image / rollout grace)."""
    monkeypatch.delenv("TRINITY_AGENT_AUTH_TOKEN", raising=False)
    with client.websocket_connect("/ws/chat") as ws:
        assert ws.receive_json() == {"ok": True}


def test_ws_enforced_missing_token_rejected(monkeypatch, client):
    """Enforced + no token → the sibling cannot open /ws/chat (run Claude)."""
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat"):
            pass


def test_ws_enforced_wrong_token_rejected(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/chat", headers={"X-Trinity-Agent-Token": "wrong-token"}
        ):
            pass


def test_ws_enforced_correct_token_accepted(monkeypatch, client):
    monkeypatch.setenv("TRINITY_AGENT_AUTH_TOKEN", TOKEN)
    with client.websocket_connect(
        "/ws/chat", headers={"X-Trinity-Agent-Token": TOKEN}
    ) as ws:
        assert ws.receive_json() == {"ok": True}


def test_chat_router_has_no_websocket_route():
    """Regression (#1159): the agent-server /ws/chat endpoint ran unauthenticated
    arbitrary Claude execution and was deleted. The pure-ASGI middleware now gates
    websocket scopes, but /ws/chat must not reappear — during the rollout grace
    window (token env not yet injected) the middleware allows everything, so a live
    code-exec WS route would still be open on every un-recreated agent."""
    chat_src = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "base-image"
        / "agent_server"
        / "routers"
        / "chat.py"
    ).read_text()
    assert ".websocket(" not in chat_src, (
        "agent-server chat router must expose no websocket routes — /ws/chat ran "
        "unauthenticated arbitrary Claude execution reachable by any sibling"
    )
