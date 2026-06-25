"""
Unit tests for the agent-server hot-reload-token endpoint (#1089).

``POST /api/credentials/reload-token`` mutates the live agent-server process
env so the NEXT claude subprocess uses the rotated subscription token (in-flight
turns keep their already-inherited old token and finish), and persists the token
to the writable-layer override (``/var/lib/trinity/oauth-token``, 0600) so it
survives a plain stop+start (F2 durability). It must NOT rewrite ``.env`` /
``.mcp.json`` or re-inject Trinity MCP — those are the destructive whole-file
flows owned by ``/api/credentials/update`` and ``/api/credentials/inject``.

Module: docker/base-image/agent_server/routers/credentials.py

`agent_server` is registered as a namespace package by tests/unit/conftest.py
(``_preload_real_agent_server``), so the real base-image router imports directly.
"""

import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_server.routers import credentials as cred_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient over the credentials router with the writable-layer override
    redirected to a tmp path (the host has no /var/lib/trinity), the sanitizer
    refresh stubbed (don't read the host ~/.env), and the MCP re-inject spied so
    we can assert the destructive whole-file flow is never triggered."""
    override = tmp_path / "oauth-token"
    monkeypatch.setattr(cred_router, "_TOKEN_OVERRIDE", override)

    refresh_calls: list[int] = []
    monkeypatch.setattr(
        cred_router, "refresh_credential_values", lambda: refresh_calls.append(1)
    )
    inject_calls: list[int] = []
    monkeypatch.setattr(
        cred_router,
        "inject_trinity_mcp_if_configured",
        lambda: (inject_calls.append(1), False)[1],
    )

    # The endpoint mutates os.environ directly (not via monkeypatch); snapshot
    # the two keys it touches and restore them so nothing leaks across tests.
    saved = {k: os.environ.get(k) for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}

    app = FastAPI()
    app.include_router(cred_router.router)
    c = TestClient(app)
    c._override = override  # type: ignore[attr-defined]
    c._refresh_calls = refresh_calls  # type: ignore[attr-defined]
    c._inject_calls = inject_calls  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_reload_sets_env_and_writes_durable_override(client):
    """Happy path: env mutated for the next subprocess + durable override
    written 0600 + sanitizer refreshed + NO destructive MCP re-inject."""
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    resp = client.post(
        "/api/credentials/reload-token", json={"token": "sk-ant-oat01-rotated"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "reloaded": True}
    # env mutated so the NEXT claude subprocess inherits the new token
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-rotated"
    # durable override written (survives a plain stop+start) with 0600 perms
    assert client._override.read_text() == "sk-ant-oat01-rotated"
    assert stat.S_IMODE(client._override.stat().st_mode) == 0o600
    # sanitizer redaction set refreshed; the whole-file MCP re-inject NOT done
    assert client._refresh_calls == [1]
    assert client._inject_calls == []


def test_override_retightened_when_preexisting_world_readable(client):
    """#1089 hardening: if the override already exists with loose perms (e.g.
    0644 left by an older write path or tampering), a reload re-tightens it to
    0600. ``os.open(..., 0o600)`` only applies its mode on *creation* — for an
    existing file the mode arg is ignored — so the atomic create is paired with
    an fchmod to enforce 0600 on the existing fd too (the old write_text()+chmod()
    always re-tightened; the os.open() refinement must not silently lose that)."""
    client._override.write_text("stale-token")
    client._override.chmod(0o644)

    resp = client.post(
        "/api/credentials/reload-token", json={"token": "sk-ant-oat01-retighten"}
    )

    assert resp.status_code == 200
    assert client._override.read_text() == "sk-ant-oat01-retighten"
    assert stat.S_IMODE(client._override.stat().st_mode) == 0o600


def test_reload_does_not_write_env_or_other_files(client):
    """The endpoint writes ONLY the override — no sibling .env / .mcp.json
    (proves it is not reusing the destructive /update or /inject flow)."""
    client.post("/api/credentials/reload-token", json={"token": "tok"})

    siblings = {p.name for p in client._override.parent.iterdir()}
    assert siblings == {"oauth-token"}


def test_remove_api_key_true_pops_anthropic_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-go")

    resp = client.post(
        "/api/credentials/reload-token",
        json={"token": "tok", "remove_api_key": True},
    )

    assert resp.status_code == 200
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_remove_api_key_defaults_false_preserves_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-stays")

    resp = client.post("/api/credentials/reload-token", json={"token": "tok"})

    assert resp.status_code == 200
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api-stays"


def test_empty_token_returns_400(client):
    resp = client.post("/api/credentials/reload-token", json={"token": ""})
    assert resp.status_code == 400
