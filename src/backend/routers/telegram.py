# mcp: channels.ts (list_channel_groups, send_group_message → /telegram/groups); binding CRUD is an owner grant surface, human-only
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
from services import channel_history, rate_limiter
from services.settings_service import get_proactive_rate_limit
from dependencies import (
    AuthorizedAgentByName,
    OwnedAgentByName,
    get_current_user,
    reject_agent_principal,
)
from models import (
    TelegramBindingResponse,
    TelegramConfigureRequest,
    TelegramGroupConfigResponse,
    TelegramGroupConfigUpdateRequest,
    TelegramGroupMessageRequest,
    TelegramProgressIndicatorRequest,
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


def _progress_indicator_enabled(binding: dict) -> bool:
    """ent#264 default-ON read predicate, evaluated in Python (never SQL —
    ``NULL != 0`` is NULL there): only an explicit 0 disables."""
    v = binding.get("progress_indicator_enabled")
    return v is None or v != 0


@auth_router.get("/{agent_name}/telegram", response_model=TelegramBindingResponse)
async def get_telegram_binding(
    agent_name: AuthorizedAgentByName,
):
    """Get Telegram bot binding status for an agent.

    Access hardened in ent#264 (previously any authenticated user): the
    response includes ``webhook_url``, which embeds the webhook secret —
    owner/shared/admin only, uniform-404 accessor (Invariant #8).
    """
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
        progress_indicator_enabled=_progress_indicator_enabled(binding),
    )


@auth_router.put(
    "/{agent_name}/telegram/progress-indicator",
    response_model=TelegramBindingResponse,
)
async def set_telegram_progress_indicator(
    agent_name: OwnedAgentByName,
    request: TelegramProgressIndicatorRequest,
    current_user: User = Depends(get_current_user),
):
    """ent#264 — toggle the in-progress status indicator for this binding.

    Dedicated route (mirrors ``PUT /voip/enabled``) so toggling never requires
    re-entering the bot token. Human-only: an agent-scoped key resolves to the
    OWNER on REST, so ownership checks alone would let an agent flip its own
    user-facing behavior toggle (ent#223 lesson).
    """
    reject_agent_principal(current_user)

    binding = db.get_telegram_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

    updated = db.set_telegram_progress_indicator(agent_name, request.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="No Telegram binding found")

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
        progress_indicator_enabled=request.enabled,
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
        # ent#264: fresh bindings default ON (column DEFAULT 1).
        progress_indicator_enabled=(
            _progress_indicator_enabled(binding) if binding else True
        ),
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
    agent_name: AuthorizedAgentByName,
):
    """List all Telegram groups this agent's bot is in.

    Access hardened alongside the binding-status GET (ent#264): group chat
    ids/titles/welcome text are tenant data — owner/shared/admin only,
    uniform-404 accessor (Invariant #8). The only follow-up action (the
    group-message POST) is already ``OwnedAgentByName``, so the read tier
    here is strictly broader than every usable consumer.
    """
    groups = db.get_telegram_groups_for_agent(agent_name)
    return [TelegramGroupConfigResponse(**g) for g in groups]


@auth_router.put("/{agent_name}/telegram/groups/{group_config_id}")
async def update_telegram_group(
    agent_name: OwnedAgentByName,
    group_config_id: int,
    config: TelegramGroupConfigUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """Update a group's trigger mode, welcome message, or completion-report
    consent settings."""
    # ent#265: the allow_proactive arm ONLY is human-only. An agent-scoped key
    # resolves to the OWNER on REST, so the owner gate alone would let an agent
    # flip its own consent on — self-granting the very control this adds
    # (ent#223's own post-ship pitfall, learnings 2026-07-24). Granting consent
    # is a human decision; existing agent-callable trigger_mode / welcome
    # updates keep working, and REPORTING under the consent stays automatic.
    if config.allow_proactive is not None:
        reject_agent_principal(current_user)

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
        allow_proactive=config.allow_proactive,   # ent#265: completion-report consent
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

# Proactive caps via the shared sliding-window limiter (#1023) — Redis-backed, so
# consistent across workers (the old per-process in-memory bucket was not). The
# per-group / per-agent limits are admin-configurable (#1609), sourced from
# settings at request time (0 = unlimited). Window fixed at 1 hour.
_PROACTIVE_RATE_LIMIT_WINDOW = 3600      # 1 hour in seconds


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

    # Rate limit: per-group then per-agent, caps from settings (#1609; 0 = skip).
    per_group = get_proactive_rate_limit("telegram_proactive_per_group")
    per_agent = get_proactive_rate_limit("telegram_proactive_per_agent")
    if per_group > 0:
        rate_limiter.enforce(
            f"telegram_proactive:{agent_name}:{chat_id}",
            per_group, _PROACTIVE_RATE_LIMIT_WINDOW,
            detail=f"Too many messages to this group (cap {per_group}/hour).",
        )
    if per_agent > 0:
        rate_limiter.enforce(
            f"telegram_proactive:{agent_name}",
            per_agent, _PROACTIVE_RATE_LIMIT_WINDOW,
            detail=f"Too many proactive messages from this agent (cap {per_agent}/hour).",
        )

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

            # #1649: record the broadcast in a channel session so it is not
            # simply lost. Telegram group sessions are keyed per (sender, chat)
            # — the adapter has no group branch — and a broadcast has no human
            # sender, so it is filed under a SYNTHETIC agent-sender key.
            #
            # Known limitation, deliberate: nothing else writes to that key, so
            # no participant's inbound session contains this message and the
            # agent still will NOT recall its broadcast when someone replies in
            # the group. This is honest bookkeeping (the message is recorded,
            # auditable, and attributable) — not a recall fix. Fixing recall
            # needs a per-chat group session, which changes existing inbound
            # group behaviour and is a separate decision.
            channel_history.persist_outbound_group_message(
                agent_name=agent_name,
                channel="telegram",
                session_identifier=channel_history.session_key_for_telegram_group(
                    bot_id=binding.get("bot_id", ""),
                    sender_id=agent_name,   # synthetic: the agent is the speaker
                    chat_id=chat_id,
                ),
                text=request.message,
            )

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
