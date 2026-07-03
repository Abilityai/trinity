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
from dependencies import get_current_user, AuthorizedAgentByName
from db_models import User
from services.idempotency_service import EffectInProgressError
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
):
    """
    Send a proactive message to a user from this agent.

    The recipient must:
    1. Be in agent_sharing for this agent with allow_proactive=1, OR
    2. Be the owner of the agent

    Rate limited to 10 messages per recipient per hour.
    """

    try:
        result = await proactive_message_service.send_message(
            agent_name=agent_name,
            recipient_email=request.recipient_email,
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

    except EffectInProgressError as e:
        # A concurrent duplicate send for the same (execution, recipient, channel)
        # is mid-flight (#1084). Retryable — never a silent skip-and-succeed.
        raise HTTPException(status_code=409, detail=str(e))

    except NotAuthorizedError as e:
        raise HTTPException(status_code=403, detail=str(e))

    except RateLimitedError as e:
        raise HTTPException(status_code=429, detail=str(e))

    except RecipientNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    except ChannelDeliveryError as e:
        raise HTTPException(status_code=502, detail=str(e))

    except Exception as e:
        logger.exception(f"Proactive message failed: {e}")
        raise HTTPException(status_code=500, detail="Internal error sending message")


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
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Not authorized to view shares")

    emails = db.get_proactive_enabled_shares(agent_name)

    return ProactiveSharesResponse(
        agent_name=agent_name,
        emails=emails,
    )
