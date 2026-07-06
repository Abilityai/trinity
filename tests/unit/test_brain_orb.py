"""Unit tests for the Brain Orb static-render foundation (#58, trinity-enterprise).

Two surfaces, both mounted on a minimal FastAPI app and driven via TestClient
(real routing + dependency injection), with Docker / agent-HTTP mocked:

  * backend proxy  — routers/agent_brain_orb.py  (flag gate, authz, proxy + error map)
  * agent-server   — docker/base-image/agent_server/routers/brain_orb.py (file read)

These are true unit tests: no Docker daemon, no running backend, no agent
container. The full data path (real container export) is covered by /verify.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import types
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routers.agent_brain_orb as bo
from dependencies import get_authorized_agent_by_name, get_current_user, get_owned_agent_by_name

_AGENT = "cornelius"


# --- fakes -----------------------------------------------------------------

def _running():
    return types.SimpleNamespace(status="running", labels={})


def _stopped():
    return types.SimpleNamespace(status="exited", labels={})


class _FakeClientCM:
    """Stands in for `async with agent_httpx_client(...) as client`."""

    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc

    async def __aenter__(self):
        client = AsyncMock()
        # The proxy calls client.request(method, url, content=...) for all routes.
        if self._exc is not None:
            client.request = AsyncMock(side_effect=self._exc)
        else:
            client.request = AsyncMock(return_value=self._result)
        return client

    async def __aexit__(self, *_a):
        return False


def _fake_httpx(*, result=None, exc=None):
    def _factory(*_args, **_kwargs):
        return _FakeClientCM(result=result, exc=exc)
    return _factory


def _resp(status_code: int, content: bytes = b""):
    return types.SimpleNamespace(status_code=status_code, content=content)


# --- backend proxy fixture -------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(bo.router)
    # Read routes use AuthorizedAgentByName; the mutating /scope uses OwnedAgentByName.
    app.dependency_overrides[get_authorized_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_owned_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(id=7, username="u", email="u@example.com")
    # Flags ON by default; individual tests flip them off. Since #85 the gates
    # are runtime resolvers imported into the router's namespace — patch the
    # module-level names (the DB-backed resolution itself is covered by
    # test_85_brain_orb_settings.py). container_reload is async.
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: True)
    monkeypatch.setattr(bo, "is_brain_orb_voice_enabled", lambda: True)   # #60 Phase 3
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: True)   # #61 Phase 4a
    monkeypatch.setattr(bo, "GEMINI_API_KEY", "test-key")      # #60 Phase 3
    monkeypatch.setattr(bo, "container_reload", AsyncMock())
    # #61 Phase 4a: the voice-token route resolves owner-ness for can_write. Default
    # to owner=True (fixture caller is the owner); shared-user tests override to False.
    monkeypatch.setattr(bo.db, "can_user_share_agent", lambda *a, **k: True)
    return TestClient(app, raise_server_exceptions=True)


_URL = f"/api/agents/{_AGENT}/brain-orb/data"


# --- backend proxy: gating -------------------------------------------------

def test_flag_off_returns_404(client, monkeypatch):
    """Platform flag is the single source of truth — off ⇒ 404, never a 5xx."""
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_agent_not_found_returns_404(client):
    with patch.object(bo, "get_agent_container", return_value=None):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "Agent not found" in r.json()["detail"]


def test_agent_stopped_returns_503(client):
    with patch.object(bo, "get_agent_container", return_value=_stopped()):
        r = client.get(_URL)
    assert r.status_code == 503


# --- backend proxy: happy path + pass-through ------------------------------

def test_success_passes_bytes_through(client):
    payload = b'{"nodes":[{"id":"n1"}],"edges":[]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_URL)
    assert r.status_code == 200
    assert r.content == payload  # byte-identical, never re-serialized
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers.get("cache-control") == "no-store"


# --- backend proxy: error mapping ------------------------------------------

def test_agent_404_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_agent_500_maps_to_502(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(500, b"boom"))
    ):
        r = client.get(_URL)
    assert r.status_code == 502


def test_connect_error_maps_to_503(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(exc=httpx.ConnectError("down"))
    ):
        r = client.get(_URL)
    assert r.status_code == 503


def test_timeout_maps_to_504(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(exc=httpx.TimeoutException("slow"))
    ):
        r = client.get(_URL)
    assert r.status_code == 504


# --- backend proxy: scopes (read) + scope (owner mutation) — #58 Phase 2 ----

_SCOPES_URL = f"/api/agents/{_AGENT}/brain-orb/scopes"
_SCOPE_URL = f"/api/agents/{_AGENT}/brain-orb/scope"


def test_scopes_success_passes_through(client):
    payload = b'{"active":["core"],"available":[{"token":"core"}]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 200
    assert r.json() == {"active": ["core"], "available": [{"token": "core"}]}


def test_scopes_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 404


def test_scopes_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 404


def test_scope_post_success_passes_through(client):
    payload = b'{"ok":true,"active":["core","Books"],"nodes":1200,"edges":3000}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.post(_SCOPE_URL, json={"tokens": ["core", "Books"]})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["active"] == ["core", "Books"]


def test_scope_post_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.post(_SCOPE_URL, json={"tokens": []})
    assert r.status_code == 404


def test_scope_post_body_too_large_413(client):
    # > 64 KiB raw body — rejected before any agent call (no patches needed).
    r = client.post(_SCOPE_URL, json={"tokens": ["x" * 70_000]})
    assert r.status_code == 413


def test_scope_post_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.post(_SCOPE_URL, json={"tokens": []})
    assert r.status_code == 404


# --- backend proxy: voice-token mint (#60 Phase 3) -------------------------

_VOICE_TOKEN_URL = f"/api/agents/{_AGENT}/brain-orb/voice-token"


def test_voice_token_success(client):
    """Happy path: mints via the service, passes the body through no-store, and the
    token field is NOT named `token` (would flip orb.js's Phase-4 write surface on)."""
    minted = {
        "ephemeral_token": "auth_tokens/abc123",
        "model": "models/gemini-3.1-flash-live-preview",
        "voice_name": "Kore",
        "expires_at": "2026-07-01T00:05:00+00:00",
        "tools": ["highlight_related_notes", "mount_scope"],
    }
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce") as enforce, patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(return_value=minted)
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["ephemeral_token"] == "auth_tokens/abc123"
    assert "token" not in body  # F1: never a bare `token` field
    assert body["tools"] == ["highlight_related_notes", "mount_scope"]
    assert r.headers.get("cache-control") == "no-store"
    enforce.assert_called_once()  # per-(user,agent) mint budget is enforced


def test_voice_token_flag_off_404(client, monkeypatch):
    """Voice flag off ⇒ 404 even with the base flag on."""
    monkeypatch.setattr(bo, "is_brain_orb_voice_enabled", lambda: False)
    r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 404


def test_voice_token_base_flag_off_404(client, monkeypatch):
    """#85: base OFF ⇒ mint 404 even with voice ON — a disabled orb must not
    let direct API calls spin up Gemini Live sessions on the platform key."""
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 404


def test_voice_token_no_key_503(client, monkeypatch):
    monkeypatch.setattr(bo, "GEMINI_API_KEY", "")
    r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 503


def test_voice_token_mint_failure_502(client):
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce"), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(side_effect=RuntimeError("gemini boom"))
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 502


def test_voice_token_value_error_maps_503(client):
    """Service raising ValueError (no key surfaced late) → 503, never a 500."""
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce"), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(side_effect=ValueError("no key"))
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 503


def test_voice_token_rate_limited_429(client):
    """Over the per-user mint budget → 429 (enforce raises), before any mint."""
    from fastapi import HTTPException as _HE
    mint = AsyncMock()
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce", side_effect=_HE(status_code=429, detail="slow down")), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", mint
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 429
    mint.assert_not_called()  # rate gate fires before the mint


# --- backend proxy: read-only /tool search broker (#60 Phase 3) ------------

_TOOL_URL = f"/api/agents/{_AGENT}/brain-orb/tool"


def test_tool_success_passes_through(client):
    payload = b'{"results":[{"title":"Dopamine","content":"..."}]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.post(_TOOL_URL, json={"query": "dopamine"})
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "Dopamine"


def test_tool_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.post(_TOOL_URL, json={"query": "x"})
    assert r.status_code == 404


def test_tool_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.post(_TOOL_URL, json={"query": "x"})
    assert r.status_code == 404


def test_tool_body_too_large_413(client):
    r = client.post(_TOOL_URL, json={"query": "x" * 20_000})
    assert r.status_code == 413


# --- agent-server routes: data read + scope hooks --------------------------

def _write_hook(path, script: str):
    """Write an executable convention hook (shebang-selected) for the agent-server."""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    from agent_server.routers import brain_orb as asbo
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(asbo, "DATA_PATH", tmp_path / "data.json")
    monkeypatch.setattr(asbo, "_SCOPES_HOOK", hooks / "scopes")
    monkeypatch.setattr(asbo, "_SCOPE_HOOK", hooks / "scope")
    monkeypatch.setattr(asbo, "_SEARCH_HOOK", hooks / "search")   # #60 Phase 3
    monkeypatch.setattr(asbo, "_ACTION_HOOK", hooks / "action")   # #61 Phase 4a
    monkeypatch.setattr(asbo, "_POSTPROCESS_CONFIG", hooks / "voice-postprocess.json")   # #73
    monkeypatch.setattr(asbo, "_POSTPROCESS_MD", hooks / "voice-postprocess.md")         # #73
    monkeypatch.setattr(asbo, "_HOME", tmp_path)   # subprocess cwd must exist on the test host
    app = FastAPI()
    app.include_router(asbo.router)
    # NB: AgentAuthMiddleware intentionally omitted — covered by its own tests;
    # here we exercise the route read / hook-exec logic in isolation.
    return types.SimpleNamespace(
        client=TestClient(app), asbo=asbo, data=tmp_path / "data.json",
        scopes_hook=hooks / "scopes", scope_hook=hooks / "scope",
        search_hook=hooks / "search", action_hook=hooks / "action",
    )


def test_agent_server_serves_data_when_present(agent_env):
    agent_env.data.write_text('{"ok":1}')
    r = agent_env.client.get("/api/brain-orb/data")
    assert r.status_code == 200
    assert r.json() == {"ok": 1}
    assert r.headers["content-type"].startswith("application/json")


def test_agent_server_404_when_absent(agent_env):
    r = agent_env.client.get("/api/brain-orb/data")
    assert r.status_code == 404


def test_agent_server_scopes_present(agent_env):
    _write_hook(agent_env.scopes_hook,
                '#!/bin/sh\necho \'{"active":["core"],"available":[{"token":"core"}]}\'\n')
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 200
    assert r.json() == {"active": ["core"], "available": [{"token": "core"}]}


def test_agent_server_scopes_absent_404(agent_env):
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 404


def test_agent_server_scope_forwards_stdin(agent_env):
    # The hook echoes the received stdin body back, proving forwarding end-to-end.
    _write_hook(agent_env.scope_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"ok\\":true,\\"received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": ["core", "Books"]})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["received"] == {"tokens": ["core", "Books"]}


def test_agent_server_scope_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 404


def test_agent_server_scope_invalid_json_502(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\necho "not json at all"\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 502


def test_agent_server_scope_nonzero_exit_502(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\necho "{}"\nexit 3\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 502


def test_run_hook_timeout_504(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\nsleep 5\necho "{}"\n')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(agent_env.asbo._run_hook(agent_env.scope_hook, timeout=0.5))
    assert ei.value.status_code == 504


# --- agent-server: read-only search hook (#60 Phase 3) ---------------------

def test_agent_server_search_forwards_stdin(agent_env):
    # Echoes the received query back, proving the read-only search hook forwards.
    _write_hook(agent_env.search_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"results\\":[],\\"query_received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "dopamine"})
    assert r.status_code == 200
    out = r.json()
    assert out["query_received"] == {"query": "dopamine"}


def test_agent_server_search_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 404


def test_agent_server_search_invalid_json_502(agent_env):
    _write_hook(agent_env.search_hook, '#!/bin/sh\necho "not json"\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 502


def test_agent_server_search_nonzero_exit_502(agent_env):
    _write_hook(agent_env.search_hook, '#!/bin/sh\necho "{}"\nexit 2\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 502


# --- mint service: v1alpha client + locked constraints (#60 Phase 3) -------

def test_mint_service_uses_v1alpha_and_locks_config(monkeypatch):
    """The mint must build its OWN v1alpha client (not the voice singleton) and
    lock the model + config, and return the token under `ephemeral_token`."""
    import services.brain_orb_voice_service as svc

    captured = {}

    class _FakeAuthTokens:
        def create(self, *, config):
            captured["config"] = config
            return types.SimpleNamespace(name="auth_tokens/xyz")

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs
            self.auth_tokens = _FakeAuthTokens()

    monkeypatch.setattr(svc, "_client", None)  # reset the module singleton
    monkeypatch.setattr(svc, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(svc.genai, "Client", _FakeClient)

    out = asyncio.run(svc.mint_voice_token("cornelius", voice_name="Kore", agent_prompt=None))

    # Own client built with api_version='v1alpha' (the singleton-reuse bug guard).
    http_options = captured["client_kwargs"]["http_options"]
    assert http_options.api_version == "v1alpha"
    # Constraints lock the model + a config carrying the (non-write) tool surface.
    cfg = captured["config"]
    assert cfg.uses == 1
    assert cfg.live_connect_constraints.model == svc.VOICE_MODEL
    tool_names = {fd.name for t in cfg.live_connect_constraints.config.tools for fd in t.function_declarations}
    assert "mount_scope" in tool_names
    assert "capture_note" not in tool_names        # F3: no write tools in the manifest
    assert "find_connections" not in tool_names
    # Token surfaced under `ephemeral_token`, never `token` (F1).
    assert out["ephemeral_token"] == "auth_tokens/xyz"
    assert "token" not in out


# ==========================================================================
# Phase 4a — owner-gated KB writes (capture + link) — trinity-enterprise#61
# ==========================================================================

_ACTIONS_URL = f"/api/agents/{_AGENT}/brain-orb/actions"
_ACTION_URL = f"/api/agents/{_AGENT}/brain-orb/action"


def _deny_owner(client):
    """Flip the OwnedAgentByName gate to 403 for one test (function-scoped app)."""
    def _raise():
        raise HTTPException(status_code=403, detail="Owner access required")
    client.app.dependency_overrides[get_owned_agent_by_name] = _raise


# --- backend /actions (read the write-surface capability) ------------------

def test_actions_get_success_passes_through(client):
    payload = b'{"enabled":true,"skills":[]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_ACTIONS_URL)
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "skills": []}


def test_actions_write_flag_off_404(client, monkeypatch):
    """Distinct kill-switch: BRAIN_ORB_WRITE_ENABLED off ⇒ 404 even though the base
    render flag is on (writes disabled without downing read/voice)."""
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.get(_ACTIONS_URL)
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_actions_no_hook_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.get(_ACTIONS_URL)
    assert r.status_code == 404


def test_actions_owner_gate_403(client):
    _deny_owner(client)
    r = client.get(_ACTIONS_URL)
    assert r.status_code == 403


# --- backend /action (capture + link) --------------------------------------

def test_action_capture_success_and_audited(client):
    payload = b'{"ok":true,"title":"a thought","path":"00-Inbox/a.md"}'
    audit = AsyncMock()
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, payload))), patch.object(
        bo.platform_audit_service, "log", audit
    ):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "a thought"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_action"] == "brain_orb_capture"


def test_action_link_success(client):
    payload = b'{"ok":true,"already":false}'
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, payload))), patch.object(
        bo.platform_audit_service, "log", AsyncMock()
    ):
        r = client.post(_ACTION_URL, json={"action": "link", "from": "A", "to": "B"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_action_unsupported_verb_rejected_400(client):
    """run_skill (arbitrary exec) remains out of scope — rejected at the boundary,
    never forwarded to the agent hook. Transcript verbs are covered separately."""
    agent_req = AsyncMock()
    for verb in ("run_skill", "delete", ""):
        with patch.object(bo, "_agent_request", agent_req):
            r = client.post(_ACTION_URL, json={"action": verb, "skill": "x"})
        assert r.status_code == 400, verb
    agent_req.assert_not_called()


def test_action_transcript_verbs_forwarded(client):
    """#66 — capture_transcript + process_transcript are accepted and forwarded to
    the agent hook (owner-gated, audited)."""
    for verb, payload in (
        ("capture_transcript", {"action": "capture_transcript", "session_id": "s1",
                                 "events": [{"event": "user_turn", "text": "hi"}]}),
        ("process_transcript", {"action": "process_transcript", "path": "~/x.md"}),
    ):
        agent_req = AsyncMock(return_value=_resp(200, b'{"ok":true}'))
        with patch.object(bo, "_agent_request", agent_req), patch.object(
            bo.platform_audit_service, "log", AsyncMock()
        ):
            r = client.post(_ACTION_URL, json=payload)
        assert r.status_code == 200, verb
        agent_req.assert_awaited_once()


_REFRESH_URL = f"/api/agents/{_AGENT}/brain-orb/refresh"


def test_refresh_success_and_audited(client):
    """#66/#67 — refresh reindexes + re-exports; owner-gated, audited, passes through."""
    audit = AsyncMock()
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true,"nodes":1073,"added_nodes":1}'))), \
         patch.object(bo.platform_audit_service, "log", audit):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 200
    assert r.json()["added_nodes"] == 1
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_action"] == "brain_orb_refresh"


def test_refresh_owner_gate_403(client):
    _deny_owner(client)
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 403
    agent_req.assert_not_called()


def test_refresh_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_enabled", lambda: False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 404


def test_refresh_write_flag_off_404(client, monkeypatch):
    """refresh drives the agent action hook, so the write kill-switch must gate it too
    (review F1) — not just BRAIN_ORB_ENABLED."""
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: False)
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 404
    agent_req.assert_not_called()   # gated before any agent call / hook exec


def test_refresh_no_hook_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ), patch.object(bo.platform_audit_service, "log", AsyncMock()):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 404


def test_agent_server_refresh_runs_action_hook(agent_env):
    # the refresh route runs the action hook with {"action":"refresh"} on stdin
    _write_hook(agent_env.action_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"ok\\":true,\\"received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/refresh")
    assert r.status_code == 200
    assert r.json()["received"] == {"action": "refresh"}


def test_agent_server_refresh_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/refresh")
    assert r.status_code == 404


# --- post-voice-processing config (#73) ---------------------------------------
_POSTPROC_URL = f"/api/agents/{_AGENT}/brain-orb/postprocess"


def test_postprocess_get_passthrough(client):
    payload = b'{"enabled":true,"prompt":"summarize this"}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_POSTPROC_URL)
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "prompt": "summarize this"}


def test_postprocess_put_success_and_audited(client):
    audit = AsyncMock()
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true,"enabled":true,"prompt":"x"}'))), \
         patch.object(bo.platform_audit_service, "log", audit):
        r = client.put(_POSTPROC_URL, json={"enabled": True, "prompt": "x"})
    assert r.status_code == 200
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_action"] == "brain_orb_postprocess_config"


def test_postprocess_owner_gate_403(client):
    _deny_owner(client)
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        assert client.get(_POSTPROC_URL).status_code == 403
        assert client.put(_POSTPROC_URL, json={"enabled": False, "prompt": ""}).status_code == 403
    agent_req.assert_not_called()


def test_postprocess_write_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: False)
    assert client.get(_POSTPROC_URL).status_code == 404
    assert client.put(_POSTPROC_URL, json={"enabled": False, "prompt": ""}).status_code == 404


def test_agent_server_postprocess_roundtrip(agent_env):
    # PUT writes the JSON config; GET reads it back (direct file I/O in the agent-server)
    r = agent_env.client.put("/api/brain-orb/postprocess", json={"enabled": True, "prompt": "hello"})
    assert r.status_code == 200 and r.json()["enabled"] is True
    g = agent_env.client.get("/api/brain-orb/postprocess")
    assert g.json() == {"enabled": True, "prompt": "hello"}


def test_agent_server_postprocess_absent_defaults(agent_env):
    # no config file → safe defaults, never 500
    g = agent_env.client.get("/api/brain-orb/postprocess")
    assert g.status_code == 200 and g.json() == {"enabled": False, "prompt": ""}


def test_action_transcript_large_body_allowed(client):
    """A whole voice conversation can exceed the 64 KiB capture/link cap — the action
    cap is raised to accommodate transcripts (#66)."""
    big = [{"event": "user_turn", "text": "x" * 200_000}]
    agent_req = AsyncMock(return_value=_resp(200, b'{"ok":true,"saved":true}'))
    with patch.object(bo, "_agent_request", agent_req), patch.object(
        bo.platform_audit_service, "log", AsyncMock()
    ):
        r = client.post(_ACTION_URL, json={"action": "capture_transcript",
                                           "session_id": "s2", "events": big})
    assert r.status_code == 200


def test_action_write_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: False)
    r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"})
    assert r.status_code == 404


def test_action_body_too_large_413(client):
    # > 1 MiB raw body — rejected before any agent call / parse (cap raised for
    # transcripts in #66; capture/link bodies are naturally tiny).
    r = client.post(_ACTION_URL, json={"action": "capture", "body": "x" * 1_100_000})
    assert r.status_code == 413


def test_action_invalid_json_400(client):
    r = client.post(_ACTION_URL, data=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_action_owner_gate_403(client):
    _deny_owner(client)
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"})
    assert r.status_code == 403
    agent_req.assert_not_called()   # owner gate fires before any forward


def test_action_rate_limited_429_and_claim_released(client):
    """Over the per-(user,agent,action) budget → 429; the in-flight idempotency claim
    is released (fail) so a later retry isn't wedged."""
    from fastapi import HTTPException as _HE
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req), patch.object(
        bo.rate_limiter, "enforce", side_effect=_HE(status_code=429, detail="slow")
    ), patch.object(bo.idempotency_service, "fail") as fail:
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 429
    agent_req.assert_not_called()
    fail.assert_called_once()   # claim released on the 429 path


def test_action_idempotency_replay_forwards_once(client):
    """A re-POST with the same key replays the stored snapshot without a second write."""
    from services.idempotency_service import IdempotencyDecision
    fresh = IdempotencyDecision(enabled=True, replay=False, in_flight=False,
                                scope="agent:cornelius", key="k")
    replay = IdempotencyDecision(enabled=True, replay=True, in_flight=False,
                                 scope="agent:cornelius", key="k",
                                 snapshot={"ok": True, "title": "note"})
    agent_req = AsyncMock(return_value=_resp(200, b'{"ok":true,"title":"note"}'))
    with patch.object(bo, "_agent_request", agent_req), patch.object(
        bo.idempotency_service, "begin", side_effect=[fresh, replay]
    ), patch.object(bo.idempotency_service, "complete") as complete, patch.object(
        bo.platform_audit_service, "log", AsyncMock()
    ):
        r1 = client.post(_ACTION_URL, json={"action": "capture", "body": "hi"},
                         headers={"Idempotency-Key": "k"})
        r2 = client.post(_ACTION_URL, json={"action": "capture", "body": "hi"},
                         headers={"Idempotency-Key": "k"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert agent_req.call_count == 1                    # forwarded exactly once
    assert complete.call_count == 1                     # completed only the fresh claim
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r2.json() == {"ok": True, "title": "note"}


def test_action_idempotency_in_flight_409(client):
    """A concurrent duplicate (in-flight claim) → 409, never a silent second write."""
    from services.idempotency_service import IdempotencyDecision
    in_flight = IdempotencyDecision(enabled=True, replay=True, in_flight=True,
                                    scope="agent:cornelius", key="k")
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req), patch.object(
        bo.idempotency_service, "begin", return_value=in_flight
    ):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"},
                        headers={"Idempotency-Key": "k"})
    assert r.status_code == 409
    agent_req.assert_not_called()


# --- voice manifest: owner-only write tools (#61 Phase 4a) -----------------

def test_mint_service_adds_write_tools_when_can_write(monkeypatch):
    """can_write=True folds capture_note + link_notes into the locked manifest;
    run_skill stays absent (Phase 4b)."""
    import services.brain_orb_voice_service as svc
    captured = {}

    class _FakeAuthTokens:
        def create(self, *, config):
            captured["config"] = config
            return types.SimpleNamespace(name="auth_tokens/w")

    class _FakeClient:
        def __init__(self, *a, **k):
            self.auth_tokens = _FakeAuthTokens()

    monkeypatch.setattr(svc, "_client", None)
    monkeypatch.setattr(svc, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(svc.genai, "Client", _FakeClient)

    out = asyncio.run(svc.mint_voice_token("cornelius", voice_name="Kore",
                                           agent_prompt=None, can_write=True))
    cfg = captured["config"]
    names = {fd.name for t in cfg.live_connect_constraints.config.tools for fd in t.function_declarations}
    assert {"capture_note", "link_notes"} <= names   # write tools present for owners
    assert "run_skill" not in names                   # exec deferred to Phase 4b (#66)
    assert "mount_scope" in names                      # read/scope tools still there
    assert "capture_note" in out["tools"]
    # #66 — transcription is locked into the token so the client receives per-turn text.
    locked = cfg.live_connect_constraints.config
    assert locked.input_audio_transcription is not None
    assert locked.output_audio_transcription is not None


def test_voice_token_shared_user_gets_readonly_manifest(client):
    """A shared (non-owner) user's mint must pass can_write=False (no write tools),
    even though the mint route itself is AuthorizedAgentByName."""
    mint = AsyncMock(return_value={"ephemeral_token": "auth_tokens/x", "tools": []})
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.db, "can_user_share_agent", return_value=False), patch.object(
        bo.rate_limiter, "enforce"
    ), patch.object(bo.brain_orb_voice_service, "mint_voice_token", mint):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 200
    assert mint.await_args.kwargs["can_write"] is False


def test_voice_token_owner_gets_write_manifest(client):
    """An owner's mint passes can_write=True (write tools folded into the manifest)."""
    mint = AsyncMock(return_value={"ephemeral_token": "auth_tokens/x", "tools": []})
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.db, "can_user_share_agent", return_value=True), patch.object(
        bo.rate_limiter, "enforce"
    ), patch.object(bo.brain_orb_voice_service, "mint_voice_token", mint):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 200
    assert mint.await_args.kwargs["can_write"] is True


def test_voice_token_write_flag_off_forces_readonly(client, monkeypatch):
    """Even an owner gets can_write=False when the write flag is off."""
    monkeypatch.setattr(bo, "is_brain_orb_write_enabled", lambda: False)
    mint = AsyncMock(return_value={"ephemeral_token": "auth_tokens/x", "tools": []})
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.db, "can_user_share_agent", return_value=True), patch.object(
        bo.rate_limiter, "enforce"
    ), patch.object(bo.brain_orb_voice_service, "mint_voice_token", mint):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 200
    assert mint.await_args.kwargs["can_write"] is False


# --- agent-server: action hook (list + capture/link forward) ---------------

def test_agent_server_actions_list(agent_env):
    _write_hook(agent_env.action_hook,
                '#!/bin/sh\ncat >/dev/null\necho \'{"enabled":true,"skills":["find-connections"]}\'\n')
    r = agent_env.client.get("/api/brain-orb/actions")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_agent_server_actions_absent_404(agent_env):
    r = agent_env.client.get("/api/brain-orb/actions")
    assert r.status_code == 404


def test_agent_server_action_forwards_stdin(agent_env):
    _write_hook(agent_env.action_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"ok\\":true,\\"received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/action", json={"action": "capture", "body": "hi"})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["received"] == {"action": "capture", "body": "hi"}


def test_agent_server_action_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/action", json={"action": "capture", "body": "x"})
    assert r.status_code == 404


def test_agent_server_action_body_too_large_413(agent_env):
    # cap raised to 1 MiB for transcripts (#66); capture/link bodies stay tiny.
    _write_hook(agent_env.action_hook, '#!/bin/sh\ncat >/dev/null\necho "{}"\n')
    r = agent_env.client.post("/api/brain-orb/action",
                              json={"action": "capture", "body": "x" * 1_100_000})
    assert r.status_code == 413


# ==========================================================================
# Coverage gaps closed 2026-07-02 (/update-tests review)
# ==========================================================================

# --- backend: the Phase-2 mutating route gets the same owner-gate proof its
# --- Phase-4 siblings have (incomplete-fix-chain escape class) ---------------

def test_scope_post_owner_gate_403(client):
    """POST /scope is OwnedAgentByName like every other mutating brain-orb route —
    a non-owner gets 403 before any agent call (only the Phase-4 routes had this)."""
    _deny_owner(client)
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        r = client.post(_SCOPE_URL, json={"tokens": ["core"]})
    assert r.status_code == 403
    agent_req.assert_not_called()


def test_postprocess_put_body_too_large_413(client):
    """PUT /postprocess is capped at 64 KiB (review F4 — the small-config cap, not
    the 1 MiB transcript cap), rejected before any agent call."""
    agent_req = AsyncMock()
    with patch.object(bo, "_agent_request", agent_req):
        r = client.put(_POSTPROC_URL, json={"enabled": True, "prompt": "x" * 70_000})
    assert r.status_code == 413
    agent_req.assert_not_called()


# --- backend: idempotency semantics beyond the replay/409 happy paths --------

def test_action_idempotency_key_folds_verb(client):
    """The stored key folds the action verb (route docstring contract) — a client
    reusing one Idempotency-Key across verbs must claim two distinct keys, so a
    `link` can never replay a `capture` snapshot."""
    from services.idempotency_service import IdempotencyDecision
    fresh = IdempotencyDecision(enabled=True, replay=False, in_flight=False,
                                scope="agent:cornelius", key="k")
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true}'))), \
         patch.object(bo.idempotency_service, "begin", return_value=fresh) as begin, \
         patch.object(bo.idempotency_service, "complete"), \
         patch.object(bo.platform_audit_service, "log", AsyncMock()):
        client.post(_ACTION_URL, json={"action": "capture", "body": "x"},
                    headers={"Idempotency-Key": "k9"})
        client.post(_ACTION_URL, json={"action": "link", "from": "A", "to": "B"},
                    headers={"Idempotency-Key": "k9"})
    keys = [c.args[1] for c in begin.call_args_list]
    assert keys == ["brain_orb_action:capture:k9", "brain_orb_action:link:k9"]


def test_action_agent_error_releases_idempotency_claim(client):
    """An agent-side failure (502 map / 504 timeout) releases the in-flight claim so
    a retry with the same key isn't wedged on 409 — only the 429 path was covered."""
    from services.idempotency_service import IdempotencyDecision
    fresh = IdempotencyDecision(enabled=True, replay=False, in_flight=False,
                                scope="agent:cornelius", key="k")
    cases = (
        (AsyncMock(return_value=_resp(500)), 502),                                  # agent error
        (AsyncMock(side_effect=HTTPException(status_code=504, detail="t")), 504),   # timeout
    )
    for agent_req, expected in cases:
        with patch.object(bo, "_agent_request", agent_req), \
             patch.object(bo.idempotency_service, "begin", return_value=fresh), \
             patch.object(bo.idempotency_service, "fail") as fail, \
             patch.object(bo.rate_limiter, "enforce"):
            r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"},
                            headers={"Idempotency-Key": "k"})
        assert r.status_code == expected
        fail.assert_called_once_with(fresh)


def test_action_success_completes_claim_with_snapshot(client):
    """A successful write completes the claim with the PARSED agent response, so a
    later replay serves the real snapshot (not None / raw bytes)."""
    from services.idempotency_service import IdempotencyDecision
    fresh = IdempotencyDecision(enabled=True, replay=False, in_flight=False,
                                scope="agent:cornelius", key="k")
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true,"title":"t"}'))), \
         patch.object(bo.idempotency_service, "begin", return_value=fresh), \
         patch.object(bo.idempotency_service, "complete") as complete, \
         patch.object(bo.platform_audit_service, "log", AsyncMock()):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"},
                        headers={"Idempotency-Key": "k"})
    assert r.status_code == 200
    complete.assert_called_once_with(fresh, None, {"ok": True, "title": "t"})


def test_action_without_idempotency_key_skips_dedup(client):
    """No Idempotency-Key header ⇒ begin() gets key=None (dedup disabled, Invariant
    #18 fail-open) and the write still executes normally."""
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true}'))), \
         patch.object(bo.idempotency_service, "begin",
                      wraps=bo.idempotency_service.begin) as begin, \
         patch.object(bo.platform_audit_service, "log", AsyncMock()):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"})
    assert r.status_code == 200
    assert begin.call_args.args[1] is None


# --- backend: audit sink failure never masks a completed write (review F5) ---

def test_action_audit_failure_never_masks_write(client):
    """A failing audit sink must not 500 a COMPLETED write — the client would retry
    with a fresh idempotency key and double-write (review F5, _safe_audit)."""
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true}'))), \
         patch.object(bo.platform_audit_service, "log",
                      AsyncMock(side_effect=RuntimeError("audit sink down"))):
        r = client.post(_ACTION_URL, json={"action": "capture", "body": "x"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_refresh_audit_failure_never_masks(client):
    """Same F5 guarantee on the refresh route (its own _safe_audit call site)."""
    with patch.object(bo, "_agent_request", AsyncMock(return_value=_resp(200, b'{"ok":true}'))), \
         patch.object(bo.platform_audit_service, "log",
                      AsyncMock(side_effect=RuntimeError("audit sink down"))):
        r = client.post(_REFRESH_URL)
    assert r.status_code == 200


# --- agent-server: _run_hook hardening branches never exercised --------------

def test_agent_server_hook_output_too_large_502(agent_env, monkeypatch):
    """A hook flooding stdout past _MAX_HOOK_OUT → 502, never buffered through
    (cap shrunk so the test stays fast)."""
    monkeypatch.setattr(agent_env.asbo, "_MAX_HOOK_OUT", 1024)
    _write_hook(agent_env.scopes_hook, '#!/usr/bin/env python3\nprint("a" * 2048)\n')
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 502


def test_agent_server_hook_not_executable_404(agent_env):
    """A hook file that exists but lacks +x is 'not supported' (404, the _hook_ready
    branch) — never a 5xx from trying to exec it."""
    agent_env.scopes_hook.write_text('#!/bin/sh\necho "{}"\n')  # deliberately no chmod
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 404


def test_agent_server_scope_body_too_large_413(agent_env):
    """The agent-server enforces its own 64 KiB _MAX_HOOK_BODY on /scope (defense in
    depth below the backend cap; only /action's 1 MiB cap was tested)."""
    _write_hook(agent_env.scope_hook, '#!/bin/sh\ncat >/dev/null\necho "{}"\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": ["x" * 70_000]})
    assert r.status_code == 413


def test_agent_server_search_body_too_large_413(agent_env):
    _write_hook(agent_env.search_hook, '#!/bin/sh\ncat >/dev/null\necho "{}"\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x" * 70_000})
    assert r.status_code == 413


# --- agent-server: postprocess config semantics (#73) ------------------------

def test_agent_server_postprocess_put_invalid_json_400(agent_env):
    r = agent_env.client.put("/api/brain-orb/postprocess", data=b"{not json",
                             headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_agent_server_postprocess_prompt_truncated_8000(agent_env):
    """The stored prompt is hard-capped at 8000 chars on write — both the PUT echo
    and the subsequent GET reflect the truncation."""
    r = agent_env.client.put("/api/brain-orb/postprocess",
                             json={"enabled": True, "prompt": "p" * 9000})
    assert r.status_code == 200
    assert len(r.json()["prompt"]) == 8000
    g = agent_env.client.get("/api/brain-orb/postprocess")
    assert len(g.json()["prompt"]) == 8000


def test_agent_server_postprocess_md_fallback(agent_env):
    """Legacy voice-postprocess.md is a prompt-only read fallback when no JSON config
    exists — enabled derives from the prompt being non-empty."""
    agent_env.asbo._POSTPROCESS_MD.write_text("summarize the session\n")
    g = agent_env.client.get("/api/brain-orb/postprocess")
    assert g.status_code == 200
    assert g.json() == {"enabled": True, "prompt": "summarize the session"}


def test_agent_server_postprocess_corrupt_json_degrades(agent_env):
    """A corrupt JSON config never 500s — GET falls through to the .md fallback and
    then to safe defaults."""
    agent_env.asbo._POSTPROCESS_CONFIG.write_text("{corrupt")
    g = agent_env.client.get("/api/brain-orb/postprocess")
    assert g.status_code == 200
    assert g.json() == {"enabled": False, "prompt": ""}


# --- mint service: system instruction + token windows ------------------------

def test_build_system_instruction_clauses_and_prompt_cap():
    """readonly vs write clause selection; the persisted agent voice prompt is
    prefixed and hard-capped at _AGENT_PROMPT_MAX; whitespace-only prompt ignored."""
    import services.brain_orb_voice_service as svc

    ro = svc._build_system_instruction(None, can_write=False)
    rw = svc._build_system_instruction(None, can_write=True)
    assert ro.endswith(svc._ORB_VOICE_READONLY_CLAUSE)
    assert rw.endswith(svc._ORB_VOICE_WRITE_CLAUSE)
    assert svc._ORB_VOICE_WRITE_CLAUSE not in ro

    long_prompt = "p" * (svc._AGENT_PROMPT_MAX + 500)
    out = svc._build_system_instruction(long_prompt, can_write=False)
    prefix, sep, rest = out.partition("\n\n")
    assert sep == "\n\n"
    assert prefix == "p" * svc._AGENT_PROMPT_MAX   # capped, prompt-injection bloat bound
    assert rest == ro

    assert svc._build_system_instruction("   ", can_write=False) == ro


def test_mint_service_new_session_window_shorter_than_expiry(monkeypatch):
    """The token must be SPENT (session opened) within the short new-session window
    even though the session itself may run VOICE_MAX_DURATION — a leaked token that
    isn't used almost immediately is dead."""
    import services.brain_orb_voice_service as svc
    captured = {}

    class _FakeAuthTokens:
        def create(self, *, config):
            captured["config"] = config
            return types.SimpleNamespace(name="auth_tokens/t")

    class _FakeClient:
        def __init__(self, *a, **k):
            self.auth_tokens = _FakeAuthTokens()

    monkeypatch.setattr(svc, "_client", None)
    monkeypatch.setattr(svc, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(svc.genai, "Client", _FakeClient)

    asyncio.run(svc.mint_voice_token("cornelius", voice_name="Kore", agent_prompt=None))
    cfg = captured["config"]
    # Both stamps derive from the same `now`, so the delta is exact.
    delta = (cfg.expire_time - cfg.new_session_expire_time).total_seconds()
    assert delta == svc.VOICE_MAX_DURATION - svc._NEW_SESSION_WINDOW_SECONDS
    assert cfg.new_session_expire_time < cfg.expire_time
