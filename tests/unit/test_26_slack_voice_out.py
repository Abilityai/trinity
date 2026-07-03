"""Slack voice-out (epic #24 / #26) — adapter branch.

Rides the shared tts_service from #25; covers the Slack-specific wiring:
MP3 synth → Slack files upload (inline audio clip), and the text-fallback branches.
"""
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

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
    from adapters.slack_adapter import SlackAdapter
    return SlackAdapter()


def test_plain_for_tts_strips_markup(adapter):
    out = adapter._plain_for_tts("**Hi** _there_ `code` [label](http://x) # head")
    assert "*" not in out and "`" not in out
    assert "label" in out and "http://x" not in out


@pytest.mark.asyncio
async def test_voice_disabled_no_upload(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": False, "voice_id": None})
    up = AsyncMock()
    monkeypatch.setattr("adapters.slack_adapter.slack_service.upload_file", up)
    ok = await adapter._maybe_send_voice("tok", "C1", "Hello", "a1", None)
    assert ok is False
    up.assert_not_called()


@pytest.mark.asyncio
async def test_voice_success_uploads_mp3(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr("services.tts_service.synthesize_mp3", AsyncMock(return_value=b"MP3"))
    up = AsyncMock(return_value=(True, None))
    monkeypatch.setattr("adapters.slack_adapter.slack_service.upload_file", up)
    ok = await adapter._maybe_send_voice("tok", "C1", "Hello", "a1", "T1")
    assert ok is True
    up.assert_awaited_once()
    kwargs = up.await_args.kwargs
    assert kwargs["filename"] == "voice.mp3"
    assert kwargs["content"] == b"MP3"
    assert kwargs["thread_ts"] == "T1"


@pytest.mark.asyncio
async def test_voice_tts_none_falls_back(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr("services.tts_service.synthesize_mp3", AsyncMock(return_value=None))
    up = AsyncMock()
    monkeypatch.setattr("adapters.slack_adapter.slack_service.upload_file", up)
    ok = await adapter._maybe_send_voice("tok", "C1", "Hello", "a1", None)
    assert ok is False
    up.assert_not_called()


@pytest.mark.asyncio
async def test_voice_upload_failure_falls_back(adapter, monkeypatch):
    from database import db
    monkeypatch.setattr(db, "get_tts_config", lambda n: {"enabled": True, "voice_id": "v1"})
    monkeypatch.setattr("services.tts_service.synthesize_mp3", AsyncMock(return_value=b"MP3"))
    up = AsyncMock(return_value=(False, "upload_failed"))
    monkeypatch.setattr("adapters.slack_adapter.slack_service.upload_file", up)
    ok = await adapter._maybe_send_voice("tok", "C1", "Hello", "a1", None)
    assert ok is False
