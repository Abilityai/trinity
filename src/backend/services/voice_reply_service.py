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
service gates on platform TTS availability, the agent-level enable, and the
per-channel voice-allowed flag.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from database import db
from services import idempotency_service

logger = logging.getLogger(__name__)

# Channels that support an outbound voice note today.
_SUPPORTED_CHANNELS = {"telegram", "slack", "whatsapp"}


@dataclass
class VoiceReplyResult:
    delivered: bool
    channel: Optional[str] = None
    reason: Optional[str] = None


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
    import services.tts_service as tts_service
    from services.settings_service import settings_service

    channel = (channel or "").lower()
    if channel not in _SUPPORTED_CHANNELS:
        return VoiceReplyResult(False, channel, "unsupported_channel")
    if not chat_id:
        return VoiceReplyResult(False, channel, "no_destination")

    # --- Gating (before the effect claim so a no-op never occupies a key) -----
    if not tts_service.is_available():
        return VoiceReplyResult(False, channel, "tts_unavailable")

    cfg = db.get_tts_config(agent_name)
    if not cfg.get("enabled"):
        return VoiceReplyResult(False, channel, "voice_disabled")
    if not cfg.get("channels", {}).get(channel, False):
        return VoiceReplyResult(False, channel, "channel_disabled")

    voice_id = cfg.get("voice_id") or settings_service.get_default_voice_id()
    if not voice_id:
        return VoiceReplyResult(False, channel, "no_voice_id")

    # --- Effect-scoped dedup (#1084) ------------------------------------------
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
        guard.snapshot = {
            "delivered": result.delivered,
            "channel": result.channel,
            "reason": result.reason,
        }
        return result


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
    try:
        share = create_share_from_bytes(
            agent_name,
            ogg,
            display_name="voice.ogg",
            expires_in=_OUTBOUND_MEDIA_EXPIRES_IN,
            created_by=agent_name,
            require_sharing_enabled=False,
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
