"""Voice notes scrub staged credential values before synthesis (abilityai/trinity#4228).

``proactive_message_service.send_message`` removes every staged credential value
(the ent#279 runtime_secret_scrub seam) from a body before it is delivered. Both
voice sends — in-session ``send_voice_reply`` and the addressed
``send_proactive_voice`` — handed the agent's raw text to text-to-speech, so a
credential an agent had fetched could be spoken aloud and delivered as audio,
and the addressed path also wrote it into the audit log's ``message_preview``.

Each test drives the real service function with the REAL seam module over a fake
Redis: the value is staged through ``stage_secret`` the way a producer stages it.
Faked: the Redis store (fakeredis), the seam's encryption (``_FakeEnc``), the db
reads, the channel providers and synthesis. Assertions read what synthesis
actually received.
"""

from __future__ import annotations

import contextlib
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

pytestmark = pytest.mark.unit

AGENT = "brief-bot"
EMAIL = "person@example.com"
MARK = "***REDACTED***"
# A value the text really contains, long enough to be staged (floor is 8) and
# shaped like no known credential, so only the staged-value store can catch it:
# the pattern sanitizer (utils.credential_sanitizer) leaves it alone, which
# test_the_secret_is_invisible_to_the_pattern_sanitizer pins.
SECRET = "plum-orchid-velvet-ember-4228"


class _FakeEnc:
    """The seam's only contract with the encryption service: encrypt(dict)->str,
    decrypt(str)->dict."""

    def encrypt(self, d: dict) -> str:
        return "ENV:" + json.dumps(d)

    def decrypt(self, s) -> dict:
        return json.loads(s[len("ENV:"):])


@pytest.fixture
def seam(monkeypatch):
    """The real runtime_secret_scrub module, backed by fakeredis."""
    import services.runtime_secret_scrub as rss
    import services.voice_reply_service as svc

    # Patch what the product resolves: the voice service's seam functions must
    # live in this module object, or the fake store below would not be the one read.
    bound = getattr(svc, "get_staged_values", None)
    if bound is not None:
        assert bound.__globals__ is rss.__dict__

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    state = types.SimpleNamespace(m=rss, fake=fake, client=fake)
    monkeypatch.setattr(rss, "get_breaker_redis", lambda: state.client)
    monkeypatch.setattr(rss, "_enc_svc", lambda: _FakeEnc())
    monkeypatch.setattr(rss, "_redis_down_until", 0.0)
    return state


@contextlib.contextmanager
def _world(*, consent=True):
    """Stub everything outside the voice service and record every text handed
    to either synthesis primitive (Telegram/WhatsApp use synthesize_voice_note,
    Slack uses synthesize_mp3)."""
    import adapters.whatsapp_adapter  # noqa: F401 — import before patching (binds at import)
    import services.voice_reply_service as svc
    from services.proactive_message_service import proactive_message_service as pms
    from services.settings_service import settings_service

    m = types.SimpleNamespace(spoken=[], audit=AsyncMock(), consent=MagicMock(return_value=consent))

    async def _note(text, voice_id):
        m.spoken.append(text)
        return b"ogg"

    async def _mp3(text, voice_id):
        m.spoken.append(text)
        return b"mp3"

    @contextlib.asynccontextmanager
    async def _guard(*a, **k):
        yield types.SimpleNamespace(replay=False, snapshot=None, dedup_enabled=True)

    cfg = {"enabled": True, "voice_id": "v1",
           "channels": {"telegram": True, "slack": True, "whatsapp": True}}
    patches = [
        patch("services.tts_service.is_available", return_value=True),
        patch.object(svc.db, "get_tts_config", return_value=cfg),
        patch.object(settings_service, "get_default_voice_id", return_value=None),
        patch.object(svc.idempotency_service, "effect_guard", new=_guard),
        patch.object(svc.db, "can_agent_message_email", new=m.consent),
        patch.object(pms, "check_rate_limit", return_value=True),
        patch.object(pms, "increment_rate_limit"),
        patch.object(pms, "audit_send", new=m.audit),
        patch.object(svc.db, "get_telegram_binding", return_value={"id": 5}),
        patch.object(svc.db, "get_telegram_chat_link_by_verified_email",
                     return_value={"telegram_user_id": "777000"}),
        patch.object(svc.db, "get_telegram_bot_token", return_value="bot-tok"),
        patch.object(svc.db, "get_slack_bot_token_for_channel", return_value="xoxb"),
        patch.object(svc.db, "get_whatsapp_binding",
                     return_value={"id": 6, "account_sid": "AC", "from_number": "whatsapp:+1",
                                   "messaging_service_sid": None}),
        patch.object(svc.db, "get_whatsapp_chat_link_by_verified_email",
                     return_value={"wa_user_phone": "whatsapp:+15550001"}),
        patch.object(svc.db, "get_whatsapp_auth_token", return_value="tw-tok"),
        patch("services.tts_service.synthesize_voice_note", new=_note),
        patch("services.tts_service.synthesize_mp3", new=_mp3),
        patch("adapters.telegram_adapter.TelegramAdapter._send_voice",
              new=AsyncMock(return_value={"message_id": 9})),
        patch("services.slack_service.slack_service.upload_file",
              new=AsyncMock(return_value=(True, None))),
        patch("services.agent_shared_files_service.create_share_from_bytes",
              return_value={"url": "https://x/api/files/1?sig=abc"}),
        patch("services.turn_audience.whatsapp_recipient",
              return_value=(EMAIL, "whatsapp:+15550001")),
        patch("adapters.whatsapp_adapter.WhatsAppAdapter._send_message",
              new=AsyncMock(return_value={"sid": "SM1"})),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield m


async def _in_session(channel, text):
    import services.voice_reply_service as svc

    return await svc.send_voice_reply(AGENT, channel, "chat-1", None, text, execution_id="exec-1")


async def _addressed(channel, text):
    import services.voice_reply_service as svc

    return await svc.send_proactive_voice(AGENT, EMAIL, channel, text, execution_id="exec-1")


def test_the_secret_is_invisible_to_the_pattern_sanitizer():
    """Precondition for every test below: a pattern-based scrub would not catch
    SECRET, so a green test proves the staged-value store was read."""
    from utils.credential_sanitizer import sanitize_text

    import config

    for text in (f"Your new key is {SECRET}, keep it safe.", f"key {SECRET}",
                 "x" * (config.TTS_MAX_CHARS - 20) + SECRET, "Zq7xR2pW"):
        assert sanitize_text(text) == text


def _stage(seam, value):
    seam.m.stage_secret(AGENT, value)
    assert seam.m.get_staged_values() == [value]  # the store really holds it


# ---------------------------------------------------------------------------
# AC 1 — a staged value never reaches synthesis, on every channel of each path
# ---------------------------------------------------------------------------

_PATHS = [
    pytest.param(_in_session, "telegram", id="in-session-telegram"),
    pytest.param(_in_session, "slack", id="in-session-slack"),
    pytest.param(_in_session, "whatsapp", id="in-session-whatsapp"),
    pytest.param(_addressed, "telegram", id="addressed-telegram"),
    pytest.param(_addressed, "whatsapp", id="addressed-whatsapp"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("send, channel", _PATHS)
async def test_synthesis_never_receives_a_staged_value(seam, send, channel):
    _stage(seam, SECRET)
    text = f"Your new key is {SECRET}, keep it safe."
    with _world() as m:
        res = await send(channel, text)
    assert res.delivered is True, res
    assert m.spoken == [f"Your new key is {MARK}, keep it safe."]


@pytest.mark.asyncio
async def test_addressed_audit_preview_is_scrubbed_on_delivery(seam):
    _stage(seam, SECRET)
    with _world() as m:
        res = await _addressed("telegram", f"key {SECRET}")
    assert res.delivered is True, res
    previews = [c.kwargs["message_preview"] for c in m.audit.await_args_list]
    assert previews == [f"key {MARK}"]


@pytest.mark.asyncio
async def test_addressed_audit_preview_is_scrubbed_on_a_refusal(seam):
    """A refusal writes its audit row before anything is synthesized, and the
    audit table is append-only — the preview must already be scrubbed there."""
    _stage(seam, SECRET)
    with _world(consent=False) as m:
        res = await _addressed("telegram", f"key {SECRET}")
    assert res.reason == "not_authorized"
    assert m.spoken == []
    previews = [c.kwargs["message_preview"] for c in m.audit.await_args_list]
    assert previews == [f"key {MARK}"]


# ---------------------------------------------------------------------------
# AC 2 — text_too_long judges the text synthesis would receive
# ---------------------------------------------------------------------------

_BOTH = [
    pytest.param(_in_session, "telegram", id="in-session"),
    pytest.param(_addressed, "telegram", id="addressed"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("send, channel", _BOTH)
async def test_scrub_that_grows_the_text_past_the_cap_is_text_too_long(seam, send, channel):
    """An 8-char value becomes the 14-char marker: a raw text exactly at the cap
    is 6 over it once scrubbed. Judging the raw text would let it through to
    synthesis, where Trinity's own tts_service._within_cost_cap refuses it before
    any provider call, and it surfaces as synthesis_failed."""
    import config

    cap = config.TTS_MAX_CHARS
    short = "Zq7xR2pW"
    assert len(short) == 8 and len(MARK) == 14
    _stage(seam, short)
    text = "x" * (cap - len(short)) + short
    assert len(text) == cap
    with _world() as m:
        res = await send(channel, text)
    assert res.reason == "text_too_long", res
    assert res.max_chars == cap
    assert f"this text is {cap + 6}." in res.guidance
    assert m.spoken == []


@pytest.mark.asyncio
@pytest.mark.parametrize("send, channel", _BOTH)
async def test_scrub_that_shrinks_the_text_under_the_cap_delivers(seam, send, channel):
    import config

    cap = config.TTS_MAX_CHARS
    _stage(seam, SECRET)
    text = "x" * (cap - 20) + SECRET
    assert len(text) > cap
    with _world() as m:
        res = await send(channel, text)
    assert res.delivered is True, res
    assert m.spoken == ["x" * (cap - 20) + MARK]
    assert len(m.spoken[0]) <= cap


# ---------------------------------------------------------------------------
# Other shapes: nothing staged, and a store outage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("send, channel", _PATHS)
async def test_nothing_staged_speaks_the_text_unchanged(seam, send, channel):
    """A fresh install that never staged a credential: the seam's store is empty
    and the note is the agent's text, byte for byte."""
    assert seam.m.get_staged_values() == []
    text = f"Plain update {SECRET[:6]} with no secret in it."
    with _world() as m:
        res = await send(channel, text)
    assert res.delivered is True, res
    assert m.spoken == [text]


@pytest.mark.asyncio
@pytest.mark.parametrize("send, channel", _BOTH)
async def test_store_outage_behaves_like_send_message(seam, send, channel):
    """The read side of the seam fails open: with the store unreachable nothing
    can be scrubbed. Voice must do exactly what send_message does then — deliver
    the text as given, never refuse or raise."""
    from services import proactive_message_service as pms

    _stage(seam, SECRET)
    seam.client = None  # the store goes down after the value was staged
    text = f"key {SECRET}"

    captured = {}

    async def _inner(self, agent_name, recipient, text, channel, reply, **_intent):
        captured["text"] = text
        return pms.DeliveryResult(success=True, channel="web", message_id="m1")

    @contextlib.asynccontextmanager
    async def _guard(*a, **k):
        yield types.SimpleNamespace(replay=False, snapshot=None, dedup_enabled=True)

    with patch.object(pms.idempotency_service, "effect_guard", new=_guard), \
         patch.object(pms.ProactiveMessageService, "_send_message_inner", _inner):
        await pms.ProactiveMessageService().send_message(
            agent_name=AGENT, recipient_email=EMAIL, text=text, channel="web",
        )
    with _world() as m:
        res = await send(channel, text)

    assert res.delivered is True, res
    assert m.spoken == [captured["text"]]
    # Today both pass the text through unchanged (the seam's stage side fails
    # closed, so no NEW value is staged while the store is down).
    assert captured["text"] == text
