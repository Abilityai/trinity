"""
Public endpoints for unauthenticated access (Phase 12.2: Public Agent Links).

These endpoints do NOT require authentication and are used by public users
to access agents via shareable links.
"""
import asyncio
import ipaddress
import json
import os
import secrets
import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from database import (
    db,
    PublicLinkInfo,
    VerificationRequest,
    VerificationConfirm,
    VerificationResponse,
    PublicChatRequest,
    PublicChatResponse,
    PublicChatMessage
)
from dependencies import get_current_user
from models import ClearSessionResponse, PublicChatHistoryResponse, User
from routers.auth import check_login_rate_limit, record_login_attempt, get_redis_client
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.email_service import email_service
from services.task_execution_service import get_task_execution_service
from services.platform_prompt_service import (
    build_public_channel_caller_prompt,
    format_user_memory_block,
    summarize_user_memory_background,
)
from services.upload_service import process_file_uploads, decode_web_file, WEB_MAX_FILES, WEB_MAX_FILE_SIZE, WEB_MAX_IMAGE_SIZE, WEB_MAX_TOTAL_IMAGE_SIZE


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public", tags=["public"])

# Rate limiting constants
MAX_VERIFICATION_REQUESTS_PER_EMAIL = 3  # per 10 minutes
MAX_CHAT_MESSAGES_PER_IP = 30  # per minute
MAX_CHAT_MESSAGES_PER_TOKEN = 60  # per minute, per public link token
PUBLIC_LINK_LOOKUP_RATE_LIMIT = 60  # max lookups per minute per IP (pentest 3.3.2)
PUBLIC_LINK_LOOKUP_RATE_WINDOW = 60  # 1 minute in seconds

# Generic error message for invalid tokens — prevents oracle-based enumeration (pentest 3.3.2)
INVALID_LINK_MESSAGE = "Invalid or expired link"

# Trusted proxy networks — only trust X-Forwarded-For / X-Real-IP from these.
# Default: RFC-1918 private ranges (covers Docker bridge networks).
# Override via TRUSTED_PROXIES env var (comma-separated CIDRs).
_trusted_proxy_networks: list | None = None


def _get_trusted_networks() -> list:
    """Parse TRUSTED_PROXIES env var into network objects (cached)."""
    global _trusted_proxy_networks
    if _trusted_proxy_networks is not None:
        return _trusted_proxy_networks

    raw = os.environ.get("TRUSTED_PROXIES", "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,127.0.0.0/8")
    networks = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if cidr:
            try:
                networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                logger.warning(f"Invalid TRUSTED_PROXIES entry ignored: {cidr}")
    _trusted_proxy_networks = networks
    return networks


def _is_trusted_proxy(ip_str: str) -> bool:
    """Check if an IP belongs to a trusted proxy network."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _get_trusted_networks())
    except ValueError:
        return False


def _get_client_ip(request: Request) -> str:
    """Get client IP for rate limiting.

    Only trusts proxy headers (X-Real-IP, X-Forwarded-For) when the direct
    TCP peer is a known trusted proxy (e.g. the nginx container in Docker).
    This prevents attackers from bypassing rate limits by spoofing headers.

    See: pentest finding 3.2.4 / GitHub issue #181.
    """
    direct_ip = request.client.host if request.client else "unknown"

    if direct_ip == "unknown":
        return direct_ip

    # Only read proxy headers when the connection comes from a trusted proxy
    if not _is_trusted_proxy(direct_ip):
        return direct_ip

    # X-Real-IP is set (and overwritten) by our nginx — preferred source
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fallback: rightmost non-trusted IP in X-Forwarded-For
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        for ip in reversed([ip.strip() for ip in forwarded_for.split(",")]):
            if not _is_trusted_proxy(ip):
                return ip

    return direct_ip


def check_public_link_rate_limit(client_ip: str) -> None:
    """
    Rate limit public link lookups per IP.
    Shared counter across all public token endpoints.

    Security fix for pentest finding 3.3.2: prevents automated scanning
    of public link tokens. Defense-in-depth alongside 192-bit token entropy.

    Fails open if Redis is unavailable (logs warning).
    """
    r = get_redis_client()
    if r is None:
        logger.warning("Public link rate limiting unavailable - Redis not connected")
        return

    key = f"public_link_lookups:{client_ip}"
    try:
        attempts = r.get(key)
        if attempts and int(attempts) >= PUBLIC_LINK_LOOKUP_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later."
            )
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, PUBLIC_LINK_LOOKUP_RATE_WINDOW)
        pipe.execute()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Public link rate limit check failed: {e}")


def _validate_public_link(token: str) -> dict:
    """
    Validate a public link token and return link data.
    Raises a generic 404 for all failure modes to prevent oracle-based enumeration.

    Security fix for pentest finding 3.3.2: normalizes error responses so
    valid/invalid/disabled/expired tokens all return the same error.
    """
    is_valid, reason, link = db.is_public_link_valid(token)
    if not is_valid:
        raise HTTPException(status_code=404, detail=INVALID_LINK_MESSAGE)
    return link


def _agent_requires_email(agent_name: str) -> bool:
    """Agent-level email requirement (unified cross-channel policy, #311).

    Replaces the per-public-link require_email flag. Source of truth is
    `agent_ownership.require_email` — same policy applied by the channel
    message router for Slack/Telegram.
    """
    return bool(db.get_access_policy(agent_name).get("require_email"))


def _agent_allows_open_access(agent_name: str) -> bool:
    """Agent-level open-access flag: any verified email may chat without approval."""
    return bool(db.get_access_policy(agent_name).get("open_access"))


@router.get("/link/{token}", response_model=PublicLinkInfo)
async def get_public_link_info(token: str, request: Request):
    """
    Get information about a public link.

    Returns whether the link is valid and if email verification is required.
    Also includes agent metadata (name, description, status flags).
    Does NOT expose sensitive data like the link ID.

    Security (pentest 3.3.2): rate limited per IP, normalized error responses.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)

    is_valid, reason, link = db.is_public_link_valid(token)

    if not is_valid:
        # Normalized response — same shape for all failure modes (pentest 3.3.2)
        return PublicLinkInfo(
            valid=False,
            require_email=False,
            agent_available=False,
            reason="invalid_or_expired"
        )

    agent_name = link["agent_name"]

    # Check if agent is available
    container = get_agent_container(agent_name)
    agent_available = container is not None and container.status == "running"

    # Get agent metadata from database
    is_autonomous = db.get_autonomy_enabled(agent_name)
    read_only_data = db.get_read_only_mode(agent_name)
    is_read_only = read_only_data.get("enabled", False)

    # Get display name and description from template.yaml (if agent running)
    agent_display_name = agent_name  # Fallback to agent name
    agent_description = None

    if agent_available:
        try:
            async with agent_httpx_client(agent_name, timeout=5.0) as client:
                response = await client.get(f"http://agent-{agent_name}:8000/api/template/info")
                if response.status_code == 200:
                    info = response.json()
                    agent_display_name = info.get("name") or info.get("display_name") or agent_name
                    agent_description = info.get("description")
        except Exception as e:
            logger.warning(f"Failed to fetch template info for {agent_name}: {e}")
            # Use container labels as fallback
            if container:
                labels = container.labels or {}
                agent_display_name = labels.get("trinity.agent-type", agent_name)

    return PublicLinkInfo(
        valid=True,
        require_email=_agent_requires_email(agent_name),
        agent_available=agent_available,
        reason=None,
        agent_display_name=agent_display_name,
        agent_description=agent_description,
        is_autonomous=is_autonomous,
        is_read_only=is_read_only
    )


@router.get("/playbooks/{token}")
async def get_public_playbooks(token: str, request: Request):
    """
    Get available skills (playbooks) for a public link's agent.

    Proxies the request to the agent's internal skills endpoint.
    No authentication required — the link token is validated instead.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    agent_name = link["agent_name"]

    # Check if agent is available
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    try:
        agent_url = f"http://agent-{agent_name}:8000/api/skills"
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            response = await client.get(agent_url)
            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Agent returned error: {response.text}"
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent is starting up, please try again")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Could not connect to agent")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch playbooks: {str(e)}")


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
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(verification.token)

    if not _agent_requires_email(link["agent_name"]):
        raise HTTPException(
            status_code=400,
            detail="This link does not require email verification"
        )

    # Rate limiting
    recent_requests = db.count_recent_verification_requests(verification.email, minutes=10)
    if recent_requests >= MAX_VERIFICATION_REQUESTS_PER_EMAIL:
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
        verification_data["code"],
        agent_name=link["agent_name"],
    )

    if not email_sent:
        logger.error(f"Failed to send verification email to {verification.email}")
        # Don't expose email failure details to client
        raise HTTPException(
            status_code=500,
            detail="Failed to send verification code. Please try again."
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
    Rate limited: 5 attempts per 10 minutes per IP (pentest 3.1.5).
    """
    # Check IP-based rate limit (pentest 3.1.5)
    client_ip = _get_client_ip(request)
    check_login_rate_limit(client_ip)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(confirmation.token)

    # Verify the code
    success, error, session_data = db.verify_code(
        link_id=link["id"],
        email=confirmation.email,
        code=confirmation.code,
        session_hours=24
    )

    if not success:
        record_login_attempt(client_ip, success=False)
        return VerificationResponse(
            verified=False,
            error=error
        )

    record_login_attempt(client_ip, success=True)
    return VerificationResponse(
        verified=True,
        session_token=session_data["session_token"],
        expires_at=session_data["expires_at"]
    )


@router.post("/chat/{token}")
async def public_chat(
    token: str,
    chat_request: PublicChatRequest,
    request: Request
):
    """
    Send a chat message via a public link with conversation persistence.

    For links requiring email verification, a valid session_token must be provided.
    For anonymous links, a session_id can be provided to maintain conversation context.
    Returns session_id for anonymous links to store in localStorage.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    # Determine session identifier and type
    session_identifier = None
    identifier_type = None
    verified_email = None

    agent_name = link["agent_name"]
    require_email = _agent_requires_email(agent_name)

    if require_email:
        # Email-required: use verified email as identifier
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
        # Defensive normalization (#446): ensure gate compares lowercased emails
        # even if the stored session email contained unexpected casing/whitespace.
        verified_email = (email or "").strip().lower()
        session_identifier = verified_email
        identifier_type = "email"

        # Unified cross-channel access gate (#311) — same logic as
        # adapters.message_router for Slack/Telegram. Owner/admin/shared
        # always pass; otherwise honor open_access or queue an access request.
        if db.email_has_agent_access(agent_name, verified_email):
            pass
        elif _agent_allows_open_access(agent_name):
            pass
        else:
            try:
                db.upsert_access_request(agent_name, verified_email, "web")
            except Exception as e:
                logger.error(f"Failed to upsert access_request for {verified_email}: {e}")
            raise HTTPException(
                status_code=403,
                detail="Your access request is pending approval. You'll be notified once the agent owner responds."
            )
    else:
        # Anonymous: use provided session_id or generate new one
        if chat_request.session_id:
            session_identifier = chat_request.session_id
        else:
            session_identifier = secrets.token_urlsafe(16)
        identifier_type = "anonymous"

    # Rate limiting by IP (primary) — pentest 3.2.4: uses real TCP peer, not spoofable header
    recent_messages = db.count_recent_messages_by_ip(client_ip, minutes=1)
    if recent_messages >= MAX_CHAT_MESSAGES_PER_IP:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment."
        )

    # Rate limiting by token (secondary) — caps total flood regardless of IP diversity
    recent_token_messages = db.count_recent_messages_by_token(link["id"], minutes=1)
    if recent_token_messages >= MAX_CHAT_MESSAGES_PER_TOKEN:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a moment."
        )

    # Check agent is available
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not available. Please try again later."
        )

    # (#364) File upload processing for public chat.
    # Rate-limited by existing IP check above. Files must be processed
    # synchronously before the async/sync fork so bytes are in the container.
    _pub_image_data: list = []
    _pub_file_descs: list = []
    if chat_request.files:
        uploader = verified_email or f"anonymous ({client_ip})"
        raw_files = [
            {
                "name": f.name,
                "mimetype": f.mimetype,
                "size": f.size,
                "data": decode_web_file(f.dict()),
                "id": f"f{i}",
            }
            for i, f in enumerate(chat_request.files)
        ]
        file_descs, _, all_writes_failed, _pub_image_data = await process_file_uploads(
            raw_files=raw_files,
            agent_name=agent_name,
            container=container,
            session_id=session_identifier,
            uploader=uploader,
            source="public",
            max_files=WEB_MAX_FILES,
            max_file_size=WEB_MAX_FILE_SIZE,
            max_image_size=WEB_MAX_IMAGE_SIZE,
            max_total_image_size=WEB_MAX_TOTAL_IMAGE_SIZE,
        )
        if all_writes_failed:
            raise HTTPException(
                status_code=502,
                detail="File upload failed: could not write to agent workspace."
            )
        _pub_file_descs = file_descs

    # Get or create chat session
    chat_session = db.get_or_create_public_chat_session(
        link_id=link["id"],
        session_identifier=session_identifier,
        identifier_type=identifier_type
    )

    # Build context from prior history before storing the new user message.
    # Must happen first — storing the user message then reading it back would
    # include the current message in both "Previous conversation:" and
    # "Current message:", sending it to the agent twice on every turn.
    context_prompt = db.build_public_chat_context(
        session_id=chat_session.id,
        new_message=chat_request.message,
        max_turns=10
    )
    if _pub_file_descs:
        context_prompt = f"{context_prompt}\n\n" + "\n".join(_pub_file_descs)

    # Store user message (after context is built so it doesn't appear twice).
    # #903: stamp the verified email as the message sender so the shared
    # sender-filtered MEM-001 summarizer (which keys on the user's own turns)
    # works on the web path identically to channels. None for anonymous
    # sessions, which never summarize.
    db.add_public_chat_message(
        session_id=chat_session.id,
        role="user",
        content=chat_request.message,
        sender_email=verified_email,
    )

    # Record usage
    db.record_public_link_usage(
        link_id=link["id"],
        email=verified_email,
        ip_address=client_ip
    )

    # MEM-001 (#895): Fetch per-user memory for email-verified sessions and inject
    # into the system prompt. The record carries two independently-written sections
    # (agent_notes + conversation_summary); format_user_memory_block renders both
    # when present and returns None when both are empty.
    memory_system_prompt = None
    if identifier_type == "email" and verified_email:
        user_memory = db.get_or_create_public_user_memory(agent_name, verified_email)
        memory_system_prompt = format_user_memory_block(user_memory)

    # EXEC-024: Execute via TaskExecutionService (unified execution path)
    # Public executions now get full tracking: execution records, activity stream,
    # slot management, credential sanitization, and Dashboard timeline visibility.
    source_email = verified_email or f"anonymous ({client_ip})"
    task_execution_service = get_task_execution_service()

    # Async mode (THINK-001): return execution_id immediately for SSE streaming
    if chat_request.async_mode:
        # Create execution record early so we have an ID
        execution = db.create_task_execution(
            agent_name=agent_name,
            message=context_prompt,
            triggered_by="public",
            source_user_email=source_email,
        )
        execution_id = execution.id if execution else None

        # Spawn background task
        asyncio.create_task(_execute_public_chat_background(
            agent_name=agent_name,
            context_prompt=context_prompt,
            source_email=source_email,
            execution_id=execution_id,
            chat_session_id=chat_session.id,
            session_identifier=session_identifier,
            identifier_type=identifier_type,
            verified_email=verified_email,
            memory_system_prompt=memory_system_prompt,
            images=_pub_image_data,
        ))

        return {
            "status": "accepted",
            "execution_id": execution_id,
            "agent_name": agent_name,
            "session_id": session_identifier if identifier_type == "anonymous" else None,
            "async_mode": True,
        }

    # Sync mode: wait for result
    result = await task_execution_service.execute_task(
        agent_name=agent_name,
        message=context_prompt,
        triggered_by="public",
        source_user_email=source_email,
        timeout_seconds=900,
        # #894: per-agent public-channel model override (None → platform default).
        model=db.get_public_channel_model(agent_name),
        # #1205: per-agent public/channel custom-instructions fragment.
        system_prompt=build_public_channel_caller_prompt(
            agent_name, memory_system_prompt
        ),
        images=_pub_image_data,
    )

    if result.status in ("failed", "cancelled"):
        # #679: a CANCELLED turn is non-delivery, not a success-like empty
        # response. It falls through to the generic 502 below (its error text
        # matches no capacity/timeout branch) — the operator stopped the work.
        error = result.error or ""
        if "at capacity" in error:
            raise HTTPException(
                status_code=429,
                detail="Agent is busy. Please try again later."
            )
        elif "timed out" in error:
            raise HTTPException(
                status_code=504,
                detail="Request timed out. Please try again with a simpler question."
            )
        else:
            logger.error(f"Public chat task failed for {agent_name}: {error}")
            raise HTTPException(
                status_code=502,
                detail="Failed to process your request. Please try again."
            )

    assistant_response = result.response

    # Store assistant response in public chat messages.
    # #903: a public-link session is always single-participant, so stamp the
    # assistant turn with the same verified email as the user turn. The
    # sender-filtered MEM-001 summarizer then keeps the assistant's replies in
    # this user's summary (they were included pre-#903) while the shared
    # multi-participant Slack thread — where the assistant turn stays null —
    # is the only place the filter drops assistant context.
    db.add_public_chat_message(
        session_id=chat_session.id,
        role="assistant",
        content=assistant_response,
        cost=result.cost,
        sender_email=verified_email,
    )

    # MEM-001: Increment message count and trigger background summarization every 5 messages
    if identifier_type == "email" and verified_email:
        new_count = db.increment_public_user_memory_count(agent_name, verified_email)
        if new_count % 5 == 0:
            asyncio.create_task(summarize_user_memory_background(
                agent_name=agent_name,
                user_email=verified_email,
                session_id=chat_session.id,
            ))

    # Get updated message count
    updated_session = db.get_public_chat_session(chat_session.id)
    message_count = updated_session.message_count if updated_session else 0

    return PublicChatResponse(
        response=assistant_response,
        session_id=session_identifier if identifier_type == "anonymous" else None,
        message_count=message_count,
        usage=None  # Usage details are tracked in the execution record
    )


# Introduction prompt - asks agent to introduce itself
INTRO_PROMPT = """Provide a brief 2-paragraph introduction of yourself.

First paragraph: Who you are and what you do.
Second paragraph: Your purpose and how you can help the user.

Be concise, welcoming, and conversational. Do not use headers, bullet points, or markdown formatting."""


@router.get("/intro/{token}")
async def get_agent_intro(
    token: str,
    request: Request,
    session_token: str = None
):
    """
    Get an introduction message from the agent.

    Sends a prompt asking the agent to introduce itself.
    Used to provide context to users before they start chatting.
    For links requiring email verification, a valid session_token query param must be provided.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    # Verify session if email required
    if _agent_requires_email(link["agent_name"]):
        if not session_token:
            raise HTTPException(
                status_code=401,
                detail="Session token required for this link"
            )

        session_valid, email = db.validate_session(link["id"], session_token)
        if not session_valid:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired session. Please verify your email again."
            )

    # Check agent is available
    container = get_agent_container(link["agent_name"])
    if not container or container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not available. Please try again later."
        )

    agent_name = link["agent_name"]

    # Execute intro prompt via parallel task endpoint
    try:
        async with agent_httpx_client(agent_name, timeout=120.0) as client:
            response = await client.post(
                f"http://agent-{agent_name}:8000/api/task",
                json={
                    "message": INTRO_PROMPT,
                    "timeout_seconds": 60
                }
            )

            if response.status_code != 200:
                logger.error(f"Agent intro failed: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=502,
                    detail="Failed to get introduction. Please try again."
                )

            result = response.json()

            return {
                "intro": result.get("response", result.get("result", ""))
            }

    except httpx.TimeoutException:
        logger.error(f"Agent intro request timed out for {link['agent_name']}")
        raise HTTPException(
            status_code=504,
            detail="Request timed out. Please try again."
        )
    except httpx.RequestError as e:
        logger.error(f"Agent intro request failed: {e}")
        raise HTTPException(
            status_code=502,
            detail="Failed to reach the agent. Please try again."
        )


@router.get("/history/{token}", response_model=PublicChatHistoryResponse)
async def get_public_chat_history(
    token: str,
    request: Request,
    session_token: str = None,
    session_id: str = None
):
    """
    Get chat history for a public link session.

    For email-required links, provide session_token query param.
    For anonymous links, provide session_id query param.
    Returns messages array for current session, or empty if no history.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    require_email = _agent_requires_email(link["agent_name"])

    # Determine session identifier
    session_identifier = None

    if require_email:
        if not session_token:
            raise HTTPException(
                status_code=401,
                detail="Session token required for this link"
            )

        session_valid, email = db.validate_session(link["id"], session_token)
        if not session_valid:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired session. Please verify your email again."
            )
        session_identifier = email.lower()
    else:
        if not session_id:
            # No session_id means no history yet
            return PublicChatHistoryResponse(
                messages=[],
                session_id="",
                message_count=0
            )
        session_identifier = session_id

    # Look up session
    chat_session = db.get_public_chat_session_by_identifier(
        link_id=link["id"],
        session_identifier=session_identifier
    )

    if not chat_session:
        return PublicChatHistoryResponse(
            messages=[],
            session_id=session_identifier if not require_email else "",
            message_count=0
        )

    # Get messages (oldest first for display)
    messages = db.get_recent_public_chat_messages(chat_session.id, limit=100)

    return PublicChatHistoryResponse(
        messages=[
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat()
            }
            for msg in messages
        ],
        session_id=session_identifier if not require_email else "",
        message_count=chat_session.message_count
    )


@router.delete("/session/{token}", response_model=ClearSessionResponse)
async def clear_public_session(
    token: str,
    request: Request,
    session_token: str = None,
    session_id: str = None
):
    """
    Clear a public chat session (start new conversation).

    For email-required links, provide session_token query param.
    For anonymous links, provide session_id query param.
    Returns new_session_id for anonymous links to update localStorage.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    require_email = _agent_requires_email(link["agent_name"])

    # Determine session identifier
    session_identifier = None

    if require_email:
        if not session_token:
            raise HTTPException(
                status_code=401,
                detail="Session token required for this link"
            )

        session_valid, email = db.validate_session(link["id"], session_token)
        if not session_valid:
            raise HTTPException(
                status_code=401,
                detail="Invalid or expired session. Please verify your email again."
            )
        session_identifier = email.lower()
    else:
        if not session_id:
            raise HTTPException(
                status_code=400,
                detail="session_id required for anonymous links"
            )
        session_identifier = session_id

    # Look up and delete session
    chat_session = db.get_public_chat_session_by_identifier(
        link_id=link["id"],
        session_identifier=session_identifier
    )

    if chat_session:
        db.clear_public_chat_session(chat_session.id)

    # For anonymous sessions, generate new session_id
    new_session_id = None
    if not require_email:
        new_session_id = secrets.token_urlsafe(16)

    return ClearSessionResponse(
        cleared=True,
        new_session_id=new_session_id
    )


# ============================================================================
# Async Public Chat Support (THINK-001 for Public Links)
# ============================================================================

async def _execute_public_chat_background(
    agent_name: str,
    context_prompt: str,
    source_email: str,
    execution_id: str,
    chat_session_id: str,
    session_identifier: str,
    identifier_type: str,
    verified_email: str = None,
    memory_system_prompt: str = None,
    images: list = None,
):
    """
    Background task for async public chat execution.

    Runs the task via TaskExecutionService (which handles slot management,
    activity tracking, and credential sanitization) and stores the assistant
    response in the public chat session.
    """
    try:
        task_execution_service = get_task_execution_service()
        result = await task_execution_service.execute_task(
            agent_name=agent_name,
            message=context_prompt,
            triggered_by="public",
            source_user_email=source_email,
            timeout_seconds=900,
            execution_id=execution_id,
            # #894: per-agent public-channel model override (None → platform default).
            model=db.get_public_channel_model(agent_name),
            # #1205: per-agent public/channel custom-instructions fragment.
            system_prompt=build_public_channel_caller_prompt(
                agent_name, memory_system_prompt
            ),
            images=images or [],
        )

        if result.status == "success" and result.response:
            # #903: single-participant web session — stamp the assistant turn
            # with the verified email (mirror the sync path) so the
            # sender-filtered summarizer keeps assistant replies in this user's
            # memory.
            db.add_public_chat_message(
                session_id=chat_session_id,
                role="assistant",
                content=result.response,
                cost=result.cost,
                sender_email=verified_email,
            )

            # MEM-001: Increment message count and trigger background summarization every 5 messages
            if identifier_type == "email" and verified_email:
                new_count = db.increment_public_user_memory_count(agent_name, verified_email)
                if new_count % 5 == 0:
                    asyncio.create_task(summarize_user_memory_background(
                        agent_name=agent_name,
                        user_email=verified_email,
                        session_id=chat_session_id,
                    ))
        elif result.status in ("failed", "cancelled"):
            # #679: non-delivery — only a SUCCESS turn with a response is posted
            # to the public session above; a cancelled turn writes nothing.
            logger.info(f"[PublicChatAsync] Task {result.status} for {agent_name}: {result.error}")
    except Exception as e:
        logger.error(f"[PublicChatAsync] Background execution error for {agent_name}: {e}")


@router.get("/executions/{token}/{execution_id}/stream")
async def public_stream_execution(
    token: str,
    execution_id: str,
    request: Request,
):
    """
    Stream execution log entries via SSE for a public chat execution.

    Validates the public link token instead of JWT authentication.
    Proxies the SSE stream from the agent container to the frontend.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    agent_name = link["agent_name"]
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # Verify the execution belongs to this agent
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != agent_name:
        raise HTTPException(status_code=404, detail="Execution not found")

    async def proxy_stream():
        """Proxy SSE stream from agent container."""
        agent_url = f"http://agent-{agent_name}:8000/api/executions/{execution_id}/stream"
        try:
            async with agent_httpx_client(agent_name, timeout=None) as client:
                async with client.stream("GET", agent_url) as response:
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Agent returned {response.status_code}'})}\n\n"
                        yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                        return

                    async for chunk in response.aiter_text():
                        yield chunk
        except httpx.ConnectError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to connect to agent'})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
        except Exception as e:
            logger.error(f"[PublicStream] Error streaming from agent {agent_name}: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

    return StreamingResponse(
        proxy_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/executions/{token}/{execution_id}/status")
async def public_execution_status(
    token: str,
    execution_id: str,
    request: Request,
):
    """
    Get the status of a public chat execution.

    Used by the frontend to poll for completion after async submission.
    Validates the public link token instead of JWT authentication.
    """
    client_ip = _get_client_ip(request)
    check_public_link_rate_limit(client_ip)
    link = _validate_public_link(token)

    agent_name = link["agent_name"]

    # Verify the execution belongs to this agent
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != agent_name:
        raise HTTPException(status_code=404, detail="Execution not found")

    return {
        "execution_id": execution.id,
        "status": execution.status,
        # #679 (F2): a CANCELLED execution carries a real status + a
        # "cancelled by user" error; surface both like failed/success so the
        # public poller gets a reason instead of a silent null body.
        "response": execution.response if execution.status in ("success", "failed", "cancelled") else None,
        "error": execution.error if execution.status in ("failed", "cancelled") else None,
    }


@router.get("/sessions/{token}")
async def get_public_link_sessions(
    token: str,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """
    List the authenticated user's chat sessions for the agent behind this public link.

    Requires JWT. Returns the caller's own sessions ordered most-recent first,
    capped at `limit` (default 20). Does not require agent sharing — the public
    link token acts as the access credential for this read-only history view.
    """
    link = _validate_public_link(token)
    agent_name = link["agent_name"]

    sessions = db.get_agent_chat_sessions(
        agent_name=agent_name,
        user_id=current_user.id,
    )
    page = sessions[:limit]

    result = []
    for s in page:
        entry = s.model_dump()
        # Attach a preview from the most recent message in the session
        recent = db.get_chat_messages(s.id, limit=1)
        entry["preview"] = recent[0].content[:120] if recent else None
        result.append(entry)

    return {
        "session_count": len(result),
        "sessions": result,
    }


@router.get("/sessions/{token}/{session_id}")
async def get_public_link_session_detail(
    token: str,
    session_id: str,
    limit: int = 100,
    current_user: User = Depends(get_current_user)
):
    """
    Get messages for a specific chat session via a public link token.

    Requires JWT. The session must belong to the authenticated user and to
    the agent referenced by the public link token.
    """
    link = _validate_public_link(token)
    agent_name = link["agent_name"]

    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.agent_name != agent_name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    if session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    messages = db.get_chat_messages(session_id, limit=limit)

    return {
        "session": session.model_dump(),
        "message_count": len(messages),
        "messages": [m.model_dump() for m in messages],
    }
