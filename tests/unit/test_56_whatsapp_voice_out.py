"""WhatsApp voice-out (epic #24 / trinity-enterprise#56) — adapter branch.

Rides the shared tts_service from #25; here we cover the WhatsApp-specific wiring:
TTS → host as audio → Twilio media send, and the text-fallback branches.
"""
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    sys.modules["utils.helpers"] = _helpers

if "database" not in sys.modules:
    _fake_db = types.ModuleType("database")
    _fake_db.db = MagicMock()
    sys.modules["database"] = _fake_db

# Import-time stubs monkeypatch can't reach (precedent:
# tests/unit/test_telegram_webhook_backfill.py) — snapshot & restore per test.
_STUBBED_MODULE_NAMES = [
    "utils.helpers",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def adapter():
    from adapters.whatsapp_adapter import WhatsAppAdapter
    return WhatsAppAdapter()


_BINDING = {"from_number": "whatsapp:+14155238886", "messaging_service_sid": None}


def test_plain_for_tts_strips_markup(adapter):
    out = adapter._plain_for_tts("**Hi** _there_ `code` [label](http://x) # head")
    assert "*" not in out and "_" not in out and "`" not in out
    assert "label" in out and "http://x" not in out


@pytest.mark.asyncio
async def test_voice_disabled_no_send(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": False, "voice_id": None})
    send = AsyncMock()
    monkeypatch.setattr(adapter, "_send_message", send)
    ok = await adapter._maybe_send_voice("SID", "tok", _BINDING, "whatsapp:+1", "Hello", "a1")
    assert ok is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_voice_success_sends_media(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr(
        "services.tts_service.synthesize_voice_note", AsyncMock(return_value=b"OGG")
    )
    monkeypatch.setattr(
        "adapters.whatsapp_adapter.create_share_from_bytes",
        lambda *a, **k: {"url": "https://host/api/files/x?sig=abc"},
    )
    send = AsyncMock(return_value={"sid": "SM1"})
    monkeypatch.setattr(adapter, "_send_message", send)
    ok = await adapter._maybe_send_voice("SID", "tok", _BINDING, "whatsapp:+1", "Hello", "a1")
    assert ok is True
    send.assert_awaited_once()
    # delivered as media, not text
    assert send.await_args.kwargs["media_url"] == "https://host/api/files/x?sig=abc"


@pytest.mark.asyncio
async def test_voice_hosting_bypasses_file_sharing_toggle(adapter, monkeypatch):
    """The voice note must host with require_sharing_enabled=False so it doesn't
    depend on the unrelated file-sharing toggle."""
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr(
        "services.tts_service.synthesize_voice_note", AsyncMock(return_value=b"OGG")
    )
    captured = {}

    def _fake_share(*a, **k):
        captured.update(k)
        return {"url": "https://host/f?sig=1"}

    monkeypatch.setattr("adapters.whatsapp_adapter.create_share_from_bytes", _fake_share)
    monkeypatch.setattr(adapter, "_send_message", AsyncMock(return_value={"sid": "x"}))
    await adapter._maybe_send_voice("SID", "tok", _BINDING, "whatsapp:+1", "Hello", "a1")
    assert captured.get("require_sharing_enabled") is False


@pytest.mark.asyncio
async def test_voice_tts_none_falls_back(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr(
        "services.tts_service.synthesize_voice_note", AsyncMock(return_value=None)
    )
    send = AsyncMock()
    monkeypatch.setattr(adapter, "_send_message", send)
    ok = await adapter._maybe_send_voice("SID", "tok", _BINDING, "whatsapp:+1", "Hello", "a1")
    assert ok is False
    send.assert_not_called()


@pytest.mark.asyncio
async def test_voice_hosting_failure_falls_back(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr(
        "services.tts_service.synthesize_voice_note", AsyncMock(return_value=b"OGG")
    )

    def _boom(*a, **k):
        raise RuntimeError("quota")

    monkeypatch.setattr("adapters.whatsapp_adapter.create_share_from_bytes", _boom)
    send = AsyncMock()
    monkeypatch.setattr(adapter, "_send_message", send)
    ok = await adapter._maybe_send_voice("SID", "tok", _BINDING, "whatsapp:+1", "Hello", "a1")
    assert ok is False
    send.assert_not_called()
