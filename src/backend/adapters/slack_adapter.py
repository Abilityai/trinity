"""
Slack channel adapter implementation.

Handles Slack-specific message parsing, response formatting (with chat:write.customize),
user verification, and agent resolution via channel bindings.

Supports:
- Channel @mentions → routed to channel-bound agent
- DMs → routed to default agent or agent picker
- chat:write.customize for per-agent name/avatar on responses

Transport-agnostic — works with both Socket Mode and webhook transports.
"""

import logging
import random
import re
import string
from typing import Optional

from database import db
from config import SLACK_AUTO_VERIFY_EMAIL
from services.slack_service import slack_service
from services.email_service import email_service
from adapters.base import ChannelAdapter, FileAttachment, NormalizedMessage, ChannelResponse

logger = logging.getLogger(__name__)


class SlackAdapter(ChannelAdapter):
    """Slack implementation of ChannelAdapter with multi-agent channel routing."""

    # =========================================================================
    # ChannelAdapter interface — identity & routing
    # =========================================================================

    @property
    def channel_type(self) -> str:
        return "slack"

    def get_rate_key(self, message: NormalizedMessage) -> str:
        return f"slack:{message.metadata.get('team_id')}:{message.sender_id}"

    def get_session_identifier(self, message: NormalizedMessage) -> str:
        team_id = message.metadata.get("team_id", "unknown")
        return f"{team_id}:{message.sender_id}:{message.channel_id}"

    def get_source_identifier(self, message: NormalizedMessage) -> str:
        return f"slack:{message.metadata.get('team_id')}:{message.sender_id}"

    async def resolve_verified_email(
        self, message: NormalizedMessage
    ) -> Optional[str]:
        """Slack workspace email is already verified — fetch via users.info.

        Issue #311. Cached per process for the lifetime of the message
        (no persistence needed: the workspace OAuth token already proves identity).
        """
        bot_token = self.get_bot_token(message)
        if not bot_token:
            return None
        try:
            email = await slack_service.get_user_email(bot_token, message.sender_id)
        except Exception as e:
            logger.warning(f"Slack resolve_verified_email failed: {e}")
            return None
        return email.lower() if email else None

    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        team_id = message.metadata.get("team_id")
        if not team_id:
            return None
        # New: slack_workspaces table
        token = db.get_slack_workspace_bot_token(team_id)
        if token:
            return token
        # Legacy: slack_link_connections
        connection = db.get_slack_connection_by_team(team_id)
        if connection:
            return connection.get("slack_bot_token")
        return None

    # =========================================================================
    # ChannelAdapter interface — message processing
    # =========================================================================

    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Parse a Slack event_callback payload into a NormalizedMessage.

        Handles:
        - message.im (DM to bot)
        - app_mention (bot @mentioned in channel)
        - message in thread where bot already responded (no @mention needed)
        """
        event = raw_event.get("event", {})
        team_id = raw_event.get("team_id")
        event_type = event.get("type")

        # Handle DM messages
        if event_type == "message" and event.get("channel_type") == "im":
            return self._parse_dm(event, team_id)

        # Handle @mentions in channels
        if event_type == "app_mention":
            return self._parse_mention(event, team_id)

        # Handle thread replies in bot channels (no @mention needed)
        if event_type == "message" and event.get("thread_ts"):
            return self._parse_thread_reply(event, team_id)

        return None

    def format_response(self, text: str) -> str:
        """Convert standard markdown to Slack mrkdwn format.

        Uses our own renderer (#293) — the third-party slackify-markdown
        library dropped nested-list indent, swallowed blank lines before
        headings, prefixed only the first blockquote line, and passed
        tables through unchanged. See `services.slack_mrkdwn` for the
        replacement and `tests/unit/test_slack_mrkdwn.py` for coverage.
        """
        try:
            from services.slack_mrkdwn import to_slack_mrkdwn
            return to_slack_mrkdwn(text)
        except Exception as e:
            logger.warning(f"Slack markdown conversion failed, sending plain text: {e}")
            return text

    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None
    ) -> None:
        """Send response with agent identity via chat:write.customize."""
        bot_token = response.metadata.get("bot_token")
        if not bot_token:
            logger.error(f"No bot token in response metadata for channel {channel_id}")
            return

        agent_name = response.metadata.get("agent_name")
        avatar_url = response.metadata.get("agent_avatar_url")

        # Upload extracted outbound files before sending text
        for f in response.files:
            success, error = await slack_service.upload_file(
                bot_token=bot_token,
                channel=channel_id,
                filename=f.filename,
                content=f.content,
                thread_ts=thread_id,
            )
            if not success:
                logger.warning(f"[SLACK] File upload failed for {f.filename}: {error}")

        formatted_text = self.format_response(response.text)

        await slack_service.send_message(
            bot_token=bot_token,
            channel=channel_id,
            text=formatted_text,
            username=agent_name,
            icon_url=avatar_url,
            thread_ts=thread_id,
        )

    async def indicate_processing(self, message: NormalizedMessage) -> None:
        """Add ⏳ reaction to the user's message."""
        bot_token = self.get_bot_token(message)
        if bot_token and message.timestamp:
            await slack_service.add_reaction(
                bot_token, message.channel_id, message.timestamp, "hourglass_flowing_sand"
            )

    async def indicate_done(self, message: NormalizedMessage) -> None:
        """Remove ⏳, add ✅ to the user's message."""
        bot_token = self.get_bot_token(message)
        if bot_token and message.timestamp:
            await slack_service.remove_reaction(
                bot_token, message.channel_id, message.timestamp, "hourglass_flowing_sand"
            )
            await slack_service.add_reaction(
                bot_token, message.channel_id, message.timestamp, "white_check_mark"
            )

    async def download_file(self, file: FileAttachment, message: NormalizedMessage) -> Optional[bytes]:
        """Download file from Slack using bot token."""
        bot_token = self.get_bot_token(message)
        if not bot_token:
            logger.error(f"No bot token for file download: {file.name}")
            return None
        return await slack_service.download_file(bot_token, file.url)

    async def on_response_sent(self, message: NormalizedMessage, agent_name: str) -> None:
        """Register active thread so subsequent replies don't need @mention."""
        if message.thread_id and message.metadata.get("team_id"):
            db.register_slack_active_thread(
                team_id=message.metadata["team_id"],
                channel_id=message.channel_id,
                thread_ts=message.thread_id,
                agent_name=agent_name,
            )

    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        """
        Resolve which agent handles this message.

        Routing priority:
        1. Channel binding (channel_id → agent)
        2. DM default agent (is_dm_default flag)
        3. Single connected agent (only one → use it)
        4. None (multiple agents, no binding — caller handles)
        """
        team_id = message.metadata.get("team_id")
        if not team_id:
            return None

        channel_id = message.channel_id
        is_dm = message.metadata.get("is_dm", False)

        # 1. Channel binding
        if not is_dm:
            agent = db.get_slack_agent_name_for_channel(team_id, channel_id)
            if agent:
                return agent

        # 2. DM default agent
        if is_dm:
            default_agent = db.get_slack_dm_default_agent(team_id)
            if default_agent:
                return default_agent

        # 3. Single connected agent
        agents = db.get_slack_agents_for_workspace(team_id)
        if len(agents) == 1:
            return agents[0]["agent_name"]

        # 4. Fallback: try legacy slack_link_connections
        connection = db.get_slack_connection_by_team(team_id)
        if connection:
            return connection.get("agent_name")

        return None

    async def enrich_message(self, message: NormalizedMessage) -> None:
        """Resolve sender display name (+ channel name for channel messages) so
        the agent knows who is speaking (#350).

        Slack events carry only opaque IDs (``U123``/``C123``); this fetches the
        display identity via the Web API and writes it into ``message.metadata``.
        ``channel_name`` is set only for non-DM (channel) messages — its presence
        is what makes the router prepend the ``[Channel: #x]`` context, so DMs
        stay clean. Best-effort: any failure leaves the bare IDs in place.
        """
        try:
            team_id = message.metadata.get("team_id")
            if not team_id:
                return
            bot_token = db.get_slack_workspace_bot_token(team_id)
            if not bot_token:
                return

            if message.sender_id:
                user_info = await slack_service.get_user_info(bot_token, message.sender_id)
                if user_info:
                    display = user_info.get("real_name") or user_info.get("display_name")
                    if display:
                        message.metadata["sender_display_name"] = display
                    if user_info.get("name"):
                        message.metadata["sender_username"] = user_info["name"]

            # Channel name only for channel (non-DM) messages — gates the
            # [Channel: #x] prefix; DMs intentionally have no channel context.
            if not message.metadata.get("is_dm") and message.channel_id:
                channel_info = await slack_service.get_channel_info(bot_token, message.channel_id)
                if channel_info and channel_info.get("name"):
                    message.metadata["channel_name"] = channel_info["name"]
        except Exception as e:  # noqa: BLE001 — enrichment is best-effort (#350)
            logger.debug(f"[SlackAdapter] enrich_message failed: {e}", exc_info=True)

    # =========================================================================
    # Slack-specific: message parsing
    # =========================================================================

    def _parse_dm(self, event: dict, team_id: str) -> Optional[NormalizedMessage]:
        """Parse a DM message."""
        if event.get("bot_id") or event.get("subtype"):
            return None

        user_id = event.get("user")
        text = event.get("text", "").strip()
        files = self._extract_files(event)

        if not text and not files:
            return None
        if not user_id:
            return None

        return NormalizedMessage(
            sender_id=user_id,
            text=text or "(file upload)",
            channel_id=event.get("channel"),
            thread_id=event.get("thread_ts"),
            timestamp=event.get("ts", ""),
            files=files,
            metadata={
                "team_id": team_id,
                "is_dm": True,
            }
        )

    def _parse_mention(self, event: dict, team_id: str) -> Optional[NormalizedMessage]:
        """Parse an @mention in a channel."""
        if event.get("bot_id") or event.get("subtype"):
            return None

        user_id = event.get("user")
        text = event.get("text", "").strip()
        files = self._extract_files(event)

        if not user_id:
            return None

        # Strip the @mention from the text (e.g., "<@U123BOT> hello" → "hello")
        text = re.sub(r'<@[A-Z0-9]+>\s*', '', text).strip()
        if not text and not files:
            return None

        return NormalizedMessage(
            sender_id=user_id,
            text=text or "(file upload)",
            channel_id=event.get("channel"),
            thread_id=event.get("thread_ts") or event.get("ts"),  # Reply in thread
            timestamp=event.get("ts", ""),
            files=files,
            metadata={
                "team_id": team_id,
                "is_dm": False,
            }
        )

    def _parse_thread_reply(self, event: dict, team_id: str) -> Optional[NormalizedMessage]:
        """
        Parse a thread reply in a bot channel (no @mention needed).

        Only responds if:
        1. Channel is bound to an agent (slack_channel_agents table)
        2. Thread was started by the bot (slack_active_threads table)
        """
        if event.get("bot_id") or event.get("subtype"):
            return None

        channel_id = event.get("channel")
        thread_ts = event.get("thread_ts")

        if not channel_id or not thread_ts:
            return None

        # Check 1: Is this a Trinity agent channel?
        agent_from_channel = db.get_slack_agent_name_for_channel(team_id, channel_id)
        if not agent_from_channel:
            return None  # Not our channel — ignore

        # Check 2: Is this a thread where the bot already responded?
        agent_from_thread = db.is_slack_active_thread(team_id, channel_id, thread_ts)
        if not agent_from_thread:
            return None  # Bot didn't start this thread — ignore

        user_id = event.get("user")
        text = event.get("text", "").strip()
        files = self._extract_files(event)

        if not user_id:
            return None

        # Strip any @mentions (user might still @mention out of habit)
        text = re.sub(r'<@[A-Z0-9]+>\s*', '', text).strip()
        if not text and not files:
            return None

        return NormalizedMessage(
            sender_id=user_id,
            text=text or "(file upload)",
            channel_id=channel_id,
            thread_id=thread_ts,
            timestamp=event.get("ts", ""),
            files=files,
            metadata={
                "team_id": team_id,
                "is_dm": False,
            }
        )

    # =========================================================================
    # Slack-specific: verification
    # =========================================================================

    async def handle_verification(self, message: NormalizedMessage) -> bool:
        """
        Handle Slack user email verification flow.

        Returns True if verified (or not required).
        Returns False if verification in progress (sends its own messages).
        """
        team_id = message.metadata.get("team_id")
        bot_token = self.get_bot_token(message)
        if not bot_token:
            return False

        # Check legacy connection for require_email setting
        connection = db.get_slack_connection_by_team(team_id)
        if not connection:
            return True  # No legacy connection = no email requirement

        require_email = connection.get("require_email", False)
        if not require_email:
            return True

        link_id = connection.get("link_id", "")
        user_id = message.sender_id
        channel = message.channel_id

        # Check if already verified
        verification = db.get_slack_user_verification(link_id, user_id, team_id)
        if verification:
            return True

        # Check for pending verification
        pending = db.get_slack_pending_verification(user_id, team_id)

        # No pending — try auto-verify or start flow
        if not pending:
            if SLACK_AUTO_VERIFY_EMAIL:
                email = await slack_service.get_user_email(bot_token, user_id)
                if email:
                    db.create_slack_user_verification(
                        link_id, user_id, team_id, email, "slack_profile"
                    )
                    return True

            db.create_slack_pending_verification(link_id, user_id, team_id)
            await slack_service.send_message(
                bot_token, channel,
                "Hi! Before we chat, I need to verify your email address.\n\n"
                "Please reply with your email address to continue."
            )
            return False

        state = pending.get("state")
        text = message.text

        if state == "awaiting_email":
            email = self._extract_email(text)
            if not email:
                await slack_service.send_message(
                    bot_token, channel,
                    "That doesn't look like a valid email address. "
                    "Please reply with your email to continue."
                )
                return False

            code = self._generate_verification_code()
            db.update_slack_pending_verification(
                user_id, team_id, email=email, code=code, state="awaiting_code"
            )
            try:
                await email_service.send_verification_code(email, code)
                await slack_service.send_message(
                    bot_token, channel,
                    f"I've sent a 6-digit verification code to {email}.\n\n"
                    "Reply with the code to complete verification. The code expires in 10 minutes."
                )
            except Exception as e:
                logger.error(f"Failed to send verification email: {e}")
                await slack_service.send_message(
                    bot_token, channel,
                    "Sorry, I couldn't send the verification email. Please try again."
                )
                db.delete_slack_pending_verification(user_id, team_id)
            return False

        if state == "awaiting_code":
            code = self._extract_code(text)
            expected_code = pending.get("code")
            if not code or code != expected_code:
                await slack_service.send_message(
                    bot_token, channel,
                    "That code doesn't match. Please enter the 6-digit code from your email."
                )
                return False

            link_id = pending.get("link_id", connection.get("link_id", ""))
            db.create_slack_user_verification(
                link_id, user_id, team_id, pending.get("email"), "email_code"
            )
            db.delete_slack_pending_verification(user_id, team_id)
            await slack_service.send_message(
                bot_token, channel,
                "Verified! You can now chat with me.\n\n"
                "What would you like to know?"
            )
            return False

        return False

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def _extract_files(event: dict) -> list:
        """Extract FileAttachment list from a Slack event's files array."""
        files = []
        for f in event.get("files", []):
            url = f.get("url_private_download") or f.get("url_private", "")
            if not url:
                continue
            files.append(FileAttachment(
                id=f.get("id", ""),
                name=f.get("name", "unknown"),
                mimetype=f.get("mimetype", "application/octet-stream"),
                size=f.get("size", 0),
                url=url,
            ))
        return files

    @staticmethod
    def _extract_email(text: str) -> Optional[str]:
        match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text)
        return match.group(0) if match else None

    @staticmethod
    def _extract_code(text: str) -> Optional[str]:
        match = re.search(r'\b(\d{6})\b', text)
        return match.group(1) if match else None

    @staticmethod
    def _generate_verification_code() -> str:
        return ''.join(random.choices(string.digits, k=6))
