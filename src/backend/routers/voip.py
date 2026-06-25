"""
VoIP telephony router (VOIP-001, #1056 — Phase 1, outbound).

Surfaces:
- Binding CRUD (owner-only): GET/PUT/DELETE /api/agents/{agent_name}/voip
- Outbound trigger (any agent-accessor): POST /api/agents/{agent_name}/voip/call
- Media Streams WebSocket (Twilio, ticket-authed): WS /api/voip/voice/{call_id}

The feature is gated by `voip_service.is_available()` (VOIP_ENABLED + GEMINI_API_KEY,
default OFF) — every endpoint 404s when off. It is additionally per-agent gated:
the trigger requires an active `voip_bindings` row.
"""

import logging
from typing import Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    Query,
    status,
)
from pydantic import BaseModel

from database import db
from dependencies import AuthorizedAgentByName, OwnedAgentByName, get_current_user
from models import User
from services import idempotency_service
from services.settings_service import settings_service
from services.voip_service import voip_service, normalize_e164
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/agents", tags=["voip"])
public_router = APIRouter(tags=["voip-public"])


# ── Request/Response models ──────────────────────────────────────────────────

class VoipConfigureRequest(BaseModel):
    account_sid: str
    auth_token: str
    from_number: str
    daily_call_cap: Optional[int] = None


class VoipBindingResponse(BaseModel):
    agent_name: str
    configured: bool
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    daily_call_cap: Optional[int] = None
    display_name: Optional[str] = None
    enabled: Optional[bool] = None


class VoipCallRequest(BaseModel):
    to_number: str
    context: Optional[str] = None
    process_transcript: bool = True


class VoipEnabledRequest(BaseModel):
    enabled: bool


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_enabled():
    if not voip_service.is_available():
        raise HTTPException(status_code=404, detail="VoIP is not enabled")


async def _validate_twilio_credentials(account_sid: str, auth_token: str) -> dict:
    """Validate AccountSid/AuthToken via Twilio's Account fetch endpoint."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, auth=(account_sid, auth_token))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Twilio API: {e}")
    if resp.status_code == 401:
        raise HTTPException(status_code=400, detail="Invalid Twilio credentials (AccountSid or AuthToken)")
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Twilio rejected credentials (status={resp.status_code})")
    return resp.json()


# ── Binding CRUD (owner-only) ────────────────────────────────────────────────

@auth_router.get("/{agent_name}/voip", response_model=VoipBindingResponse)
async def get_voip_binding(agent_name: OwnedAgentByName):
    """Get the Twilio voice binding status for an agent (owner-only)."""
    _require_enabled()
    binding = db.get_voip_binding(agent_name)
    if not binding:
        return VoipBindingResponse(agent_name=agent_name, configured=False)
    return VoipBindingResponse(
        agent_name=agent_name,
        configured=True,
        account_sid=binding["account_sid"],
        from_number=binding["from_number"],
        daily_call_cap=binding.get("daily_call_cap"),
        display_name=binding.get("display_name"),
        enabled=binding.get("enabled"),
    )


@auth_router.put("/{agent_name}/voip", response_model=VoipBindingResponse)
async def configure_voip_binding(
    agent_name: OwnedAgentByName,
    config: VoipConfigureRequest,
    current_user: User = Depends(get_current_user),
):
    """Configure a Twilio voice sender for an agent (owner-only).

    Validates the AccountSid/AuthToken with Twilio, encrypts the AuthToken,
    and creates/replaces the binding.
    """
    _require_enabled()
    account_sid = config.account_sid.strip()
    auth_token = config.auth_token.strip()
    from_number = normalize_e164(config.from_number)

    if not account_sid.startswith("AC") or len(account_sid) != 34:
        raise HTTPException(status_code=400, detail="AccountSid must start with 'AC' and be 34 characters long.")

    account_info = await _validate_twilio_credentials(account_sid, auth_token)
    display_name = account_info.get("friendly_name") or None

    binding = db.create_voip_binding(
        agent_name=agent_name,
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
        daily_call_cap=config.daily_call_cap,
        display_name=display_name,
        created_by=str(current_user.id) if current_user else None,
    )
    logger.info(f"VoIP binding configured for agent={agent_name} sid={account_sid[:8]}...")
    return VoipBindingResponse(
        agent_name=agent_name,
        configured=True,
        account_sid=binding["account_sid"],
        from_number=binding["from_number"],
        daily_call_cap=binding.get("daily_call_cap"),
        display_name=binding.get("display_name"),
        enabled=binding.get("enabled"),
    )


@auth_router.put("/{agent_name}/voip/enabled", response_model=VoipBindingResponse)
async def set_voip_enabled(
    agent_name: OwnedAgentByName,
    config: VoipEnabledRequest,
):
    """Enable/disable an agent's VoIP without re-entering credentials (owner-only, #28).

    The call path already refuses disabled bindings (voip_service); this flips the
    `enabled` flag in place. 404 when no binding exists so the UI never shows a
    disabled-but-nonexistent state.
    """
    _require_enabled()
    updated = db.set_voip_enabled(agent_name, config.enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="No VoIP binding configured for this agent")
    binding = db.get_voip_binding(agent_name)
    return VoipBindingResponse(
        agent_name=agent_name,
        configured=True,
        account_sid=binding["account_sid"],
        from_number=binding["from_number"],
        daily_call_cap=binding.get("daily_call_cap"),
        display_name=binding.get("display_name"),
        enabled=binding.get("enabled"),
    )


@auth_router.delete("/{agent_name}/voip")
async def delete_voip_binding(agent_name: OwnedAgentByName):
    """Remove the Twilio voice binding for an agent (owner-only)."""
    _require_enabled()
    deleted = db.delete_voip_binding(agent_name)
    return {"deleted": deleted, "agent_name": agent_name}


# ── Outbound trigger ─────────────────────────────────────────────────────────

@auth_router.post("/{agent_name}/voip/call")
async def place_voip_call(
    request: VoipCallRequest,
    agent_name: AuthorizedAgentByName,
    current_user: User = Depends(get_current_user),
    idempotency_key: Optional[str] = Header(None),
):
    """Place an outbound phone call from the agent to a user (JWT/MCP authed).

    Rate-limited per (owner, destination) + a durable per-agent daily cap.
    Accepts an optional Idempotency-Key (Invariant #18) so a retried trigger
    doesn't place two calls.
    """
    _require_enabled()
    public_url = settings_service.get_setting("public_chat_url", "")

    # Idempotency gate (#525): a duplicate trigger must not double-dial.
    scope = idempotency_service.make_agent_scope(agent_name)
    idem = idempotency_service.begin(scope, idempotency_key)
    if idem.replay:
        if idem.in_flight:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="A duplicate call request is still being processed.")
        return idem.snapshot or {"status": "ringing"}

    try:
        result = await voip_service.place_outbound_call(
            agent_name=agent_name,
            to_number=request.to_number,
            initiator_user_id=current_user.id,
            initiator_email=current_user.email or current_user.username,
            public_url=public_url,
            context=request.context,
            process_transcript=request.process_transcript,
        )
    except HTTPException:
        idempotency_service.fail(idem)  # nothing durable dialed → release the claim
        raise
    except Exception as e:  # noqa: BLE001
        idempotency_service.fail(idem)
        logger.error("VoIP call trigger failed for agent=%s: %s", agent_name, e)
        raise HTTPException(status_code=500, detail="Failed to place call")

    idempotency_service.complete(idem, result.get("call_id"), result)
    await platform_audit_service.log(
        event_type=AuditEventType.EXECUTION,
        event_action="voip_call_placed",
        source="api",
        actor_user=current_user,
        target_type="agent",
        target_id=agent_name,
        details={"to_number": result.get("to_number"), "call_id": result.get("call_id")},
    )
    return result


# ── Media Streams WebSocket (Twilio) ─────────────────────────────────────────

@public_router.websocket("/api/voip/voice/{call_id}")
async def voip_media_stream(
    websocket: WebSocket,
    call_id: str,
    ticket: Optional[str] = Query(default=None),
):
    """Twilio Media Streams bridge — ticket-authed, no JWT (Twilio can't send one)."""
    from adapters.transports.twilio_media_stream import handle_media_stream
    await handle_media_stream(websocket, call_id, ticket)
