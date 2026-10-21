"""
Telegram webhook transport — inbound POST from Telegram Bot API.

Receives updates at POST /api/telegram/webhook/{webhook_secret}.
Validates via X-Telegram-Bot-Api-Secret-Token header.
Processes asynchronously — returns 200 immediately.

Supports:
- message: text, media, commands
- my_chat_member: bot added/removed from groups (TGRAM-GROUP)
- chat_member: user join/leave for welcome messages (TGRAM-GROUP)

Albums (ent#934): Telegram sends an album as one update per item sharing a
`media_group_id`; the items are buffered in Redis (`inbound_coalescer`) and
reach the pipeline as ONE merged update.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import Request

from adapters.transports.base import ChannelTransport
from adapters.transports.inbound_coalescer import InboundCoalescer, get_async_redis
from database import db

logger = logging.getLogger(__name__)

# ent#934 — album coalescing. The window re-arms on every item, so it is the
# quiet time after the album's LAST item; Telegram caps an album at 10 items.
ALBUM_FLUSH_WINDOW_S = 1.5
ALBUM_MAX_ITEMS = 10
_ALBUM_PREFIX = "trinity:telegram:album"

# Per-update dedup. Telegram delivers over up to 40 parallel connections and
# documents that updates can arrive out of order, so "skip anything <= the last
# update_id" drops a real update that loses the race — an album is exactly such
# a burst. Each update_id is claimed once instead (SET NX), for a day.
_UPDATE_DEDUP_PREFIX = "trinity:telegram:update"
_UPDATE_DEDUP_TTL_S = 24 * 60 * 60


class TelegramWebhookTransport(ChannelTransport):
    """Telegram webhooks — inbound POST, needs public URL."""

    def __init__(self, adapter, router):
        super().__init__(adapter, router)
        self._albums: Optional[InboundCoalescer] = InboundCoalescer(
            prefix=_ALBUM_PREFIX,
            window_s=ALBUM_FLUSH_WINDOW_S,
            max_items=ALBUM_MAX_ITEMS,
            on_flush=self._flush_album,
        )
        self._dedup_redis = None  # resolved lazily; tests inject a fake
        # Strong references to in-flight update tasks: a bare create_task can be
        # garbage-collected before it finishes.
        self._tasks: set = set()

    async def start(self) -> None:
        """Passive transport (the FastAPI endpoint receives updates). Delivers any
        album a restart left buffered in Redis (ent#934)."""
        self._running = True
        if self._albums is not None:
            try:
                await self._albums.sweep_orphans()
            except Exception as e:
                logger.warning(f"Telegram album sweep at start failed (non-fatal): {e}")
        logger.info("Telegram webhook transport ready")

    async def stop(self) -> None:
        """Cancel pending album timers; their albums stay in Redis for the next sweep."""
        self._running = False
        if self._albums is not None:
            await self._albums.stop()

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
        if not await self._claim_update(binding, update_id):
            logger.debug(f"Telegram webhook: skipping duplicate update_id={update_id}")
            return {"ok": True}

        # Update last_update_id (monotonic — the Redis-down fallback's watermark)
        db.update_telegram_last_update_id(binding["agent_name"], update_id)

        # 5. Inject routing metadata for the adapter
        update["_bot_id"] = binding.get("bot_id", "")
        update["_bot_username"] = binding.get("bot_username", "")
        update["_agent_name"] = binding["agent_name"]

        # 6. Process asynchronously — return 200 immediately
        task = asyncio.create_task(self._process_update(update, binding))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return {"ok": True}

    async def _claim_update(self, binding: dict, update_id: int) -> bool:
        """True the first time this update_id is seen for this bot (ent#934).

        Falls back to the old watermark when Redis is unavailable, so dedup
        degrades to today's behaviour rather than disappearing.
        """
        try:
            r = self._dedup_redis or get_async_redis()
            key = f"{_UPDATE_DEDUP_PREFIX}:{binding.get('id')}:{update_id}"
            return bool(await r.set(key, "1", nx=True, ex=_UPDATE_DEDUP_TTL_S))
        except Exception as e:
            logger.warning(f"Telegram webhook: update dedup unavailable ({e}); using watermark")
            return update_id > binding.get("last_update_id", 0)

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

            # An album item (ent#934) — buffer it; the album reaches the
            # pipeline once, merged. Items from bots are never buffered (the
            # adapter drops them anyway). A False from add() means "not
            # buffered" (Redis down, a late item, a cap) — handle it as today.
            media_group_id = message.get("media_group_id")
            if (media_group_id and self._albums is not None
                    and not message.get("from", {}).get("is_bot", False)):
                chat_id = message.get("chat", {}).get("id", "")
                scope = str(binding.get("id") or binding.get("agent_name"))
                if await self._albums.add(scope, f"{chat_id}:{media_group_id}", update):
                    return

            # Regular message — route through the standard pipeline
            await self.on_event(update)
        except Exception as e:
            logger.error(f"Telegram update processing error: {e}", exc_info=True)

    async def _flush_album(self, updates: list, recovered: bool = False) -> None:
        """One merged update for the whole album → the standard pipeline."""
        from adapters.telegram_adapter import merge_album_updates

        merged = merge_album_updates(updates)
        logger.info(
            f"Telegram album: {len(updates)} item(s) as one turn for "
            f"agent={merged.get('_agent_name')}{' (recovered after restart)' if recovered else ''}"
        )
        await self.on_event(merged)


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
