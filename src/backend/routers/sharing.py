"""
Agent sharing routes for the Trinity backend.
"""
import asyncio
import json
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from models import User
from database import db, AgentShare, AgentOperatorAccess, AgentShareRequest
from dependencies import get_current_user, OwnedAgentByName, CurrentUser
from services.docker_service import get_agent_container
from services.platform_audit_service import platform_audit_service, AuditEventType
from services.proactive_message_service import proactive_message_service

logger = logging.getLogger(__name__)

# #951: Channels we can proactively notify the requester on when their access
# request is approved. `web` users see the change via the existing
# `agent_shared` WebSocket dashboard event and don't need a chat callback.
_NOTIFIABLE_APPROVAL_CHANNELS = {"telegram", "slack", "whatsapp"}


def _build_access_grant_text(agent_name: str) -> str:
    """User-facing notification body when an access request is approved.

    Mirrors the tone of the pending-approval reply emitted by
    `adapters/message_router.py` ("I'll let you know once the agent owner
    responds.") so the user recognises this as the promised follow-up.
    """
    return (
        f"✅ Access to {agent_name} approved by the agent owner. "
        f"You can now message the agent here."
    )


async def _notify_access_request_approval(
    agent_name: str,
    recipient_email: str,
    channel: str,
) -> None:
    """Fire-and-forget post-approval notification (#951).

    Failures are caught + audit-logged inside
    `proactive_message_service.send_access_grant_notification`; this wrapper
    only ensures an exception escaping the task doesn't show up as an
    "Task exception was never retrieved" warning in the backend logs.
    """
    try:
        await proactive_message_service.send_access_grant_notification(
            agent_name=agent_name,
            recipient_email=recipient_email,
            channel=channel,  # type: ignore[arg-type]
            text=_build_access_grant_text(agent_name),
        )
    except Exception as e:
        logger.warning(
            f"[#951] Background access-grant notification crashed for "
            f"{recipient_email} on {channel}: {e}"
        )

router = APIRouter(prefix="/api/agents", tags=["sharing"])


# ---------------------------------------------------------------------------
# Models for unified access control (Issue #311)
# ---------------------------------------------------------------------------

class AccessPolicy(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessPolicyUpdate(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessRequest(BaseModel):
    id: str
    agent_name: str
    email: str
    channel: str | None = None
    requested_at: str
    status: str


class AccessRequestDecision(BaseModel):
    approve: bool

# WebSocket manager will be injected from main.py
manager = None

def set_websocket_manager(ws_manager):
    """Set the WebSocket manager for broadcasting events."""
    global manager
    manager = ws_manager


@router.post("/{agent_name}/share", response_model=AgentShare)
async def share_agent_endpoint(
    agent_name: OwnedAgentByName,
    share_request: AgentShareRequest,
    request: Request,
    current_user: CurrentUser
):
    """Share an agent with another user by email."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    current_user_data = db.get_user_by_username(current_user.username)
    current_user_email = (current_user_data.get("email") or "") if current_user_data else ""
    if current_user_email and current_user_email.lower() == share_request.email.lower():
        raise HTTPException(status_code=400, detail="Cannot share an agent with yourself")

    share = db.share_agent(agent_name, current_user.username, share_request.email)
    if not share:
        raise HTTPException(status_code=409, detail=f"Agent is already shared with {share_request.email}")

    # Auto-add email to whitelist if email auth is enabled (Phase 12.4)
    from config import EMAIL_AUTH_ENABLED
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    if email_auth_setting.lower() == "true":
        try:
            db.add_to_whitelist(
                share_request.email,
                current_user.username,
                source="agent_sharing",
                default_role="user",  # chat-only grant; don't promote to creator (#314)
            )
        except Exception:
            # Already whitelisted or error - continue anyway
            pass

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_shared",
            "data": {"name": agent_name, "shared_with": share_request.email}
        }))

    # SEC-001: audit share
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="share",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"shared_with": share_request.email},
    )

    return share


@router.delete("/{agent_name}/share/{email}")
async def unshare_agent_endpoint(
    agent_name: OwnedAgentByName,
    email: str,
    request: Request,
    current_user: CurrentUser
):
    """Remove sharing access for a user."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    success = db.unshare_agent(agent_name, current_user.username, email)
    if not success:
        raise HTTPException(status_code=404, detail=f"No sharing found for {email}")

    if manager:
        await manager.broadcast(json.dumps({
            "event": "agent_unshared",
            "data": {"name": agent_name, "removed_user": email}
        }))

    # SEC-001: audit unshare
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="unshare",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"removed_email": email},
    )

    return {"message": f"Sharing removed for {email}"}


@router.get("/{agent_name}/shares", response_model=list[AgentShare])
async def get_agent_shares_endpoint(
    agent_name: OwnedAgentByName,
    request: Request
):
    """Get all users an agent is shared with."""
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    shares = db.get_agent_shares(agent_name)
    return shares


@router.get("/{agent_name}/access", response_model=list[AgentOperatorAccess])
async def get_agent_access_endpoint(
    agent_name: OwnedAgentByName,
    request: Request
):
    """Operator (Trinity-user) access roster for the Access tab (#17).

    Resolves each allow-list email against `users`: resolved → active operator
    (username/role/last-active), unresolved → pending invite.
    """
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    return db.get_agent_operator_access(agent_name)


# ---------------------------------------------------------------------------
# Unified channel access control (Issue #311)
# ---------------------------------------------------------------------------

@router.get("/{agent_name}/access-policy", response_model=AccessPolicy)
async def get_access_policy_endpoint(
    agent_name: OwnedAgentByName,
    current_user: CurrentUser,
):
    """Get the per-agent channel access policy."""
    return AccessPolicy(**db.get_access_policy(agent_name))


@router.put("/{agent_name}/access-policy", response_model=AccessPolicy)
async def update_access_policy_endpoint(
    agent_name: OwnedAgentByName,
    update: AccessPolicyUpdate,
    current_user: CurrentUser,
):
    """Update the per-agent channel access policy (owner-only)."""
    db.set_access_policy(
        agent_name,
        update.require_email,
        update.open_access,
        update.group_auth_mode,
    )
    return AccessPolicy(**db.get_access_policy(agent_name))


@router.get("/{agent_name}/access-requests", response_model=List[AccessRequest])
async def list_access_requests_endpoint(
    agent_name: OwnedAgentByName,
    current_user: CurrentUser,
    status: str = "pending",
):
    """List access requests for this agent (owner-only)."""
    rows = db.list_access_requests(agent_name, status)
    return [AccessRequest(**r) for r in rows]


@router.post("/{agent_name}/access-requests/{request_id}/decide", response_model=AccessRequest)
async def decide_access_request_endpoint(
    agent_name: OwnedAgentByName,
    request_id: str,
    decision: AccessRequestDecision,
    request: Request,
    current_user: CurrentUser,
):
    """Approve or deny a pending access request (owner-only).

    Approval inserts the email into agent_sharing so future messages from
    that user across any channel are admitted automatically.
    """
    existing = db.get_access_request(request_id)
    if not existing or existing["agent_name"] != agent_name:
        raise HTTPException(status_code=404, detail="Access request not found")

    user = db.get_user_by_username(current_user.username)
    if not user:
        raise HTTPException(status_code=403, detail="User not found")

    updated = db.decide_access_request(request_id, decision.approve, user["id"])
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update request")

    if decision.approve:
        # Insert into agent_sharing so the email is admitted on future messages.
        # share_agent is idempotent (returns None if already shared).
        db.share_agent(agent_name, current_user.username, existing["email"])

        # Auto-add to whitelist if email auth is enabled (parity with /share endpoint)
        from config import EMAIL_AUTH_ENABLED
        email_auth_setting = db.get_setting_value(
            "email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower()
        )
        if email_auth_setting.lower() == "true":
            try:
                db.add_to_whitelist(
                    existing["email"],
                    current_user.username,
                    source="access_request",
                    default_role="user",  # chat-only grant; don't promote to creator (#314)
                )
            except Exception:
                pass

        if manager:
            await manager.broadcast(json.dumps({
                "event": "agent_shared",
                "data": {"name": agent_name, "shared_with": existing["email"]},
            }))

        # #951: notify the requester on their originating channel. Fire-and-
        # forget — approval must succeed even if the channel transport is
        # unavailable or the user has since revoked their chat binding. Audit
        # of delivered/skipped/failed lives in the service method.
        channel = (existing.get("channel") or "").lower()
        if channel in _NOTIFIABLE_APPROVAL_CHANNELS:
            asyncio.create_task(
                _notify_access_request_approval(
                    agent_name=agent_name,
                    recipient_email=existing["email"],
                    channel=channel,
                )
            )

    # SEC-001: audit access request decision
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHORIZATION,
        event_action="access_request_approved" if decision.approve else "access_request_rejected",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        target_type="agent",
        target_id=agent_name,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"email": existing["email"], "access_request_id": request_id},
    )

    return AccessRequest(**updated)
