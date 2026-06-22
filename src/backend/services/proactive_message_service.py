"""
Proactive Message Service (Issue #321).

Enables agents to send proactive messages to users across channels (Telegram, Slack, web).
Handles authorization, recipient resolution, rate limiting, and dispatch.

Key features:
- Explicit opt-in consent via allow_proactive flag on agent_sharing
- Redis-based rate limiting (survives restarts)
- Audit logging for all sends
- Channel resolution by verified email
"""

import logging
from dataclasses import dataclass
from typing import Optional, Literal

from database import db
from services import idempotency_service
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

# Rate limiting configuration
RATE_LIMIT_MAX_PER_HOUR = 10  # messages per recipient per hour
RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour


class ProactiveMessageError(Exception):
    """Base exception for proactive messaging errors."""
    pass


class NotAuthorizedError(ProactiveMessageError):
    """Recipient has not opted in to proactive messages."""
    pass


class RecipientNotFoundError(ProactiveMessageError):
    """No channel endpoint found for the recipient email."""
    pass


class RateLimitedError(ProactiveMessageError):
    """Rate limit exceeded for this agent-recipient pair."""
    pass


class ChannelDeliveryError(ProactiveMessageError):
    """Failed to deliver message via channel."""
    pass


@dataclass
class DeliveryResult:
    """Result of a proactive message delivery attempt."""
    success: bool
    channel: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class ProactiveMessageService:
    """
    Service for sending proactive messages from agents to users.

    Authorization: Agent can only message users who have:
    1. Been shared the agent AND set allow_proactive=1, OR
    2. Are the owner of the agent (always allowed)

    Channel resolution:
    - telegram: Look up telegram_chat_links by verified_email
    - slack: Look up Slack user by email via users.lookupByEmail API
    - whatsapp: Look up whatsapp_chat_links by verified_email (explicit only;
      NOT part of `auto` fallback because Twilio's 24-hour session window can
      cause unexpected failures outside recent user interaction)
    - web: WebSocket push + persist to public_chat_messages
    - auto: Try channels in order: telegram -> slack -> web
    """

    def __init__(self):
        self._redis_client = None

    def _get_redis(self):
        """Lazy-load Redis client."""
        if self._redis_client is None:
            from routers.auth import get_redis_client
            self._redis_client = get_redis_client()
        return self._redis_client

    def _rate_limit_key(self, agent_name: str, recipient_email: str) -> str:
        """Build rate limit key including both agent and recipient."""
        return f"proactive_msg:{agent_name}:{recipient_email.lower()}"

    def _check_rate_limit(self, agent_name: str, recipient_email: str) -> bool:
        """Check if sending is allowed under rate limits. Returns True if OK."""
        redis = self._get_redis()
        if not redis:
            # Redis unavailable — allow (degraded mode)
            logger.warning("Redis unavailable for rate limiting, allowing message")
            return True

        key = self._rate_limit_key(agent_name, recipient_email)
        try:
            count = redis.get(key)
            if count is None:
                return True
            return int(count) < RATE_LIMIT_MAX_PER_HOUR
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}")
            return True

    def _increment_rate_limit(self, agent_name: str, recipient_email: str) -> None:
        """Increment the rate limit counter for this agent-recipient pair."""
        redis = self._get_redis()
        if not redis:
            return

        key = self._rate_limit_key(agent_name, recipient_email)
        try:
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, RATE_LIMIT_WINDOW_SECONDS)
            pipe.execute()
        except Exception as e:
            logger.warning(f"Rate limit increment failed: {e}")

    async def _audit_send(
        self,
        agent_name: str,
        recipient_email: str,
        channel: str,
        success: bool,
        error: Optional[str] = None,
        message_preview: Optional[str] = None,
    ) -> None:
        """Log proactive message send to audit trail."""
        try:
            await platform_audit_service.log(
                event_type=AuditEventType.PROACTIVE_MESSAGE,
                event_action="send",
                source="proactive_message_service",
                actor_agent_name=agent_name,
                target_type="user",
                target_id=recipient_email,
                details={
                    "channel": channel,
                    "success": success,
                    "error": error,
                    "message_preview": message_preview[:100] if message_preview else None,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to audit proactive message: {e}")

    async def send_message(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: Literal["auto", "telegram", "slack", "whatsapp", "web"] = "auto",
        reply_to_thread: bool = False,
        execution_id: Optional[str] = None,
        dedup_label: str = "",
    ) -> DeliveryResult:
        """
        Send a proactive message to a user.

        Args:
            agent_name: The agent sending the message
            recipient_email: Verified email of the recipient
            text: Message content
            channel: Target channel or "auto" for automatic selection
            reply_to_thread: Continue in last thread if one exists (channel-dependent)
            execution_id: The execution this send belongs to (#1084). When the
                turn it ran in is re-delivered (pull-mode at-least-once), the same
                resolved (recipient, channel) within the same execution dedupes to
                exactly one send — the LLM-generated body is NOT part of the key.
                Fail-open when absent/invalid (old image): the send proceeds.
            dedup_label: Optional agent-supplied discriminator (#1084) so an agent
                can intentionally send two distinct messages to the same recipient
                in one turn. Default "" → at-most-one send per (recipient, channel)
                per turn.

        Returns:
            DeliveryResult with success status and channel used

        Raises:
            NotAuthorizedError: Recipient hasn't opted in
            RecipientNotFoundError: No channel endpoint for recipient
            RateLimitedError: Rate limit exceeded
            ChannelDeliveryError: Channel delivery failed
        """
        recipient_email = recipient_email.lower()

        # Effect-scoped dedup (#1084): guard at the entry, keyed on the resolved
        # recipient + channel (never the body). A chunked message is ONE effect —
        # a mid-chunk crash re-sends the whole message on retry (at-least-once for
        # chunks, documented in effect-idempotency.md). A concurrent in-flight
        # duplicate raises EffectInProgressError (retryable, not a silent skip).
        async with idempotency_service.effect_guard(
            "message",
            {"recipient": recipient_email, "channel": channel},
            execution_id=execution_id,
            agent_name=agent_name,
            dedup_label=dedup_label,
        ) as guard:
            if guard.replay:
                snap = guard.snapshot or {}
                return DeliveryResult(
                    success=snap.get("success", True),
                    channel=snap.get("channel", channel),
                    message_id=snap.get("message_id"),
                    error=snap.get("error"),
                )
            result = await self._send_message_inner(
                agent_name, recipient_email, text, channel, reply_to_thread
            )
            guard.snapshot = {
                "success": result.success,
                "channel": result.channel,
                "message_id": result.message_id,
                "error": result.error,
            }
            return result

    async def _send_message_inner(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: Literal["auto", "telegram", "slack", "whatsapp", "web"],
        reply_to_thread: bool,
    ) -> DeliveryResult:
        """Auth → rate-limit → channel delivery. Wrapped by send_message's
        effect guard (#1084), so a success here is recorded for replay and an
        exception releases the claim for retry."""
        message_preview = text[:100] if text else ""

        # 1. Authorization check
        if not db.can_agent_message_email(agent_name, recipient_email):
            await self._audit_send(agent_name, recipient_email, channel, False, "not_authorized")
            raise NotAuthorizedError(
                f"Agent '{agent_name}' is not authorized to message '{recipient_email}'. "
                "Recipient must opt in via allow_proactive flag."
            )

        # 2. Rate limit check
        if not self._check_rate_limit(agent_name, recipient_email):
            await self._audit_send(agent_name, recipient_email, channel, False, "rate_limited")
            raise RateLimitedError(
                f"Rate limit exceeded: max {RATE_LIMIT_MAX_PER_HOUR} messages per hour to this recipient."
            )

        # 3. Channel resolution and delivery
        channels_to_try = (
            ["telegram", "slack", "web"] if channel == "auto"
            else [channel]
        )

        last_error = None
        for ch in channels_to_try:
            try:
                result = await self._deliver_via_channel(
                    agent_name, recipient_email, text, ch, reply_to_thread
                )
                if result.success:
                    self._increment_rate_limit(agent_name, recipient_email)
                    await self._audit_send(
                        agent_name, recipient_email, ch, True,
                        message_preview=message_preview
                    )
                    return result
                last_error = result.error
            except RecipientNotFoundError:
                # Try next channel
                continue
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Proactive message to {ch} failed: {e}")
                continue

        # All channels failed
        error_msg = last_error or "No delivery channel available for recipient"
        await self._audit_send(agent_name, recipient_email, channel, False, error_msg)
        raise RecipientNotFoundError(error_msg)

    async def send_access_grant_notification(
        self,
        agent_name: str,
        recipient_email: str,
        channel: Literal["telegram", "slack", "whatsapp"],
        text: str,
    ) -> DeliveryResult:
        """Notify a user that their channel access request was just approved.

        Bypasses the `allow_proactive` opt-in (this is a response to a request
        the user explicitly initiated, not unsolicited outreach) and skips the
        per-recipient rate limit (one-shot, not a campaign). Still emits an
        audit event with the delivery outcome so #951's "delivered / skipped /
        failed" requirement is observable.

        Channel resolution is explicit — the caller passes the originating
        channel from `access_requests.channel`. Web is handled upstream via
        the existing `agent_shared` WebSocket event and never reaches here.
        """
        recipient_email = recipient_email.lower()
        message_preview = text[:100] if text else ""

        try:
            result = await self._deliver_via_channel(
                agent_name, recipient_email, text, channel, reply_to_thread=False
            )
        except RecipientNotFoundError as e:
            await self._audit_send(
                agent_name, recipient_email, channel, False,
                error=f"recipient_not_found: {e}",
                message_preview=message_preview,
            )
            return DeliveryResult(success=False, channel=channel, error=str(e))
        except Exception as e:
            logger.warning(
                f"[#951] Access-grant notification to {channel} failed: {e}"
            )
            await self._audit_send(
                agent_name, recipient_email, channel, False,
                error=str(e), message_preview=message_preview,
            )
            return DeliveryResult(success=False, channel=channel, error=str(e))

        await self._audit_send(
            agent_name, recipient_email, channel,
            success=result.success,
            error=result.error,
            message_preview=message_preview,
        )
        return result

    async def _deliver_via_channel(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: str,
        reply_to_thread: bool,
    ) -> DeliveryResult:
        """Deliver message via specific channel."""
        if channel == "telegram":
            return await self._deliver_telegram(agent_name, recipient_email, text)
        elif channel == "slack":
            return await self._deliver_slack(agent_name, recipient_email, text)
        elif channel == "whatsapp":
            return await self._deliver_whatsapp(agent_name, recipient_email, text)
        elif channel == "web":
            return await self._deliver_web(agent_name, recipient_email, text)
        else:
            raise ValueError(f"Unknown channel: {channel}")

    async def _deliver_telegram(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via Telegram."""
        from adapters.telegram_adapter import TelegramAdapter

        # Get binding for this agent
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            raise RecipientNotFoundError(f"No Telegram bot configured for agent '{agent_name}'")

        # Look up chat link by verified email
        chat_link = db.get_telegram_chat_link_by_verified_email(binding["id"], recipient_email)
        if not chat_link:
            raise RecipientNotFoundError(
                f"No Telegram user with verified email '{recipient_email}' for agent '{agent_name}'"
            )

        # Get bot token (decrypted)
        bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            raise ChannelDeliveryError("Failed to retrieve bot token")

        # Send via adapter
        adapter = TelegramAdapter()
        try:
            # The chat_link contains telegram_user_id which is the chat_id for DMs
            chat_id = chat_link["telegram_user_id"]
            result = await adapter._send_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
            )
            if result:
                message_id = result.get("message_id")
                return DeliveryResult(
                    success=True,
                    channel="telegram",
                    message_id=str(message_id) if message_id else None,
                )
            else:
                return DeliveryResult(success=False, channel="telegram", error="Send failed")
        except Exception as e:
            logger.error(f"Telegram delivery failed: {e}")
            return DeliveryResult(success=False, channel="telegram", error=str(e))

    async def _deliver_slack(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via Slack DM."""
        from services.slack_service import slack_service

        # Get Slack workspace connections
        # We need to find a workspace where this user exists
        workspaces = db.get_all_slack_workspaces()
        if not workspaces:
            raise RecipientNotFoundError("No Slack workspaces connected")

        for workspace in workspaces:
            bot_token = workspace.get("bot_token")
            if not bot_token:
                continue

            # Try to find user by email in this workspace
            user = await slack_service.get_user_by_email(bot_token, recipient_email)
            if not user:
                continue

            # Open DM channel
            channel_id = await slack_service.open_dm_channel(bot_token, user["id"])
            if not channel_id:
                continue

            # Send message with agent identity
            success, error = await slack_service.send_message(
                bot_token=bot_token,
                channel=channel_id,
                text=text,
                username=agent_name,
            )

            if success:
                return DeliveryResult(success=True, channel="slack")
            else:
                return DeliveryResult(success=False, channel="slack", error=error)

        raise RecipientNotFoundError(
            f"User '{recipient_email}' not found in any connected Slack workspace"
        )

    async def _deliver_whatsapp(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via WhatsApp (Twilio).

        Note: Twilio enforces a 24-hour session window for business-initiated
        freeform messages. Outside the window, Twilio returns error 63016 and
        delivery fails. The failure surfaces in `DeliveryResult.error`; the
        agent owner is responsible for configuring approved message templates
        for out-of-window outreach.
        """
        from adapters.whatsapp_adapter import WhatsAppAdapter

        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            raise RecipientNotFoundError(f"No WhatsApp binding configured for agent '{agent_name}'")

        chat_link = db.get_whatsapp_chat_link_by_verified_email(binding["id"], recipient_email)
        if not chat_link:
            raise RecipientNotFoundError(
                f"No WhatsApp user with verified email '{recipient_email}' for agent '{agent_name}'"
            )

        auth_token = db.get_whatsapp_auth_token(agent_name)
        if not auth_token:
            raise ChannelDeliveryError("Failed to retrieve Twilio AuthToken")

        adapter = WhatsAppAdapter()
        try:
            formatted_text = adapter.format_response(text)
            # Respect Twilio's 1600-char WhatsApp body limit — agent-generated
            # proactive messages can exceed it; split the same way send_response does.
            chunks = adapter._split_message(formatted_text)
            last_sid: Optional[str] = None
            for chunk in chunks:
                result = await adapter._send_message(
                    account_sid=binding["account_sid"],
                    auth_token=auth_token,
                    from_number=binding["from_number"],
                    messaging_service_sid=binding.get("messaging_service_sid"),
                    to_number=chat_link["wa_user_phone"],
                    body=chunk,
                )
                if not result:
                    return DeliveryResult(
                        success=False,
                        channel="whatsapp",
                        error="Send failed (Twilio returned error — see logs)",
                    )
                last_sid = result.get("sid") or last_sid

            return DeliveryResult(
                success=True,
                channel="whatsapp",
                message_id=str(last_sid) if last_sid else None,
            )
        except Exception as e:
            logger.error(f"WhatsApp delivery failed: {e}")
            return DeliveryResult(success=False, channel="whatsapp", error=str(e))

    async def _deliver_web(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via web (WebSocket push + DB persist).

        NOTE: Web delivery requires the recipient to have an active public link session
        with this agent. For v1, we skip web delivery and rely on Telegram/Slack.
        Full web delivery (with inbox-style persistence) is deferred to v2.
        """
        # v1: Web delivery not yet implemented - requires refactoring public_chat
        # to support agent_name-based sessions instead of link_id-based sessions.
        raise RecipientNotFoundError(
            f"Web delivery not yet implemented for proactive messaging. "
            f"Recipient '{recipient_email}' must have Telegram or Slack configured."
        )


# Singleton instance
proactive_message_service = ProactiveMessageService()
