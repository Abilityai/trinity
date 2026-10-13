"""
Telegram webhook transport — inbound POST from Telegram Bot API.

Receives updates at POST /api/telegram/webhook/{webhook_secret}.
Validates via X-Telegram-Bot-Api-Secret-Token header.
Processes asynchronously — returns 200 immediately.

Supports:
- message: text, media, commands
- my_chat_member: bot added/removed from groups (TGRAM-GROUP)
- chat_member: user join/leave for welcome messages (TGRAM-GROUP)
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import Request

from adapters.transports.base import ChannelTransport
from database import db

logger = logging.getLogger(__name__)


class TelegramWebhookTransport(ChannelTransport):
    """Telegram webhooks — inbound POST, needs public URL."""

    async def start(self) -> None:
        """No-op: webhook transport is passive (FastAPI endpoint handles requests)."""
        self._running = True
        logger.info("Telegram webhook transport ready")

    async def stop(self) -> None:
        """No-op: nothing to clean up."""
        self._running = False

    async def handle_webhook(self, request: Request, webhook_secret: str) -> dict:
        """
        Called by the FastAPI endpoint when Telegram sends an update.

        Returns 200 immediately. Processes the update asynchronously.
        """
        # 1. Resolve binding by webhook secret
        binding = db.get_telegram_binding_by_webhook_secret(webhook_secret)
        if not binding:
            logger.warning(f"Telegram webhook: unknown webhook_secret")
            return {"ok": False}

        # 2. Validate X-Telegram-Bot-Api-Secret-Token header
        expected_token = binding.get("telegram_secret_token")
        actual_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_token or actual_token != expected_token:
            logger.warning(f"Telegram webhook: invalid secret token for agent={binding['agent_name']}")
            return {"ok": False}

        # 3. Parse the update
        try:
            body = await request.body()
            update = json.loads(body)
        except Exception as e:
            logger.error(f"Telegram webhook: failed to parse update: {e}")
            return {"ok": True}  # Return 200 so Telegram doesn't retry

        # 4. Dedup by update_id
        update_id = update.get("update_id", 0)
        if update_id <= binding.get("last_update_id", 0):
            logger.debug(f"Telegram webhook: skipping duplicate update_id={update_id}")
            return {"ok": True}

        # Update last_update_id
        db.update_telegram_last_update_id(binding["agent_name"], update_id)

        # 5. Inject routing metadata for the adapter
        update["_bot_id"] = binding.get("bot_id", "")
        update["_bot_username"] = binding.get("bot_username", "")
        update["_agent_name"] = binding["agent_name"]

        # 6. Process asynchronously — return 200 immediately
        asyncio.create_task(self._process_update(update, binding))

        return {"ok": True}

    async def _process_update(self, update: dict, binding: dict) -> None:
        """Process a Telegram update through the adapter → router pipeline."""
        try:
            # Handle member events (bot added/removed, user join/leave)
            if update.get("my_chat_member") or update.get("chat_member"):
                await self.adapter.handle_member_event(update, binding)
                return

            # Check for bot commands first
            message = update.get("message", {})
            text = message.get("text", "")

            if text.startswith("/"):
                # Handle commands directly in the adapter. ent#600: an untagged
                # command in a mention-mode group (bare `/reset`, `/help`) used
                # to be inert only because parse_message returned None; it now
                # parses as observe-only, so gate here — trigger rules must not
                # change. `/reset@bot` is the deliberate group-reset gesture.
                normalized = self.adapter.parse_message(update)
                if normalized and not normalized.metadata.get("observe_only"):
                    command_response = await self.adapter.handle_command(normalized)
                    if command_response:
                        bot_token = db.get_telegram_bot_token(binding["agent_name"])
                        if bot_token:
                            # Handle /reset by clearing the session
                            if text.strip().split("@")[0] == "/reset":
                                session_id = self.adapter.get_session_identifier(normalized)
                                session = db.get_or_create_public_chat_session(
                                    binding["agent_name"], session_id, "telegram"
                                )
                                sid = session.id if hasattr(session, 'id') else session.get("id")
                                if sid:
                                    db.clear_public_chat_session(sid)

                            await self.adapter._send_message(
                                bot_token=bot_token,
                                chat_id=str(message.get("chat", {}).get("id", "")),
                                text=command_response,
                                parse_mode="HTML",
                            )
                        return

            # Regular message — route through the standard pipeline
            await self.on_event(update)
        except Exception as e:
            logger.error(f"Telegram update processing error: {e}", exc_info=True)


async def register_webhook(agent_name: str, public_url: str) -> bool:
    """
    Register a Telegram webhook URL for an agent's bot.

    Called on bot configuration and on backend startup (reconciliation).
    Requests message and member update types for group support.
    """
    import httpx

    binding = db.get_telegram_binding(agent_name)
    if not binding:
        logger.error(f"No Telegram binding for agent {agent_name}")
        return False

    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        logger.error(f"Could not decrypt bot token for agent {agent_name}")
        return False

    webhook_url = f"{public_url}/api/telegram/webhook/{binding['webhook_secret']}"
    secret_token = binding.get("telegram_secret_token", "")

    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload = {
        "url": webhook_url,
        "secret_token": secret_token,
        "allowed_updates": ["message", "my_chat_member", "chat_member"],
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            result = resp.json()

            if result.get("ok"):
                db.update_telegram_webhook_url(agent_name, webhook_url)
                logger.info(f"Telegram webhook registered for agent={agent_name}")
                return True
            else:
                logger.error(f"Telegram setWebhook failed: {result.get('description')}")
                return False
    except Exception as e:
        logger.error(f"Telegram setWebhook error: {e}", exc_info=True)
        return False


async def delete_webhook(agent_name: str) -> bool:
    """Remove a Telegram webhook when a bot is disconnected."""
    import httpx

    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url)
            return resp.json().get("ok", False)
    except Exception as e:
        logger.error(f"Telegram deleteWebhook error: {e}", exc_info=True)
        return False
