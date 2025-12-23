"""
Public endpoints for unauthenticated access (Phase 12.2: Public Agent Links).

These endpoints do NOT require authentication and are used by public users
to access agents via shareable links.
"""
import httpx
import logging
from fastapi import APIRouter, HTTPException, Request

from database import (
    db,
    PublicLinkInfo,
    VerificationRequest,
    VerificationConfirm,
    VerificationResponse,
    PublicChatRequest,
    PublicChatResponse
)
from services.docker_service import get_agent_container
from services.email_service import email_service
from services.audit_service import log_audit_event



logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public", tags=["public"])

# Rate limiting constants
MAX_VERIFICATION_REQUESTS_PER_EMAIL = 3  # per 10 minutes
MAX_CHAT_MESSAGES_PER_IP = 30  # per minute


def _get_client_ip(request: Request) -> str:
    """Get client IP address from request."""
    # Check for forwarded headers (behind proxy)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("/link/{token}", response_model=PublicLinkInfo)
async def get_public_link_info(token: str, request: Request):
    """
    Get information about a public link.

    Returns whether the link is valid and if email verification is required.
    Does NOT expose sensitive data like the link ID.
    """
    is_valid, reason, link = db.is_public_link_valid(token)

    if not is_valid:
        return PublicLinkInfo(
            valid=False,
            require_email=False,
            agent_available=False,
            reason=reason
        )

    # Check if agent is available
    container = get_agent_container(link["agent_name"])
    agent_available = container is not None and container.status == "running"

    return PublicLinkInfo(
        valid=True,
        require_email=link["require_email"],
        agent_available=agent_available,
        reason=None
    )


@router.post("/verify/request")
async def request_verification_code(
    verification: VerificationRequest,
    request: Request
):
    """
    Request an email verification code.

    Sends a 6-digit code to the provided email address.
    Rate limited to 3 requests per email per 10 minutes.
    """
    # Validate link token
    is_valid, reason, link = db.is_public_link_valid(verification.token)
    if not is_valid:
        raise HTTPException(status_code=404, detail=f"Invalid link: {reason}")

    if not link["require_email"]:
        raise HTTPException(
            status_code=400,
            detail="This link does not require email verification"
        )

    # Rate limiting
    recent_requests = db.count_recent_verification_requests(verification.email, minutes=10)
    if recent_requests >= MAX_VERIFICATION_REQUESTS_PER_EMAIL:
        await log_audit_event(
            event_type="public_verification",
            action="request_code",
            user_id="public",
            agent_name=link["agent_name"],
            ip_address=_get_client_ip(request),
            result="rate_limited",
            severity="warning",
            details={"email": verification.email, "requests_count": recent_requests}
        )
        raise HTTPException(
            status_code=429,
            detail="Too many verification requests. Please wait 10 minutes."
        )

    # Create verification code
    verification_data = db.create_verification(
        link_id=link["id"],
        email=verification.email,
        expiry_minutes=10
    )

    # Send email
    email_sent = await email_service.send_verification_code(
        verification.email,
        verification_data["code"]
    )

    if not email_sent:
        logger.error(f"Failed to send verification email to {verification.email}")
        # Don't expose email failure details to client
        raise HTTPException(
            status_code=500,
            detail="Failed to send verification code. Please try again."
        )

    await log_audit_event(
        event_type="public_verification",
        action="request_code",
        user_id="public",
        agent_name=link["agent_name"],
        ip_address=_get_client_ip(request),
        result="success",
        details={"email": verification.email}
    )

    return {
        "message": "Verification code sent",
        "expires_in_seconds": verification_data["expires_in_seconds"]
    }


@router.post("/verify/confirm", response_model=VerificationResponse)
async def confirm_verification_code(
    confirmation: VerificationConfirm,
    request: Request
):
    """
    Confirm an email verification code.

    Returns a session token if the code is valid.
    """
    # Validate link token
    is_valid, reason, link = db.is_public_link_valid(confirmation.token)
    if not is_valid:
        raise HTTPException(status_code=404, detail=f"Invalid link: {reason}")

    # Verify the code
    success, error, session_data = db.verify_code(
        link_id=link["id"],
        email=confirmation.email,
        code=confirmation.code,
        session_hours=24
    )

    if not success:
        await log_audit_event(
            event_type="public_verification",
            action="confirm_code",
            user_id="public",
            agent_name=link["agent_name"],
            ip_address=_get_client_ip(request),
            result="failed",
            details={"email": confirmation.email, "error": error}
        )
        return VerificationResponse(
            verified=False,
            error=error
        )

    await log_audit_event(
        event_type="public_verification",
        action="confirm_code",
        user_id="public",
        agent_name=link["agent_name"],
        ip_address=_get_client_ip(request),
        result="success",
        details={"email": confirmation.email}
    )

    return VerificationResponse(
        verified=True,
        session_token=session_data["session_token"],
        expires_at=session_data["expires_at"]
    )


@router.post("/chat/{token}", response_model=PublicChatResponse)
async def public_chat(
    token: str,
    chat_request: PublicChatRequest,
    request: Request
):
    """
    Send a chat message via a public link.

    Uses the parallel task execution endpoint (stateless, no conversation context).
    For links requiring email verification, a valid session_token must be provided.
    """
    client_ip = _get_client_ip(request)

    # Validate link token
    is_valid, reason, link = db.is_public_link_valid(token)
    if not is_valid:
        raise HTTPException(status_code=404, detail=f"Invalid link: {reason}")

    # Verify session if email required
    verified_email = None
    if link["require_email"]:
        if not chat_request.session_token:
            raise HTTPException(
                status_code=401,
                detail="Session token required for this link"
            )

        session_valid, email = db.validate_session(link["id"], chat_request.session_token)
        if not session_valid:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired session. Please verify your email again."
            )
        verified_email = email

    # Rate limiting by IP
    recent_messages = db.count_recent_messages_by_ip(client_ip, minutes=1)
    if recent_messages >= MAX_CHAT_MESSAGES_PER_IP:
        await log_audit_event(
            event_type="public_chat",
            action="message",
            user_id="public",
            agent_name=link["agent_name"],
            ip_address=client_ip,
            result="rate_limited",
            severity="warning",
            details={"email": verified_email}
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment."
        )

    # Check agent is available
    container = get_agent_container(link["agent_name"])
    if not container or container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not available. Please try again later."
        )

    agent_name = link["agent_name"]

    # Record usage BEFORE making the request
    db.record_public_link_usage(
        link_id=link["id"],
        email=verified_email,
        ip_address=client_ip
    )

    # Execute via parallel task endpoint (stateless)
    # Uses Docker network to reach agent container directly
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/task",
                json={
                    "message": chat_request.message,
                    "timeout_seconds": 120
                }
            )

            if response.status_code != 200:
                logger.error(f"Agent task failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to process your request. Please try again."
                )

            result = response.json()

            await log_audit_event(
                event_type="public_chat",
                action="message",
                user_id="public",
                agent_name=link["agent_name"],
                ip_address=client_ip,
                result="success",
                details={
                    "email": verified_email,
                    "link_id": link["id"],
                    "message_length": len(chat_request.message)
                }
            )

            return PublicChatResponse(
                response=result.get("response", result.get("result", "")),
                usage=result.get("usage")
            )

    except httpx.TimeoutException:
        logger.error(f"Agent request timed out for {link['agent_name']}")
        raise HTTPException(
            status_code=504,
            detail="Request timed out. Please try again with a simpler question."
        )
    except httpx.RequestError as e:
        logger.error(f"Agent request failed: {e}")
        raise HTTPException(
            status_code=502,
            detail="Failed to reach the agent. Please try again."
        )
