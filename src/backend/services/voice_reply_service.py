"""Per-message voice reply delivery (trinity-enterprise#117).

Backs the ``send_voice_reply`` MCP tool. Given an agent, a resolved channel
destination (channel + chat id + optional thread), and text, this synthesizes a
voice note and delivers it to the channel — reusing each adapter's send primitive.

Design mirrors ``proactive_message_service`` (an ``effect_guard`` sink):
- **Fail-soft**: any synthesis/credential/delivery miss returns
  ``VoiceReplyResult(delivered=False, reason=...)`` — never raises — so the agent
  falls back to a text reply (FR-6). The only raised error is
  ``EffectInProgressError`` (a concurrent in-flight duplicate → 409).
- **Effect idempotency (#1084)**: wrapped in ``effect_guard`` keyed on the
  resolved (chat id, channel) — never the LLM body — so a re-delivered turn does
  not double-speak.
- **Voice resolution**: agent ``tts_voice_id`` else the platform default voice.

Gating is layered: the router validates the execution + channel trigger; this
service gates on platform TTS availability, the agent-level enable, the
per-channel voice-allowed flag, and the ``TTS_MAX_CHARS`` cost cap.

**Proactive voice (abilityai/trinity-enterprise#700)** — ``send_proactive_voice``
is the addressed sibling: the caller names a recipient email + channel instead
of replying into the execution's conversation, so a run with no inbound turn (a
schedule, a loop, a reminder) can speak. It resolves the destination the way
``proactive_message_service`` does (the recipient's verified chat link) and
passes the SAME gates as proactive text — ``db.can_agent_message_email``
consent, the shared per-recipient rate limit, a ``PROACTIVE_MESSAGE`` audit row
— so voice is never the cheaper way around proactive-messaging consent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import config
from database import db
from services import idempotency_service

logger = logging.getLogger(__name__)

# Channels that support an outbound voice note today.
_SUPPORTED_CHANNELS = {"telegram", "slack", "whatsapp"}

# Channels an ADDRESSED (proactive) voice note can reach (ent#700). Slack is
# excluded: its voice path resolves the bot token by bound channel id, and a
# proactive Slack send is a DM opened per workspace — a different credential
# route, deferred rather than half-wired.
_PROACTIVE_CHANNELS = {"telegram", "whatsapp"}


@dataclass
class VoiceReplyResult:
    delivered: bool
    channel: Optional[str] = None
    reason: Optional[str] = None
    # #2157 pattern: a plain-language sentence the agent acts on, for refusals
    # where the bare reason code would mislead (ent#700 / trinity#2999).
    guidance: Optional[str] = None
    # Set with reason="text_too_long" so the caller knows the bound to fit.
    max_chars: Optional[int] = None


# Outcomes a retry of the same turn can fix: the provider blipped. Released
# from the effect guard instead of recorded, so the retry sends again. A
# configuration miss (no_binding, synthesis_failed on a bad key, hosting_failed)
# stays recorded — retrying cannot help and would only re-spend synthesis.
_TRANSIENT_REASONS = {"send_failed", "delivery_error"}


class _ReleaseClaim(Exception):
    """Raised inside the effect guard for an outcome that must NOT be replayed —
    a refusal decided before anything was sent (rate limit, no chat link) or a
    transient delivery failure — so the guard releases the claim and a retry of
    the same turn can still deliver."""

    def __init__(self, result: VoiceReplyResult):
        super().__init__(result.reason)
        self.result = result


def _too_long(channel: str, text: str) -> Optional[VoiceReplyResult]:
    """The cost cap as a named contract (trinity#2999 AC 5, ent#700 AC 6) —
    checked before synthesis so it never surfaces as ``synthesis_failed``."""
    cap = config.TTS_MAX_CHARS
    if len(text or "") <= cap:
        return None
    return VoiceReplyResult(
        False, channel, "text_too_long",
        guidance=(
            f"A voice note can be at most {cap} characters; this text is {len(text)}. "
            "Speak a short summary instead and send the detail as text."
        ),
        max_chars=cap,
    )


def _gate(agent_name: str, channel: str, text: str):
    """Shared pre-claim gating for both paths. Returns ``(voice_id, None)`` when
    voice can go out, else ``(None, VoiceReplyResult)``."""
    import services.tts_service as tts_service
    from services.settings_service import settings_service

    too_long = _too_long(channel, text)
    if too_long:
        return None, too_long
    if not tts_service.is_available():
        return None, VoiceReplyResult(False, channel, "tts_unavailable")

    cfg = db.get_tts_config(agent_name)
    if not cfg.get("enabled"):
        return None, VoiceReplyResult(False, channel, "voice_disabled")
    if not cfg.get("channels", {}).get(channel, False):
        return None, VoiceReplyResult(False, channel, "channel_disabled")

    voice_id = cfg.get("voice_id") or settings_service.get_default_voice_id()
    if not voice_id:
        return None, VoiceReplyResult(False, channel, "no_voice_id")
    return voice_id, None


async def send_voice_reply(
    agent_name: str,
    channel: str,
    chat_id: str,
    thread_id: Optional[str],
    text: str,
    *,
    execution_id: Optional[str] = None,
    dedup_label: str = "",
) -> VoiceReplyResult:
    """Synthesize + deliver ``text`` as a voice note on ``channel`` to ``chat_id``.

    Raises ``idempotency_service.EffectInProgressError`` when a voice note for this
    turn is already being sent; otherwise never raises (returns a not-delivered
    result the caller reports so the agent replies with text)."""
    channel = (channel or "").lower()
    if channel not in _SUPPORTED_CHANNELS:
        return VoiceReplyResult(False, channel, "unsupported_channel")
    if not chat_id:
        return VoiceReplyResult(False, channel, "no_destination")

    # --- Gating (before the effect claim so a no-op never occupies a key) -----
    voice_id, refused = _gate(agent_name, channel, text)
    if refused:
        return refused

    # --- Effect-scoped dedup (#1084) ------------------------------------------
    try:
        return await _guarded_in_session(
            agent_name, channel, chat_id, thread_id, text, voice_id,
            execution_id=execution_id, dedup_label=dedup_label,
        )
    except _ReleaseClaim as released:
        return released.result


async def _guarded_in_session(
    agent_name, channel, chat_id, thread_id, text, voice_id, *, execution_id, dedup_label
) -> VoiceReplyResult:
    async with idempotency_service.effect_guard(
        "voice_reply",
        {"recipient": str(chat_id), "channel": channel},
        execution_id=execution_id,
        agent_name=agent_name,
        dedup_label=dedup_label,
    ) as guard:
        if guard.replay:
            snap = guard.snapshot or {}
            return VoiceReplyResult(
                delivered=snap.get("delivered", True),
                channel=snap.get("channel", channel),
                reason=snap.get("reason"),
            )
        result = await _deliver(agent_name, channel, chat_id, thread_id, text, voice_id)
        if result.reason in _TRANSIENT_REASONS:
            raise _ReleaseClaim(result)
        guard.snapshot = {
            "delivered": result.delivered,
            "channel": result.channel,
            "reason": result.reason,
        }
        return result


async def send_proactive_voice(
    agent_name: str,
    recipient_email: str,
    channel: str,
    text: str,
    *,
    execution_id: Optional[str] = None,
    dedup_label: str = "",
) -> VoiceReplyResult:
    """Speak ``text`` to ``recipient_email`` on ``channel`` — the addressed path
    (abilityai/trinity-enterprise#700). Never needs an inbound turn.

    Order mirrors ``proactive_message_service.send_message``: consent (refused
    before any claim, so it never occupies a key) → effect guard keyed on the
    resolved ``(recipient email, channel)`` → rate limit → chat-link resolution →
    delivery. The rate limit and the audit row are the proactive text path's own
    (one per-recipient budget across text and voice). Raises
    ``EffectInProgressError`` for an in-flight duplicate; otherwise never raises.
    """
    from services.proactive_message_service import proactive_message_service as pms
    from services.settings_service import get_proactive_rate_limit

    recipient_email = (recipient_email or "").strip().lower()
    channel = (channel or "").lower()
    preview = (text or "")[:100]

    if channel not in _PROACTIVE_CHANNELS:
        if channel in _SUPPORTED_CHANNELS:
            return VoiceReplyResult(
                False, channel, "proactive_channel_unsupported",
                guidance=(
                    f"A voice note cannot be sent proactively on {channel} yet — only on "
                    "telegram or whatsapp. Use send_message to reach this person in text, "
                    "or address them on telegram/whatsapp if they are linked there."
                ),
            )
        return VoiceReplyResult(False, channel, "unsupported_channel")
    if not recipient_email:
        return VoiceReplyResult(False, channel, "no_destination")

    voice_id, refused = _gate(agent_name, channel, text)
    if refused:
        return refused

    async def _audit(success: bool, error: Optional[str] = None) -> None:
        await pms.audit_send(
            agent_name, recipient_email, channel, success, error,
            message_preview=preview, extra={"modality": "voice"},
        )

    # 1. Consent — the gate an inbound turn satisfies implicitly.
    if not db.can_agent_message_email(agent_name, recipient_email):
        await _audit(False, "not_authorized")
        return VoiceReplyResult(
            False, channel, "not_authorized",
            guidance=(
                "This person has not opted in to proactive messages from you, so no voice "
                "note was sent. Do not retry and do not message them another way; the agent "
                "owner can enable proactive messages for them."
            ),
        )

    try:
        async with idempotency_service.effect_guard(
            "voice_reply",
            {"recipient": recipient_email, "channel": channel},
            execution_id=execution_id,
            agent_name=agent_name,
            dedup_label=dedup_label,
        ) as guard:
            if guard.replay:
                snap = guard.snapshot or {}
                return VoiceReplyResult(
                    delivered=snap.get("delivered", True),
                    channel=snap.get("channel", channel),
                    reason=snap.get("reason"),
                )

            # 2. Rate limit — shared with send_message (same Redis key).
            if not pms.check_rate_limit(agent_name, recipient_email):
                await _audit(False, "rate_limited")
                limit = get_proactive_rate_limit("proactive_dm_per_recipient")
                raise _ReleaseClaim(VoiceReplyResult(
                    False, channel, "rate_limited",
                    guidance=(
                        f"You have reached the limit of {limit} proactive messages per hour "
                        "to this person (text and voice share it). Nothing was sent; try "
                        "again later rather than switching channel."
                    ),
                ))

            # 3. Destination — the recipient's verified chat link (send_message's lookup).
            chat_id = _resolve_linked_chat(agent_name, channel, recipient_email)
            if not chat_id:
                await _audit(False, "recipient_not_linked")
                raise _ReleaseClaim(VoiceReplyResult(
                    False, channel, "recipient_not_linked",
                    guidance=(
                        f"This person has no verified {channel} chat with you, so a voice "
                        "note cannot reach them there. Use send_message (channel auto) to "
                        "reach them in text."
                    ),
                ))

            result = await _deliver(agent_name, channel, chat_id, None, text, voice_id)
            if result.delivered:
                pms.increment_rate_limit(agent_name, recipient_email)
            await _audit(result.delivered, result.reason)
            if result.reason in _TRANSIENT_REASONS:
                raise _ReleaseClaim(result)
            guard.snapshot = {
                "delivered": result.delivered,
                "channel": result.channel,
                "reason": result.reason,
            }
            return result
    except _ReleaseClaim as refusal:
        return refusal.result


def _resolve_linked_chat(agent_name: str, channel: str, recipient_email: str) -> Optional[str]:
    """recipient email → the channel address, via the same verified chat-link
    lookups ``proactive_message_service`` uses. ``None`` when not linked."""
    if channel == "telegram":
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return None
        link = db.get_telegram_chat_link_by_verified_email(binding["id"], recipient_email)
        # A DM's chat id IS the user id (proactive_message_service relies on it too).
        return str(link["telegram_user_id"]) if link and link.get("telegram_user_id") else None
    if channel == "whatsapp":
        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            return None
        link = db.get_whatsapp_chat_link_by_verified_email(binding["id"], recipient_email)
        return str(link["wa_user_phone"]) if link and link.get("wa_user_phone") else None
    return None


async def _deliver(
    agent_name: str,
    channel: str,
    chat_id: str,
    thread_id: Optional[str],
    text: str,
    voice_id: str,
) -> VoiceReplyResult:
    """Channel dispatch. Reuses each adapter's stateless send primitive. Any miss
    → not-delivered (never raises)."""
    import services.tts_service as tts_service

    try:
        if channel == "telegram":
            return await _deliver_telegram(agent_name, chat_id, thread_id, text, voice_id, tts_service)
        if channel == "slack":
            return await _deliver_slack(agent_name, chat_id, thread_id, text, voice_id, tts_service)
        if channel == "whatsapp":
            return await _deliver_whatsapp(agent_name, chat_id, text, voice_id, tts_service)
    except Exception as e:  # noqa: BLE001 — voice is additive; never break the turn
        logger.warning("[voice_reply] delivery failed for %s/%s: %s", agent_name, channel, e)
        return VoiceReplyResult(False, channel, "delivery_error")
    return VoiceReplyResult(False, channel, "unsupported_channel")


async def _deliver_telegram(agent_name, chat_id, thread_id, text, voice_id, tts_service):
    from adapters.telegram_adapter import TelegramAdapter

    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        return VoiceReplyResult(False, "telegram", "no_binding")
    ogg = await tts_service.synthesize_voice_note(text, voice_id)
    if not ogg:
        return VoiceReplyResult(False, "telegram", "synthesis_failed")
    result = await TelegramAdapter()._send_voice(
        bot_token, str(chat_id), ogg, reply_to_message_id=thread_id or None
    )
    if result is None:
        return VoiceReplyResult(False, "telegram", "send_failed")
    return VoiceReplyResult(True, "telegram")


async def _deliver_slack(agent_name, chat_id, thread_id, text, voice_id, tts_service):
    from services.slack_service import slack_service

    # Slack tokens are workspace-scoped; resolve by channel id (SLACK-002 bound
    # channels). DM-default / SLACK-001 link paths fall back to text.
    bot_token = db.get_slack_bot_token_for_channel(str(chat_id))
    if not bot_token:
        return VoiceReplyResult(False, "slack", "no_binding")
    mp3 = await tts_service.synthesize_mp3(text, voice_id)  # Slack renders MP3 inline
    if not mp3:
        return VoiceReplyResult(False, "slack", "synthesis_failed")
    success, error = await slack_service.upload_file(
        bot_token=bot_token,
        channel=str(chat_id),
        filename="voice.mp3",
        content=mp3,
        thread_ts=thread_id or None,
    )
    if not success:
        logger.warning("[voice_reply] slack upload failed: %s", error)
        return VoiceReplyResult(False, "slack", "send_failed")
    return VoiceReplyResult(True, "slack")


async def _deliver_whatsapp(agent_name, to_number, text, voice_id, tts_service):
    from adapters.whatsapp_adapter import WhatsAppAdapter, _OUTBOUND_MEDIA_EXPIRES_IN
    from services.agent_shared_files_service import create_share_from_bytes

    binding = db.get_whatsapp_binding(agent_name)
    auth_token = db.get_whatsapp_auth_token(agent_name)
    if not binding or not auth_token:
        return VoiceReplyResult(False, "whatsapp", "no_binding")
    ogg = await tts_service.synthesize_voice_note(text, voice_id)
    if not ogg:
        return VoiceReplyResult(False, "whatsapp", "synthesis_failed")
    # Host the transient voice note for Twilio to fetch (voice-out has its own gate,
    # so this bypasses the file-sharing toggle; MIME/quota/disk checks still apply).
    # ent#549: the note is hosted in the store the Workspace Files tab lists, so it
    # is addressed to the number it is going to — a `voice.ogg` for one person
    # used to appear in every rostered client's tab.
    from services.turn_audience import whatsapp_recipient
    addressed_email, addressed_channel = whatsapp_recipient(binding.get("id"), to_number)
    try:
        share = create_share_from_bytes(
            agent_name,
            ogg,
            display_name="voice.ogg",
            expires_in=_OUTBOUND_MEDIA_EXPIRES_IN,
            created_by=agent_name,
            require_sharing_enabled=False,
            addressed_to_email=addressed_email,
            addressed_to_channel=addressed_channel,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[voice_reply] whatsapp hosting failed for %s: %s", agent_name, e)
        return VoiceReplyResult(False, "whatsapp", "hosting_failed")
    url = share.get("url") or ""
    if not url:
        return VoiceReplyResult(False, "whatsapp", "hosting_failed")
    result = await WhatsAppAdapter._send_message(
        account_sid=binding["account_sid"],
        auth_token=auth_token,
        from_number=binding["from_number"],
        messaging_service_sid=binding.get("messaging_service_sid"),
        to_number=str(to_number),
        body="",
        media_url=url,
    )
    if result is None:
        return VoiceReplyResult(False, "whatsapp", "send_failed")
    return VoiceReplyResult(True, "whatsapp")
