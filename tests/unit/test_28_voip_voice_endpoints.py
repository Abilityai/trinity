"""
HTTP-level tests for #28 — the new voice/voip config endpoints.

Mounts the real `routers/voice.py` and `routers/voip.py` routers on minimal
FastAPI apps with the auth dependencies overridden and `db`/`voip_service`
stubbed, then drives them through a `TestClient`. This exercises the genuine
FastAPI routing + dependency injection that the db-layer unit tests in
`test_28_voip_voice_config.py` don't reach (reviewer I1):

- `PUT /api/agents/{name}/voice/name` — owner-gated, 400 on a voice outside
  GEMINI_VOICE_NAMES, empty clears, valid persists.
- `GET  /api/agents/{name}/voice/name` — returns the persisted voice + the
  selectable list.
- `PUT /api/agents/{name}/voip/enabled` — owner-gated, 404 when no binding,
  200 reflecting the new state.

Modules: src/backend/routers/voice.py, src/backend/routers/voip.py
"""

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.voice as voice_mod  # noqa: E402
import routers.voip as voip_mod  # noqa: E402
from dependencies import (  # noqa: E402
    get_current_user,
    get_authorized_agent,
    get_owned_agent,
    get_owned_agent_by_name,
)

_AGENT = "myagent"
_USER = types.SimpleNamespace(
    username="owner", email="owner@example.com", role="user", id=1
)


# ---------------------------------------------------------------------------
# /voice/name (voice router)
# ---------------------------------------------------------------------------

@pytest.fixture
def voice_client(monkeypatch):
    app = FastAPI()
    app.include_router(voice_mod.router)
    app.dependency_overrides[get_authorized_agent] = lambda: _AGENT
    app.dependency_overrides[get_owned_agent] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: _USER
    # db is stubbed per-test; default to a benign persisted voice.
    monkeypatch.setattr(voice_mod.db, "get_voice_name", lambda _n: "Kore", raising=False)
    monkeypatch.setattr(voice_mod.db, "set_voice_name", lambda *_a: True, raising=False)
    return TestClient(app), app


class TestVoiceNameEndpoint:
    def test_get_returns_voice_and_list(self, voice_client, monkeypatch):
        client, _ = voice_client
        monkeypatch.setattr(voice_mod.db, "get_voice_name", lambda _n: "Puck")
        r = client.get(f"/api/agents/{_AGENT}/voice/name")
        assert r.status_code == 200
        body = r.json()
        assert body["voice_name"] == "Puck"
        assert "Kore" in body["available_voices"]
        assert body["default_voice"] == "Kore"

    def test_put_valid_voice_persists(self, voice_client, monkeypatch):
        client, _ = voice_client
        calls = []
        monkeypatch.setattr(voice_mod.db, "set_voice_name", lambda n, v: calls.append((n, v)) or True)
        r = client.put(f"/api/agents/{_AGENT}/voice/name", json={"voice_name": "Charon"})
        assert r.status_code == 200
        assert r.json()["voice_name"] == "Charon"
        assert calls == [(_AGENT, "Charon")]

    def test_put_unknown_voice_400(self, voice_client):
        client, _ = voice_client
        r = client.put(f"/api/agents/{_AGENT}/voice/name", json={"voice_name": "Bogus"})
        assert r.status_code == 400
        assert "Bogus" in r.json()["detail"]

    def test_put_empty_clears_to_default(self, voice_client, monkeypatch):
        client, _ = voice_client
        cleared = []
        monkeypatch.setattr(voice_mod.db, "set_voice_name", lambda n, v: cleared.append(v) or True)
        monkeypatch.setattr(voice_mod.db, "get_voice_name", lambda _n: "Kore")
        r = client.put(f"/api/agents/{_AGENT}/voice/name", json={"voice_name": ""})
        assert r.status_code == 200
        assert cleared == [None]  # empty → cleared to NULL
        assert r.json()["voice_name"] == "Kore"

    def test_put_is_owner_gated(self, voice_client):
        client, app = voice_client

        def _deny():
            raise HTTPException(status_code=403, detail="Owner access required")

        app.dependency_overrides[get_owned_agent] = _deny
        r = client.put(f"/api/agents/{_AGENT}/voice/name", json={"voice_name": "Puck"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# /voip/enabled (voip router)
# ---------------------------------------------------------------------------

_BINDING = {
    "account_sid": "AC" + "0" * 32,
    "from_number": "+14155550100",
    "daily_call_cap": 50,
    "display_name": "Acme",
    "enabled": False,
}


@pytest.fixture
def voip_client(monkeypatch):
    app = FastAPI()
    app.include_router(voip_mod.auth_router)
    app.dependency_overrides[get_owned_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: _USER
    # _require_enabled() gate: pretend the platform flag is on.
    monkeypatch.setattr(voip_mod.voip_service, "is_available", lambda: True)
    return TestClient(app), app


class TestVoipEnabledEndpoint:
    def test_toggle_success_reflects_state(self, voip_client, monkeypatch):
        client, _ = voip_client
        calls = []
        monkeypatch.setattr(voip_mod.db, "set_voip_enabled", lambda n, e: calls.append((n, e)) or True)
        monkeypatch.setattr(voip_mod.db, "get_voip_binding", lambda _n: dict(_BINDING, enabled=False))
        r = client.put(f"/api/agents/{_AGENT}/voip/enabled", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert calls == [(_AGENT, False)]
        # Token never leaks in the response.
        assert "auth_token" not in r.json()

    def test_toggle_missing_binding_404(self, voip_client, monkeypatch):
        client, _ = voip_client
        monkeypatch.setattr(voip_mod.db, "set_voip_enabled", lambda _n, _e: False)
        r = client.put(f"/api/agents/{_AGENT}/voip/enabled", json={"enabled": True})
        assert r.status_code == 404

    def test_404_when_voip_flag_off(self, voip_client, monkeypatch):
        client, _ = voip_client
        monkeypatch.setattr(voip_mod.voip_service, "is_available", lambda: False)
        r = client.put(f"/api/agents/{_AGENT}/voip/enabled", json={"enabled": True})
        assert r.status_code == 404

    def test_is_owner_gated(self, voip_client):
        client, app = voip_client

        def _deny():
            raise HTTPException(status_code=403, detail="Owner access required")

        app.dependency_overrides[get_owned_agent_by_name] = _deny
        r = client.put(f"/api/agents/{_AGENT}/voip/enabled", json={"enabled": True})
        assert r.status_code == 403
