"""
Telegram channel adapter implementation.

Handles Telegram-specific message parsing, response formatting (HTML),
agent resolution via bot bindings, and bot commands.

Supports:
- Private chats (DMs) → routed to bot-bound agent
- Group chats → @mention or reply-to-bot triggers (TGRAM-GROUP)
- Photos, documents → downloaded and passed as context
- /start, /help, /reset commands
- Typing indicator via sendChatAction
- Member events (bot added/removed, user join/leave)
"""

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from database import db
from adapters.base import ChannelAdapter, FileAttachment, NormalizedMessage, ChannelResponse
from services.email_service import EmailService

logger = logging.getLogger(__name__)

# Telegram message length limit
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org"

# Group chat types
_GROUP_CHAT_TYPES = {"group", "supergroup"}

# In-flight progress indicator (ent#264). Threshold before the elapsed-time
# placeholder appears, and the edit cadence once it has (well under Telegram's
# ≈1 msg/s per-chat / ≈20 msg/min group budget). Tunable constants, not config
# — a per-binding threshold surface is deliberate scope-out.
_PROGRESS_THRESHOLD_SECONDS = 30.0
_PROGRESS_INTERVAL_SECONDS = 60.0

# 👀 is in Telegram's allowed bot reaction set (⚙️ and ✅ are NOT — the Slack
# ⏳→✅ pattern does not port to Telegram reactions).
_PROGRESS_ACK_EMOJI = "👀"

# Consecutive failed progress HTTP attempts (placeholder sends, edits, and
# timeouts alike) before the per-turn degraded flag stops all further progress
# HTTP — a fleet-wide Telegram outage must quiesce, not retry per-turn-per-minute.
_PROGRESS_MAX_CONSECUTIVE_FAILURES = 2

# Pending login TTL in seconds (matches code expiry)
_PENDING_LOGIN_TTL = 600  # 10 minutes


def _get_pending_login_key(binding_id: int, user_id: str) -> str:
    """Redis key for pending Telegram login."""
    return f"telegram_pending_login:{binding_id}:{user_id}"


def _get_pending_login(binding_id: int, user_id: str) -> Optional[str]:
    """Get pending login email from Redis."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            return r.get(_get_pending_login_key(binding_id, user_id))
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login lookup: {e}")
    return None


def _set_pending_login(binding_id: int, user_id: str, email: str) -> None:
    """Store pending login email in Redis with TTL."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            r.setex(_get_pending_login_key(binding_id, user_id), _PENDING_LOGIN_TTL, email)
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login store: {e}")


def _clear_pending_login(binding_id: int, user_id: str) -> None:
    """Clear pending login from Redis."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            r.delete(_get_pending_login_key(binding_id, user_id))
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login clear: {e}")


class TelegramAdapter(ChannelAdapter):
    """Telegram implementation of ChannelAdapter with per-agent bot routing."""

    # ent#264 — progress-driver capability declaration (see base.py): the
    # router arms its per-turn ticker off these. All indicator state rides
    # `message.metadata` (this adapter is a long-lived singleton handling
    # concurrent turns — never store per-turn state on the instance).
    progress_threshold_seconds = _PROGRESS_THRESHOLD_SECONDS
    progress_interval_seconds = _PROGRESS_INTERVAL_SECONDS

    # =========================================================================
    # ChannelAdapter interface — identity & routing
    # =========================================================================

    @property
    def channel_type(self) -> str:
        return "telegram"

    def get_rate_key(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        # In groups, add a per-group rate key component
        if message.metadata.get("is_group"):
            return f"telegram:{bot_id}:group:{message.channel_id}"
        return f"telegram:{bot_id}:{message.sender_id}"

    def get_session_identifier(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        chat_id = message.channel_id
        return f"{bot_id}:{message.sender_id}:{chat_id}"

    def get_source_identifier(self, message: NormalizedMessage) -> str:
        bot_id = message.metadata.get("bot_id", "unknown")
        return f"telegram:{bot_id}:{message.sender_id}"

    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return None
        return db.get_telegram_bot_token(agent_name)

    # =========================================================================
    # ChannelAdapter interface — message processing
    # =========================================================================

    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Parse a Telegram Update into a NormalizedMessage.

        Handles:
        - Private chat text messages
        - Group chat messages (filtered by @mention or reply-to-bot)
        - Photo messages (caption + photo indicator)
        - Document messages (caption + document indicator)
        """
        message = raw_event.get("message")
        if not message:
            return None

        # Skip messages from bots (prevents bot loops)
        from_user = message.get("from", {})
        if from_user.get("is_bot", False):
            return None

        user_id = str(from_user.get("id", ""))
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "private")
        username = from_user.get("username")

        if not user_id or not chat_id:
            return None

        # Resolve bot → agent via the binding stored in metadata by transport
        bot_id = raw_event.get("_bot_id", "")
        bot_username = raw_event.get("_bot_username", "")
        agent_name = raw_event.get("_agent_name", "")
        is_group = chat_type in _GROUP_CHAT_TYPES

        # Group chat filtering: only process @mentions or replies to bot.
        # ent#264: stash the ack-eligibility gate while the trigger facts are
        # in hand — the progress indicator (👀 reaction + placeholder) fires
        # only for turns that will visibly reply: DMs, @mention/reply-triggered
        # group turns, and `all`-trigger-mode groups (the bot replies to
        # everything there). Observe mode stays excluded so the indicator
        # never reveals a silently-observing bot.
        progress_ack_eligible = True
        if is_group:
            is_mentioned = self._is_bot_mentioned(message, bot_username)
            is_reply = self._is_reply_to_bot(message, bot_id)
            progress_ack_eligible = is_mentioned or is_reply

            if not is_mentioned and not is_reply:
                # Check trigger mode — if "all" or "observe", process anyway
                # Issue #349: "observe" mode passes all messages but agent can return [NO_REPLY]
                binding = db.get_telegram_binding(agent_name)
                if binding:
                    group_config = db.get_telegram_group_config(binding["id"], chat_id)
                    trigger_mode = group_config.get("trigger_mode") if group_config else None
                    if trigger_mode not in ("all", "observe"):
                        return None
                    progress_ack_eligible = trigger_mode == "all"
                else:
                    return None

        # Extract text content
        text = message.get("text", "").strip()

        # Strip @mention from text in groups for cleaner agent input
        if is_group and bot_username and text:
            text = re.sub(rf'@{re.escape(bot_username)}\b', '', text).strip()

        # Extract file attachments (photos, documents)
        files = self._extract_files(message)

        # Handle media messages — extract caption or description for context
        media_context = self._extract_media_context(message)
        if media_context:
            text = media_context if not text else f"{text}\n\n{media_context}"

        # Allow messages with files even if no text
        if not text and not files:
            return None

        # Provide placeholder text for file-only messages
        if not text and files:
            text = "(file upload)"

        return NormalizedMessage(
            sender_id=user_id,
            text=text,
            channel_id=chat_id,
            thread_id=str(message.get("message_id", "")),
            timestamp=str(message.get("date", "")),
            files=files,
            metadata={
                "bot_id": bot_id,
                "bot_username": bot_username,
                "agent_name": agent_name,
                "username": username,
                "is_group": is_group,
                "chat_type": chat_type,
                "chat_title": chat.get("title"),
                "has_photo": "photo" in message,
                "has_document": "document" in message,
                "raw_message": message,
                # ent#264: True for DMs, mention/reply-triggered group turns,
                # and `all`-trigger-mode groups; False for observe-mode
                # un-mentioned turns (typing only — no visible indicator).
                "progress_ack_eligible": progress_ack_eligible,
            }
        )

    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None
    ) -> None:
        """Send response to Telegram chat with HTML formatting."""
        bot_token = response.metadata.get("bot_token")
        if not bot_token:
            logger.error(f"No bot token in response metadata for chat {channel_id}")
            return

        text = response.text
        if not text:
            return

        # Voice replies v2 (ent#117): replies are TEXT by default. Voice is now a
        # per-message capability the agent opts into via the send_voice_reply MCP
        # tool during the turn — the adapter no longer speaks replies unconditionally.
        # Convert markdown to Telegram HTML
        html_text = self._markdown_to_html(text)

        # Split long messages at paragraph boundaries
        chunks = self._split_message(html_text)

        # In groups, always reply to the triggering message for threaded context
        reply_to = thread_id if response.metadata.get("is_group") else None

        for chunk in chunks:
            await self._send_message(
                bot_token=bot_token,
                chat_id=channel_id,
                text=chunk,
                reply_to_message_id=reply_to,
                parse_mode="HTML",
            )

    # =========================================================================
    # Unified access control (Issue #311)
    # =========================================================================

    async def resolve_verified_email(
        self, message: NormalizedMessage
    ) -> Optional[str]:
        """Look up the verified email bound to this Telegram user, if any."""
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return None
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return None
        return db.get_telegram_verified_email(binding["id"], message.sender_id)

    async def record_inbound_activity(
        self, message: NormalizedMessage, agent_name: str
    ) -> None:
        """Count this DM on the Sharing-tab client roster (#1533)."""
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return
        db.record_telegram_inbound(
            binding["id"],
            message.sender_id,
            message.metadata.get("username"),
        )

    async def prompt_auth(
        self,
        message: NormalizedMessage,
        agent_name: str,
        bot_token: Optional[str] = None,
    ) -> None:
        """Send a Telegram-native auth prompt with /login instructions."""
        if not bot_token:
            bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            return
        text = (
            "🔒 This agent requires a verified email.\n\n"
            "Send <code>/login your@email.com</code> and I'll email you a 6-digit code. "
            "Then reply with <code>/login 123456</code> to complete verification."
        )
        await self._send_message(
            bot_token=bot_token,
            chat_id=message.channel_id,
            text=text,
            reply_to_message_id=message.thread_id,
            parse_mode="HTML",
        )

    # =========================================================================
    # Group Authentication (group_auth_mode support)
    # =========================================================================

    async def is_group_verified(
        self,
        message: NormalizedMessage,
        agent_name: str,
    ) -> bool:
        """Check if this Telegram group has at least one verified member."""
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return True  # No binding = allow (shouldn't happen)
        return db.is_telegram_group_verified(binding["id"], message.channel_id)

    async def set_group_verified(
        self,
        message: NormalizedMessage,
        agent_name: str,
        email: str,
    ) -> None:
        """Mark this Telegram group as verified by the given email."""
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return
        db.set_telegram_group_verified(binding["id"], message.channel_id, email)
        logger.info(
            f"Telegram group {message.channel_id} verified by {email} "
            f"for agent {agent_name}"
        )

    async def prompt_group_auth(
        self,
        message: NormalizedMessage,
        agent_name: str,
        bot_token: Optional[str] = None,
    ) -> None:
        """Prompt for group verification with Telegram-specific instructions."""
        if not bot_token:
            bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            return
        text = (
            "🔒 This agent requires at least one verified member in the group.\n\n"
            "Send <code>/login your@email.com</code> to verify your email, "
            "then reply with <code>/login 123456</code> to complete verification.\n\n"
            "Once verified, everyone in this group can chat with me."
        )
        await self._send_message(
            bot_token=bot_token,
            chat_id=message.channel_id,
            text=text,
            reply_to_message_id=message.thread_id,
            parse_mode="HTML",
        )

    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        """Resolve which agent handles this message (set by transport layer)."""
        agent_name = message.metadata.get("agent_name")

        # For group chats, auto-create group config on first interaction
        if message.metadata.get("is_group") and agent_name:
            binding = db.get_telegram_binding(agent_name)
            if binding:
                db.get_or_create_telegram_group_config(
                    binding_id=binding["id"],
                    chat_id=message.channel_id,
                    chat_title=message.metadata.get("chat_title"),
                    chat_type=message.metadata.get("chat_type", "group"),
                )

        return agent_name

    async def indicate_processing(self, message: NormalizedMessage) -> None:
        """Telegram processing ack (ent#264): typing garnish + 👀 reaction.

        Resolves the binding ONCE, stashes the per-turn indicator config on
        ``message.metadata["_progress_cfg"]`` for the later hooks (single
        binding read per turn), sends the typing action (existing behavior,
        preserved for every turn including observe-mode), and — when the
        per-binding toggle is on and the turn is ack-eligible — sets the 👀
        reaction on the triggering message.

        NEVER raises: the entire body (including the DB read) is wrapped —
        a raise here would abort the turn with no user-visible error. Toggle
        reads are per-turn: flipping the toggle mid-run takes effect on the
        NEXT turn (documented behavior).
        """
        try:
            agent_name = message.metadata.get("agent_name", "")
            binding = db.get_telegram_binding(agent_name) if agent_name else None
            bot_token: Optional[str] = None
            enabled = False
            if binding:
                # Decrypt from the row already in hand — do NOT also call
                # get_telegram_bot_token(), which re-reads the binding.
                bot_token = db.decrypt_telegram_bot_token(
                    binding.get("bot_token_encrypted") or ""
                )
                # Default-ON predicate, evaluated in Python (never SQL —
                # NULL != 0 is NULL there): only an explicit 0 disables.
                v = binding.get("progress_indicator_enabled")
                enabled = v is None or v != 0
            message.metadata["_progress_cfg"] = {
                "enabled": enabled,
                "bot_token": bot_token,
            }

            if not bot_token:
                return

            # Typing garnish — existing behavior preserved verbatim (fires on
            # every turn, including observe-mode group turns).
            await self._send_chat_action(bot_token, message.channel_id, "typing")

            # 👀 reaction ack — the persistent pre-threshold signal. Failure
            # (reactions disabled per-chat, permissions, old chat → 400) is
            # swallowed: the ladder continues with typing + placeholder.
            if (
                enabled
                and message.metadata.get("progress_ack_eligible", True)
                and message.thread_id
            ):
                ok = await self._set_message_reaction(
                    bot_token,
                    message.channel_id,
                    message.thread_id,
                    _PROGRESS_ACK_EMOJI,
                )
                if ok:
                    message.metadata["_indicator_reaction_set"] = True
        except Exception as e:  # noqa: BLE001 — must never abort the turn
            logger.debug(f"[TELEGRAM] indicate_processing failed (non-fatal): {e}")

    async def indicate_progress(
        self, message: NormalizedMessage, elapsed_seconds: float
    ) -> None:
        """ent#264 tick: send the elapsed-time placeholder on the first call,
        edit it in place on subsequent calls. Same gate as the reaction ack
        (toggle + ack-eligibility), read from the per-turn metadata stash — no
        DB call per tick. Fail-soft; lets CancelledError propagate."""
        try:
            cfg = message.metadata.get("_progress_cfg") or {}
            bot_token = cfg.get("bot_token")
            if (
                not bot_token
                or not cfg.get("enabled")
                or not message.metadata.get("progress_ack_eligible", True)
                or message.metadata.get("_indicator_degraded")
            ):
                return

            text = self._format_progress_text(elapsed_seconds)

            placeholder_id = message.metadata.get("_indicator_placeholder_id")
            if placeholder_id:
                ok = await self._edit_message_text(
                    bot_token, message.channel_id, placeholder_id, text
                )
                self._track_indicator_failure(message, ok)
                return

            # First tick — send the placeholder inside its OWN future so a
            # terminal resolve landing mid-send can still record the
            # message_id (the id write lives INSIDE the shielded coroutine:
            # code after `await shield(fut)` never runs on cancellation).
            fut = asyncio.ensure_future(
                self._send_and_record_placeholder(message, bot_token, text)
            )
            message.metadata["_indicator_inflight"] = fut
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                # fut stays in metadata — _resolve_indicator settles it.
                raise
            message.metadata.pop("_indicator_inflight", None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a tick must never fail the turn
            logger.debug(f"[TELEGRAM] indicate_progress failed (non-fatal): {e}")

    async def indicate_done(self, message: NormalizedMessage) -> None:
        """ent#264 terminal resolve: clear the 👀 reaction and delete the
        elapsed-time placeholder (fallback: edit it to a short neutral
        terminal line). The router cancels-and-awaits the progress driver
        BEFORE calling this, so no tick can land after the teardown.

        Cleared at EVERY terminal — no success-👍 swap (✅ is not in
        Telegram's allowed bot reaction set; the reply itself is the
        completion signal). No-op when nothing was armed. Never raises."""
        try:
            cfg = message.metadata.get("_progress_cfg") or {}
            bot_token = cfg.get("bot_token")
            if not bot_token:
                return

            if message.metadata.pop("_indicator_reaction_set", None):
                await self._set_message_reaction(
                    bot_token, message.channel_id, message.thread_id, None
                )

            placeholder_id = message.metadata.pop("_indicator_placeholder_id", None)
            if placeholder_id:
                deleted = await self._delete_message(
                    bot_token, message.channel_id, placeholder_id
                )
                if not deleted:
                    # Neutral fallback — deliberately NOT "see reply below"
                    # (a [NO_REPLY] turn has no reply below). Static template
                    # text only, never agent/error content.
                    fallback = (
                        "✔ Done."
                        if message.metadata.get("_indicator_terminal_ok", True)
                        else "⚠️ Finished with an error."
                    )
                    await self._edit_message_text(
                        bot_token, message.channel_id, placeholder_id, fallback
                    )
        except Exception as e:  # noqa: BLE001 — never mask the terminal path
            logger.debug(f"[TELEGRAM] indicate_done failed (non-fatal): {e}")

    async def _send_and_record_placeholder(
        self, message: NormalizedMessage, bot_token: str, text: str
    ) -> None:
        """Send the first placeholder AND record its message_id — both inside
        this coroutine, which runs shielded from driver cancellation, so the
        id is recorded even when a resolve cancels the tick mid-send."""
        reply_to = message.thread_id if message.metadata.get("is_group") else None
        result = await self._send_placeholder_message(
            bot_token, message.channel_id, text, reply_to
        )
        ok = bool(result and result.get("message_id"))
        if ok:
            message.metadata["_indicator_placeholder_id"] = str(result["message_id"])
        self._track_indicator_failure(message, ok)

    @staticmethod
    def _track_indicator_failure(message: NormalizedMessage, ok: bool) -> None:
        """Count consecutive failed progress HTTP attempts (sends, edits, and
        timeouts alike); at the cap, set the per-turn degraded flag that stops
        all further progress HTTP. Success resets the streak."""
        if ok:
            message.metadata["_indicator_failures"] = 0
            return
        failures = int(message.metadata.get("_indicator_failures", 0)) + 1
        message.metadata["_indicator_failures"] = failures
        if failures >= _PROGRESS_MAX_CONSECUTIVE_FAILURES:
            message.metadata["_indicator_degraded"] = True
            logger.debug(
                "[TELEGRAM] progress indicator degraded after "
                f"{failures} consecutive failed attempts"
            )

    @staticmethod
    def _format_progress_text(elapsed_seconds: float) -> str:
        """Static elapsed-time template ONLY — never agent/error content (the
        ent#224 egress class is closed by construction). The UTC stamp makes a
        restart-stranded placeholder self-dating instead of an unresolvable lie."""
        now_utc = datetime.now(timezone.utc).strftime("%H:%M")
        if elapsed_seconds < 60:
            elapsed = f"{int(elapsed_seconds)}s"
        else:
            elapsed = f"{int(elapsed_seconds // 60)} min"
        return f"⏳ Working on it — {elapsed} elapsed · updated {now_utc} UTC"

    # =========================================================================
    # Group chat helpers (TGRAM-GROUP)
    # =========================================================================

    @staticmethod
    def _is_bot_mentioned(message: dict, bot_username: str) -> bool:
        """Check if the bot is @mentioned in message entities."""
        if not bot_username:
            return False
        entities = message.get("entities", [])
        text = message.get("text", "")
        for entity in entities:
            if entity.get("type") == "mention":
                offset = entity.get("offset", 0)
                length = entity.get("length", 0)
                mention_text = text[offset:offset + length]
                # Mention text is "@username"
                if mention_text.lower() == f"@{bot_username.lower()}":
                    return True
        return False

    @staticmethod
    def _is_reply_to_bot(message: dict, bot_id: str) -> bool:
        """Check if this message is a reply to one of the bot's own messages."""
        reply_to = message.get("reply_to_message")
        if not reply_to:
            return False
        reply_from = reply_to.get("from", {})
        # Compare as strings — bot_id is stored as TEXT in DB,
        # Telegram sends integer IDs
        return str(reply_from.get("id", "")) == str(bot_id)

    # =========================================================================
    # Member event handling (TGRAM-GROUP)
    # =========================================================================

    async def handle_member_event(
        self,
        update: dict,
        binding: dict,
    ) -> None:
        """
        Handle chat member updates (bot added/removed, user join/leave).

        Events:
        - my_chat_member: bot's own status changed in a chat
        - chat_member: another user's status changed (requires bot admin)
        """
        my_member = update.get("my_chat_member")
        other_member = update.get("chat_member")

        if my_member:
            await self._handle_bot_member_change(my_member, binding)
        elif other_member:
            await self._handle_user_member_change(other_member, binding)

    async def _handle_bot_member_change(self, event: dict, binding: dict) -> None:
        """Handle the bot being added to or removed from a group."""
        chat = event.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "")
        chat_title = chat.get("title", "")

        if chat_type not in _GROUP_CHAT_TYPES:
            return

        new_status = event.get("new_chat_member", {}).get("status", "")
        old_status = event.get("old_chat_member", {}).get("status", "")

        if new_status in ("member", "administrator") and old_status in ("left", "kicked"):
            # Bot was added to group — create config
            logger.info(f"Bot added to group '{chat_title}' (chat_id={chat_id}) for agent={binding['agent_name']}")
            db.get_or_create_telegram_group_config(
                binding_id=binding["id"],
                chat_id=chat_id,
                chat_title=chat_title,
                chat_type=chat_type,
            )
        elif new_status in ("left", "kicked") and old_status in ("member", "administrator"):
            # Bot was removed from group — deactivate config
            logger.info(f"Bot removed from group '{chat_title}' (chat_id={chat_id}) for agent={binding['agent_name']}")
            db.deactivate_telegram_group_config(binding["id"], chat_id)

    async def _handle_user_member_change(self, event: dict, binding: dict) -> None:
        """Handle a user joining or leaving a group (welcome messages)."""
        chat = event.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "")

        if chat_type not in _GROUP_CHAT_TYPES:
            return

        new_status = event.get("new_chat_member", {}).get("status", "")
        old_status = event.get("old_chat_member", {}).get("status", "")
        user = event.get("new_chat_member", {}).get("user", {})

        # Skip bot users
        if user.get("is_bot", False):
            return

        # Only handle user joins
        if new_status != "member" or old_status not in ("left", "kicked"):
            return

        # Check if welcome messages are enabled for this group
        group_config = db.get_telegram_group_config(binding["id"], chat_id)
        if not group_config or not group_config.get("welcome_enabled"):
            return

        welcome_text = group_config.get("welcome_text")
        if not welcome_text:
            return

        # Personalize welcome message
        user_name = user.get("first_name", "there")
        personalized = welcome_text.replace("{name}", user_name)

        bot_token = db.get_telegram_bot_token(binding["agent_name"])
        if bot_token:
            await self._send_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text=personalized,
                parse_mode="HTML",
            )
            logger.info(f"Sent welcome message to {user_name} in group {chat_id}")

    # =========================================================================
    # Bot commands
    # =========================================================================

    def is_command(self, message: NormalizedMessage) -> bool:
        """Check if message is a bot command."""
        return message.text.startswith("/")

    async def handle_command(self, message: NormalizedMessage) -> Optional[str]:
        """
        Handle /start, /help, /reset commands.
        Returns response text, or None if not a command.
        """
        text = message.text.strip()
        agent_name = message.metadata.get("agent_name", "Agent")

        # In groups, commands may have @botname suffix (e.g., /help@mybot)
        bot_username = message.metadata.get("bot_username", "")
        if bot_username:
            text = re.sub(rf'@{re.escape(bot_username)}$', '', text)

        if text == "/start" or text.startswith("/start "):
            return (
                f"Hello! I'm <b>{agent_name}</b>, a Trinity agent.\n\n"
                "Send me a message to get started.\n\n"
                "Commands:\n"
                "/help — List capabilities\n"
                "/reset — Clear conversation history"
            )

        if text == "/help":
            return (
                f"I'm <b>{agent_name}</b>.\n\n"
                "You can send me:\n"
                "- Text messages\n"
                "- Photos (I'll analyze them)\n"
                "- Documents (I'll read them)\n\n"
                "Commands:\n"
                "/start — Welcome message\n"
                "/help — This help text\n"
                "/reset — Clear our conversation history"
            )

        if text == "/reset":
            # Clear session — the transport/router will handle this
            return "Conversation history cleared. Let's start fresh!"

        # /login state machine (Issue #311)
        if text == "/login" or text.startswith("/login "):
            return await self._handle_login_command(message, text)

        if text == "/logout":
            return await self._handle_logout_command(message)

        if text == "/whoami":
            email = await self.resolve_verified_email(message)
            if email:
                return f"You are verified as <code>{email}</code>."
            return "You are not verified. Send <code>/login your@email.com</code> to verify."

        return None

    async def _handle_login_command(
        self, message: NormalizedMessage, text: str
    ) -> Optional[str]:
        """Handle /login {email} (request code) and /login {code} (verify)."""
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return "Login is unavailable for this chat."

        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return "Login is unavailable for this chat."

        # /login with no argument
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return (
                "Usage:\n"
                "<code>/login your@email.com</code> — request a verification code\n"
                "<code>/login 123456</code> — confirm the code I emailed you"
            )

        arg = parts[1].strip()

        # 6-digit code path
        if arg.isdigit() and len(arg) == 6:
            pending_email = _get_pending_login(binding["id"], message.sender_id)
            if not pending_email:
                return (
                    "I don't have a pending login for you. Send "
                    "<code>/login your@email.com</code> first."
                )
            result = db.verify_login_code(pending_email, arg)
            if not result:
                return "❌ Invalid or expired code. Try again or request a new one."
            db.set_telegram_verified_email(binding["id"], message.sender_id, pending_email)
            _clear_pending_login(binding["id"], message.sender_id)

            # Run the same access gate as message_router so the user learns
            # immediately whether they're in or in the approval queue. Without
            # this, a verified-but-not-shared user sees "you can chat normally"
            # and then hits "access pending" on their next message.
            policy = db.get_access_policy(agent_name)
            if db.email_has_agent_access(agent_name, pending_email) or policy.get("open_access"):
                return (
                    f"✅ Verified! You're now signed in as <code>{pending_email}</code>.\n"
                    "You can chat normally now."
                )

            try:
                db.upsert_access_request(agent_name, pending_email, "telegram")
            except Exception as e:
                logger.error(
                    f"Failed to upsert access_request for {pending_email} on agent={agent_name}: {e}"
                )
            return (
                f"✅ Verified as <code>{pending_email}</code>.\n"
                "🔒 Your access request is pending approval — "
                "I'll let you know once the agent owner responds."
            )

        # Email path
        email = arg.lower()
        if "@" not in email or " " in email or len(email) > 254:
            return "That doesn't look like an email address. Try <code>/login you@example.com</code>."

        try:
            code_data = db.create_login_code(email, expiry_minutes=10)
        except Exception as e:
            logger.error(f"Failed to create login code for {email}: {e}")
            return "Couldn't create a verification code. Please try again later."

        try:
            email_service = EmailService()
            sent = await email_service.send_verification_code(email, code_data["code"])
        except Exception as e:
            logger.error(f"Failed to send verification email to {email}: {e}")
            sent = False

        _set_pending_login(binding["id"], message.sender_id, email)

        if not sent:
            return (
                f"⚠️ I couldn't send the email to <code>{email}</code>. "
                "Ask the agent owner to check email delivery."
            )
        return (
            f"📧 Sent a 6-digit code to <code>{email}</code>.\n"
            "Reply with <code>/login 123456</code> to finish verification."
        )

    async def _handle_logout_command(self, message: NormalizedMessage) -> str:
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return "Logout is unavailable for this chat."
        binding = db.get_telegram_binding(agent_name)
        if not binding:
            return "Logout is unavailable for this chat."
        db.clear_telegram_verified_email(binding["id"], message.sender_id)
        _clear_pending_login(binding["id"], message.sender_id)
        return "👋 Logged out. Send <code>/login your@email.com</code> to sign in again."

    # =========================================================================
    # Telegram API helpers
    # =========================================================================

    async def _send_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> Optional[dict]:
        """Send a message via Telegram Bot API with retry on 429."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_to_message_id:
            payload["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload)

                if resp.status_code == 429:
                    # Rate limited — respect retry_after
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"Telegram rate limited, retry_after={retry_after}s")
                    import asyncio
                    await asyncio.sleep(min(retry_after, 30))
                    resp = await client.post(url, json=payload)

                if resp.status_code != 200:
                    error_body = resp.text
                    logger.error(f"Telegram sendMessage failed ({resp.status_code}): {error_body}")

                    # If HTML parsing failed, retry with plain text
                    if "can't parse entities" in error_body.lower():
                        payload["parse_mode"] = ""
                        payload["text"] = self._strip_html(text)
                        resp = await client.post(url, json=payload)
                        if resp.status_code != 200:
                            logger.error(f"Telegram plain text fallback also failed: {resp.text}")
                            return None

                return resp.json().get("result")
        except Exception as e:
            logger.error(f"Telegram sendMessage error: {e}", exc_info=True)
            return None

    # _send_voice is retained (now driven by services/voice_reply_service.py, ent#117);
    # the old adapter-driven _maybe_send_voice was removed with the always-voice path.
    async def _send_voice(
        self,
        bot_token: str,
        chat_id: str,
        ogg_bytes: bytes,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Send an OGG/Opus voice note via Telegram ``sendVoice``. Retries once on
        429; returns the message result, or None on failure (caller uses text)."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendVoice"
        data = {"chat_id": str(chat_id)}
        if reply_to_message_id:
            # sendVoice takes reply_parameters as a JSON string in multipart form.
            import json as _json
            data["reply_parameters"] = _json.dumps({
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            })
        files = {"voice": ("voice.ogg", ogg_bytes, "audio/ogg")}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, data=data, files=files)
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"Telegram sendVoice rate limited, retry_after={retry_after}s")
                    import asyncio
                    await asyncio.sleep(min(retry_after, 30))
                    resp = await client.post(url, data=data, files=files)
                if resp.status_code != 200:
                    logger.error(f"Telegram sendVoice failed ({resp.status_code}): {resp.text[:300]}")
                    return None
                return resp.json().get("result")
        except Exception as e:
            logger.error(f"Telegram sendVoice error: {e}", exc_info=True)
            return None

    async def _send_chat_action(self, bot_token: str, chat_id: str, action: str) -> None:
        """Send a chat action (typing indicator)."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendChatAction"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"chat_id": chat_id, "action": action})
        except Exception as e:
            logger.debug(f"Failed to send chat action: {e}")

    # -------------------------------------------------------------------------
    # In-flight indicator primitives (ent#264) — _send_message-shaped: httpx,
    # fail-soft, log status + response body only (NEVER the URL, which embeds
    # the bot token).
    # -------------------------------------------------------------------------

    async def _set_message_reaction(
        self,
        bot_token: str,
        chat_id: str,
        message_id: str,
        emoji: Optional[str],
    ) -> bool:
        """setMessageReaction — set one emoji, or clear with ``emoji=None``
        (empty reaction list). Single attempt, no retry: a missed reaction is
        cosmetic, and a 400 here is the reactions-disabled-per-chat case."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/setMessageReaction"
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "reaction": [{"type": "emoji", "emoji": emoji}] if emoji else [],
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.debug(
                        f"Telegram setMessageReaction failed ({resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                    return False
                return True
        except Exception as e:  # noqa: BLE001 — fail-soft primitive
            logger.debug(f"Telegram setMessageReaction error: {e}")
            return False

    async def _edit_message_text(
        self, bot_token: str, chat_id: str, message_id: str, text: str
    ) -> bool:
        """editMessageText — one 429 retry honoring retry_after (capped 30s);
        'message is not modified' counts as success. Explicit HTML parse mode
        (never MarkdownV2, whose reserved —/·/. would 400 on our template)."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.debug(
                        f"Telegram editMessageText rate limited, retry_after={retry_after}s"
                    )
                    await asyncio.sleep(min(retry_after, 30))
                    resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                if "message is not modified" in resp.text.lower():
                    return True
                logger.debug(
                    f"Telegram editMessageText failed ({resp.status_code}): {resp.text[:200]}"
                )
                return False
        except Exception as e:  # noqa: BLE001 — fail-soft primitive
            logger.debug(f"Telegram editMessageText error: {e}")
            return False

    async def _delete_message(
        self, bot_token: str, chat_id: str, message_id: str
    ) -> bool:
        """deleteMessage — single attempt, fail-soft (caller falls back to an
        edit-to-done on failure)."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": int(message_id)}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.debug(
                        f"Telegram deleteMessage failed ({resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                    return False
                return True
        except Exception as e:  # noqa: BLE001 — fail-soft primitive
            logger.debug(f"Telegram deleteMessage error: {e}")
            return False

    async def _send_placeholder_message(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """sendMessage for the ent#264 progress placeholder — _send_message-
        shaped (one 429 retry honoring retry_after) plus
        ``disable_notification`` (a "working…" ping must not push-notify; the
        real reply is the notification) and explicit HTML parse mode. Static
        template text only — no HTML-fallback path needed."""
        url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": True,
        }
        if reply_to_message_id:
            payload["reply_parameters"] = {
                "message_id": int(reply_to_message_id),
                "allow_sending_without_reply": True,
            }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.debug(
                        f"Telegram placeholder send rate limited, retry_after={retry_after}s"
                    )
                    await asyncio.sleep(min(retry_after, 30))
                    resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.debug(
                        f"Telegram placeholder send failed ({resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                    return None
                return resp.json().get("result")
        except Exception as e:  # noqa: BLE001 — fail-soft primitive
            logger.debug(f"Telegram placeholder send error: {e}")
            return None

    # =========================================================================
    # Message formatting
    # =========================================================================

    def format_response(self, text: str) -> str:
        """Convert standard markdown to Telegram HTML format."""
        return self._markdown_to_html(text)

    # Fenced code: optional language becomes <pre><code class="language-X">
    # (Telegram renders syntax highlighting for it). Fences are parsed by a
    # linear split on the ``` delimiter — one regex over the whole (agent- and
    # user-length) text is superlinear here (py/polynomial-redos). Issue #2277.
    _MD_FENCE_LANG_RE = re.compile(r"\w{0,32}[ \t]*")
    # Tags this converter emits — consumed by the entity-safe splitter.
    _HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")

    @staticmethod
    def _stash_fenced_code(text: str, hold) -> str:
        """Replace each closed ``` fence with a stashed <pre><code> block.

        Linear scan via str.split — every fence body is visited exactly once,
        and a trailing unclosed fence stays literal text.
        """
        parts = text.split("```")
        if len(parts) < 3:
            return text
        out = [parts[0]]
        i = 1
        while i + 1 < len(parts):
            block = parts[i]
            first, sep, rest = block.partition("\n")
            if sep and TelegramAdapter._MD_FENCE_LANG_RE.fullmatch(first):
                lang, code = first.strip(), rest
            else:
                lang, code = "", block
            cls = f' class="language-{lang}"' if lang else ""
            out.append(hold(f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>"))
            out.append(parts[i + 1])
            i += 2
        if i < len(parts):
            out.append("```" + parts[i])
        return "".join(out)

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert common Markdown to Telegram HTML, escape-first (#2277).

        Code spans are stashed before everything else so their content is
        escaped exactly once and never styled; all remaining text is
        HTML-escaped BEFORE conversion so raw <, >, & survive parse_mode=HTML
        instead of tripping "can't parse entities" (which used to strip all
        formatting from precisely the messages that contain code).
        """
        original = text
        try:
            stash = []

            def _hold(fragment):
                stash.append(fragment)
                return f"\x00{len(stash) - 1}\x00"

            text = TelegramAdapter._stash_fenced_code(text, _hold)
            # Inline code: `text`
            text = re.sub(
                r"`([^`\n]+)`",
                lambda m: _hold(f"<code>{html.escape(m.group(1), quote=False)}</code>"),
                text,
            )

            text = html.escape(text, quote=False)

            # Tables are readable only in monospace; stashed pre-styling so
            # cells never carry nested tags into <pre> (invalid in Telegram).
            text = re.sub(
                r"(?m)^(?:\|.*\|[ \t]*\n?){2,}",
                lambda m: _hold(f"<pre>{m.group(0).rstrip()}</pre>") + "\n",
                text,
            )

            # Links: [text](url)
            text = re.sub(
                r"\[([^\]\n]{1,256})\]\((https?://[^)\s]{1,1024})\)", r'<a href="\2">\1</a>', text
            )
            # Headers: #/## emphasized, ###+ plain bold. Trailing #/space
            # trimming happens in code — expressing it in-pattern needs
            # ambiguous adjacent quantifiers (py/polynomial-redos).
            def _header(m):
                body = m.group(2).rstrip().rstrip("#").rstrip()
                if not body:
                    return m.group(0)
                if len(m.group(1)) <= 2:
                    return f"<b><u>{body}</u></b>"
                return f"<b>{body}</b>"

            text = re.sub(r"(?m)^(#{1,6})[ \t]+(.+)$", _header, text)
            # Horizontal rules (before bold/italic so *** and ___ don't pair up)
            text = re.sub(r"(?m)^(?:---+|\*\*\*+|___+)[ \t]*$", "———", text)
            # Bullets (before italic so a leading * is never an emphasis opener)
            text = re.sub(r"(?m)^([ \t]*)[-*][ \t]+", r"\1• ", text)
            # Bold: **text** or __text__
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
            # Italic: *text* (but not inside words like file_name)
            text = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", text)
            # Strikethrough: ~~text~~
            text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
            # Spoiler: ||text||
            text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)

            # "> " runs → <blockquote>; long runs collapse client-side.
            def _quote(m):
                lines = [
                    re.sub(r"^&gt;[ \t]?", "", ln)
                    for ln in m.group(0).rstrip("\n").split("\n")
                ]
                body = "\n".join(lines)
                attr = " expandable" if len(lines) > 5 or len(body) > 400 else ""
                return f"<blockquote{attr}>{body}</blockquote>\n"

            text = re.sub(r"(?m)^(?:&gt;[ \t]?.*\n?)+", _quote, text)

            return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
        except Exception:
            logger.warning("Telegram markdown→HTML conversion failed", exc_info=True)
            return html.escape(original, quote=False)

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags and unescape entities for plain text fallback."""
        return html.unescape(re.sub(r"<[^<>]*>", "", text))

    @staticmethod
    def _split_message(text: str) -> list:
        """Split text into chunks respecting Telegram's 4096 char limit.

        Entity-safe (#2277): never cuts inside a <tag>, and tags still open at
        a cut are closed there and reopened in the next chunk — otherwise the
        continuation fails HTML parse and falls back to unformatted text.
        """
        if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return [text]

        # Reserve room for the closing tags a cut can append.
        limit = TELEGRAM_MAX_MESSAGE_LENGTH - 100
        chunks = []
        while text:
            if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                chunks.append(text)
                break

            # Find a good split point — paragraph, then sentence, then hard cut
            split_at = limit
            for sep in ["\n\n", "\n", ". ", " "]:
                idx = text.rfind(sep, 0, limit)
                if idx > limit // 2:
                    split_at = idx + len(sep)
                    break

            head = text[:split_at]
            if head.rfind("<") > head.rfind(">"):
                # Cut landed inside a tag — back up to its opening bracket.
                split_at = head.rfind("<")
                if split_at <= 0:
                    split_at = limit
                head = text[:split_at]

            open_tags = []
            for m in TelegramAdapter._HTML_TAG_RE.finditer(head):
                if m.group(1):
                    if open_tags and open_tags[-1][0] == m.group(2).lower():
                        open_tags.pop()
                else:
                    open_tags.append((m.group(2).lower(), m.group(3) or ""))

            chunks.append(
                head + "".join(f"</{name}>" for name, _ in reversed(open_tags))
            )
            text = (
                "".join(f"<{name}{attrs}>" for name, attrs in open_tags)
                + text[split_at:]
            )

        return chunks

    # =========================================================================
    # Media context extraction
    # =========================================================================

    @staticmethod
    def _extract_media_context(message: dict) -> Optional[str]:
        """Extract descriptive context from media messages."""
        parts = []

        if "photo" in message:
            caption = message.get("caption", "")
            parts.append(f"[User sent a photo{': ' + caption if caption else ''}]")

        if "document" in message:
            doc = message["document"]
            filename = doc.get("file_name", "unknown")
            caption = message.get("caption", "")
            parts.append(f"[User sent a document: {filename}{' — ' + caption if caption else ''}]")

        if "sticker" in message:
            sticker = message["sticker"]
            emoji = sticker.get("emoji", "")
            parts.append(f"[User sent a sticker: {emoji}]")

        if "location" in message:
            loc = message["location"]
            parts.append(f"[User shared a location: {loc.get('latitude')}, {loc.get('longitude')}]")

        if "voice" in message:
            parts.append("[User sent a voice message — voice transcription is not yet available]")

        if "video_note" in message:
            parts.append("[User sent a video note — transcription not yet available]")

        if "video" in message:
            caption = message.get("caption", "")
            parts.append(f"[User sent a video{': ' + caption if caption else ''}]")

        return "\n".join(parts) if parts else None

    @staticmethod
    def _extract_files(message: dict) -> list:
        """
        Extract FileAttachment objects from Telegram message photos/documents.

        Photos: Use the largest available size (last in array).
        Documents: Use file_id, file_name, mime_type, file_size.
        """
        files = []

        # Handle photos — Telegram sends array of sizes, use largest (last)
        if "photo" in message:
            photos = message["photo"]
            if photos:
                largest = photos[-1]  # Highest resolution
                file_id = largest.get("file_id", "")
                file_size = largest.get("file_size", 0)
                # Telegram photos don't have explicit filenames
                files.append(FileAttachment(
                    id=file_id,
                    name="photo.jpg",  # Telegram photos are always JPEG
                    mimetype="image/jpeg",
                    size=file_size,
                    url=file_id,  # We'll use file_id as URL, download via getFile
                ))

        # Handle documents (files with explicit names/types)
        if "document" in message:
            doc = message["document"]
            file_id = doc.get("file_id", "")
            files.append(FileAttachment(
                id=file_id,
                name=doc.get("file_name", "document"),
                mimetype=doc.get("mime_type", "application/octet-stream"),
                size=doc.get("file_size", 0),
                url=file_id,  # We'll use file_id as URL, download via getFile
            ))

        return files

    async def download_file(
        self, file: FileAttachment, message: NormalizedMessage
    ) -> Optional[bytes]:
        """
        Download a file from Telegram using the Bot API.

        Two-step process:
        1. Call getFile(file_id) to get the file_path
        2. Download from https://api.telegram.org/file/bot<token>/<file_path>

        Note: Telegram files are available for ~1 hour after getFile.
        Max file size via Bot API is 20MB.
        """
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            logger.error("[TELEGRAM] No agent_name in message metadata for file download")
            return None

        bot_token = db.get_telegram_bot_token(agent_name)
        if not bot_token:
            logger.error(f"[TELEGRAM] No bot token for agent {agent_name}")
            return None

        file_id = file.id  # We stored file_id in the id field

        try:
            # Step 1: Get file path via getFile API
            get_file_url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(get_file_url, json={"file_id": file_id})

                if resp.status_code != 200:
                    logger.error(f"[TELEGRAM] getFile failed ({resp.status_code}): {resp.text}")
                    return None

                result = resp.json()
                if not result.get("ok"):
                    logger.error(f"[TELEGRAM] getFile error: {result.get('description')}")
                    return None

                file_path = result.get("result", {}).get("file_path")
                if not file_path:
                    logger.error("[TELEGRAM] No file_path in getFile response")
                    return None

                # Step 2: Download the actual file
                # Note: URL contains bot token — never log it
                download_url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
                download_resp = await client.get(download_url)

                if download_resp.status_code != 200:
                    logger.error(f"[TELEGRAM] File download failed ({download_resp.status_code})")
                    return None

                data = download_resp.content
                logger.info(f"[TELEGRAM] Downloaded {file.name} ({len(data)} bytes)")
                return data

        except httpx.TimeoutException:
            logger.error(f"[TELEGRAM] Timeout downloading {file.name}")
            return None
        except Exception as e:
            logger.error(f"[TELEGRAM] Error downloading {file.name}: {e}", exc_info=True)
            return None
