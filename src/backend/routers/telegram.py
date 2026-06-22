"""
Telegram bot integration router (TELEGRAM-001, TGRAM-GROUP).

Thin HTTP layer that delegates to the channel adapter abstraction.

Public Endpoints (no auth — validated by webhook secret + header token):
- POST /api/telegram/webhook/{webhook_secret} — Receive Telegram updates

Authenticated Endpoints:
- GET    /api/agents/{name}/telegram              — Bot binding status
- PUT    /api/agents/{name}/telegram              — Configure bot token
- DELETE /api/agents/{name}/telegram              — Remove bot binding
- POST   /api/agents/{name}/telegram/test         — Send test message
- GET    /api/agents/{name}/telegram/groups        — List group configs (TGRAM-GROUP)
- PUT    /api/agents/{name}/telegram/groups/{id}   — Update group config
- DELETE /api/agents/{name}/telegram/groups/{id}   — Remove group config
"""

import logging
from typing import Optional, List

import httpx
from fastapi import APIRouter, HTTPException, Request, Depends

from database import db
from dependencies import get_current_user, OwnedAgentByName
from models import (
    TelegramBindingResponse,
    TelegramConfigureRequest,
    TelegramGroupConfigResponse,
    TelegramGroupConfigUpdateRequest,
    TelegramGroupMessageRequest,
    TelegramTestRequest,
    TelegramWebhookResponse,
    User,
)

logger = logging.getLogger(__name__)


# =========================================================================
# Transport reference — set by startup hook in main.py
# =========================================================================

_webhook_transport = None


def set_webhook_transport(transport):
    """Set the webhook transport instance (called from main.py startup)."""
    global _webhook_transport
    _webhook_transport = transport


# =========================================================================
# Public Router (webhook receiver — no JWT auth, validated by secret token)
# =========================================================================

public_router = APIRouter(prefix="/api/telegram", tags=["telegram-public"])


@public_router.post("/webhook/{webhook_secret}", response_model=TelegramWebhookResponse)
async def handle_telegram_webhook(webhook_secret: str, request: Request):
    """
    Receive Telegram Bot API updates.

    Authentication: webhook_secret in URL for routing + X-Telegram-Bot-Api-Secret-Token header.
    Always returns 200 to prevent Telegram retries.
    """
    if not _webhook_transport:
        logger.warning("Telegram webhook received but transport not initialized")
        return TelegramWebhookResponse(ok=True)

    result = await _webhook_transport.handle_webhook(request, webhook_secret)
    return TelegramWebhookResponse(ok=result.get("ok", True))


# =========================================================================
# Authenticated Router (bot configuration + group config)
# =========================================================================

auth_router = APIRouter(prefix="/api/agents", tags=["telegram"])


@auth_router.get("/{agent_name}/telegram", response_model=TelegramBindingResponse)
async def get_telegram_binding(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get Telegram bot binding status for an agent."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        return TelegramBindingResponse(agent_name=agent_name, configured=False)

    bot_username = binding.get("bot_username")
    groups = db.get_telegram_groups_for_agent(agent_name)
    return TelegramBindingResponse(
        agent_name=agent_name,
        bot_username=bot_username,
        bot_id=binding.get("bot_id"),
        webhook_url=binding.get("webhook_url"),
        bot_link=f"https://t.me/{bot_username}" if bot_username else None,
        configured=True,
        group_count=len(groups),
    )


@auth_router.put("/{agent_name}/telegram", response_model=TelegramBindingResponse)
async def configure_telegram_bot(
    agent_name: OwnedAgentByName,
    config: TelegramConfigureRequest,
):
    """
    Configure a Telegram bot for an agent.

    Validates the bot token via getMe API, stores encrypted,
    and registers the webhook if a public URL is available.
    """
    bot_token = config.bot_token.strip()

    # Validate token format: {bot_id}:{secret}
    if ":" not in bot_token:
        raise HTTPException(status_code=400, detail="Invalid bot token format. Expected format: 123456:ABC-DEF")

    # Validate token via getMe
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            result = resp.json()

            if not result.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid bot token: {result.get('description', 'Unknown error')}"
                )

            bot_info = result["result"]
            bot_username = bot_info.get("username")
            bot_id = str(bot_info.get("id"))

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Telegram API: {e}")

    # Check bot_id isn't already bound to another agent
    existing = db.get_telegram_binding_by_bot_id(bot_id)
    if existing and existing["agent_name"] != agent_name:
        raise HTTPException(
            status_code=409,
            detail=f"This bot is already bound to agent '{existing['agent_name']}'"
        )

    # Create binding (encrypted token)
    binding = db.create_telegram_binding(
        agent_name=agent_name,
        bot_token=bot_token,
        bot_username=bot_username,
        bot_id=bot_id,
    )

    # Register webhook if public URL is available
    from services.settings_service import settings_service
    public_url = settings_service.get_setting("public_chat_url", "")
    warning: Optional[str] = None
    if public_url:
        from adapters.transports.telegram_webhook import register_webhook
        await register_webhook(agent_name, public_url)
        # Refresh binding to get updated webhook_url
        binding = db.get_telegram_binding(agent_name)
    else:
        warning = (
            "Bot connected, but webhook not registered: 'public_chat_url' is not "
            "set in Settings. The bot will start receiving messages automatically "
            "once a public URL is saved."
        )
        logger.warning(
            f"Telegram bot configured for agent={agent_name} without public_chat_url — "
            "webhook registration deferred until the setting is saved"
        )

    logger.info(f"Telegram bot configured for agent={agent_name} bot=@{bot_username}")

    return TelegramBindingResponse(
        agent_name=agent_name,
        bot_username=bot_username,
        bot_id=bot_id,
        webhook_url=binding.get("webhook_url") if binding else None,
        bot_link=f"https://t.me/{bot_username}" if bot_username else None,
        configured=True,
        group_count=0,
        warning=warning,
    )


@auth_router.delete("/{agent_name}/telegram")
async def delete_telegram_binding(
    agent_name: OwnedAgentByName,
):
    """Remove Telegram bot binding from an agent."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

    # Remove webhook from Telegram
    from adapters.transports.telegram_webhook import delete_webhook
    await delete_webhook(agent_name)

    # Delete from DB (cascades to group configs and chat links)
    db.delete_telegram_binding(agent_name)

    logger.info(f"Telegram bot removed for agent={agent_name}")
    return {"ok": True, "message": f"Telegram bot removed from {agent_name}"}


@auth_router.post("/{agent_name}/telegram/test")
async def test_telegram_bot(
    agent_name: OwnedAgentByName,
    test: TelegramTestRequest,
):
    """Send a test message via the agent's Telegram bot."""
    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        raise HTTPException(status_code=404, detail="No Telegram binding found or token decryption failed")

    # If no chat_id provided, just verify the bot can make API calls
    if not test.chat_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
                result = resp.json()
                if result.get("ok"):
                    bot_info = result["result"]
                    return {
                        "ok": True,
                        "message": f"Bot @{bot_info.get('username')} is operational",
                        "bot_info": bot_info,
                    }
                else:
                    return {"ok": False, "message": result.get("description", "Unknown error")}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # Send test message to specific chat
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": test.chat_id,
                    "text": test.message,
                    "parse_mode": "HTML",
                }
            )
            result = resp.json()
            if result.get("ok"):
                return {"ok": True, "message": "Test message sent successfully"}
            else:
                return {"ok": False, "message": result.get("description", "Failed to send")}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# =========================================================================
# Group Config Endpoints (TGRAM-GROUP)
# =========================================================================


@auth_router.get(
    "/{agent_name}/telegram/groups",
    response_model=List[TelegramGroupConfigResponse],
)
async def list_telegram_groups(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """List all Telegram groups this agent's bot is in."""
    groups = db.get_telegram_groups_for_agent(agent_name)
    return [TelegramGroupConfigResponse(**g) for g in groups]


@auth_router.put("/{agent_name}/telegram/groups/{group_config_id}")
async def update_telegram_group(
    agent_name: OwnedAgentByName,
    group_config_id: int,
    config: TelegramGroupConfigUpdateRequest,
):
    """Update a group's trigger mode or welcome message settings."""
    # Validate trigger_mode if provided
    # Issue #349: Added 'observe' mode - agent sees all messages but can return [NO_REPLY]
    if config.trigger_mode is not None and config.trigger_mode not in ("mention", "all", "observe"):
        raise HTTPException(status_code=400, detail="trigger_mode must be 'mention', 'all', or 'observe'")

    # Validate welcome_text length
    if config.welcome_text is not None and len(config.welcome_text) > 4096:
        raise HTTPException(status_code=400, detail="welcome_text must be 4096 characters or less")

    # Verify the group config belongs to this agent's binding
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

    # Check ownership: group config must belong to this agent's binding
    groups = db.get_telegram_groups_for_agent(agent_name)
    if not any(g["id"] == group_config_id for g in groups):
        raise HTTPException(status_code=404, detail="Group config not found for this agent")

    updated = db.update_telegram_group_config(
        group_config_id=group_config_id,
        trigger_mode=config.trigger_mode,
        welcome_enabled=config.welcome_enabled,
        welcome_text=config.welcome_text,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Group config not found")

    return updated


@auth_router.delete("/{agent_name}/telegram/groups/{group_config_id}")
async def delete_telegram_group(
    agent_name: OwnedAgentByName,
    group_config_id: int,
):
    """Remove a group config (bot will ignore messages from this group)."""
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

    # Deactivate rather than delete — preserves history
    groups = db.get_telegram_groups_for_agent(agent_name)
    target = next((g for g in groups if g["id"] == group_config_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Group config not found")

    db.deactivate_telegram_group_config(binding["id"], target["chat_id"])
    return {"ok": True, "message": "Group config removed"}


# =========================================================================
# Proactive Group Messaging (Issue #349)
# =========================================================================

# Rate limiting constants
_PROACTIVE_RATE_LIMIT_PER_GROUP = 10     # messages per hour per group
_PROACTIVE_RATE_LIMIT_PER_AGENT = 100    # messages per hour per agent
_PROACTIVE_RATE_LIMIT_WINDOW = 3600      # 1 hour in seconds

# In-memory rate limit buckets (agent:group → timestamps)
_proactive_rate_buckets: dict = {}


def _check_proactive_rate_limit(agent_name: str, chat_id: str) -> tuple[bool, str]:
    """
    Check rate limits for proactive messaging.

    Returns (allowed, error_message). If not allowed, error_message explains why.
    """
    import time

    now = time.time()
    window = _PROACTIVE_RATE_LIMIT_WINDOW

    # Clean old entries
    group_key = f"{agent_name}:{chat_id}"
    agent_key = f"{agent_name}:*"

    # Group-level check
    group_bucket = _proactive_rate_buckets.get(group_key, [])
    group_bucket = [t for t in group_bucket if now - t < window]
    _proactive_rate_buckets[group_key] = group_bucket

    if len(group_bucket) >= _PROACTIVE_RATE_LIMIT_PER_GROUP:
        return False, f"Rate limited: max {_PROACTIVE_RATE_LIMIT_PER_GROUP} messages/hour to this group"

    # Agent-level check (sum across all groups)
    agent_total = 0
    for key, bucket in list(_proactive_rate_buckets.items()):
        if key.startswith(f"{agent_name}:"):
            cleaned = [t for t in bucket if now - t < window]
            _proactive_rate_buckets[key] = cleaned
            agent_total += len(cleaned)

    if agent_total >= _PROACTIVE_RATE_LIMIT_PER_AGENT:
        return False, f"Rate limited: max {_PROACTIVE_RATE_LIMIT_PER_AGENT} proactive messages/hour"

    # Allow and record
    _proactive_rate_buckets[group_key] = group_bucket + [now]
    return True, ""


@auth_router.post("/{agent_name}/telegram/groups/{chat_id}/messages")
async def send_telegram_group_message(
    agent_name: OwnedAgentByName,
    chat_id: str,
    request: TelegramGroupMessageRequest,
):
    """
    Send a proactive message to a Telegram group (Issue #349).

    The agent must have an active binding for this group. Rate limited to
    prevent spam: 10 messages/hour/group and 100 messages/hour/agent.
    """
    # Validate binding exists
    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found for this agent")

    # Validate group belongs to this agent
    groups = db.get_telegram_groups_for_agent(agent_name)
    target_group = next((g for g in groups if g["chat_id"] == chat_id and g["is_active"]), None)
    if not target_group:
        raise HTTPException(status_code=404, detail="Group not found or not active for this agent")

    # Check rate limits
    allowed, error_msg = _check_proactive_rate_limit(agent_name, chat_id)
    if not allowed:
        raise HTTPException(status_code=429, detail=error_msg)

    # Validate message length
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(request.message) > 4096:
        raise HTTPException(status_code=400, detail="Message exceeds Telegram's 4096 character limit")

    # Get bot token and send
    bot_token = db.get_telegram_bot_token(agent_name)
    if not bot_token:
        raise HTTPException(status_code=500, detail="Failed to retrieve bot token")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": request.message,
                    "parse_mode": "HTML",
                }
            )

            if response.status_code == 429:
                # Telegram rate limit
                retry_after = response.json().get("parameters", {}).get("retry_after", 60)
                raise HTTPException(
                    status_code=429,
                    detail=f"Telegram rate limit. Retry after {retry_after} seconds."
                )

            if response.status_code != 200:
                error_body = response.json()
                logger.error(f"Telegram API error: {error_body}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Telegram API error: {error_body.get('description', 'Unknown error')}"
                )

            result = response.json().get("result", {})
            return {
                "ok": True,
                "message_id": result.get("message_id"),
                "chat_id": chat_id,
                "group_title": target_group.get("chat_title"),
            }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Telegram API timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send proactive message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send message")
