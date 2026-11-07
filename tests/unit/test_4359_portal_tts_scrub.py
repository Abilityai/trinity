"""Workspace read-aloud scrubs staged credential values before synthesis (abilityai/trinity#4359).

``client_portal.service.synthesize_portal_tts`` speaks text the Workspace client
sends back (the reply it already shows). It passed that text straight to
``tts_service.synthesize_mp3``, so a value staged in the ent#279 runtime scrub
seam after the reply was saved — or text that never went through a scrubbed
save — reached the third-party TTS provider as given. ``send_message`` and both
voice sends (#4228) scrub first; this surface now does too.

Each test drives the real ``synthesize_portal_tts`` and the REAL
``tts_service.synthesize_mp3`` (so the shared cost cap is the real one), with the
REAL seam module over fakeredis; the value is staged through ``stage_secret``.
Faked: the Redis store (fakeredis), the seam's encryption (``_FakeEnc``), the
roster / availability / voice gates, the API key, and the provider's HTTP client.
Assertions read the JSON body the provider was sent.
"""

from __future__ import annotations

import contextlib
import json
import types
from unittest.mock import patch

import fakeredis
import pytest

pytestmark = pytest.mark.unit

AGENT = "brief-bot"
EMAIL = "person@example.com"
MARK = "***REDACTED***"
# Long enough to be staged (floor is 8) and shaped like no known credential, so
# only the staged-value store can catch it — the pattern sanitizer leaves it
# alone, which test_the_secret_is_invisible_to_the_pattern_sanitizer pins.
SECRET = "plum-orchid-velvet-ember-4359"


class _FakeEnc:
    """The seam's only contract with the encryption service: encrypt(dict)->str,
    decrypt(str)->dict."""

    def encrypt(self, d: dict) -> str:
        return "ENV:" + json.dumps(d)

    def decrypt(self, s) -> dict:
        return json.loads(s[len("ENV:"):])


@pytest.fixture
def seam(monkeypatch):
    """The real runtime_secret_scrub module, backed by fakeredis. The portal
    service imports the seam inside the function, so it resolves this module
    object at call time and reads the fake store below."""
    import services.runtime_secret_scrub as rss

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    state = types.SimpleNamespace(m=rss, fake=fake, client=fake)
    monkeypatch.setattr(rss, "get_breaker_redis", lambda: state.client)
    monkeypatch.setattr(rss, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(rss, "_redis_down_until", 0.0)
    return state


class _Provider:
    """Stands in for httpx.AsyncClient inside tts_service: records the text in
    every request body and answers 200 with MP3 bytes."""

    def __init__(self):
        self.sent: list[str] = []

    def client(self, *a, **k):
        provider = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                provider.sent.append(json["text"])
                return types.SimpleNamespace(status_code=200, content=b"MP3", text="")

        return _Client()


@contextlib.contextmanager
def _world():
    import client_portal.service as svc
    from services import tts_service

    provider = _Provider()
    with patch.object(svc, "agent_on_roster", return_value=True), \
         patch.object(tts_service, "is_available", return_value=True), \
         patch.object(tts_service, "resolve_voice_id", return_value="v1"), \
         patch.object(tts_service, "_resolve_api_key", return_value="test-key"), \
         patch.object(tts_service.httpx, "AsyncClient", new=provider.client):
        yield provider


async def _speak(text):
    import client_portal.service as svc

    return await svc.synthesize_portal_tts(AGENT, EMAIL, text)


def _stage(seam, value):
    seam.m.stage_secret(AGENT, value)
    assert seam.m.get_staged_values() == [value]  # the store really holds it


def test_the_secret_is_invisible_to_the_pattern_sanitizer():
    """Precondition for every test below: a pattern-based scrub would not catch
    these values, so a green test proves the staged-value store was read."""
    import config
    from utils.credential_sanitizer import sanitize_text

    for text in (f"Your new key is {SECRET}, keep it safe.",
                 "x" * (config.TTS_MAX_CHARS - 20) + SECRET, "Zq7xR2pW"):
        assert sanitize_text(text) == text


@pytest.mark.asyncio
async def test_provider_never_receives_a_staged_value(seam):
    _stage(seam, SECRET)
    with _world() as provider:
        audio = await _speak(f"  Your new key is {SECRET}, keep it safe.  ")
    assert audio == b"MP3"
    assert provider.sent == [f"Your new key is {MARK}, keep it safe."]


@pytest.mark.asyncio
async def test_nothing_staged_speaks_the_text_unchanged(seam):
    assert seam.m.get_staged_values() == []
    text = f"Plain update {SECRET[:6]} with no secret in it."
    with _world() as provider:
        audio = await _speak(f"  {text}\n")
    assert audio == b"MP3"
    assert provider.sent == [text]


@pytest.mark.asyncio
async def test_store_outage_speaks_the_text_as_given(seam):
    """The read side of the seam fails open, like send_message and the voice
    sends: with the store unreachable nothing can be scrubbed, and the reply is
    still spoken — never a 5xx. (The stage side fails closed, so no NEW value is
    delivered while the store is down.)"""
    _stage(seam, SECRET)
    seam.client = None  # the store goes down after the value was staged
    text = f"key {SECRET}"
    with _world() as provider:
        audio = await _speak(text)
    assert audio == b"MP3"
    assert provider.sent == [text]


@pytest.mark.asyncio
async def test_scrub_that_grows_the_text_past_the_cap_is_refused(seam):
    """An 8-char value becomes the 14-char marker: a raw text exactly at the cap
    is 6 over it once scrubbed. The real cost cap must judge the scrubbed text,
    so the provider is never called and the client keeps its text (422)."""
    import client_portal.service as svc
    import config

    cap = config.TTS_MAX_CHARS
    short = "Zq7xR2pW"
    assert len(short) == 8 and len(MARK) == 14
    _stage(seam, short)
    text = "x" * (cap - len(short)) + short
    assert len(text) == cap
    with _world() as provider:
        with pytest.raises(svc.ClientPortalError) as e:
            await _speak(text)
    assert e.value.status_code == 422
    assert provider.sent == []


@pytest.mark.asyncio
async def test_scrub_that_shrinks_the_text_under_the_cap_is_spoken(seam):
    import config

    cap = config.TTS_MAX_CHARS
    _stage(seam, SECRET)
    text = "x" * (cap - 20) + SECRET
    assert len(text) > cap
    with _world() as provider:
        audio = await _speak(text)
    assert audio == b"MP3"
    assert provider.sent == ["x" * (cap - 20) + MARK]
    assert len(provider.sent[0]) <= cap
