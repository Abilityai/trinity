"""
Channel-agnostic message router.

Receives NormalizedMessage from any adapter, resolves the agent,
builds context, dispatches to the agent via TaskExecutionService,
persists messages, and returns the response through the adapter.

Uses the same execution path as web public chat (EXEC-024) for:
- Execution records and audit trail
- Activity tracking (Dashboard timeline)
- Slot management (capacity limits)
- Credential sanitization
"""

import logging
import time
from typing import List, Optional
from collections import defaultdict

from database import db
from services.docker_service import get_agent_container
from services.settings_service import settings_service
from services.task_execution_service import get_task_execution_service
from adapters.base import ChannelAdapter, ChannelResponse, NormalizedMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurable defaults (overridable via settings_service)
# ---------------------------------------------------------------------------

_DEFAULT_RATE_LIMIT_MAX = 30        # messages per window
_DEFAULT_RATE_LIMIT_WINDOW = 60     # seconds
_DEFAULT_CHANNEL_TIMEOUT = 120      # seconds
_DEFAULT_CHANNEL_ALLOWED_TOOLS = "WebSearch,WebFetch"


def _get_rate_limit_max() -> int:
    return int(settings_service.get_setting("channel_rate_limit_max", str(_DEFAULT_RATE_LIMIT_MAX)))


def _get_rate_limit_window() -> int:
    return int(settings_service.get_setting("channel_rate_limit_window", str(_DEFAULT_RATE_LIMIT_WINDOW)))


def _get_channel_timeout() -> int:
    return int(settings_service.get_setting("channel_timeout_seconds", str(_DEFAULT_CHANNEL_TIMEOUT)))


def _get_channel_allowed_tools() -> List[str]:
    raw = settings_service.get_setting("channel_allowed_tools", _DEFAULT_CHANNEL_ALLOWED_TOOLS)
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter with periodic pruning
# ---------------------------------------------------------------------------

_rate_limit_buckets: dict = defaultdict(list)  # key → list of timestamps
_PRUNE_INTERVAL = 300  # prune stale buckets every 5 minutes
_last_prune_time: float = 0.0


def _prune_stale_buckets() -> None:
    """Remove empty or fully-expired buckets to prevent memory leaks."""
    global _last_prune_time
    now = time.time()
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return
    _last_prune_time = now
    window = _get_rate_limit_window()
    stale_keys = [k for k, v in _rate_limit_buckets.items() if not v or v[-1] < now - window]
    for k in stale_keys:
        del _rate_limit_buckets[k]
    if stale_keys:
        logger.debug(f"[ROUTER] Pruned {len(stale_keys)} stale rate-limit buckets")


def _check_rate_limit(key: str) -> bool:
    """Returns True if allowed, False if rate limited."""
    _prune_stale_buckets()
    now = time.time()
    window = _get_rate_limit_window()
    max_msgs = _get_rate_limit_max()
    bucket = _rate_limit_buckets[key]
    # Remove expired entries
    _rate_limit_buckets[key] = [t for t in bucket if now - t < window]
    if len(_rate_limit_buckets[key]) >= max_msgs:
        return False
    _rate_limit_buckets[key].append(now)
    return True


class ChannelMessageRouter:
    """Channel-agnostic message dispatcher."""

    async def handle_message(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        """Process an incoming message through the full pipeline."""
        try:
            await self._handle_message_inner(adapter, message)
        except Exception as e:
            logger.error(f"[ROUTER] Unhandled error in handle_message: {e}", exc_info=True)

    async def _handle_message_inner(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        logger.info(f"[ROUTER] START: sender={message.sender_id}, channel={message.channel_id}, team={message.metadata.get('team_id')}")

        # 1. Resolve agent
        agent_name = await adapter.get_agent_name(message)
        logger.debug(f"[ROUTER] Step 1 - resolved agent: {agent_name}")
        if not agent_name:
            logger.warning(f"[ROUTER] No agent found for channel {message.channel_id}")
            return

        # 2. Resolve bot token (needed for all responses)
        bot_token = self._get_bot_token(adapter, message)
        logger.debug(f"[ROUTER] Step 2 - bot_token: {'yes' if bot_token else 'NO'}")
        if not bot_token:
            logger.error(f"[ROUTER] No bot token for team {message.metadata.get('team_id')}")
            return

        # 3. Rate limiting per Slack user
        rate_key = f"slack:{message.metadata.get('team_id')}:{message.sender_id}"
        if not _check_rate_limit(rate_key):
            logger.warning(f"[ROUTER] Rate limited: {rate_key}")
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="You're sending messages too quickly. Please wait a moment.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            return

        # 4. Check agent availability
        container = get_agent_container(agent_name)
        container_status = container.status if container else "not_found"
        logger.debug(f"[ROUTER] Step 4 - container: {container_status}")
        if not container or container.status != "running":
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I'm not available right now. Please try again later.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
            )
            return

        # 5. Handle verification (base class default: always verified)
        logger.debug(f"[ROUTER] Step 5 - running verification")
        verified = await adapter.handle_verification(message)
        logger.debug(f"[ROUTER] Step 5 - verified: {verified}")
        if not verified:
            return

        # 6. Get/create session
        logger.debug(f"[ROUTER] Step 6 - creating session")
        session_identifier = self._build_session_identifier(message)
        session = db.get_or_create_public_chat_session(
            agent_name, session_identifier, "slack"
        )
        session_id = session.id if hasattr(session, 'id') else session["id"]
        logger.debug(f"[ROUTER] Step 6 - session_id: {session_id}")

        # 7. Build context prompt (same as web public chat)
        context_prompt = db.build_public_chat_context(session_id, message.text)
        logger.debug(f"[ROUTER] Step 7 - context built ({len(context_prompt)} chars)")

        # 8. Show processing indicator (⏳ on Slack, typing on Telegram, etc.)
        await adapter.indicate_processing(message)

        # 9. Execute via TaskExecutionService (same path as web public chat)
        logger.debug(f"[ROUTER] Step 9 - executing via TaskExecutionService")
        source_email = f"slack:{message.metadata.get('team_id')}:{message.sender_id}"

        # Security: restrict tools for public channel users
        # No file access (Read exposes .env/credentials), no Bash, no Write/Edit
        # Configurable via settings_service (default: WebSearch, WebFetch)
        public_allowed_tools = _get_channel_allowed_tools()

        try:
            task_execution_service = get_task_execution_service()

            result = await task_execution_service.execute_task(
                agent_name=agent_name,
                message=context_prompt,
                triggered_by="slack",
                source_user_email=source_email,
                timeout_seconds=None,  # Uses agent's configured timeout (TIMEOUT-001)
                allowed_tools=public_allowed_tools,
            )

            if result.status == "failed":
                error_msg = result.error or "Unknown error"
                logger.error(f"[ROUTER] Step 9 - task failed: {error_msg}")
                await adapter.indicate_done(message)

                # User-friendly error messages
                if result.error_code == "timeout":
                    response_text = "Sorry, that took too long to process. Try a simpler request or ask me to be more concise."
                elif "at capacity" in error_msg:
                    response_text = "I'm busy right now. Please try again in a moment."
                elif "billing" in error_msg.lower() or "credit" in error_msg.lower():
                    response_text = "I'm having trouble processing your request. Please try again later."
                else:
                    response_text = "Sorry, I encountered an error processing your message."

                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(text=response_text, metadata={"bot_token": bot_token, "agent_name": agent_name}),
                    thread_id=message.thread_id,
                )
                return

            response_text = result.response or ""
            logger.debug(f"[ROUTER] Step 9 - agent responded ({len(response_text)} chars, cost=${result.cost or 0:.4f})")

        except Exception as e:
            logger.error(f"[ROUTER] Step 9 - execution error: {e}", exc_info=True)
            await adapter.indicate_done(message)
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I encountered an error processing your message. Please try again.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            return

        # 10. Done processing — show completion indicator
        await adapter.indicate_done(message)

        # 11. Persist messages in session
        logger.debug(f"[ROUTER] Step 11 - persisting messages")
        db.add_public_chat_message(session_id, "user", message.text)
        db.add_public_chat_message(session_id, "assistant", response_text, cost=result.cost)

        # 12. Send response to channel
        logger.debug(f"[ROUTER] Step 12 - sending response")
        await adapter.send_response(
            message.channel_id,
            ChannelResponse(text=response_text, metadata={"bot_token": bot_token, "agent_name": agent_name}),
            thread_id=message.thread_id,
        )

        # 13. Post-response hook (thread tracking, etc.)
        await adapter.on_response_sent(message, agent_name)

        logger.info(f"[ROUTER] DONE: {agent_name}, execution_id={result.execution_id}")

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _get_bot_token(self, adapter: ChannelAdapter, message: NormalizedMessage) -> Optional[str]:
        """Get bot token from adapter."""
        if hasattr(adapter, 'get_bot_token'):
            return adapter.get_bot_token(message.metadata.get("team_id"))
        return None

    def _build_session_identifier(self, message: NormalizedMessage) -> str:
        """Build a session identifier: team:user:channel for isolation."""
        team_id = message.metadata.get("team_id", "unknown")
        channel_id = message.channel_id
        return f"{team_id}:{message.sender_id}:{channel_id}"


# Singleton instance
message_router = ChannelMessageRouter()
