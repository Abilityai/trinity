"""Proactive voice notes — an addressing surface for send_voice_reply
(abilityai/trinity-enterprise#700; defect half abilityai/trinity#2999).

Before this, ``send_voice_reply`` could only speak into the conversation the
execution row was stamped with, and only an inbound channel turn stamps it — so
a scheduled run got ``not_a_channel_turn`` while ``send_message`` to the same
person on the same channel delivered. The tool now also takes
``recipient_email`` + ``channel``. Addressed sends must pass the SAME gates as
proactive text — consent, the shared per-recipient rate limit, an audit row —
or a voice note becomes the cheaper way around proactive-messaging consent.

Each test drives the real service / real route; only the db reads, the
proactive-message gate primitives, and the channel provider are stubbed.
"""

from __future__ import annotations

import contextlib
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services.settings_service import settings_service

pytestmark = pytest.mark.unit

AGENT = "brief-bot"
EMAIL = "person@example.com"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Guard:
    """A fake effect_guard that records the identity it was claimed with and
    whether the body exited by exception (= the claim was released)."""

    def __init__(self, replay=False, snapshot=None):
        self.replay = replay
        self.snapshot_in = snapshot
        self.calls = []
        self.released = None
        self.completed_snapshot = None

    def __call__(self, effect_type, identity, **kw):
        self.calls.append((effect_type, identity, kw))
        outer = self

        @contextlib.asynccontextmanager
        async def _cm():
            state = types.SimpleNamespace(
                replay=outer.replay, snapshot=outer.snapshot_in, dedup_enabled=True
            )
            try:
                yield state
            except BaseException:
                outer.released = True
                raise
            outer.released = False
            outer.completed_snapshot = state.snapshot

        return _cm()


def _cfg(enabled=True, voice_id="v1", channels=None):
    return {
        "enabled": enabled,
        "voice_id": voice_id,
        "channels": channels or {"telegram": True, "slack": True, "whatsapp": True},
    }


@contextlib.contextmanager
def _world(
    *,
    consent=True,
    rate_ok=True,
    tg_link={"telegram_user_id": "777000"},
    tg_send=None,
    bot_token="bot-tok",
    wa_link={"wa_user_phone": "whatsapp:+15550001"},
    guard=None,
    cfg=None,
):
    """Patch the outside world for a proactive voice send. Yields a namespace
    of the mocks the assertions read."""
    import services.voice_reply_service as svc
    from services.proactive_message_service import proactive_message_service as pms
    # Import BEFORE patching: whatsapp_adapter binds `whatsapp_recipient` at
    # import time, so a first import under the patch would keep the mock for
    # the rest of the session and leak into other files.
    import adapters.whatsapp_adapter  # noqa: F401

    guard = guard or _Guard()
    m = types.SimpleNamespace(
        guard=guard,
        consent=MagicMock(return_value=consent),
        rate_check=MagicMock(return_value=rate_ok),
        rate_incr=MagicMock(),
        audit=AsyncMock(),
        tg_send=tg_send or AsyncMock(return_value={"message_id": 9}),
        wa_send=AsyncMock(return_value={"sid": "SM1"}),
        synth=AsyncMock(return_value=b"ogg"),
    )
    with patch("services.tts_service.is_available", return_value=True), \
         patch.object(svc.db, "get_tts_config", return_value=cfg or _cfg()), \
         patch.object(settings_service, "get_default_voice_id", return_value=None), \
         patch.object(svc.idempotency_service, "effect_guard", new=guard), \
         patch.object(svc.db, "can_agent_message_email", new=m.consent), \
         patch.object(pms, "check_rate_limit", new=m.rate_check), \
         patch.object(pms, "increment_rate_limit", new=m.rate_incr), \
         patch.object(pms, "audit_send", new=m.audit), \
         patch.object(svc.db, "get_telegram_binding", return_value={"id": 5}), \
         patch.object(svc.db, "get_telegram_chat_link_by_verified_email", return_value=tg_link), \
         patch.object(svc.db, "get_telegram_bot_token", return_value=bot_token), \
         patch.object(svc.db, "get_whatsapp_binding",
                      return_value={"id": 6, "account_sid": "AC", "from_number": "whatsapp:+1",
                                    "messaging_service_sid": None}), \
         patch.object(svc.db, "get_whatsapp_chat_link_by_verified_email", return_value=wa_link), \
         patch.object(svc.db, "get_whatsapp_auth_token", return_value="tw-tok"), \
         patch("services.tts_service.synthesize_voice_note", new=m.synth), \
         patch("adapters.telegram_adapter.TelegramAdapter._send_voice", new=m.tg_send), \
         patch("services.agent_shared_files_service.create_share_from_bytes",
               return_value={"url": "https://x/api/files/1?sig=abc"}), \
         patch("services.turn_audience.whatsapp_recipient", return_value=(EMAIL, "whatsapp:+15550001")), \
         patch("adapters.whatsapp_adapter.WhatsAppAdapter._send_message", new=m.wa_send):
        yield m


async def _proactive(channel="telegram", text="Good morning.", **kw):
    import services.voice_reply_service as svc

    return await svc.send_proactive_voice(
        AGENT, EMAIL, channel, text, execution_id="exec-sched", **kw
    )


# ---------------------------------------------------------------------------
# service: the addressed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_delivers_to_the_recipients_linked_chat():
    with _world() as m:
        res = await _proactive()
    assert res.delivered is True, res
    assert res.channel == "telegram"
    # Resolved like send_message: the verified chat link's telegram user id.
    assert m.tg_send.await_args.args[1] == "777000"
    # Gates the proactive text path enforces.
    m.consent.assert_called_once_with(AGENT, EMAIL)
    m.rate_check.assert_called_once_with(AGENT, EMAIL)
    m.rate_incr.assert_called_once_with(AGENT, EMAIL)
    audit = m.audit.await_args
    assert audit.args[:4] == (AGENT, EMAIL, "telegram", True)
    assert audit.kwargs["extra"] == {"modality": "voice"}
    # #1084 dedup on the resolved (recipient, channel) — not the chat id, not the body.
    effect_type, identity, kw = m.guard.calls[0]
    assert effect_type == "voice_reply"
    assert identity == {"recipient": EMAIL, "channel": "telegram"}
    assert kw["execution_id"] == "exec-sched"


@pytest.mark.asyncio
async def test_whatsapp_delivers_to_the_linked_number():
    with _world() as m:
        res = await _proactive(channel="whatsapp")
    assert res.delivered is True, res
    assert m.wa_send.await_args.kwargs["to_number"] == "whatsapp:+15550001"
    m.rate_incr.assert_called_once()


@pytest.mark.asyncio
async def test_recipient_email_is_normalised_before_gates_and_dedup():
    import services.voice_reply_service as svc

    with _world() as m:
        res = await svc.send_proactive_voice(
            AGENT, "Person@Example.COM", "telegram", "hi", execution_id="e"
        )
    assert res.delivered is True
    m.consent.assert_called_once_with(AGENT, EMAIL)
    assert m.guard.calls[0][1]["recipient"] == EMAIL


@pytest.mark.asyncio
async def test_no_consent_is_refused_before_any_claim_or_send():
    with _world(consent=False) as m:
        res = await _proactive()
    assert res.delivered is False
    assert res.reason == "not_authorized"
    assert res.guidance
    assert m.guard.calls == [], "a refused send must not occupy an effect key"
    m.tg_send.assert_not_called()
    m.synth.assert_not_called()
    assert m.audit.await_args.args[3] is False


@pytest.mark.asyncio
async def test_rate_limit_is_shared_with_text_and_releases_the_claim():
    with _world(rate_ok=False) as m:
        res = await _proactive()
    assert res.delivered is False
    assert res.reason == "rate_limited"
    assert res.guidance
    m.tg_send.assert_not_called()
    m.rate_incr.assert_not_called()
    assert m.guard.released is True, "a pre-send refusal must release the claim so a retry can deliver"


@pytest.mark.asyncio
async def test_unlinked_recipient_is_named_and_releases_the_claim():
    with _world(tg_link=None) as m:
        res = await _proactive()
    assert res.delivered is False
    assert res.reason == "recipient_not_linked"
    assert res.guidance
    m.tg_send.assert_not_called()
    m.rate_incr.assert_not_called()
    assert m.guard.released is True


@pytest.mark.asyncio
async def test_replay_does_not_resend_or_spend_rate_budget():
    snap = {"delivered": True, "channel": "telegram", "reason": None}
    with _world(guard=_Guard(replay=True, snapshot=snap)) as m:
        res = await _proactive()
    assert res.delivered is True
    m.tg_send.assert_not_called()
    m.rate_check.assert_not_called()
    m.rate_incr.assert_not_called()


@pytest.mark.asyncio
async def test_slack_is_refused_by_name():
    with _world() as m:
        res = await _proactive(channel="slack")
    assert res.delivered is False
    assert res.reason == "proactive_channel_unsupported"
    assert res.guidance
    assert m.guard.calls == []


@pytest.mark.asyncio
async def test_channel_voice_flag_still_applies():
    cfg = _cfg(channels={"telegram": False, "slack": True, "whatsapp": True})
    with _world(cfg=cfg) as m:
        res = await _proactive()
    assert res.reason == "channel_disabled"
    m.tg_send.assert_not_called()


# ---------------------------------------------------------------------------
# service: the cost cap is a named reason on BOTH paths (#2999 AC 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_cap_text_is_text_too_long_on_the_proactive_path():
    import config

    with _world() as m:
        res = await _proactive(text="x" * (config.TTS_MAX_CHARS + 1))
    assert res.reason == "text_too_long"
    assert res.max_chars == config.TTS_MAX_CHARS
    assert res.guidance
    m.synth.assert_not_called()
    m.consent.assert_not_called()


@pytest.mark.asyncio
async def test_over_cap_text_is_text_too_long_in_session_and_at_cap_is_not():
    import config
    import services.voice_reply_service as svc

    with _world() as m:
        over = await svc.send_voice_reply(
            AGENT, "telegram", "chat1", None, "x" * (config.TTS_MAX_CHARS + 1)
        )
        at = await svc.send_voice_reply(
            AGENT, "telegram", "chat1", None, "x" * config.TTS_MAX_CHARS
        )
    assert over.reason == "text_too_long"
    assert at.delivered is True
    assert m.synth.await_count == 1


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


def _execution(**over):
    row = types.SimpleNamespace(
        id="exec-sched",
        agent_name=AGENT,
        status="running",
        triggered_by="schedule",
        source_channel=None,
        source_channel_chat_id=None,
        source_channel_thread=None,
        source_channel_agent=None,
        source_channel_client=None,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


async def _route(execution, body, *, key_agent=AGENT):
    import routers.agents as ar
    from services import voice_reply_service

    calls = {"in_session": [], "proactive": []}

    async def _in_session(**kw):
        calls["in_session"].append(kw)
        return voice_reply_service.VoiceReplyResult(True, kw["channel"])

    async def _proactive_send(agent_name, recipient_email, channel, text, **kw):
        calls["proactive"].append(
            dict(agent_name=agent_name, recipient_email=recipient_email,
                 channel=channel, text=text, **kw)
        )
        return voice_reply_service.VoiceReplyResult(True, channel)

    db_mock = MagicMock()
    db_mock.get_execution.return_value = execution
    user = types.SimpleNamespace(agent_name=key_agent, id=1, username="u", role="user")
    with patch("routers.agents.db", db_mock), \
         patch.object(voice_reply_service, "send_voice_reply", _in_session), \
         patch.object(voice_reply_service, "send_proactive_voice", _proactive_send):
        res = await ar.send_voice_reply_endpoint(AGENT, body, user)
    return res, calls


@pytest.mark.asyncio
async def test_a_scheduled_run_can_address_a_recipient():
    from models import VoiceReplyRequest

    res, calls = await _route(
        _execution(),
        VoiceReplyRequest(text="Morning brief.", execution_id="exec-sched",
                          recipient_email=EMAIL, channel="telegram"),
    )
    assert res["delivered"] is True, res
    assert calls["in_session"] == []
    assert calls["proactive"] == [dict(
        agent_name=AGENT, recipient_email=EMAIL, channel="telegram",
        text="Morning brief.", execution_id="exec-sched", dedup_label="",
    )]


@pytest.mark.asyncio
async def test_an_unaddressed_scheduled_call_says_how_to_address():
    from models import VoiceReplyRequest

    res, calls = await _route(
        _execution(), VoiceReplyRequest(text="hi", execution_id="exec-sched")
    )
    assert res["delivered"] is False
    assert res["reason"] == "not_a_channel_turn"
    assert "recipient_email" in res["guidance"]
    assert calls == {"in_session": [], "proactive": []}


@pytest.mark.asyncio
async def test_the_in_session_reply_is_unchanged():
    from models import VoiceReplyRequest

    res, calls = await _route(
        _execution(triggered_by="telegram", source_channel="telegram",
                   source_channel_chat_id="123", source_channel_thread="42"),
        VoiceReplyRequest(text="hi", execution_id="exec-sched"),
    )
    assert res["delivered"] is True
    assert calls["proactive"] == []
    assert calls["in_session"][0]["chat_id"] == "123"


@pytest.mark.asyncio
async def test_a_delegated_child_may_address_because_the_address_is_consent_gated():
    """FR-9 refuses the inherited conversation because that route had no
    consent check; an explicit address goes through the consent gate."""
    from models import VoiceReplyRequest

    res, calls = await _route(
        _execution(triggered_by="agent", source_channel="telegram",
                   source_channel_chat_id="123", source_channel_agent="parent"),
        VoiceReplyRequest(text="done", execution_id="exec-sched",
                          recipient_email=EMAIL, channel="telegram"),
    )
    assert res["delivered"] is True
    assert len(calls["proactive"]) == 1
    assert calls["in_session"] == []


@pytest.mark.asyncio
async def test_an_agent_key_still_speaks_only_as_itself_when_addressing():
    from models import VoiceReplyRequest

    with pytest.raises(HTTPException) as e:
        await _route(
            _execution(),
            VoiceReplyRequest(text="x", execution_id="exec-sched",
                              recipient_email=EMAIL, channel="telegram"),
            key_agent="other-agent",
        )
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_a_foreign_execution_is_not_a_dedup_scope():
    from models import VoiceReplyRequest

    res, calls = await _route(
        _execution(agent_name="someone-else"),
        VoiceReplyRequest(text="x", execution_id="exec-sched",
                          recipient_email=EMAIL, channel="telegram"),
    )
    assert res["reason"] == "execution_not_found"
    assert calls["proactive"] == []


@pytest.mark.asyncio
async def test_route_surfaces_service_guidance_and_max_chars():
    import routers.agents as ar
    from models import VoiceReplyRequest
    from services import voice_reply_service

    async def _refuse(*a, **kw):
        return voice_reply_service.VoiceReplyResult(
            False, "telegram", "text_too_long", guidance="shorten it", max_chars=1500
        )

    db_mock = MagicMock()
    db_mock.get_execution.return_value = _execution()
    user = types.SimpleNamespace(agent_name=AGENT, id=1, username="u", role="user")
    with patch("routers.agents.db", db_mock), \
         patch.object(voice_reply_service, "send_proactive_voice", _refuse):
        res = await ar.send_voice_reply_endpoint(
            AGENT,
            VoiceReplyRequest(text="x", execution_id="exec-sched",
                              recipient_email=EMAIL, channel="telegram"),
            user,
        )
    assert res == {"delivered": False, "channel": "telegram", "reason": "text_too_long",
                   "guidance": "shorten it", "max_chars": 1500}


# ---------------------------------------------------------------------------
# request model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fields", [
    {"recipient_email": EMAIL},
    {"channel": "telegram"},
])
def test_address_needs_both_recipient_and_channel(fields):
    from models import VoiceReplyRequest

    with pytest.raises(ValidationError):
        VoiceReplyRequest(text="x", execution_id="e", **fields)


def test_unknown_channel_is_rejected():
    from models import VoiceReplyRequest

    with pytest.raises(ValidationError):
        VoiceReplyRequest(text="x", execution_id="e", recipient_email=EMAIL, channel="web")


# ---------------------------------------------------------------------------
# Q3: a transient delivery failure releases the dedup slot; config refusals don't
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_send_failure_releases_the_claim_on_the_proactive_path():
    with _world(tg_send=AsyncMock(return_value=None)) as m:
        res = await _proactive()
    assert res.delivered is False
    assert res.reason == "send_failed"
    assert m.guard.released is True, "a retry in the same run must try the send again"
    m.rate_incr.assert_not_called()


@pytest.mark.asyncio
async def test_a_transient_send_failure_releases_the_claim_in_session():
    import services.voice_reply_service as svc

    with _world(tg_send=AsyncMock(return_value=None)) as m:
        res = await svc.send_voice_reply(AGENT, "telegram", "chat1", None, "hi", execution_id="e")
    assert res.reason == "send_failed"
    assert m.guard.released is True


@pytest.mark.asyncio
async def test_a_config_refusal_inside_the_guard_stays_recorded():
    """No bot credential is configuration, not a blip — retrying cannot help, so
    the outcome is recorded for replay rather than released."""
    with _world(bot_token=None) as m:
        res = await _proactive()
    assert res.reason == "no_binding"
    assert m.guard.released is False
    assert m.guard.completed_snapshot["reason"] == "no_binding"


# ---------------------------------------------------------------------------
# Q1: role addressing (`to`) — the one resolver send_message uses (ent#606)
# ---------------------------------------------------------------------------

OWNER = "owner@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"


class _Provider:
    def __init__(self, people):
        self.people = people

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.people.get(role)
        return None if value is None else {"emails": value}


@pytest.fixture
def ra(monkeypatch):
    from services import assignment_provider, role_addressing

    assignment_provider.clear_provider()
    monkeypatch.setattr(role_addressing, "owner_email", lambda agent: OWNER)
    yield role_addressing
    assignment_provider.clear_provider()


def _provide(people):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(people))


def test_the_service_resolver_returns_the_one_person_in_the_role(ra):
    _provide({"approver": [ALICE]})
    assert ra.resolve_single_recipient(AGENT, to="approver", recipient_email=None) == ALICE
    assert ra.resolve_single_recipient(AGENT, to=None, recipient_email=BOB) == BOB


@pytest.mark.parametrize("people,code", [
    ({"viewer": [ALICE, BOB]}, "role_resolves_to_several"),
    ({}, "role_unassigned"),
])
def test_the_service_resolver_refuses_by_name_without_naming_anyone(ra, people, code):
    _provide(people)
    with pytest.raises(ra.RecipientRefused) as e:
        ra.resolve_single_recipient(AGENT, to="viewer", recipient_email=None, noun="voice note")
    assert e.value.detail["code"] == code
    assert e.value.detail["role"] == "viewer"
    assert ALICE not in str(e.value.detail) and BOB not in str(e.value.detail)


def test_the_messages_router_uses_the_service_resolver(ra, monkeypatch):
    """Invariant #1: the rule lives in the service; the router only maps it."""
    from routers import messages
    from models import SendMessageRequest

    seen = []
    monkeypatch.setattr(ra, "resolve_single_recipient",
                        lambda *a, **kw: seen.append(kw) or "someone@example.com")
    out = messages._recipient(AGENT, SendMessageRequest(text="hi", to="primary"))
    assert out == "someone@example.com"
    assert seen and seen[0]["to"] == "primary"


@pytest.mark.asyncio
async def test_a_scheduled_run_can_address_a_role(ra):
    from models import VoiceReplyRequest

    _provide({"approver": [ALICE]})
    res, calls = await _route(
        _execution(),
        VoiceReplyRequest(text="Brief.", execution_id="exec-sched", to="approver", channel="telegram"),
    )
    assert res["delivered"] is True, res
    assert calls["proactive"][0]["recipient_email"] == ALICE


@pytest.mark.asyncio
async def test_a_role_that_reaches_several_people_is_refused_by_role(ra):
    from models import VoiceReplyRequest

    _provide({"viewer": [ALICE, BOB]})
    res, calls = await _route(
        _execution(),
        VoiceReplyRequest(text="x", execution_id="exec-sched", to="viewer", channel="telegram"),
    )
    assert res["delivered"] is False
    assert res["reason"] == "role_resolves_to_several"
    assert res["guidance"]
    assert ALICE not in str(res) and BOB not in str(res)
    assert calls["proactive"] == []


@pytest.mark.parametrize("fields", [
    {"to": "primary"},                                            # no channel
    {"to": "primary", "recipient_email": EMAIL, "channel": "telegram"},  # two addresses
    {"to": "operator", "channel": "telegram"},                    # not a message recipient
])
def test_a_role_address_is_validated(fields):
    from models import VoiceReplyRequest

    with pytest.raises(ValidationError):
        VoiceReplyRequest(text="x", execution_id="e", **fields)
