"""Public-link chat orchestration (#1028).

The 289-line ``public_chat`` handler was the largest function left in any
router — session identity, access gating, rate accounting, upload decoding,
memory injection, sync/async dispatch and persistence, all inside
``routers/public.py`` (Invariant #1 says routers hold no business logic).
It lives here now, in the ``chat_execution_service`` #1483 shape: the service
raises ``PublicChatError`` and the thin route maps it 1:1 to
``HTTPException``.

The split line is deliberate: client-IP extraction, the per-IP rate limit and
public-link token resolution stay in the router — they are HTTP concerns, and
the service receives the *resolved* ``link`` plus the already-derived
``client_ip`` so it never touches a ``Request``.

``agent_requires_email`` / ``agent_allows_open_access`` moved here with it
(they are db-reads, not HTTP), and the router re-imports them for its other
routes — one definition, not a copy.
"""
import asyncio
import logging
import secrets

from database import db
# PublicChatRequest stays router-side (it types the route body);
# the response model is shared through the same home the router uses.
from database import PublicChatResponse
from services.docker_service import get_agent_container
from services.platform_prompt_service import (
    build_public_channel_caller_prompt,
    format_user_memory_block,
    summarize_user_memory_background,
)
from services.task_execution_service import get_task_execution_service
from services.upload_service import (
    WEB_MAX_FILES,
    WEB_MAX_FILE_SIZE,
    WEB_MAX_IMAGE_SIZE,
    WEB_MAX_TOTAL_IMAGE_SIZE,
    decode_web_file,
    process_file_uploads,
)
# The chat-turn rate constants moved here with the accounting that reads them
# (per-IP and per-token message caps; the connection-level public-link rate
# limit stays in the router beside its Redis window).
MAX_CHAT_MESSAGES_PER_IP = 30  # per minute
MAX_CHAT_MESSAGES_PER_TOKEN = 60  # per minute, per public link token

logger = logging.getLogger(__name__)


class PublicChatError(Exception):
    """A refusal the route maps to HTTP, 1:1 (the #1483 / CanvasError shape)."""

    def __init__(self, status_code: int, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def agent_requires_email(agent_name: str) -> bool:
    """Agent-level email requirement (unified cross-channel policy, #311).

    Replaces the per-public-link require_email flag. Source of truth is
    `agent_ownership.require_email` — same policy applied by the channel
    message router for Slack/Telegram.
    """
    return bool(db.get_access_policy(agent_name).get("require_email"))


def agent_allows_open_access(agent_name: str) -> bool:
    """Agent-level open-access flag: any verified email may chat without approval."""
    return bool(db.get_access_policy(agent_name).get("open_access"))


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


async def run_public_chat(link: dict, chat_request, client_ip: str):
    """
    Send a chat message via a public link with conversation persistence.

    For links requiring email verification, a valid session_token must be provided.
    For anonymous links, a session_id can be provided to maintain conversation context.
    Returns session_id for anonymous links to store in localStorage.
    """

    # Determine session identifier and type
    session_identifier = None
    identifier_type = None
    verified_email = None

    agent_name = link["agent_name"]
    require_email = agent_requires_email(agent_name)

    if require_email:
        # Email-required: use verified email as identifier
        if not chat_request.session_token:
            raise PublicChatError(
                status_code=401,
                detail="Session token required for this link"
            )

        session_valid, email = db.validate_session(link["id"], chat_request.session_token)
        if not session_valid:
            raise PublicChatError(
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
        elif agent_allows_open_access(agent_name):
            pass
        else:
            try:
                db.upsert_access_request(agent_name, verified_email, "web")
            except Exception as e:
                logger.error(f"Failed to upsert access_request for {verified_email}: {e}")
            raise PublicChatError(
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
        raise PublicChatError(
            status_code=429,
            detail="Too many requests. Please wait a moment."
        )

    # Rate limiting by token (secondary) — caps total flood regardless of IP diversity
    recent_token_messages = db.count_recent_messages_by_token(link["id"], minutes=1)
    if recent_token_messages >= MAX_CHAT_MESSAGES_PER_TOKEN:
        raise PublicChatError(
            status_code=429,
            detail="Too many requests. Please wait a moment."
        )

    # Check agent is available
    container = get_agent_container(agent_name)
    if not container or container.status != "running":
        raise PublicChatError(
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
            raise PublicChatError(
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
            raise PublicChatError(
                status_code=429,
                detail="Agent is busy. Please try again later."
            )
        elif "timed out" in error:
            raise PublicChatError(
                status_code=504,
                detail="Request timed out. Please try again with a simpler question."
            )
        else:
            logger.error(f"Public chat task failed for {agent_name}: {error}")
            raise PublicChatError(
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
