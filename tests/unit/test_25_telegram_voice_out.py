"""Telegram voice-out (epic #24 / #25) — shared TTS service + adapter branch.

Covers:
- tts_service cost-cap / no-key / provider-error / success (MP3)
- ffmpeg transcode success + ffmpeg-missing fallback
- synthesize_voice_note end-to-end + fail-soft
- telegram_adapter._maybe_send_voice decision branches (enabled/disabled,
  synth-success → sendVoice, synth-None → text fallback)
"""
import asyncio
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

import config  # noqa: E402
import services.tts_service as tts_service  # noqa: E402


# #762: this file stubs modules into sys.modules — an import-time `utils.helpers`
# preload (above, before importing config/tts_service, which monkeypatch can't
# reach) and a lazy `database` stub in the `adapter` fixture. Declaring the
# `_STUBBED_MODULE_NAMES` + `_restore_sys_modules` pair snapshots and restores
# them per test so the stubs never leak across files, and satisfies
# tests/lint_sys_modules.py (precedent: test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = ["utils.helpers", "database"]


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


# --------------------------------------------------------------------------- #
# tts_service.synthesize_mp3
# --------------------------------------------------------------------------- #

def _mock_httpx(status=200, content=b"MP3BYTES"):
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.text = "" if status == 200 else "provider error"
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def test_is_available(monkeypatch):
    # ent#117: the key is resolved at call time (settings → env), not read from
    # the frozen config value — patch the resolver.
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "")
    assert tts_service.is_available() is False
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    assert tts_service.is_available() is True


@pytest.mark.asyncio
async def test_synthesize_mp3_no_key(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "")
    assert await tts_service.synthesize_mp3("hello", "voice1") is None


@pytest.mark.asyncio
async def test_synthesize_mp3_over_cost_cap(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    monkeypatch.setattr(config, "TTS_MAX_CHARS", 10)
    assert await tts_service.synthesize_mp3("x" * 11, "voice1") is None


@pytest.mark.asyncio
async def test_synthesize_mp3_empty_text(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    assert await tts_service.synthesize_mp3("", "voice1") is None


@pytest.mark.asyncio
async def test_synthesize_mp3_no_voice_id(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    assert await tts_service.synthesize_mp3("hello", "") is None


@pytest.mark.asyncio
async def test_synthesize_mp3_success(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    monkeypatch.setattr(config, "TTS_MAX_CHARS", 1500)
    client = _mock_httpx(200, b"MP3BYTES")
    monkeypatch.setattr(tts_service.httpx, "AsyncClient", lambda *a, **k: client)
    out = await tts_service.synthesize_mp3("hello", "voice1")
    assert out == b"MP3BYTES"
    # key rides an xi-api-key header, never the body
    _, kwargs = client.post.call_args
    assert kwargs["headers"]["xi-api-key"] == "sk-xxx"


@pytest.mark.asyncio
async def test_synthesize_mp3_provider_error(monkeypatch):
    monkeypatch.setattr(tts_service, "_resolve_api_key", lambda: "sk-xxx")
    client = _mock_httpx(429, b"")
    monkeypatch.setattr(tts_service.httpx, "AsyncClient", lambda *a, **k: client)
    assert await tts_service.synthesize_mp3("hello", "voice1") is None


# --------------------------------------------------------------------------- #
# tts_service.to_ogg_opus (ffmpeg)
# --------------------------------------------------------------------------- #

def _mock_proc(returncode=0, stdout=b"OGGOPUS", stderr=b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_to_ogg_opus_success(monkeypatch):
    proc = _mock_proc(0, b"OGGOPUS")
    monkeypatch.setattr(tts_service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    assert await tts_service.to_ogg_opus(b"MP3BYTES") == b"OGGOPUS"


@pytest.mark.asyncio
async def test_to_ogg_opus_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(
        tts_service.asyncio, "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError()),
    )
    assert await tts_service.to_ogg_opus(b"MP3BYTES") is None


@pytest.mark.asyncio
async def test_to_ogg_opus_nonzero_rc(monkeypatch):
    proc = _mock_proc(1, b"", b"boom")
    monkeypatch.setattr(tts_service.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    assert await tts_service.to_ogg_opus(b"MP3BYTES") is None


@pytest.mark.asyncio
async def test_to_ogg_opus_empty_input():
    assert await tts_service.to_ogg_opus(b"") is None


# --------------------------------------------------------------------------- #
# tts_service.synthesize_voice_note (end-to-end)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_voice_note_full_success(monkeypatch):
    monkeypatch.setattr(tts_service, "synthesize_mp3", AsyncMock(return_value=b"MP3"))
    monkeypatch.setattr(tts_service, "to_ogg_opus", AsyncMock(return_value=b"OGG"))
    assert await tts_service.synthesize_voice_note("hi", "v1") == b"OGG"


@pytest.mark.asyncio
async def test_voice_note_synth_fails_short_circuits(monkeypatch):
    monkeypatch.setattr(tts_service, "synthesize_mp3", AsyncMock(return_value=None))
    transcode = AsyncMock()
    monkeypatch.setattr(tts_service, "to_ogg_opus", transcode)
    assert await tts_service.synthesize_voice_note("hi", "v1") is None
    transcode.assert_not_called()


# NOTE (ent#117): the adapter-driven always-voice `_maybe_send_voice` was removed —
# voice is now a per-message capability delivered by services/voice_reply_service.py
# (agent opts in via the send_voice_reply MCP tool). Those decision branches are
# covered by tests/unit/test_117_voice_replies_v2.py. This file keeps the still-valid
# tts_service synthesis/transcode coverage above.
