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

import logging
import re
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

        # Group chat filtering: only process @mentions or replies to bot
        if is_group:
            is_mentioned = self._is_bot_mentioned(message, bot_username)
            is_reply = self._is_reply_to_bot(message, bot_id)

            if not is_mentioned and not is_reply:
                # Check trigger mode — if "all" or "observe", process anyway
                # Issue #349: "observe" mode passes all messages but agent can return [NO_REPLY]
                binding = db.get_telegram_binding(agent_name)
                if binding:
                    group_config = db.get_telegram_group_config(binding["id"], chat_id)
                    trigger_mode = group_config.get("trigger_mode") if group_config else None
                    if trigger_mode not in ("all", "observe"):
                        return None
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

        # Voice-out (epic #24 / #25): if the agent has spoken replies enabled with
        # a voice id, try to deliver the reply as a Telegram voice note. Strictly
        # additive — any failure (no TTS key, over the cost cap, provider or
        # transcode error) returns None and we fall through to the text path below.
        agent_name = response.metadata.get("agent_name")
        if agent_name and await self._maybe_send_voice(bot_token, channel_id, text, agent_name, thread_id, response):
            return

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
        """Send typing indicator to Telegram chat."""
        bot_token = db.get_telegram_bot_token(message.metadata.get("agent_name", ""))
        if bot_token:
            await self._send_chat_action(bot_token, message.channel_id, "typing")

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

    async def _maybe_send_voice(
        self,
        bot_token: str,
        chat_id: str,
        text: str,
        agent_name: str,
        thread_id: Optional[str],
        response: ChannelResponse,
    ) -> bool:
        """Try to deliver ``text`` as a Telegram voice note (epic #24 / #25).

        Returns True only when a voice note was actually sent. Any miss — feature
        off, no voice id, TTS unavailable, over the cost cap, provider/transcode
        error, or a failed ``sendVoice`` — returns False so the caller falls back
        to text. Never raises.
        """
        try:
            cfg = db.get_tts_config(agent_name)
        except Exception as e:
            logger.warning(f"voice-out: config lookup failed for {agent_name}: {e}")
            return False
        if not cfg.get("enabled") or not cfg.get("voice_id"):
            return False

        import services.tts_service as tts_service

        # Speak plain text, not markup (strip the markdown→HTML rendering).
        spoken = self._strip_html(self._markdown_to_html(text))
        ogg = await tts_service.synthesize_voice_note(spoken, cfg["voice_id"])
        if not ogg:
            return False

        reply_to = thread_id if response.metadata.get("is_group") else None
        result = await self._send_voice(bot_token, chat_id, ogg, reply_to_message_id=reply_to)
        return result is not None

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

    # =========================================================================
    # Message formatting
    # =========================================================================

    def format_response(self, text: str) -> str:
        """Convert standard markdown to Telegram HTML format."""
        return self._markdown_to_html(text)

    @staticmethod
    def _markdown_to_html(text: str) -> str:
        """Convert common Markdown to Telegram HTML. Plain text fallback on failure."""
        try:
            # Bold: **text** or __text__
            text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
            text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
            # Italic: *text* or _text_ (but not inside words like file_name)
            text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'<i>\1</i>', text)
            # Code blocks: ```text```
            text = re.sub(r'```(\w*)\n?(.*?)```', r'<pre>\2</pre>', text, flags=re.DOTALL)
            # Inline code: `text`
            text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
            # Strikethrough: ~~text~~
            text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
            return text
        except Exception:
            return text

    @staticmethod
    def _strip_html(text: str) -> str:
        """Strip HTML tags for plain text fallback."""
        return re.sub(r'<[^>]+>', '', text)

    @staticmethod
    def _split_message(text: str) -> list:
        """Split text into chunks respecting Telegram's 4096 char limit."""
        if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                chunks.append(text)
                break

            # Find a good split point — paragraph, then sentence, then hard cut
            split_at = TELEGRAM_MAX_MESSAGE_LENGTH
            for sep in ["\n\n", "\n", ". ", " "]:
                idx = text.rfind(sep, 0, TELEGRAM_MAX_MESSAGE_LENGTH)
                if idx > TELEGRAM_MAX_MESSAGE_LENGTH // 2:
                    split_at = idx + len(sep)
                    break

            chunks.append(text[:split_at])
            text = text[split_at:]

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
