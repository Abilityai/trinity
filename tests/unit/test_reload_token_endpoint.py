"""
Unit tests for the agent-server hot-reload-token endpoint (#1089).

``POST /api/credentials/reload-token`` mutates the live agent-server process
env so the NEXT claude subprocess uses the rotated subscription token (in-flight
turns keep their already-inherited old token and finish), and persists the token
to the writable-layer override (``/var/lib/trinity/oauth-token``, 0600) so it
survives a plain stop+start (F2 durability). It must NOT rewrite ``.env`` /
``.mcp.json`` or re-inject Trinity MCP — those are the destructive whole-file
flows owned by ``/api/credentials/inject`` (``/api/credentials/update`` was
the other such flow and was deleted in #2008 — it had no callers).

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
from agent_server.services import execution_env as exec_env

# Every env name the endpoint touches (#2114: ANTHROPIC_AUTH_TOKEN rides along
# with ANTHROPIC_API_KEY — same Claude key-over-OAuth precedence).
_TOUCHED_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN",) + exec_env.SUBSCRIPTION_SHADOW_KEYS


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient over the credentials router with the writable-layer override
    redirected to a tmp path (the host has no /var/lib/trinity), the sanitizer
    refresh stubbed (don't read the host ~/.env), the .env parse stubbed empty
    (#2114 env_shadow must not depend on the host filesystem), and the MCP
    re-inject spied so we can assert the destructive whole-file flow is never
    triggered."""
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
    # Deterministic .env view for env_shadow — per-test overrides re-set this.
    monkeypatch.setattr(cred_router, "parse_env_file", lambda: {})

    # The endpoint mutates os.environ directly (not via monkeypatch); snapshot
    # the keys it touches and restore them so nothing leaks across tests.
    saved = {k: os.environ.get(k) for k in _TOUCHED_KEYS}

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
        # #2114: the endpoint also mutates PACKAGE-module state on the imported
        # agent_server.services.execution_env — runtime overrides and the
        # suppression-warn memo. Left dirty, a later test importing the package
        # module (rather than a fresh _load copy) inherits a force-unset that
        # silently changes its spawn env — order-dependent flake material.
        for k in _TOUCHED_KEYS:
            exec_env.clear_runtime_override(k)
        exec_env._SPAWN_SUPPRESS_WARNED.clear()


def test_reload_sets_env_and_writes_durable_override(client):
    """Happy path: env mutated for the next subprocess + durable override
    written 0600 + sanitizer refreshed + NO destructive MCP re-inject."""
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    resp = client.post(
        "/api/credentials/reload-token", json={"token": "sk-ant-oat01-rotated"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "reloaded": True, "env_shadow": []}
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
    """#2114: True pops BOTH API-key-style Claude auth names from the process
    env AND arms force-unset overrides so the next spawn cannot re-inherit
    them from `.env`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-go")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-auth-should-go")

    resp = client.post(
        "/api/credentials/reload-token",
        json={"token": "tok", "remove_api_key": True},
    )

    assert resp.status_code == 200
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    # The spawn layer sees the force-unset (None) — this, not the os.environ
    # pop, is what reaches build_execution_env post-#1999.
    overrides = exec_env.runtime_overrides()
    assert overrides["ANTHROPIC_API_KEY"] is None
    assert overrides["ANTHROPIC_AUTH_TOKEN"] is None
    # Nothing in .env (fixture stubs the parse empty) → nothing shadowed.
    assert resp.json()["env_shadow"] == []


def test_remove_api_key_reports_env_shadow_names_only(client, monkeypatch):
    """#2114: when the CURRENT .env still carries a force-unset key, the
    response names it (never its value) so the backend can log the shadow at
    switch time — the durable operator signal."""
    monkeypatch.setattr(
        cred_router,
        "parse_env_file",
        lambda: {"ANTHROPIC_API_KEY": "sk-ant-api-stale-value", "UNRELATED": "x"},
    )

    resp = client.post(
        "/api/credentials/reload-token",
        json={"token": "tok", "remove_api_key": True},
    )

    assert resp.status_code == 200
    assert resp.json()["env_shadow"] == ["ANTHROPIC_API_KEY"]
    assert "sk-ant-api-stale-value" not in resp.text  # names only, never values


def test_remove_api_key_defaults_false_preserves_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-stays")

    resp = client.post("/api/credentials/reload-token", json={"token": "tok"})

    assert resp.status_code == 200
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api-stays"
    assert resp.json()["env_shadow"] == []


def test_empty_token_returns_400(client):
    resp = client.post("/api/credentials/reload-token", json={"token": ""})
    assert resp.status_code == 400
