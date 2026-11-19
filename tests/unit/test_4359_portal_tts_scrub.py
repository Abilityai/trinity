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

What is spoken carries the seam's spoken placeholder, a plain word, not the stored
marker. It is never longer than a staged value, so the cost cap judges the text
as the client sent it, and the status a client gets does not depend on what is
staged.

The direct voice call (``DirectBridge.speak_execution``) is driven too: its reply
text is scrubbed before ``speakable()`` rewrites it.
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
MARK = "***REDACTED***"  # the stored marker: never spoken
SPOKEN = "redacted"  # the spoken placeholder
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
    state = types.SimpleNamespace(m=rss, fake=fake, client=fake, reads=0)
    real_hgetall = fake.hgetall

    def _counted_hgetall(*a, **k):
        state.reads += 1
        return real_hgetall(*a, **k)

    monkeypatch.setattr(fake, "hgetall", _counted_hgetall)
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
    seam.reads = 0


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
    assert provider.sent == [f"Your new key is {SPOKEN}, keep it safe."]


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
async def test_a_text_at_the_cap_still_fits_after_the_scrub(seam):
    """An 8-char value at the end of a text exactly at the cap: the placeholder is
    no longer than the value, so the real cost cap inside synthesize_mp3 still
    passes it and the provider is called."""
    import config

    cap = config.TTS_MAX_CHARS
    short = "Zq7xR2pW"
    _stage(seam, short)
    text = "x" * (cap - len(short)) + short
    assert len(text) == cap
    with _world() as provider:
        audio = await _speak(text)
    assert audio == b"MP3"
    assert provider.sent == ["x" * (cap - len(short)) + SPOKEN]


@pytest.mark.asyncio
async def test_the_status_does_not_depend_on_what_is_staged(seam):
    """The client chooses this text and the staged store is shared by every agent.
    If the cap judged the scrubbed text, an over-cap text holding a guessed value
    would be spoken (200) when the guess is staged and refused (422) when it is
    not. The cap judges the text as sent, so both are 422 and nothing is sent."""
    import client_portal.service as svc
    import config

    cap = config.TTS_MAX_CHARS
    text = "x" * (cap - 20) + SECRET
    assert len(text) > cap
    statuses = []
    for staged in (False, True):
        if staged:
            _stage(seam, SECRET)
        with _world() as provider:
            with pytest.raises(svc.ClientPortalError) as e:
                await _speak(text)
        statuses.append(e.value.status_code)
        assert provider.sent == []
    assert statuses == [422, 422]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate, expected", [
    pytest.param("is_available", 404, id="voice-unavailable"),
    pytest.param("resolve_voice_id", 404, id="no-voice"),
])
async def test_a_refusal_never_reads_the_store(seam, gate, expected):
    import client_portal.service as svc
    from services import tts_service

    _stage(seam, SECRET)
    with _world() as provider, patch.object(tts_service, gate, return_value=None):
        with pytest.raises(svc.ClientPortalError) as e:
            await _speak(f"key {SECRET}")
    assert e.value.status_code == expected
    assert seam.reads == 0
    assert provider.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value, text", [
    pytest.param("plum-orchid-velvet ", "Your key is plum-orchid-velvet ", id="trailing-space"),
    pytest.param(" plum-orchid-velvet", " plum-orchid-velvet is your key", id="leading-space"),
])
async def test_a_value_with_edge_whitespace_is_scrubbed_before_the_strip(seam, value, text):
    """The Workspace secret field stages a value as typed, edge whitespace included
    (credential_sink only refuses an all-blank one). Stripping the text first would
    break the exact match at the text's edge and send the raw value to the provider,
    so the scrub must see the text as the client sent it."""
    _stage(seam, value)
    with _world() as provider:
        audio = await _speak(text)
    assert audio == b"MP3"
    assert len(provider.sent) == 1
    assert value.strip() not in provider.sent[0]
    assert SPOKEN in provider.sent[0]


# ---------------------------------------------------------------------------
# The direct voice call: the reply's text is scrubbed before speakable()
# ---------------------------------------------------------------------------

# speakable() drops `__` anywhere in a line, so this value would reach the
# provider as "plumorchidvelvet4359" if the scrub ran on the rewritten text.
MARKED_SECRET = "plum__orchid__velvet4359"


class _Socket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


class _SpeechProvider:
    name = "fake"

    def __init__(self):
        self.said = []

    async def synthesize_stream(self, text, voice=None):
        self.said.append(text)
        yield b"\x00\x01"


def _call(monkeypatch, *, entries=(), reply=None):
    """Drive one turn of a direct call with the real speakable/sentences and the
    real seam: the stream carries ``entries`` (Claude Code shape) and the turn
    ends with ``reply`` persisted."""
    import asyncio

    from services import direct_voice_service as dvs

    monkeypatch.setattr(dvs, "REPLY_POLL_SECONDS", 0)
    monkeypatch.setattr(dvs, "REPLY_SETTLE_SECONDS", 0)
    row = {"content": reply, "gate_hold": False} if reply is not None else None
    monkeypatch.setattr(dvs, "_read_turn_end", lambda _e: ("success", row))

    def attach(_agent, _execution):
        async def gen():
            for text in entries:
                entry = {"type": "assistant", "parent_tool_use_id": None,
                         "message": {"content": [{"type": "text", "text": text}]}}
                yield f"data: {json.dumps(entry)}\n\n"
            yield 'data: {"type": "stream_end"}\n\n'
        return gen()

    session = dvs.DirectVoiceSession(session_id="vd_x", agent_name=AGENT, user_id=1,
                                     client_email=EMAIL, portal_session_id="thread",
                                     max_duration=60)
    socket, provider = _Socket(), _SpeechProvider()

    async def go():
        bridge = dvs.DirectBridge(socket, session, provider, attach, runtime="claude-code")
        speaker = asyncio.ensure_future(bridge.run_speaker())
        await bridge.speak_execution("e1")
        while not bridge._speech.empty():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        speaker.cancel()

    asyncio.run(go())
    spoken_frames = [f["text"] for f in socket.frames if f["type"] == "spoken"]
    return provider.said, spoken_frames


def _leaks(lines, value):
    """True when any spoken line holds the value, raw or as speakable() rewrites it."""
    from services.speech.spoken_text import speakable

    return any(value in line or speakable(value) in line for line in lines)


def test_a_streamed_value_that_speakable_would_rewrite_is_never_spoken(seam, monkeypatch):
    _stage(seam, MARKED_SECRET)
    said, frames = _call(monkeypatch, entries=[f"Your key is {MARKED_SECRET}. Keep it safe."],
                         reply=f"Your key is {MARKED_SECRET}. Keep it safe.")
    assert said == [f"Your key is {SPOKEN}.", "Keep it safe."]
    assert frames == said
    assert not _leaks(said, MARKED_SECRET)


def test_a_value_staged_after_the_reply_was_saved_is_scrubbed_from_the_tail(seam, monkeypatch):
    """No stream (or a stream that said nothing): the persisted reply is spoken.
    A value staged after it was saved is still in it raw."""
    _stage(seam, MARKED_SECRET)
    said, _ = _call(monkeypatch, reply=f"Use {MARKED_SECRET} to sign in.")
    assert said == [f"Use {SPOKEN} to sign in."]


def test_the_stored_marker_in_a_saved_reply_is_said_as_the_placeholder(seam, monkeypatch):
    """A value staged before the reply was saved was scrubbed at the save, so the
    reply carries the stored marker. It is said as the spoken placeholder, not
    read out as asterisks."""
    said, frames = _call(monkeypatch, reply=f"Use {MARK} to sign in.")
    assert said == [f"Use {SPOKEN} to sign in."]
    assert frames == said


def test_a_call_with_nothing_staged_speaks_the_reply_unchanged(seam, monkeypatch):
    said, _ = _call(monkeypatch, entries=["It is Monday. Anything else?"],
                    reply="It is Monday. Anything else?")
    assert said == ["It is Monday.", "Anything else?"]
