"""#2695 — the Workspace mic renders on speech-to-text CAPABILITY, not key presence.

ElevenLabs keys carry per-endpoint permissions. A key granted Text-to-Speech but
not Speech-to-Text passed `bool(tts_service.is_available())` and rendered a mic
that failed on every press with `401 missing_permissions`. These tests pin the
replacement gate end to end — the pure classifier, the cache, the fail-soft
direction, and every consumer of the verdict (roster card, agent page, the `/stt`
endpoint, the admin panel), by EXECUTING each one rather than reading its source.

No real Redis, no real HTTP: the provider is a stubbed `httpx.AsyncClient`, the
cache is the module's own per-process fallback (Redis stubbed to None).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import services.stt_capability_service as stt
import services.tts_service as tts_service
from client_portal import service as portal_service
from client_portal.service import ClientPortalError

KEY = "sk_example_key_with_tts_only"
ROW = {"agent_name": "acme-bot", "owner": "owner@example.com"}


@pytest.fixture(autouse=True)
def _no_redis_fresh_cache(monkeypatch):
    """Redis absent → the per-process fallback is the whole cache; cleared per test."""
    monkeypatch.setattr(stt, "_redis", lambda: None)
    stt._local.clear()
    stt._inflight.clear()
    yield
    stt._local.clear()
    stt._inflight.clear()


def _stub_provider(monkeypatch, *, status: int, body: str = "", raises: Exception | None = None):
    """Make `httpx.AsyncClient().post` answer one fixed provider response."""
    calls: list[dict] = []

    class _Resp:
        def __init__(self):
            self.status_code = status
            self.text = body

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append({"url": url, **kw})
            if raises is not None:
                raise raises
            return _Resp()

    monkeypatch.setattr(stt.httpx, "AsyncClient", _Client)
    return calls


MISSING = json.dumps({"detail": {"status": "missing_permissions",
                                 "message": "The API key you used is missing the permission speech_to_text"}})


# ---- the classifier is the partition --------------------------------------

@pytest.mark.parametrize("status,body,verdict,detail", [
    (401, MISSING, stt.VERDICT_REFUSED, "missing_permissions"),
    (403, json.dumps({"detail": "forbidden"}), stt.VERDICT_REFUSED, "forbidden"),
    (401, "not json", stt.VERDICT_REFUSED, "http_401"),
    (422, json.dumps({"detail": [{"loc": ["file"], "msg": "invalid"}]}), stt.VERDICT_CAPABLE, None),
    (400, "", stt.VERDICT_CAPABLE, None),
    (429, "", stt.VERDICT_CAPABLE, None),
    (200, json.dumps({"text": ""}), stt.VERDICT_CAPABLE, None),
    (500, "", stt.VERDICT_UNKNOWN, "http_500"),
    (503, "", stt.VERDICT_UNKNOWN, "http_503"),
])
def test_provider_answers_partition_into_verdicts(status, body, verdict, detail):
    cap = stt.classify_response(status, body)
    assert cap.verdict == verdict
    assert cap.detail == detail
    assert cap.checked_at is not None


def test_only_a_refusal_hides_the_mic():
    """The fail-SOFT direction: everything but a definitive refusal renders."""
    assert stt.SttCapability(stt.VERDICT_REFUSED).allowed is False
    assert stt.SttCapability(stt.VERDICT_CAPABLE).allowed is True
    assert stt.SttCapability(stt.VERDICT_UNKNOWN).allowed is True
    assert stt.UNCONFIGURED.allowed is True   # presence is the other gate's job


# ---- the probe -------------------------------------------------------------

def test_probe_sends_no_audio_and_a_refusal_is_recorded(monkeypatch):
    calls = _stub_provider(monkeypatch, status=401, body=MISSING)
    cap = asyncio.run(stt.probe(KEY))
    assert cap.verdict == stt.VERDICT_REFUSED and cap.detail == "missing_permissions"
    assert len(calls) == 1
    assert calls[0]["url"] == stt.STT_URL
    assert calls[0]["headers"] == {"xi-api-key": KEY}
    # One byte, never audio: the probe must cost no transcription minutes.
    _name, payload, _ctype = calls[0]["files"]["file"]
    assert payload == b"\0"


def test_probe_that_cannot_complete_is_unknown_never_refused(monkeypatch):
    _stub_provider(monkeypatch, status=0, raises=ConnectionError("provider down"))
    cap = asyncio.run(stt.probe(KEY))
    assert cap.verdict == stt.VERDICT_UNKNOWN
    assert cap.allowed is True


# ---- the cache -------------------------------------------------------------

def test_verdict_is_cached_so_the_roster_does_not_probe_per_request(monkeypatch):
    calls = _stub_provider(monkeypatch, status=401, body=MISSING)

    async def _twice():
        a = await stt.ensure_capability(KEY)
        b = await stt.ensure_capability(KEY)
        return a, b

    a, b = asyncio.run(_twice())
    assert a.verdict == b.verdict == stt.VERDICT_REFUSED
    assert len(calls) == 1


def test_cache_is_keyed_on_the_key_so_a_changed_key_is_a_miss(monkeypatch):
    calls = _stub_provider(monkeypatch, status=401, body=MISSING)
    asyncio.run(stt.ensure_capability(KEY))
    assert len(calls) == 1
    # Never the key itself in the row name — it lands in Redis listings.
    assert KEY not in stt.cache_key(KEY)
    assert stt.cache_key(KEY) != stt.cache_key(KEY + "-rotated")

    _stub_provider(monkeypatch, status=422)
    cap = asyncio.run(stt.ensure_capability(KEY + "-rotated"))
    assert cap.verdict == stt.VERDICT_CAPABLE     # the new key was asked, not the old row


def test_invalidate_forgets_this_key_only(monkeypatch):
    _stub_provider(monkeypatch, status=401, body=MISSING)
    asyncio.run(stt.ensure_capability(KEY))
    assert stt.read_cached(KEY).verdict == stt.VERDICT_REFUSED
    stt.invalidate(KEY)
    assert stt.read_cached(KEY) is None


def test_unknown_has_a_short_ttl_and_a_decided_verdict_a_long_one():
    assert stt._ttl_for(stt.SttCapability(stt.VERDICT_UNKNOWN)) == stt.TTL_UNKNOWN_SECONDS
    assert stt._ttl_for(stt.SttCapability(stt.VERDICT_REFUSED)) == stt.TTL_DECIDED_SECONDS
    assert stt._ttl_for(stt.SttCapability(stt.VERDICT_CAPABLE)) == stt.TTL_DECIDED_SECONDS
    assert stt.TTL_UNKNOWN_SECONDS < stt.TTL_DECIDED_SECONDS


def test_a_slow_probe_answers_unknown_now_and_fills_the_cache_later(monkeypatch):
    """The roster must never wait on the provider past its budget — but the
    probe it started still lands, so the NEXT reader gets the real verdict."""
    class _Resp:
        status_code = 401
        text = MISSING

    class _Slow:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            await asyncio.sleep(0.2)
            return _Resp()

    monkeypatch.setattr(stt.httpx, "AsyncClient", _Slow)

    async def _run():
        first = await stt.ensure_capability(KEY, wait_seconds=0.01)
        task = stt._inflight[stt.cache_key(KEY)]
        await task
        second = await stt.ensure_capability(KEY)
        return first, second

    first, second = asyncio.run(_run())
    assert first.verdict == stt.VERDICT_UNKNOWN and first.allowed is True
    assert second.verdict == stt.VERDICT_REFUSED


def test_no_key_is_unconfigured_without_a_probe(monkeypatch):
    calls = _stub_provider(monkeypatch, status=422)
    cap = asyncio.run(stt.ensure_capability(""))
    assert cap is stt.UNCONFIGURED
    assert calls == []


# ---- the consumers ---------------------------------------------------------

def _card(*, tts_ready, stt_ready):
    with patch.object(tts_service, "resolve_voice_from_config", return_value=None):
        return portal_service._row_to_card(
            dict(ROW), tts_ready, "platform-default",
            is_platform=False, runtime="claude-code",
            model_context=portal_service._model_context(),
            stt_ready=stt_ready,
        )


def test_card_hides_the_mic_when_the_key_cannot_transcribe():
    assert _card(tts_ready=True, stt_ready=False).stt_available is False


def test_card_renders_the_mic_when_the_key_can_transcribe():
    assert _card(tts_ready=True, stt_ready=True).stt_available is True


def test_card_without_a_key_is_closed_regardless_of_capability():
    assert _card(tts_ready=False, stt_ready=True).stt_available is False


def test_stt_ready_is_the_capability_verdict_and_fails_soft(monkeypatch):
    with patch("services.settings_service.settings_service.get_elevenlabs_api_key",
               return_value=KEY):
        _stub_provider(monkeypatch, status=401, body=MISSING)
        assert asyncio.run(portal_service._stt_ready(True)) is False
        stt._local.clear()
        _stub_provider(monkeypatch, status=0, raises=TimeoutError("slow"))
        assert asyncio.run(portal_service._stt_ready(True)) is True   # unknown → mic stays
        assert asyncio.run(portal_service._stt_ready(False)) is False  # no key → closed


def test_roster_threads_the_capability_into_every_card(monkeypatch):
    """The roster READS the verdict once and every card carries it — executed,
    not grepped: the SQL, Docker and model reads are stubbed, the gate is not."""
    monkeypatch.setattr(portal_service, "_roster_rows",
                        lambda email, include_owned: [dict(ROW), {**ROW, "agent_name": "beta"}])

    async def _avail(names):
        return {n: "ready" for n in names}

    async def _runtimes(names):
        return {n: "claude-code" for n in names}

    monkeypatch.setattr(portal_service, "_availability_map", _avail)
    monkeypatch.setattr(portal_service, "_runtime_map", _runtimes)
    monkeypatch.setattr(portal_service, "_default_voice_id", lambda: None)
    monkeypatch.setattr(portal_service, "_multi_agent_chat_available", lambda: False)
    seen: list[bool] = []

    async def _gate(tts_ready):
        seen.append(tts_ready)
        return False

    monkeypatch.setattr(portal_service, "_stt_ready", _gate)
    with patch.object(tts_service, "is_available", return_value=True), \
            patch.object(tts_service, "resolve_voice_from_config", return_value=None):
        roster = asyncio.run(portal_service.get_roster("client@example.com"))

    assert seen == [True]                                   # once per load, not per card
    assert [c.stt_available for c in roster.agents] == [False, False]


def test_endpoint_refuses_when_the_key_cannot_transcribe(monkeypatch):
    """The card bit and the `/stt` gate are one condition (#2212's rule, kept)."""
    async def _call():
        return await portal_service.transcribe_portal_audio(
            "acme-bot", "client@example.com", "voice.webm", "audio/webm", b"x" * 4000
        )

    with patch.object(portal_service, "agent_on_roster", return_value=True), \
            patch.object(tts_service, "is_available", return_value=True), \
            patch("services.settings_service.settings_service.get_elevenlabs_api_key",
                  return_value=KEY):
        _stub_provider(monkeypatch, status=401, body=MISSING)
        with pytest.raises(ClientPortalError) as exc:
            asyncio.run(_call())
        assert exc.value.status_code == 404
        assert "not available" in exc.value.detail


def test_a_live_refusal_teaches_the_cache(monkeypatch):
    """The symptom in the issue — every real transcription answering 401 — must
    hide the mic on the next load even if the probe never ran."""
    monkeypatch.setattr(stt, "ensure_capability",
                        lambda *a, **kw: _ready(stt.SttCapability(stt.VERDICT_UNKNOWN)))
    _stub_provider(monkeypatch, status=401, body=MISSING)   # the REAL /stt call

    async def _call():
        return await portal_service.transcribe_portal_audio(
            "acme-bot", "client@example.com", "voice.webm", "audio/webm", b"x" * 4000
        )

    with patch.object(portal_service, "agent_on_roster", return_value=True), \
            patch.object(tts_service, "is_available", return_value=True), \
            patch("services.settings_service.settings_service.get_elevenlabs_api_key",
                  return_value=KEY):
        with pytest.raises(ClientPortalError) as exc:
            asyncio.run(_call())
    assert exc.value.status_code == 422
    assert stt.read_cached(KEY).verdict == stt.VERDICT_REFUSED


def test_a_live_non_auth_failure_teaches_nothing(monkeypatch):
    stt.record_live_refusal(KEY, 422, "bad audio")
    assert stt.read_cached(KEY) is None


async def _ready(value):
    return value


def test_settings_state_distinguishes_configured_from_can_transcribe(monkeypatch):
    from routers import settings as settings_router

    with patch("services.settings_service.settings_service.get_elevenlabs_api_key",
               return_value=KEY), \
            patch("services.settings_service.settings_service.elevenlabs_key_source",
                  return_value="override"), \
            patch("services.settings_service.settings_service.get_default_voice_id",
                  return_value=None):
        _stub_provider(monkeypatch, status=401, body=MISSING)
        state = asyncio.run(settings_router._elevenlabs_settings_state_with_capability())

    assert state["key_configured"] is True
    assert state["stt_capability"] == stt.VERDICT_REFUSED
    assert state["stt_detail"] == "missing_permissions"
    assert state["stt_checked_at"] is not None
    assert KEY not in json.dumps(state)


def test_settings_state_without_a_key_is_unconfigured(monkeypatch):
    from routers import settings as settings_router

    calls = _stub_provider(monkeypatch, status=422)
    with patch("services.settings_service.settings_service.get_elevenlabs_api_key",
               return_value=""), \
            patch("services.settings_service.settings_service.elevenlabs_key_source",
                  return_value="none"), \
            patch("services.settings_service.settings_service.get_default_voice_id",
                  return_value=None):
        state = asyncio.run(settings_router._elevenlabs_settings_state_with_capability())
    assert state["key_configured"] is False
    assert state["stt_capability"] == stt.VERDICT_UNCONFIGURED
    assert calls == []
