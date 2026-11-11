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

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Optional, Literal

from database import db
from services import channel_history, idempotency_service
from services.platform_audit_service import platform_audit_service, AuditEventType
from services.runtime_secret_scrub import get_staged_values, scrub_text
from services.settings_service import get_proactive_rate_limit  # #1609

logger = logging.getLogger(__name__)

# Rate limiting configuration. The per-recipient cap is admin-configurable
# (#1609) — sourced from settings at check time via
# ``settings_service.get_proactive_rate_limit("proactive_dm_per_recipient")``
# (0 = unlimited). RATE_LIMIT_MAX_PER_HOUR remains the shipped default there.
RATE_LIMIT_MAX_PER_HOUR = 10  # messages per recipient per hour (default)
RATE_LIMIT_WINDOW_SECONDS = 3600  # 1 hour


class ProactiveMessageError(Exception):
    """Base exception for proactive messaging errors.

    ``reason`` is a stable machine code beside the sentence
    (trinity-enterprise#1221), so a refusal can say WHY without the caller
    parsing text — and without naming the person, for a role-addressed send.
    """

    def __init__(self, message: str = "", reason: Optional[str] = None,
                 channels: Optional[dict] = None):
        super().__init__(message)
        self.reason = reason
        # The per-channel reason map of a send that tried several channels.
        self.channels = channels


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
    # #1600: the recipient's channel-session key, set by the `_deliver_*` helper
    # on success so the caller can persist the outbound turn into the SAME
    # session an inbound reply will use. Each helper derives it by calling its
    # own adapter's `get_session_identifier()` — never by re-deriving the format
    # here, which would drift from the adapter and silently mint a second
    # session. None ⇒ the channel can't resolve a session (nothing to persist).
    session_identifier: Optional[str] = None
    # ent#665: set only when the caller supplied an idempotency_key.
    sent: Optional[bool] = None
    suppressed_by: Optional[str] = None
    first_sent_at: Optional[str] = None
    first_execution_id: Optional[str] = None
    # trinity-enterprise#1221: the helper wrote its own history (a Workspace
    # row IS the conversation), so `_persist_outbound` must not write a twin.
    history_written: bool = False
    # How the person will see it. "workspace_inbox" = a row in their Workspace
    # conversation: delivered, but nothing notifies them — they see it on their
    # next visit. None for channels that push (Telegram, Slack, WhatsApp).
    delivery: Optional[str] = None


#: Every web refusal from the shared Workspace gate, as this send's code.
_WORKSPACE_REFUSAL_CODES = {
    "workspace_delivery_no_target": "workspace_not_shared",
    "workspace_delivery_target_unreachable": "workspace_not_shared",
    "workspace_delivery_target_blocked": "workspace_blocked",
    "workspace_delivery_target_unverifiable": "workspace_unverifiable",
}

WEB_DELIVERY_WORKSPACE_INBOX = "workspace_inbox"


def _web_message_id(agent_name: str, execution_id: Optional[str], recipient_email: str,
                    channel: str, dedup_label: str, idempotency_key: Optional[str]) -> str:
    """The Workspace row id for one send (trinity-enterprise#1221).

    Derived from the effect identity, so a retry after a post-commit failure (the
    effect guard releases its claim when the audit or rate-limit bump raises)
    writes nothing: ``add_portal_message_once`` decides the duplicate in SQL.
    Keyless sends get a fresh id, matching the effect guard's own keyless rule.
    The requested ``channel`` is in the material because it is in the guard's
    identity: ``auto`` and ``web`` are two effects, so they must be two rows.
    """
    if not execution_id:
        return f"proactive-{uuid.uuid4().hex}"
    material = "\x1f".join([agent_name, execution_id, recipient_email.lower(), "web",
                            channel, dedup_label or "", idempotency_key or ""])
    return f"proactive-{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _telegram_target(agent_name: str, recipient_email: str):
    """(binding, chat_link) for a Telegram DM, or RecipientNotFoundError.
    Sync and local — shared by the send and `reachable_channels`."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise RecipientNotFoundError(
            f"No Telegram bot configured for agent '{agent_name}'", reason="telegram_no_bot")
    chat_link = db.get_telegram_chat_link_by_verified_email(binding["id"], recipient_email)
    if not chat_link:
        raise RecipientNotFoundError(
            f"No Telegram user with verified email '{recipient_email}' for agent '{agent_name}'",
            reason="telegram_not_linked",
        )
    _refuse_blocked(agent_name, "telegram", str(chat_link["telegram_user_id"]))
    return binding, chat_link


def _whatsapp_target(agent_name: str, recipient_email: str):
    """(binding, chat_link) for a WhatsApp DM, or RecipientNotFoundError."""
    binding = db.get_whatsapp_binding(agent_name)
    if not binding:
        raise RecipientNotFoundError(
            f"No WhatsApp binding configured for agent '{agent_name}'", reason="whatsapp_no_bot")
    chat_link = db.get_whatsapp_chat_link_by_verified_email(binding["id"], recipient_email)
    if not chat_link:
        raise RecipientNotFoundError(
            f"No WhatsApp user with verified email '{recipient_email}' for agent '{agent_name}'",
            reason="whatsapp_not_linked",
        )
    _refuse_blocked(agent_name, "whatsapp", chat_link["wa_user_phone"])
    return binding, chat_link


def _slack_workspaces() -> list:
    """The connected Slack workspaces, or RecipientNotFoundError. Whether the
    person is a member of one is a network lookup, made only by the send."""
    workspaces = db.get_all_slack_workspaces()
    if not workspaces:
        raise RecipientNotFoundError("No Slack workspaces connected", reason="slack_not_connected")
    return workspaces


def reachable_channels(agent_name: str, recipient_email: str) -> set:
    """The channels that could deliver a proactive message to this person now.

    Sync, local reads only, no writes and no network (trinity-enterprise#1221) —
    the Execution Context asks it on prompt composition. Built from the same
    predicates the send applies, so the prompt and the send cannot disagree.
    Slack counts as reachable when any workspace is connected (membership needs
    a network lookup): the answer can under-claim "impossible", never
    over-claim. Staleness is the caller's cache (the assignment provider's 60 s).
    Only channels ``send_message`` accepts are probed: WhatsApp is not one, and
    ``auto`` never tries it, so a WhatsApp link alone reaches nobody here.
    """
    from services.schedule_workspace_delivery import (
        WorkspaceDeliveryRefused, check_workspace_reachable,
    )

    email = (recipient_email or "").lower()
    out: set = set()
    try:
        _telegram_target(agent_name, email)
        out.add("telegram")
    except ProactiveMessageError:
        pass
    try:
        if any(w.get("bot_token") for w in _slack_workspaces()):
            out.add("slack")
    except ProactiveMessageError:
        pass
    try:
        check_workspace_reachable(agent_name, email)
        out.add("web")
    except WorkspaceDeliveryRefused:
        pass
    return out


def _refuse_blocked(agent_name: str, channel: str, client_id: str) -> None:
    """trinity-enterprise#21: a client the owner blocked on this agent's channel
    gets nothing from the agent on it — no proactive message, no notification.
    A ChannelDeliveryError, so `auto` still tries the next channel (the block is
    per channel) and an explicit channel fails with this reason."""
    if db.is_channel_client_blocked(agent_name, channel, client_id):
        raise ChannelDeliveryError(
            f"recipient_blocked: this client is blocked on the agent's {channel} channel",
            reason=f"{channel}_blocked",
        )


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
    - web: a row in the person's Workspace Main conversation with this agent
      (trinity-enterprise#1221) — the agent's own key only; nothing notifies them
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

    def check_rate_limit(self, agent_name: str, recipient_email: str) -> bool:
        """Check if sending is allowed under rate limits. Returns True if OK."""
        # #1609: admin-configurable per-recipient cap; 0 = unlimited.
        limit = get_proactive_rate_limit("proactive_dm_per_recipient")
        if limit <= 0:
            return True

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
            return int(count) < limit
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}")
            return True

    def increment_rate_limit(self, agent_name: str, recipient_email: str) -> None:
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

    async def audit_send(
        self,
        agent_name: str,
        recipient_email: str,
        channel: str,
        success: bool,
        error: Optional[str] = None,
        message_preview: Optional[str] = None,
        action: str = "send",
        extra: Optional[dict] = None,
    ) -> None:
        """Log proactive message send to audit trail."""
        try:
            await platform_audit_service.log(
                event_type=AuditEventType.PROACTIVE_MESSAGE,
                event_action=action,
                source="proactive_message_service",
                actor_agent_name=agent_name,
                target_type="user",
                target_id=recipient_email,
                details={
                    "channel": channel,
                    "success": success,
                    "error": error,
                    "message_preview": message_preview[:100] if message_preview else None,
                    **(extra or {}),
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
        idempotency_key: Optional[str] = None,
        idempotency_ttl: Optional[int] = None,
        as_agent_key: bool = False,
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
                Without a usable id: refused on a pull-mode agent, else sent and
                logged as degraded (#2392, `idempotency_service.effect_guard`).
            dedup_label: Optional agent-supplied discriminator (#1084) so an agent
                can intentionally send two distinct messages to the same recipient
                in one turn. Default "" → at-most-one send per (recipient, channel)
                per turn.
            idempotency_key / idempotency_ttl: Caller-declared cross-execution
                key (ent#665): one delivery per (agent, recipient, key) within the
                TTL across ALL executions. A suppressed send returns
                ``sent=False`` and is audited and noted in the conversation.
            as_agent_key: The caller is this agent's own key. Only then may the
                send write into a person's Workspace (`web`, trinity-enterprise#1221):
                a human the agent is shared with must not plant an agent-voiced
                row in another member's conversation (the ent#610 class).

        Returns:
            DeliveryResult with success status and channel used

        Raises:
            NotAuthorizedError: Recipient hasn't opted in
            RecipientNotFoundError: No channel endpoint for recipient
            RateLimitedError: Rate limit exceeded
            ChannelDeliveryError: Channel delivery failed
        """
        recipient_email = recipient_email.lower()

        # ent#279: scrub the body at the TOP -- before channel delivery AND before
        # the durable persist in _persist_outbound (D19). The normal reply path is
        # already scrub-before-send transitively (the applier scrubs before result
        # construction); an agent pasting a fetched vault value into a proactive
        # message adds nothing the operator does not already hold in Settings.
        _staged = get_staged_values()
        if _staged:
            text = scrub_text(_staged, text)

        # Effect-scoped dedup (#1084): guard at the entry, keyed on the resolved
        # recipient + channel (never the body). A chunked message is ONE effect —
        # a mid-chunk crash re-sends the whole message on retry (at-least-once for
        # chunks, documented in effect-idempotency.md). A concurrent in-flight
        # duplicate raises EffectInProgressError (retryable, not a silent skip).
        # ent#665: the intent key is part of the per-turn identity, so a turn's
        # suppressed send under key A never replays for its send under key B.
        # Keyless → the dict (and so the key hash) is unchanged.
        identity = {"recipient": recipient_email, "channel": channel}
        if idempotency_key:
            identity["intent"] = idempotency_key
        async with idempotency_service.effect_guard(
            "message",
            identity,
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
                    sent=snap.get("sent"),
                    suppressed_by=snap.get("suppressed_by"),
                    first_sent_at=snap.get("first_sent_at"),
                    first_execution_id=snap.get("first_execution_id"),
                    delivery=snap.get("delivery"),
                )
            result = await self._send_message_inner(
                agent_name, recipient_email, text, channel, reply_to_thread,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                idempotency_ttl=idempotency_ttl,
                web=_WebSend(
                    allowed=as_agent_key,
                    execution_id=execution_id,
                    message_id=_web_message_id(agent_name, execution_id, recipient_email,
                                               channel, dedup_label, idempotency_key),
                ),
            )
            guard.snapshot = {
                "success": result.success,
                "channel": result.channel,
                "message_id": result.message_id,
                "error": result.error,
                "sent": result.sent,
                "suppressed_by": result.suppressed_by,
                "first_sent_at": result.first_sent_at,
                "first_execution_id": result.first_execution_id,
                "delivery": result.delivery,
            }
            return result

    async def _send_message_inner(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: Literal["auto", "telegram", "slack", "whatsapp", "web"],
        reply_to_thread: bool,
        execution_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        idempotency_ttl: Optional[int] = None,
        web: Optional["_WebSend"] = None,
    ) -> DeliveryResult:
        """Auth → intent key → rate-limit → channel delivery. Wrapped by
        send_message's effect guard (#1084), so a success here is recorded for
        replay and an exception releases the claim for retry."""
        message_preview = text[:100] if text else ""

        # 1. Authorization check
        if not db.can_agent_message_email(agent_name, recipient_email):
            await self.audit_send(agent_name, recipient_email, channel, False, "not_authorized")
            raise NotAuthorizedError(
                f"Agent '{agent_name}' is not authorized to message '{recipient_email}'. "
                "Recipient must opt in via allow_proactive flag."
            )

        # 2. Caller-declared intent key (ent#665): after consent (a revoked
        # recipient is refused, not "suppressed"), before the rate limit (a
        # suppressed send costs no budget).
        async with idempotency_service.intent_guard(
            "message",
            agent_name=agent_name,
            target=recipient_email,
            idempotency_key=idempotency_key,
            ttl_seconds=idempotency_ttl,
            execution_id=execution_id,
        ) as intent:
            if intent.suppressed:
                first_channel = intent.first.get("channel") or channel
                await self.audit_send(
                    agent_name, recipient_email, first_channel, False,
                    message_preview=message_preview, action="suppressed",
                    extra={
                        "suppressed_by": idempotency_service.SUPPRESSED_BY_IDEMPOTENCY_KEY,
                        "idempotency_key": idempotency_key,
                        "first_sent_at": intent.first_sent_at,
                        "first_execution_id": intent.first_execution_id,
                    },
                )
                channel_history.persist_suppressed_note(
                    agent_name=agent_name,
                    channel=first_channel,
                    session_identifier=intent.first.get("session_identifier"),
                    idempotency_key=idempotency_key,
                    first_sent_at=intent.first_sent_at,
                )
                return DeliveryResult(success=True, channel=first_channel, **intent.result_fields())
            result = await self._rate_limit_and_deliver(
                agent_name, recipient_email, text, channel, reply_to_thread, message_preview,
                web=web,
            )
            intent.record = {"channel": result.channel, "session_identifier": result.session_identifier}
            for k, v in intent.result_fields().items():
                setattr(result, k, v)
            return result

    async def _rate_limit_and_deliver(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: str,
        reply_to_thread: bool,
        message_preview: str,
        web: Optional["_WebSend"] = None,
    ) -> DeliveryResult:
        """Rate limit, then try each channel; raises when none delivers.

        Every channel that does not deliver leaves its reason in a
        ``{channel: code}`` map carried on the final RecipientNotFoundError
        (trinity-enterprise#1221) — before, `auto` dropped each reason and the
        agent was told only that nothing reached the person."""
        # 2. Rate limit check
        if not self.check_rate_limit(agent_name, recipient_email):
            await self.audit_send(agent_name, recipient_email, channel, False, "rate_limited")
            raise RateLimitedError(
                f"Rate limit exceeded: max {RATE_LIMIT_MAX_PER_HOUR} messages per hour to this recipient."
            )

        # 3. Channel resolution and delivery
        channels_to_try = (
            ["telegram", "slack", "web"] if channel == "auto"
            else [channel]
        )

        last_error = None
        reasons: dict = {}
        for ch in channels_to_try:
            try:
                # The web context reaches only the web helper; every other
                # channel keeps its call shape.
                result = await self._deliver_via_channel(
                    agent_name, recipient_email, text, ch, reply_to_thread,
                    **({"web": web} if ch == "web" else {}),
                )
                if result.success:
                    self.increment_rate_limit(agent_name, recipient_email)
                    # #1600: record the outbound turn in the recipient's channel
                    # session so the agent's next turn knows what it sent.
                    # Deliberately here and not in `_deliver_*`: those helpers are
                    # shared with `send_access_grant_notification`, whose
                    # persistence is out of scope (#951). Keeping the call on the
                    # send_message path draws that line structurally rather than
                    # with a flag.
                    if not result.history_written:
                        self._persist_outbound(agent_name, recipient_email, text, result)
                    await self.audit_send(
                        agent_name, recipient_email, ch, True,
                        message_preview=message_preview
                    )
                    return result
                last_error = result.error
                reasons[ch] = "delivery_failed"
            except RecipientNotFoundError as e:
                # Try next channel, keeping why this one could not.
                reasons[ch] = e.reason or "unavailable"
                continue
            except Exception as e:
                last_error = str(e)
                reasons[ch] = getattr(e, "reason", None) or "delivery_failed"
                logger.warning(f"Proactive message to {ch} failed: {e}")
                continue

        # All channels failed
        error_msg = last_error or "No delivery channel available for recipient"
        await self.audit_send(agent_name, recipient_email, channel, False, error_msg,
                              extra={"channels": reasons})
        raise RecipientNotFoundError(error_msg, reason="no_channel", channels=reasons)

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
            await self.audit_send(
                agent_name, recipient_email, channel, False,
                error=f"recipient_not_found: {e}",
                message_preview=message_preview,
            )
            return DeliveryResult(success=False, channel=channel, error=str(e))
        except Exception as e:
            logger.warning(
                f"[#951] Access-grant notification to {channel} failed: {e}"
            )
            await self.audit_send(
                agent_name, recipient_email, channel, False,
                error=str(e), message_preview=message_preview,
            )
            return DeliveryResult(success=False, channel=channel, error=str(e))

        await self.audit_send(
            agent_name, recipient_email, channel,
            success=result.success,
            error=result.error,
            message_preview=message_preview,
        )
        return result

    def _persist_outbound(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        result: DeliveryResult,
    ) -> None:
        """Append a delivered proactive message to the recipient's channel history (#1600).

        Without this the agent has no record of its own outreach: the next inbound
        turn builds context from `build_public_chat_context`, which only ever saw
        turns the message router persisted. The agent then repeats itself,
        contradicts the message, or can't parse the reply ("yes, sounds good" — to
        what?).

        Attribution mirrors router step 11 (#903): role `assistant`, `sender_label`
        = agent name, and `sender_email` = the recipient. A proactive send always
        targets ONE verified email — i.e. a DM, a single-participant session — so
        `_assistant_sender_email`'s rule ("stamp the email for single-participant
        sessions, None for shared threads") resolves to the recipient. That keeps
        sender-filtered MEM-001 summarization folding these into the right user's
        memory (#903, AC-7).

        Called ONLY on confirmed delivery, so a failed send never writes a phantom
        assistant turn (AC-5).

        The session is created if absent rather than skipped: the chat-link
        `session_id` column this was expected to key off is **never written by any
        code path** (#1600 — a vestigial column), so "reuse the existing session"
        isn't available. `get_or_create_public_chat_session` with the adapter's own
        identifier lands in exactly the session a later inbound reply resolves to.

        Fail-soft: the message is already delivered, so a persistence error must
        never surface as a delivery failure. It's logged loudly instead — a silent
        swallow here is what made the original bug invisible.
        """
        if not result.session_identifier:
            logger.warning(
                "[#1600] proactive %s message to %s delivered but NOT persisted: "
                "no session identifier resolved for agent %s",
                result.channel, recipient_email, agent_name,
            )
            return
        try:
            session = db.get_or_create_public_chat_session(
                agent_name, result.session_identifier, result.channel
            )
            session_id = session.id if hasattr(session, "id") else session["id"]
            db.add_public_chat_message(
                session_id,
                "assistant",
                text,
                sender_email=recipient_email,
                sender_label=agent_name,
            )
            logger.debug(
                "[#1600] persisted proactive %s message to session %s",
                result.channel, session_id,
            )
        except Exception:
            logger.error(
                "[#1600] failed to persist delivered proactive %s message for agent %s "
                "— the message WAS delivered; history will be missing this turn",
                result.channel, agent_name, exc_info=True,
            )

    async def _deliver_via_channel(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        channel: str,
        reply_to_thread: bool,
        web: Optional["_WebSend"] = None,
    ) -> DeliveryResult:
        """Deliver message via specific channel."""
        if channel == "telegram":
            return await self._deliver_telegram(agent_name, recipient_email, text)
        elif channel == "slack":
            return await self._deliver_slack(agent_name, recipient_email, text)
        elif channel == "whatsapp":
            return await self._deliver_whatsapp(agent_name, recipient_email, text)
        elif channel == "web":
            return await self._deliver_web(agent_name, recipient_email, text, web or _WebSend())
        else:
            raise ValueError(f"Unknown channel: {channel}")

    async def _deliver_telegram(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
    ) -> DeliveryResult:
        """Deliver via Telegram."""
        from adapters.base import NormalizedMessage
        from adapters.telegram_adapter import TelegramAdapter

        binding, chat_link = _telegram_target(agent_name, recipient_email)

        # Get bot token (decrypted)
        bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            raise ChannelDeliveryError("Failed to retrieve bot token")

        # Send via adapter
        adapter = TelegramAdapter()
        try:
            # The chat_link contains telegram_user_id which is the chat_id for DMs
            chat_id = chat_link["telegram_user_id"]
            # #2277: proactive sends must go through the same markdown→HTML
            # converter and entity-safe splitter as send_response — raw agent
            # markdown under parse_mode=HTML loses all formatting on any `<`.
            result = None
            for chunk in adapter._split_message(adapter.format_response(text)):
                result = await adapter._send_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                )
                if result is None:
                    return DeliveryResult(
                        success=False, channel="telegram",
                        error="Telegram chunk delivery unconfirmed; later chunks were not sent",
                    )
            if result:
                message_id = result.get("message_id")
                # #1600: resolve the recipient's session key through the adapter
                # itself. A Telegram DM's chat_id IS the user id (the send above
                # relies on the same fact), and `bot_id` is read from the binding
                # exactly as telegram_webhook injects it — so this reproduces the
                # inbound key byte-for-byte instead of guessing at the format.
                probe = NormalizedMessage(
                    sender_id=str(chat_id),
                    text="",
                    channel_id=str(chat_id),
                    timestamp="",
                    metadata={"bot_id": binding.get("bot_id", ""), "is_group": False},
                )
                return DeliveryResult(
                    success=True,
                    channel="telegram",
                    message_id=str(message_id) if message_id else None,
                    session_identifier=adapter.get_session_identifier(probe),
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
        from adapters.base import NormalizedMessage
        from adapters.slack_adapter import SlackAdapter
        from services.slack_service import slack_service

        # Get Slack workspace connections
        # We need to find a workspace where this user exists
        workspaces = _slack_workspaces()

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
                # #1600: Slack has no chat-link table, but the DM session key is
                # `team:sender:channel` — and all three are already resolved
                # above (workspace team_id, the looked-up user, the opened DM
                # channel). So Slack persists the same as the others rather than
                # being documented as a known limitation. `is_dm=True` selects
                # the adapter's DM branch, which is what an inbound DM from this
                # user takes.
                probe = NormalizedMessage(
                    sender_id=str(user["id"]),
                    text="",
                    channel_id=str(channel_id),
                    timestamp="",
                    metadata={"team_id": workspace.get("team_id"), "is_dm": True},
                )
                return DeliveryResult(
                    success=True,
                    channel="slack",
                    session_identifier=SlackAdapter().get_session_identifier(probe),
                )
            else:
                return DeliveryResult(success=False, channel="slack", error=error)

        raise RecipientNotFoundError(
            f"User '{recipient_email}' not found in any connected Slack workspace",
            reason="slack_user_not_found",
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
        from adapters.base import NormalizedMessage
        from adapters.whatsapp_adapter import WhatsAppAdapter

        binding, chat_link = _whatsapp_target(agent_name, recipient_email)

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

            # #1600: same session key an inbound WhatsApp DM resolves to —
            # derived by the adapter, not re-implemented here. sender_id IS the
            # wa phone, which is what the chat link stores.
            probe = NormalizedMessage(
                sender_id=str(chat_link["wa_user_phone"]),
                text="",
                channel_id=str(chat_link["wa_user_phone"]),
                timestamp="",
                metadata={"binding_id": binding["id"]},
            )
            return DeliveryResult(
                success=True,
                channel="whatsapp",
                message_id=str(last_sid) if last_sid else None,
                session_identifier=adapter.get_session_identifier(probe),
            )
        except Exception as e:
            logger.error(f"WhatsApp delivery failed: {e}")
            return DeliveryResult(success=False, channel="whatsapp", error=str(e))

    async def _deliver_web(
        self,
        agent_name: str,
        recipient_email: str,
        text: str,
        web: "_WebSend",
    ) -> DeliveryResult:
        """Deliver into the person's Workspace conversation (trinity-enterprise#1221).

        "web" is the person's Workspace conversation with this agent — the only
        web conversation keyed by person. The row lands in their **Main**,
        through the same gate a scheduled Workspace delivery uses (block list
        fail-closed, then the roster incl. owned agents), so a person who cannot
        open the agent is never written to. Marked ``source="proactive"``: the UI
        never reads it as a turn's answer, and the agent's next turn is told it
        as a message it sent between turns. Nothing pushes it — the person sees
        it on their next visit, which the result says (``delivery``).
        """
        from client_portal import db as portal_db
        from services.channel_completion_report import PROACTIVE_SOURCE
        from services.schedule_workspace_delivery import (
            WorkspaceDeliveryRefused, resolve_main_destination,
        )
        from utils.helpers import utc_now_iso

        if not web.allowed:
            raise RecipientNotFoundError(
                "Only the agent itself may write into a person's Workspace conversation.",
                reason="web_agent_only",
            )
        try:
            session_id = resolve_main_destination(agent_name, recipient_email)
        except WorkspaceDeliveryRefused as e:
            code = _WORKSPACE_REFUSAL_CODES.get(e.reason)
            if code is None:  # session_unavailable — a fault, not an address
                return DeliveryResult(success=False, channel="web", error=e.detail)
            raise RecipientNotFoundError(e.detail, reason=code) from e

        if self._is_recipients_own_turn(web.execution_id, session_id):
            raise RecipientNotFoundError(
                "This turn is already their conversation with you — reply in this turn instead.",
                reason="workspace_in_turn",
            )

        msg_id = web.message_id or f"proactive-{uuid.uuid4().hex}"
        try:
            portal_db.add_portal_message_once(
                msg_id, agent_name, recipient_email, "assistant", text, utc_now_iso(),
                session_id, source=PROACTIVE_SOURCE,
            )
        except Exception as e:  # noqa: BLE001 — reported, never a silent success
            logger.error("[ent#1221] Workspace write for agent %s failed: %s", agent_name, e)
            return DeliveryResult(success=False, channel="web",
                                  error=f"Workspace write failed ({type(e).__name__})")
        # A False from the insert is an earlier attempt's row under the same
        # identity: the message is there, which is what this send promises.
        return DeliveryResult(
            success=True, channel="web", message_id=msg_id,
            history_written=True, delivery=WEB_DELIVERY_WORKSPACE_INBOX,
        )

    @staticmethod
    def _is_recipients_own_turn(execution_id: Optional[str], session_id: str) -> bool:
        """The calling turn is the recipient's own Main turn: its reply is the
        message, and a row written mid-turn would sit beside that reply."""
        from config import PORTAL_SOURCE_CHANNEL

        if not execution_id:
            return False
        try:
            row = db.get_execution(execution_id)
        except Exception:  # noqa: BLE001 — an unreadable row is not a turn match
            return False
        if row is None:
            return False
        return (getattr(row, "source_channel", None) == PORTAL_SOURCE_CHANNEL
                and getattr(row, "source_channel_chat_id", None) == session_id)


@dataclass
class _WebSend:
    """What a `web` delivery needs from the send that asked for it."""
    allowed: bool = False
    execution_id: Optional[str] = None
    message_id: Optional[str] = None


# Singleton instance
proactive_message_service = ProactiveMessageService()
