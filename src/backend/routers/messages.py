"""
Proactive Messaging Router (Issue #321).

Enables agents to send proactive messages to users across channels.
Authorization via allow_proactive flag on agent_sharing.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from models import (
    ProactiveShareUpdate,
    ProactiveSharesResponse,
    SendMessageRequest,
    SendMessageResponse,
)

from database import db
from dependencies import get_current_user, AuthorizedAgentByName, assert_agent_owner
from db_models import User
from services import role_addressing
from services.idempotency_service import EffectInProgressError, EffectUnguardedError
from services.proactive_message_service import (
    proactive_message_service,
    NotAuthorizedError,
    RecipientNotFoundError,
    RateLimitedError,
    ChannelDeliveryError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["messages"])


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/{agent_name}/messages", response_model=SendMessageResponse)
async def send_proactive_message(
    request: SendMessageRequest,
    agent_name: AuthorizedAgentByName,
    current_user: User = Depends(get_current_user),
):
    """
    Send a proactive message to a user from this agent.

    The recipient must:
    1. Be in agent_sharing for this agent with allow_proactive=1, OR
    2. Be the owner of the agent

    Rate limited to 10 messages per recipient per hour.
    """

    recipient = _recipient(agent_name, request, current_user)
    try:
        result = await proactive_message_service.send_message(
            agent_name=agent_name,
            recipient_email=recipient,
            text=request.text,
            channel=request.channel,
            reply_to_thread=request.reply_to_thread,
            execution_id=request.execution_id,
            dedup_label=request.dedup_label,
        )

        return SendMessageResponse(
            success=result.success,
            channel=result.channel,
            message_id=result.message_id,
            error=result.error,
        )

    except EffectUnguardedError as e:
        # #2392: pull-mode agent, no usable execution id — refused, not retryable.
        raise HTTPException(status_code=422, detail={"reason": "effect_unguarded", "message": str(e)})

    except EffectInProgressError as e:
        # A concurrent duplicate send for the same (execution, recipient, channel)
        # is mid-flight (#1084). Retryable — never a silent skip-and-succeed.
        raise HTTPException(status_code=409, detail=str(e))

    except NotAuthorizedError as e:
        raise HTTPException(status_code=403, detail=_role_only(
            request, "role_not_opted_in",
            "The person in the {role} role has not opted in to proactive messages "
            "from this agent.") or str(e))

    except RateLimitedError as e:
        raise HTTPException(status_code=429, detail=str(e))

    except RecipientNotFoundError as e:
        raise HTTPException(status_code=404, detail=_role_only(
            request, "role_no_channel",
            "No delivery channel reaches the person in the {role} role.") or str(e))

    except ChannelDeliveryError as e:
        raise HTTPException(status_code=502, detail=str(e))

    except Exception as e:
        logger.exception(f"Proactive message failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error sending message")


def _role_only(request: SendMessageRequest, code: str, message: str):
    """A role-addressed delivery failure, described by the role alone (ent#606).

    The service's own error text names the resolved person's email — fine when
    the agent supplied that email itself, a disclosure when the agent only named
    a role. Returns None for email-addressed sends so their detail is unchanged.
    """
    if not request.to:
        return None
    return {"code": code, "role": request.to, "message": message.format(role=request.to)}


def _recipient(agent_name: str, request: SendMessageRequest, current_user: User = None) -> str:
    """The one person this message goes to (ent#606).

    `to` names a role, resolved by the same rule asks and reports use
    (`services/role_addressing`). A role nobody fills, or that several people
    fill, is refused by name — a message has one recipient, and silently picking
    one of several is exactly what the ruling forbids. `recipient_email` still
    works for two releases and is logged as deprecated.
    """
    if not request.to:
        # Same condition as reports: only an agent key choosing a person by
        # email is the migration the log tracks; a human sender is not.
        if current_user is not None and current_user.agent_name:
            role_addressing.log_email_addressing(agent_name, "message")
        return request.recipient_email
    try:
        r = role_addressing.resolve(agent_name, request.to)
    except role_addressing.RoleRefused as e:
        raise HTTPException(status_code=422, detail={
            "code": e.code, "role": e.role, "message": e.message + " Message primary instead."})
    if len(r.people) > 1:
        raise HTTPException(status_code=422, detail={
            "code": "role_resolves_to_several", "role": request.to, "count": len(r.people),
            "message": f"{len(r.people)} people fill the {request.to} role and a message has "
                       "one recipient — message primary, or raise an ask."})
    if not r.single:
        raise HTTPException(status_code=422, detail={
            "code": "role_unassigned", "role": request.to,
            "message": f"Nobody fills the {request.to} role for this agent yet."})
    return r.single


@router.put("/{agent_name}/shares/proactive", response_model=dict)
async def update_proactive_setting(
    agent_name: str,
    request: ProactiveShareUpdate,
    current_user: User = Depends(get_current_user),
):
    """
    Update the allow_proactive flag for a sharing record.

    Only the agent owner or admin can modify this setting.
    """
    success = db.set_allow_proactive(
        agent_name=agent_name,
        email=request.email,
        allow=request.allow_proactive,
        setter_username=current_user.username,
    )

    if not success:
        raise HTTPException(
            status_code=404,
            detail="Share not found or not authorized to modify"
        )

    return {
        "success": True,
        "agent_name": agent_name,
        "email": request.email,
        "allow_proactive": request.allow_proactive,
    }


@router.get("/{agent_name}/shares/proactive", response_model=ProactiveSharesResponse)
async def get_proactive_shares(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """
    List all emails that have opted in to receive proactive messages from this agent.

    Only the agent owner or admin can view this list.
    """
    assert_agent_owner(current_user, agent_name, detail="Not authorized to view shares")

    emails = db.get_proactive_enabled_shares(agent_name)

    return ProactiveSharesResponse(
        agent_name=agent_name,
        emails=emails,
    )
