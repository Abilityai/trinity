"""trinity-enterprise#1046: the speech providers, over a mocked transport.

The request a provider really builds and the response it really parses, so a mistake in
either shows up here and not on a call.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import direct_voice_service as dvs  # noqa: E402
from services.speech import elevenlabs, gemini, registry  # noqa: E402
from services.speech.base import SpeechError  # noqa: E402

KEY = "test-speech-key"
PCM = b"\x01\x00" * 40


def _route(monkeypatch, module, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient",
                        lambda timeout=None: real(transport=httpx.MockTransport(handler), timeout=timeout))


def _gemini():
    return gemini.GeminiSpeech(KEY, stt_model="stt", tts_models=("tts-a", "tts-b"), default_voice="Kore")


def _sse(*audio, rate=24000):
    lines = []
    for pcm in audio:
        event = {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": f"audio/l16;rate={rate}", "data": base64.b64encode(pcm).decode()}}]}}]}
        lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def _collect(stream):
    async def go():
        return [chunk async for chunk in stream]
    return asyncio.run(go())


def test_gemini_transcribes_with_the_key_in_a_header_and_a_padded_wav(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        # The transcribe model's real answer shape (recorded 2026-11-02).
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"audioTranscription": {"text": " what time is it "}}], "role": "model"}, "finishReason": "STOP"}]})

    _route(monkeypatch, gemini, handler)
    out = asyncio.run(_gemini().transcribe(PCM))
    assert out.text == "what time is it"
    assert KEY not in seen["url"] and seen["key"] == KEY and "stt:generateContent" in seen["url"]
    wav = base64.b64decode(seen["body"]["contents"][0]["parts"][1]["inlineData"]["data"])
    assert wav[:4] == b"RIFF" and len(wav) > 16000


def test_a_general_model_answers_in_text_and_its_thoughts_are_not_the_transcript(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"text": "thinking about it", "thought": True}, {"text": "what time is it"}]}}]}))
    assert asyncio.run(_gemini().transcribe(PCM)).text == "what time is it"


def test_gemini_silence_is_an_empty_transcript_not_an_error(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "STOP"}]}))
    assert asyncio.run(_gemini().transcribe(PCM)).text == ""


def test_a_provider_error_never_carries_the_key_or_the_body(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(500, text=f"bad {KEY}"))
    with pytest.raises(SpeechError) as exc:
        asyncio.run(_gemini().transcribe(PCM))
    assert KEY not in str(exc.value) and "500" in str(exc.value)


def test_a_network_failure_is_a_speech_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    _route(monkeypatch, gemini, handler)
    with pytest.raises(SpeechError):
        asyncio.run(_gemini().transcribe(PCM))
    with pytest.raises(SpeechError):
        _collect(_gemini().synthesize_stream("Hello."))


def test_tts_falls_through_to_the_next_model_when_one_refuses(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "tts-a" in request.url.path:
            return httpx.Response(429)
        return httpx.Response(200, content=_sse(b"\x01\x00\x02\x00"))

    _route(monkeypatch, gemini, handler)
    assert b"".join(_collect(_gemini().synthesize_stream("Hello."))) == b"\x01\x00\x02\x00"
    assert len(calls) == 2


def test_tts_with_no_audio_from_any_model_is_an_error(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(200, content=b"data: {}\n\n"))
    with pytest.raises(SpeechError):
        _collect(_gemini().synthesize_stream("Hello."))


def test_tts_keeps_samples_whole_across_chunks(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(200, content=_sse(b"\x01\x00\x02", b"\x00\x03\x00")))
    chunks = _collect(_gemini().synthesize_stream("Hello."))
    assert all(len(c) % 2 == 0 for c in chunks)
    assert b"".join(chunks) == b"\x01\x00\x02\x00\x03\x00"


def test_tts_refuses_a_rate_the_player_would_play_at_the_wrong_speed(monkeypatch):
    _route(monkeypatch, gemini, lambda r: httpx.Response(200, content=_sse(b"\x01\x00", rate=16000)))
    with pytest.raises(SpeechError):
        _collect(_gemini().synthesize_stream("Hello."))


def test_elevenlabs_transcribes(monkeypatch):
    seen = {}

    def handler(request):
        seen["key"] = request.headers["xi-api-key"]
        return httpx.Response(200, json={"text": " hi there "})

    _route(monkeypatch, elevenlabs, handler)
    assert asyncio.run(elevenlabs.ElevenLabsSpeech(KEY).transcribe(PCM)).text == "hi there"
    assert seen["key"] == KEY


def test_elevenlabs_needs_a_voice_and_streams_pcm(monkeypatch):
    with pytest.raises(SpeechError):
        _collect(elevenlabs.ElevenLabsSpeech(KEY).synthesize_stream("Hello."))
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"\x01\x00\x02")

    _route(monkeypatch, elevenlabs, handler)
    out = _collect(elevenlabs.ElevenLabsSpeech(KEY, default_voice="v1").synthesize_stream("Hello."))
    assert b"".join(out) == b"\x01\x00"
    assert "/v1/stream" in seen["url"] and "output_format=pcm_24000" in seen["url"]


class _Provider:
    def __init__(self, name):
        self.name = name
        self.voices = []

    async def synthesize_stream(self, text, voice=None):
        self.voices.append(voice)
        yield b"\x00\x00"


class _Socket:
    async def send_json(self, frame):
        pass


@pytest.mark.parametrize("name,expected", [("gemini", "Kore"), ("elevenlabs", None)])
def test_the_session_voice_name_goes_only_to_the_provider_that_understands_it(monkeypatch, name, expected):
    monkeypatch.setattr("services.runtime_secret_scrub.get_staged_values", lambda: [])
    session = dvs.DirectVoiceSession(session_id="vd_x", agent_name="a", user_id=1, client_email="a@example.com",
                                     portal_session_id="t", max_duration=60, voice_name="Kore")
    provider = _Provider(name)
    bridge = dvs.DirectBridge(_Socket(), session, provider, lambda *a: None)
    asyncio.run(bridge._speak_sentence(0, "Hello."))
    assert provider.voices == [expected]


@pytest.fixture
def keys(monkeypatch):
    """Which keys exist, on the real resolvers the registry reads."""
    from services import settings_service as module
    state = {"gemini": None, "elevenlabs": None, "voice": None}
    monkeypatch.setattr(module, "get_gemini_api_key", lambda: state["gemini"])
    monkeypatch.setattr(module.settings_service, "get_elevenlabs_api_key", lambda: state["elevenlabs"])
    monkeypatch.setattr(module.settings_service, "get_default_voice_id", lambda: state["voice"])
    return state


def test_auto_takes_gemini_first_then_elevenlabs(keys):
    assert registry.direct_voice_readiness("auto") == (None, registry.REASON_NO_KEY)
    keys.update(elevenlabs="k", voice="v")
    assert registry.direct_voice_readiness("auto") == ("ElevenLabs", None)
    keys.update(gemini="k")
    assert registry.direct_voice_readiness("auto") == ("Gemini", None)


def test_a_chosen_provider_does_not_fall_back_and_says_what_is_missing(keys):
    keys.update(elevenlabs="k")   # a key with no default voice is not ready
    assert registry.direct_voice_readiness("elevenlabs") == (None, registry.REASON_NO_ELEVENLABS)
    assert registry.direct_voice_readiness("gemini") == (None, registry.REASON_NO_GEMINI)
    assert registry.resolve_direct_provider("gemini") is None
