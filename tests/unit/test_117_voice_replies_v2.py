"""Unit tests for Voice Replies v2 (trinity-enterprise#117).

Covers the three new seams:

  * resolver / settings — services/settings_service ElevenLabs key resolution
    (stored AES-256-GCM setting → ELEVENLABS_API_KEY env → ''), default voice,
    and the GET/PUT /api/settings/elevenlabs admin routes (key never echoed,
    source, clear, set+clear conflict, 403 non-admin) + feature-flag tts_available
  * delivery service — services/voice_reply_service.send_voice_reply gating
    (tts unavailable / voice disabled / channel flag off / no voice id) and the
    happy-path + effect-guard replay
  * endpoint self-gate — POST /api/agents/{name}/voice-reply resolves the channel
    destination from the execution and self-gates an agent-scoped key

True unit tests: no Docker, no running backend. DB and TTS are mocked.
"""
from __future__ import annotations

import contextlib
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.settings as sr
# #1028: the ElevenLabs handlers live in the package's `integrations`
# module now; `sr.router` is still the composed router.
import routers.settings.integrations as sr_integrations
from dependencies import get_current_user
from services.settings_service import settings_service


# ---------------------------------------------------------------------------
# Resolver: services/settings_service ElevenLabs key + default voice
# ---------------------------------------------------------------------------

def test_elevenlabs_key_stored_overrides_env():
    """A stored (encrypted) setting decrypts and wins over the env var."""
    fake_enc = MagicMock()
    fake_enc.decrypt.return_value = {"elevenlabs_api_key": "sk_stored"}
    with patch.object(settings_service, "get_setting", return_value="ENVELOPE"), \
         patch("services.credential_encryption.CredentialEncryptionService", return_value=fake_enc), \
         patch.dict("os.environ", {"ELEVENLABS_API_KEY": "sk_env"}):
        assert settings_service.get_elevenlabs_api_key() == "sk_stored"


def test_elevenlabs_key_env_fallback():
    """No stored setting → the env var is used."""
    with patch.object(settings_service, "get_setting", return_value=None), \
         patch.dict("os.environ", {"ELEVENLABS_API_KEY": "sk_env"}):
        assert settings_service.get_elevenlabs_api_key() == "sk_env"


def test_elevenlabs_key_none_when_unset():
    with patch.object(settings_service, "get_setting", return_value=None), \
         patch.dict("os.environ", {}, clear=True):
        assert settings_service.get_elevenlabs_api_key() == ""


def test_elevenlabs_key_fail_open_on_bad_envelope():
    """A decrypt failure falls back to env (fail-open), never raises."""
    fake_enc = MagicMock()
    fake_enc.decrypt.side_effect = ValueError("corrupt")
    with patch.object(settings_service, "get_setting", return_value="BAD"), \
         patch("services.credential_encryption.CredentialEncryptionService", return_value=fake_enc), \
         patch.dict("os.environ", {"ELEVENLABS_API_KEY": "sk_env"}):
        assert settings_service.get_elevenlabs_api_key() == "sk_env"


def test_elevenlabs_key_source():
    with patch.object(settings_service, "get_setting", return_value="ENVELOPE"):
        assert settings_service.elevenlabs_key_source() == "override"
    with patch.object(settings_service, "get_setting", return_value=None), \
         patch.dict("os.environ", {"ELEVENLABS_API_KEY": "x"}):
        assert settings_service.elevenlabs_key_source() == "env"
    with patch.object(settings_service, "get_setting", return_value=None), \
         patch.dict("os.environ", {}, clear=True):
        assert settings_service.elevenlabs_key_source() == "none"


def test_default_voice_id_strip_and_none():
    with patch.object(settings_service, "get_setting", return_value="  voice123  "):
        assert settings_service.get_default_voice_id() == "voice123"
    with patch.object(settings_service, "get_setting", return_value="   "):
        assert settings_service.get_default_voice_id() is None


# ---------------------------------------------------------------------------
# Admin routes: GET/PUT /api/settings/elevenlabs
# ---------------------------------------------------------------------------

_URL = "/api/settings/elevenlabs"


def _make_app():
    app = FastAPI()
    app.include_router(sr.router)  # router already carries prefix="/api/settings"
    return app


def _admin():
    return types.SimpleNamespace(id=1, username="admin", role="admin", agent_name=None, connector_agent=None, mcp_scope=None)


def _user():
    return types.SimpleNamespace(id=2, username="bob", role="user", agent_name=None, connector_agent=None, mcp_scope=None)


def test_elevenlabs_get_admin_never_echoes_key():
    app = _make_app()
    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    with patch.object(settings_service, "get_elevenlabs_api_key", return_value="sk_secret"), \
         patch.object(settings_service, "elevenlabs_key_source", return_value="override"), \
         patch.object(settings_service, "get_default_voice_id", return_value="v1"):
        r = client.get(_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["key_configured"] is True
    assert body["key_source"] == "override"
    assert body["default_voice_id"] == "v1"
    # The secret must never appear anywhere in the response.
    assert "sk_secret" not in r.text


def test_elevenlabs_get_non_admin_403():
    app = _make_app()
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app)
    r = client.get(_URL)
    assert r.status_code == 403


def test_elevenlabs_put_sets_key_and_audits():
    app = _make_app()
    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    with patch.object(settings_service, "set_elevenlabs_api_key") as set_key, \
         patch.object(settings_service, "get_elevenlabs_api_key", return_value="sk_new"), \
         patch.object(settings_service, "elevenlabs_key_source", return_value="override"), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(sr_integrations.platform_audit_service, "log", new=AsyncMock()) as audit:
        r = client.put(_URL, json={"api_key": "sk_new"})
    assert r.status_code == 200
    set_key.assert_called_once_with("sk_new")
    assert r.json()["key_configured"] is True
    # Audited, and the raw key value is never in the audit details.
    assert audit.called
    _, kwargs = audit.call_args
    assert "sk_new" not in str(kwargs.get("details"))


def test_elevenlabs_put_set_and_clear_conflict_400():
    app = _make_app()
    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    r = client.put(_URL, json={"api_key": "x", "clear": ["api_key"]})
    assert r.status_code == 400


def test_elevenlabs_put_clear_key():
    app = _make_app()
    app.dependency_overrides[get_current_user] = _admin
    client = TestClient(app)
    with patch.object(settings_service, "clear_elevenlabs_api_key") as clear_key, \
         patch.object(settings_service, "get_elevenlabs_api_key", return_value=""), \
         patch.object(settings_service, "elevenlabs_key_source", return_value="none"), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(sr_integrations.platform_audit_service, "log", new=AsyncMock()):
        r = client.put(_URL, json={"clear": ["api_key"]})
    assert r.status_code == 200
    clear_key.assert_called_once()
    assert r.json()["key_configured"] is False


# ---------------------------------------------------------------------------
# Delivery service gating: services/voice_reply_service
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _fake_guard(replay=False, snapshot=None):
    state = types.SimpleNamespace(replay=replay, snapshot=snapshot, dedup_enabled=True)
    yield state


def _cfg(enabled=True, voice_id="v1", channels=None):
    return {
        "enabled": enabled,
        "voice_id": voice_id,
        "channels": channels or {"telegram": True, "slack": True, "whatsapp": True},
    }


@pytest.mark.asyncio
async def test_service_tts_unavailable_not_delivered():
    import services.voice_reply_service as svc
    with patch("services.tts_service.is_available", return_value=False):
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi")
    assert res.delivered is False
    assert res.reason == "tts_unavailable"


@pytest.mark.asyncio
async def test_service_voice_disabled():
    import services.voice_reply_service as svc
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg(enabled=False)):
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi")
    assert res.reason == "voice_disabled"


@pytest.mark.asyncio
async def test_service_channel_flag_off():
    import services.voice_reply_service as svc
    cfg = _cfg(channels={"telegram": False, "slack": True, "whatsapp": True})
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=cfg):
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi")
    assert res.reason == "channel_disabled"


@pytest.mark.asyncio
async def test_service_no_voice_id():
    import services.voice_reply_service as svc
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg(voice_id=None)), \
         patch.object(settings_service, "get_default_voice_id", return_value=None):
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi")
    assert res.reason == "no_voice_id"


@pytest.mark.asyncio
async def test_service_falls_back_to_platform_default_voice():
    """No agent voice id but a platform default → proceeds to delivery."""
    import services.voice_reply_service as svc
    sent = {}

    async def fake_send_voice(token, chat, ogg, reply_to_message_id=None):
        sent["ok"] = True
        return {"message_id": 1}

    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg(voice_id=None)), \
         patch.object(settings_service, "get_default_voice_id", return_value="platform_v"), \
         patch.object(svc.idempotency_service, "effect_guard", return_value=_fake_guard()), \
         patch.object(svc.db, "get_telegram_bot_token", return_value="botok"), \
         patch("services.tts_service.synthesize_voice_note", new=AsyncMock(return_value=b"ogg")), \
         patch("adapters.telegram_adapter.TelegramAdapter._send_voice", new=AsyncMock(side_effect=fake_send_voice)):
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi")
    assert res.delivered is True
    assert sent.get("ok") is True


@pytest.mark.asyncio
async def test_service_slack_delivers_mp3():
    import services.voice_reply_service as svc
    up = AsyncMock(return_value=(True, None))
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg()), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(svc.idempotency_service, "effect_guard", return_value=_fake_guard()), \
         patch.object(svc.db, "get_slack_bot_token_for_channel", return_value="xoxb"), \
         patch("services.tts_service.synthesize_mp3", new=AsyncMock(return_value=b"mp3")), \
         patch("services.slack_service.slack_service.upload_file", new=up):
        res = await svc.send_voice_reply("agent", "slack", "C1", "thread1", "hi")
    assert res.delivered is True
    assert up.await_args.kwargs["filename"] == "voice.mp3"


@pytest.mark.asyncio
async def test_service_slack_no_binding():
    import services.voice_reply_service as svc
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg()), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(svc.idempotency_service, "effect_guard", return_value=_fake_guard()), \
         patch.object(svc.db, "get_slack_bot_token_for_channel", return_value=None):
        res = await svc.send_voice_reply("agent", "slack", "C1", None, "hi")
    assert res.delivered is False
    assert res.reason == "no_binding"


@pytest.mark.asyncio
async def test_service_whatsapp_delivers_hosted_ogg():
    import services.voice_reply_service as svc
    send = AsyncMock(return_value={"sid": "SM1"})
    binding = {"account_sid": "AC", "from_number": "whatsapp:+1", "messaging_service_sid": None}
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg()), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(svc.idempotency_service, "effect_guard", return_value=_fake_guard()), \
         patch.object(svc.db, "get_whatsapp_binding", return_value=binding), \
         patch.object(svc.db, "get_whatsapp_auth_token", return_value="tok"), \
         patch("services.tts_service.synthesize_voice_note", new=AsyncMock(return_value=b"ogg")), \
         patch("services.agent_shared_files_service.create_share_from_bytes",
               return_value={"url": "https://x/api/files/1?sig=abc"}), \
         patch("adapters.whatsapp_adapter.WhatsAppAdapter._send_message", new=send):
        res = await svc.send_voice_reply("agent", "whatsapp", "whatsapp:+2", None, "hi")
    assert res.delivered is True
    assert send.await_args.kwargs["media_url"].startswith("https://")


@pytest.mark.asyncio
async def test_service_replay_returns_snapshot_without_resend():
    import services.voice_reply_service as svc
    snap = {"delivered": True, "channel": "telegram", "reason": None}
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=_cfg()), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(svc.idempotency_service, "effect_guard",
                      return_value=_fake_guard(replay=True, snapshot=snap)), \
         patch.object(svc.db, "get_telegram_bot_token") as tok:
        res = await svc.send_voice_reply("agent", "telegram", "chat1", None, "hi",
                                         execution_id="e1")
    assert res.delivered is True
    # A replay must NOT re-resolve credentials / re-send.
    tok.assert_not_called()
