"""
WhatsApp/Twilio integration router (WHATSAPP-001).

Thin HTTP layer delegating to the channel adapter abstraction.

Public Endpoints (no JWT — validated by webhook secret + Twilio HMAC):
- POST /api/whatsapp/webhook/{webhook_secret}

Authenticated Endpoints (agent owner only):
- GET    /api/agents/{name}/whatsapp           — binding status
- PUT    /api/agents/{name}/whatsapp           — configure Twilio credentials
- DELETE /api/agents/{name}/whatsapp           — remove binding
- POST   /api/agents/{name}/whatsapp/test      — validate credentials / send test message

Deployment prerequisite:
  Cloudflare Tunnel ingress rules must route `/api/whatsapp/webhook/*` to the
  frontend service (see `docs/requirements/PUBLIC_EXTERNAL_ACCESS_SETUP.md`).
  Without this, Twilio webhooks return 404 at the edge and never reach backend.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from adapters.transports.twilio_webhook import (
    backfill_webhook_urls,
    compute_webhook_url,
)
from database import db
from dependencies import OwnedAgentByName, get_current_user
from models import User, WhatsAppBindingResponse, WhatsAppConfigureRequest, WhatsAppTestRequest
from services.settings_service import settings_service

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
# Public Router (webhook receiver — no JWT auth, HMAC validated)
# =========================================================================

public_router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp-public"])

# Empty TwiML response — Twilio accepts this as an ack; response is sent async
# via REST so we never put reply content in the webhook response body.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


@public_router.post("/webhook/{webhook_secret}")
async def handle_twilio_webhook(webhook_secret: str, request: Request):
    """
    Receive Twilio WhatsApp webhooks.

    Authentication: webhook_secret in URL routes to binding; X-Twilio-Signature
    HMAC-SHA1 (validated by `twilio.request_validator.RequestValidator`) proves
    Twilio sent the request. Returns 200 with empty TwiML by default so Twilio
    never retries; returns 403 only on signature mismatch.
    """
    if not _webhook_transport:
        logger.warning("[WHATSAPP] Webhook received but transport not initialized")
        return Response(content=_EMPTY_TWIML, media_type="application/xml")

    result = await _webhook_transport.handle_webhook(request, webhook_secret)
    if result.get("status") == 403:
        raise HTTPException(status_code=403, detail="Invalid signature")
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


# =========================================================================
# Authenticated Router (binding configuration)
# =========================================================================

auth_router = APIRouter(prefix="/api/agents", tags=["whatsapp"])


def _validate_from_number(from_number: str) -> str:
    """Ensure the from_number is in Twilio's 'whatsapp:+E164' format."""
    value = (from_number or "").strip()
    if not value.startswith("whatsapp:+"):
        raise HTTPException(
            status_code=400,
            detail="from_number must be in format 'whatsapp:+E164', e.g. 'whatsapp:+14155238886'",
        )
    rest = value[len("whatsapp:+"):]
    if not rest.isdigit() or len(rest) < 7 or len(rest) > 15:
        raise HTTPException(
            status_code=400,
            detail="from_number phone digits invalid; expected E.164 (7–15 digits after +)",
        )
    return value


async def _validate_twilio_credentials(account_sid: str, auth_token: str) -> dict:
    """Call Twilio's Account fetch to validate credentials. Returns account info."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, auth=(account_sid, auth_token))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Twilio API: {e}")

    if resp.status_code == 401:
        raise HTTPException(status_code=400, detail="Invalid Twilio credentials (AccountSid or AuthToken)")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Twilio rejected credentials (status={resp.status_code})",
        )
    return resp.json()


@auth_router.get("/{agent_name}/whatsapp", response_model=WhatsAppBindingResponse)
async def get_whatsapp_binding(
    agent_name: OwnedAgentByName,
):
    """Get WhatsApp/Twilio binding status for an agent.

    Owner-only: the response includes the webhook URL (which carries the
    binding's routing secret), so non-owners must not see it.
    """
    binding = db.get_whatsapp_binding(agent_name)
    if not binding:
        return WhatsAppBindingResponse(agent_name=agent_name, configured=False)

    # Recompute webhook_url from current settings to stay in sync.
    public_url = settings_service.get_setting("public_chat_url", "")
    webhook_url = compute_webhook_url(public_url, binding["webhook_secret"]) if public_url else None

    warning = None
    if not public_url:
        warning = (
            "public_chat_url is not set in Settings. Configure it, then paste the "
            "generated webhook URL into the Twilio Console."
        )
    elif not binding.get("webhook_url") or binding.get("webhook_url") != webhook_url:
        # Backfill DB to match
        db.update_whatsapp_webhook_url(agent_name, webhook_url)

    return WhatsAppBindingResponse(
        agent_name=agent_name,
        configured=True,
        account_sid=binding["account_sid"],
        from_number=binding["from_number"],
        messaging_service_sid=binding.get("messaging_service_sid"),
        display_name=binding.get("display_name"),
        is_sandbox=binding.get("is_sandbox", False),
        webhook_url=webhook_url,
        warning=warning,
    )


@auth_router.put("/{agent_name}/whatsapp", response_model=WhatsAppBindingResponse)
async def configure_whatsapp_binding(
    agent_name: OwnedAgentByName,
    config: WhatsAppConfigureRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Configure a Twilio WhatsApp sender for an agent.

    Validates the AccountSid/AuthToken by calling Twilio's Account endpoint,
    encrypts the AuthToken, and creates or replaces the binding.
    """
    account_sid = config.account_sid.strip()
    auth_token = config.auth_token.strip()
    from_number = _validate_from_number(config.from_number)

    if not account_sid.startswith("AC") or len(account_sid) != 34:
        raise HTTPException(
            status_code=400,
            detail="AccountSid must start with 'AC' and be 34 characters long.",
        )

    account_info = await _validate_twilio_credentials(account_sid, auth_token)
    display_name = account_info.get("friendly_name") or None

    binding = db.create_whatsapp_binding(
        agent_name=agent_name,
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
        messaging_service_sid=config.messaging_service_sid,
        display_name=display_name,
        created_by=str(current_user.id) if current_user else None,
    )

    # Compute and persist webhook_url if a public URL is configured.
    public_url = settings_service.get_setting("public_chat_url", "")
    warning = None
    webhook_url = None
    if public_url:
        webhook_url = compute_webhook_url(public_url, binding["webhook_secret"])
        db.update_whatsapp_webhook_url(agent_name, webhook_url)
    else:
        warning = (
            "Binding saved, but public_chat_url is not set in Settings. "
            "Once saved, the webhook URL will appear here for pasting into Twilio."
        )

    logger.info(f"WhatsApp binding configured for agent={agent_name} sid={account_sid[:8]}...")

    return WhatsAppBindingResponse(
        agent_name=agent_name,
        configured=True,
        account_sid=account_sid,
        from_number=from_number,
        messaging_service_sid=config.messaging_service_sid,
        display_name=display_name,
        is_sandbox=binding.get("is_sandbox", False),
        webhook_url=webhook_url,
        warning=warning,
    )


@auth_router.delete("/{agent_name}/whatsapp")
async def delete_whatsapp_binding_endpoint(
    agent_name: OwnedAgentByName,
):
    """Remove the Twilio WhatsApp binding for an agent."""
    binding = db.get_whatsapp_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No WhatsApp binding found")
    db.delete_whatsapp_binding(agent_name)
    logger.info(f"WhatsApp binding removed for agent={agent_name}")
    return {"ok": True, "message": f"WhatsApp binding removed from {agent_name}"}


@auth_router.post("/{agent_name}/whatsapp/test")
async def test_whatsapp_binding(
    agent_name: OwnedAgentByName,
    test: WhatsAppTestRequest,
):
    """Validate credentials and optionally send a test WhatsApp message."""
    binding = db.get_whatsapp_binding(agent_name)
    if not binding:
        raise HTTPException(status_code=404, detail="No WhatsApp binding found")

    auth_token = db.get_whatsapp_auth_token(agent_name)
    if not auth_token:
        raise HTTPException(status_code=500, detail="Could not decrypt AuthToken")

    # No destination → just verify credentials
    if not test.to_number:
        try:
            await _validate_twilio_credentials(binding["account_sid"], auth_token)
            return {"ok": True, "message": "Twilio credentials are valid"}
        except HTTPException as e:
            return {"ok": False, "message": e.detail}

    to_number = _validate_from_number(test.to_number)

    from adapters.whatsapp_adapter import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    result = await adapter._send_message(
        account_sid=binding["account_sid"],
        auth_token=auth_token,
        from_number=binding["from_number"],
        messaging_service_sid=binding.get("messaging_service_sid"),
        to_number=to_number,
        body=test.message,
    )
    if not result:
        return {"ok": False, "message": "Test message failed — check backend logs for Twilio error code"}
    return {"ok": True, "message": f"Test message sent (MessageSid={result.get('sid', '?')})"}


# Re-export the backfill helper so `routers/settings.py` can import from here
# (matches the import shape used for Telegram).
__all__ = [
    "public_router",
    "auth_router",
    "set_webhook_transport",
    "backfill_webhook_urls",
]
