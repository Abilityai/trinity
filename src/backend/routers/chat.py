"""
Agent chat and activity routes for the Trinity backend.

Includes execution queue integration to prevent parallel execution on the same agent.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import json
import logging
import asyncio
import uuid
from datetime import datetime
from typing import NamedTuple, NoReturn, Optional

from models import User, ChatMessageRequest, ModelChangeRequest, ParallelTaskRequest, ActivityType, ActivityState, TaskExecutionStatus, ExecutionSource
from dependencies import get_current_user, get_authorized_agent, get_owned_agent
from services.agent_call_limiter import BackendAgentCallBudgetExhausted
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.activity_service import activity_service
from services.upload_service import process_file_uploads, decode_web_file, WEB_MAX_FILES, WEB_MAX_FILE_SIZE, WEB_MAX_IMAGE_SIZE, WEB_MAX_TOTAL_IMAGE_SIZE
from services.capacity_manager import (
    CapacityFull,
    CircuitOpen,
    PersistentTaskPayload,
    get_capacity_manager,
)
from services import idempotency_service
from services.task_execution_service import (
    _compute_context_used,
    dispatch_breaker_active,
    get_task_execution_service,
    agent_post_with_retry,
)
from database import db
from utils.credential_sanitizer import sanitize_dict, sanitize_execution_log, sanitize_response
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    get_platform_system_prompt,
    is_execution_context_enabled,
)
from utils.helpers import utc_now_iso
from services.platform_audit_service import platform_audit_service, AuditEventType

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/agents", tags=["chat"])

# WebSocket manager (injected from main.py)
_websocket_manager = None


# Sync HTTP long-poll primitives live in services/sync_waiter.py so they're
# importable from tests without pulling in the full router/auth chain.
# (Issue #498)
from services.sync_waiter import signal_sync_waiter, wait_for_sync_terminal
from routers.auth import get_redis_client


# #1068 (demotion PR 1): durable per-day usage counter for the deprecated
# per-task timeout override. Survives log rotation, so the deletion soak gate
# becomes a cheap `redis-cli HGETALL deprecation:task_timeout_seconds` ("any
# hits in the last N days?") instead of a best-effort log scrape. Read it
# before opening the field-deletion PR.
_DEPRECATION_TIMEOUT_HKEY = "deprecation:task_timeout_seconds"
_DEPRECATION_HKEY_TTL_S = 60 * 60 * 24 * 180  # self-clean ~6mo after traffic stops


def _record_deprecated_task_timeout_use() -> None:
    """Best-effort: bump the per-day usage counter. Never blocks the request."""
    try:
        r = get_redis_client()
        if r is None:
            return
        now = datetime.utcnow()
        pipe = r.pipeline()
        pipe.hincrby(_DEPRECATION_TIMEOUT_HKEY, now.strftime("%Y-%m-%d"), 1)
        pipe.hset(_DEPRECATION_TIMEOUT_HKEY, "last_seen", now.isoformat() + "Z")
        pipe.expire(_DEPRECATION_TIMEOUT_HKEY, _DEPRECATION_HKEY_TTL_S)
        pipe.execute()
    except Exception as e:  # telemetry only — must not affect task dispatch
        logger.debug("[#1068] usage counter update failed: %s", e)


def _raise_circuit_open_503(agent_name: str, execution_id, exc: CircuitOpen) -> NoReturn:
    """Map a dispatch-breaker CircuitOpen to HTTP 503 (#526).

    Closes the pre-created execution row FAILED(circuit_open) when one exists
    (the /task paths create it before acquire; /chat acquires first so passes
    None), then raises 503 with ``X-Circuit-Open`` + ``Retry-After``. No backlog
    row was ever created — acquire raised before the overflow branch.
    """
    if execution_id:
        try:
            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error="circuit_open: agent unhealthy (dispatch breaker open)",
            )
        except Exception as e:
            logger.warning(
                f"[Chat] Failed to mark execution {execution_id} FAILED on circuit open: {e}"
            )
    retry_after = max(0, int(exc.retry_after_seconds))
    raise HTTPException(
        status_code=503,
        detail={"error": "circuit_open", "retry_after_seconds": retry_after},
        headers={"X-Circuit-Open": "true", "Retry-After": str(retry_after)},
    )


def set_websocket_manager(manager):
    """Set WebSocket manager for broadcasting collaboration events."""
    global _websocket_manager
    _websocket_manager = manager


async def broadcast_collaboration_event(source_agent: str, target_agent: str, action: str = "chat"):
    """Broadcast agent collaboration event to all WebSocket clients."""
    if _websocket_manager:
        event = {
            "type": "agent_collaboration",
            "source_agent": source_agent,
            "target_agent": target_agent,
            "action": action,
            "timestamp": utc_now_iso()
        }
        await _websocket_manager.broadcast(json.dumps(event))
    else:
        print(f"[Warning] WebSocket manager not set, skipping collaboration broadcast")


class ChatAdmission(NamedTuple):
    """Result of the chat admission gate (#1026 slice 1) when a request is
    cleared to proceed. Carries the values the rest of the endpoint needs."""
    idem: object
    execution_id: str
    capacity_result: object
    capacity: object
    queue_result: str
    chat_timeout: int


async def _admit_chat_request(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
    x_via_mcp: Optional[str],
    x_mcp_key_id: Optional[str],
    x_mcp_key_name: Optional[str],
    idempotency_key: Optional[str],
):
    """Admission gate for chat_with_agent (#1026 slice 1).

    Runs the idempotency gate (#525), the dispatch-breaker fast-fail (#526), and
    the CapacityManager.acquire (#428). Returns either:
      - a ``JSONResponse`` — the caller must return it directly (idempotent
        replay snapshot); or
      - a ``ChatAdmission`` — the request is cleared and carries the
        ``idem`` / ``execution_id`` / ``capacity_result`` / ``capacity`` the
        rest of the endpoint consumes.

    Raises ``HTTPException`` directly on the deny paths (409 in-flight replay,
    503 breaker-open, 429 capacity-full — which also releases the idempotency
    claim so the caller can retry).
    """
    # RELIABILITY-006 (#525): idempotency gate. Short-circuit duplicate
    # requests before consuming a capacity slot. The header is optional — when
    # absent, dedup is off and the request proceeds normally (back-compat).
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(name), idempotency_key
    )
    if idem.replay:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="idempotent_replay",
            source="mcp" if x_via_mcp else "api",
            actor_user=current_user if not x_source_agent else None,
            actor_agent_name=x_source_agent,
            mcp_key_id=x_mcp_key_id,
            mcp_key_name=x_mcp_key_name,
            target_type="agent",
            target_id=name,
            endpoint=f"/api/agents/{name}/chat",
            details={
                "idempotency_key": idempotency_key,
                "execution_id": idem.execution_id,
                "in_flight": idem.in_flight,
            },
        )
        if idem.in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed.",
                    "execution_id": idem.execution_id,
                },
            )
        return JSONResponse(
            content=idem.snapshot
            or {"execution": {"task_execution_id": idem.execution_id}},
            headers={"X-Idempotent-Replay": "true"},
        )

    # Determine execution source
    if x_source_agent:
        source = ExecutionSource.AGENT
    else:
        source = ExecutionSource.USER

    # CAPACITY-CONSOLIDATE (#428): single CapacityManager.acquire call replaces
    # the prior ExecutionQueue.submit + SlotService.acquire_slot pair. /chat
    # shares the agent's parallel pool with /task (same `max_parallel_tasks`)
    # and spills to an in-memory queue (depth 3, preserved from the original
    # ExecutionQueue MAX_QUEUE_SIZE) when the pool is full. The agent's Claude
    # subprocess is the actual serial bottleneck downstream.
    import uuid as _uuid
    capacity = get_capacity_manager()
    chat_execution_id = str(_uuid.uuid4())
    chat_timeout = db.get_execution_timeout(name)
    max_parallel_tasks = db.get_max_parallel_tasks(name)
    # #526 F1: /chat does NOT record dispatch outcomes (only
    # task_execution_service does), so it must NOT consume a half-open probe
    # permit — doing so stalls the breaker's backoff and leaks chat requests to
    # an auth-dead agent. Gate /chat with a PURE STATE READ instead: fast-fail
    # 503 whenever the breaker is open (incl. the half-open window) and let the
    # /task path drive recovery. acquire() below runs WITHOUT breaker_enabled so
    # /chat never touches the probe machinery.
    if dispatch_breaker_active(name):
        from services.dispatch_breaker import DispatchBreaker
        _disp = DispatchBreaker(name).to_dict()
        if _disp.get("state") == "open":
            logger.warning(f"[Chat] Agent '{name}' dispatch circuit open, rejecting request")
            # Nothing dispatched — release the idempotency claim so the caller
            # can retry with the same key once the breaker recovers (#525),
            # mirroring the CapacityFull branch below.
            idempotency_service.fail(idem)
            _raise_circuit_open_503(
                name, None, CircuitOpen(name, int(_disp.get("retry_after_seconds") or 0))
            )
    try:
        capacity_result = await capacity.acquire(
            agent_name=name,
            execution_id=chat_execution_id,
            max_concurrent=max_parallel_tasks,
            message_preview=request.message[:100] if request.message else "",
            timeout_seconds=chat_timeout,
            overflow_policy="queue_in_memory",
            source=source,
            source_agent=x_source_agent,
            source_user_id=str(current_user.id),
            source_user_email=current_user.email or current_user.username,
            message=request.message,
        )
        queue_result = (
            "running"
            if capacity_result.state == "admitted"
            else f"queued:{capacity_result.queue_position}"
        )
        logger.info(f"[Chat] Agent '{name}' execution {chat_execution_id}: {queue_result}")
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="chat_started",
            source="mcp" if x_via_mcp else "api",
            actor_user=current_user if not x_source_agent else None,
            actor_agent_name=x_source_agent,
            mcp_key_id=x_mcp_key_id,
            mcp_key_name=x_mcp_key_name,
            mcp_scope="agent" if x_source_agent else ("user" if x_via_mcp else None),
            target_type="agent",
            target_id=name,
            endpoint=f"/api/agents/{name}/chat",
            request_id=None,
            details={
                "execution_id": chat_execution_id,
                "queue_result": queue_result,
                "source": source.value if hasattr(source, "value") else str(source),
                "message_length": len(request.message) if request.message else 0,
            },
        )
    except CapacityFull as e:
        logger.warning(f"[Chat] Agent '{name}' at capacity, rejecting request (reason={e.reason})")
        # Nothing dispatched — release the idempotency claim so the caller can
        # retry with the same key once capacity frees up (#525).
        idempotency_service.fail(idem)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Agent queue is full",
                "agent": name,
                "queue_length": e.depth or 0,
                "retry_after": 30,
                "message": f"Agent '{name}' is busy. Please try again later."
            }
        )

    return ChatAdmission(
        idem=idem,
        execution_id=chat_execution_id,
        capacity_result=capacity_result,
        capacity=capacity,
        queue_result=queue_result,
        chat_timeout=chat_timeout,
    )


class ChatExecutionContext(NamedTuple):
    """Execution-setup handoff for chat_with_agent (#1026 slice 2). Carries the
    records/ids the downstream execute+finalize body consumes. As with
    ChatAdmission (slice 1), every field here is referenced downstream — leaving
    one out strands a local and NameErrors the admitted path."""
    execution: object
    task_execution_id: object
    triggered_by: str
    subscription_id: object
    collaboration_activity_id: object
    chat_activity_id: object
    session: object
    is_queued: bool


async def _prepare_chat_execution(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
    x_via_mcp: Optional[str],
    x_mcp_key_id: Optional[str],
    x_mcp_key_name: Optional[str],
    idem: object,
    chat_execution_id: str,
    capacity_result: object,
    queue_result: str,
) -> ChatExecutionContext:
    """Execution setup for chat_with_agent (#1026 slice 2).

    Creates the task-execution record (#96), looks up the agent subscription
    (SUB-004), broadcasts the agent-to-agent collaboration event + activity,
    gets/creates the chat session, tracks the chat-start activity, and logs the
    inbound user message. Returns a ChatExecutionContext carrying the ids/records
    the downstream execute+finalize body consumes.
    """
    is_queued = capacity_result.state == "queued_in_memory"
    # Backwards-compat names: existing code below references `execution.id`.
    # Map the new chat_execution_id onto the old shape so the rest of the
    # function stays diff-minimal.
    class _ExecutionLite:
        def __init__(self, eid: str):
            self.id = eid
    execution = _ExecutionLite(chat_execution_id)

    # Create execution record for ALL chat calls (user, MCP, and agent-to-agent)
    # This ensures every execution appears in the Tasks tab for unified tracking (#96)
    task_execution_id = None
    # Determine triggered_by: "agent" for agent-to-agent, "mcp" for user MCP calls, "chat" for UI chat
    if x_source_agent:
        triggered_by = "agent"
    elif x_via_mcp:
        triggered_by = "mcp"
    else:
        triggered_by = "chat"
    # Look up subscription for this agent (best-effort, for usage tracking SUB-004)
    # We fetch this early so it can be passed to the execution record too
    try:
        _exec_subscription_id = db.get_agent_subscription_id(name)
    except Exception:
        _exec_subscription_id = None

    task_execution = db.create_task_execution(
        agent_name=name,
        message=request.message,
        triggered_by=triggered_by,
        source_user_id=current_user.id,
        source_user_email=current_user.email or current_user.username,
        source_agent_name=x_source_agent,
        source_mcp_key_id=x_mcp_key_id,
        source_mcp_key_name=x_mcp_key_name,
        subscription_id=_exec_subscription_id,
    )
    task_execution_id = task_execution.id if task_execution else None
    idempotency_service.attach_execution(idem, task_execution_id)
    logger.info(f"[Chat] Created task execution {task_execution_id} for {triggered_by} call on agent '{name}'")

    # Broadcast collaboration event if this is agent-to-agent communication
    collaboration_activity_id = None
    if x_source_agent:
        await broadcast_collaboration_event(
            source_agent=x_source_agent,
            target_agent=name,
            action="chat"
        )

        # Track agent collaboration activity
        collaboration_activity_id = await activity_service.track_activity(
            agent_name=x_source_agent,  # Activity belongs to source agent
            activity_type=ActivityType.AGENT_COLLABORATION,
            user_id=current_user.id,
            triggered_by="agent",
            related_execution_id=task_execution_id,  # Database execution ID for structured queries
            details={
                "source_agent": x_source_agent,
                "target_agent": name,
                "action": "chat",
                "message_preview": request.message[:100],
                "execution_id": task_execution_id,  # Also in details for WebSocket events
                "queue_status": queue_result
            }
        )

    # Get or create chat session for this user+agent
    # Reuse _exec_subscription_id already fetched above (SUB-004)
    _chat_subscription_id = _exec_subscription_id
    session = db.get_or_create_chat_session(
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        subscription_id=_chat_subscription_id,
    )

    # Track chat start activity
    # triggered_by: "agent" for agent-to-agent, "mcp" for user MCP calls, "user" for UI chat
    activity_triggered_by = "agent" if x_source_agent else ("mcp" if x_via_mcp else "user")
    chat_activity_id = await activity_service.track_activity(
        agent_name=name,
        activity_type=ActivityType.CHAT_START,
        user_id=current_user.id,
        triggered_by=activity_triggered_by,
        parent_activity_id=collaboration_activity_id,  # Link to collaboration if agent-initiated
        related_execution_id=task_execution_id,  # Database execution ID for structured queries
        details={
            "message_preview": request.message[:100],
            "source_agent": x_source_agent,
            "execution_id": task_execution_id,  # Also in details for WebSocket events
            "queue_status": queue_result
        }
    )

    # Log user message to database
    db.add_chat_message(
        session_id=session.id,
        agent_name=name,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        role="user",
        content=request.message
    )

    return ChatExecutionContext(
        execution=execution,
        task_execution_id=task_execution_id,
        triggered_by=triggered_by,
        subscription_id=_chat_subscription_id,
        collaboration_activity_id=collaboration_activity_id,
        chat_activity_id=chat_activity_id,
        session=session,
        is_queued=is_queued,
    )


@router.post("/{name}/chat")
async def chat_with_agent(
    request: ChatMessageRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
    x_mcp_key_id: Optional[str] = Header(None),
    x_mcp_key_name: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None),
):
    """
    Proxy chat messages to agent's internal web server and persist to database.

    This endpoint enforces single-execution-at-a-time via the execution queue.
    If the agent is busy, the request is queued (up to 3 waiting).
    If the queue is full, returns 429 Too Many Requests.

    Issue #98: Chat executions now also acquire a capacity slot so that
    SlotService is the single source of truth for agent load. The queue
    still enforces serial chat; the slot tracks resource usage visible
    in the capacity meter.

    Headers:
    - X-Source-Agent: Set when one agent calls another (agent-to-agent)
    - X-Via-MCP: Set for all MCP calls (both user and agent-scoped)
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # Admission gate (#1026 slice 1): idempotency (#525) + dispatch breaker
    # (#526) + capacity acquire (#428). Returns a JSONResponse on idempotent
    # replay, or a ChatAdmission to proceed; raises 409/503/429 on deny.
    admission = await _admit_chat_request(
        name=name,
        request=request,
        current_user=current_user,
        x_source_agent=x_source_agent,
        x_via_mcp=x_via_mcp,
        x_mcp_key_id=x_mcp_key_id,
        x_mcp_key_name=x_mcp_key_name,
        idempotency_key=idempotency_key,
    )
    if isinstance(admission, JSONResponse):
        return admission
    idem = admission.idem
    chat_execution_id = admission.execution_id
    capacity_result = admission.capacity_result
    capacity = admission.capacity
    queue_result = admission.queue_result
    chat_timeout = admission.chat_timeout

    # Execution setup (#1026 slice 2): exec record (#96) + subscription (SUB-004)
    # + collaboration broadcast/activity + session + chat-start activity + inbound
    # user-message log. Returns the ids/records the execute+finalize body below
    # consumes; every field is unpacked (slice-1 lesson: a stranded local
    # NameErrors the admitted path).
    ctx = await _prepare_chat_execution(
        name=name,
        request=request,
        current_user=current_user,
        x_source_agent=x_source_agent,
        x_via_mcp=x_via_mcp,
        x_mcp_key_id=x_mcp_key_id,
        x_mcp_key_name=x_mcp_key_name,
        idem=idem,
        chat_execution_id=chat_execution_id,
        capacity_result=capacity_result,
        queue_result=queue_result,
    )
    execution = ctx.execution
    task_execution_id = ctx.task_execution_id
    triggered_by = ctx.triggered_by
    _chat_subscription_id = ctx.subscription_id
    collaboration_activity_id = ctx.collaboration_activity_id
    chat_activity_id = ctx.chat_activity_id
    session = ctx.session
    is_queued = ctx.is_queued

    # Execute + finalize (#1026 slice 3): dispatch to the agent, persist the
    # assistant message + observability, complete activities, write the terminal
    # execution row, store the idempotency snapshot, and (on error) run SUB-003
    # auto-switch + map the HTTPException. Always releases the slot + idem claim.
    return await _run_chat_and_finalize(
        name=name,
        request=request,
        current_user=current_user,
        x_source_agent=x_source_agent,
        x_mcp_key_name=x_mcp_key_name,
        triggered_by=triggered_by,
        task_execution_id=task_execution_id,
        _chat_subscription_id=_chat_subscription_id,
        chat_activity_id=chat_activity_id,
        collaboration_activity_id=collaboration_activity_id,
        session=session,
        execution=execution,
        queue_result=queue_result,
        is_queued=is_queued,
        chat_timeout=chat_timeout,
        idem=idem,
        capacity=capacity,
    )


async def _run_chat_and_finalize(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
    x_mcp_key_name: Optional[str],
    triggered_by: str,
    task_execution_id: object,
    _chat_subscription_id: object,
    chat_activity_id: object,
    collaboration_activity_id: object,
    session: object,
    execution: object,
    queue_result: str,
    is_queued: bool,
    chat_timeout: int,
    idem: object,
    capacity: object,
):
    """Execute the chat against the agent and finalize (#1026 slice 3).

    Dispatches the request to the agent server, persists the assistant message +
    observability, completes the chat/collaboration activities, writes the
    terminal execution-status row, and stores the idempotency snapshot. On the
    agent-call error paths it records the failure, runs SUB-003 auto-switch
    (429/auth), and raises the mapped HTTPException. The ``finally`` always
    releases the capacity slot and any still-in-flight idempotency claim.
    Returns the agent response dict (the endpoint's response body).
    """
    idem_done = False
    execution_success = False
    try:
        # chat_timeout already fetched above for slot acquisition (Issue #98)

        payload = {"message": request.message, "stream": False}
        if request.model:
            payload["model"] = request.model
        # Inject platform instructions + execution context (#171) into every chat request.
        # Resolve the agent runtime (best-effort, never raises) so the MCP-tool
        # naming in the platform prompt matches the harness (#1187 F-MCP). Lazy +
        # guarded import so a re-import under a stubbed services.docker_service
        # (unit tests) can't break dispatch; Claude default on any failure.
        try:
            from services.docker_service import get_agent_runtime
            agent_runtime = get_agent_runtime(name)
        except Exception:
            agent_runtime = "claude-code"
        try:
            exec_ctx = ExecutionContext(
                agent_name=name,
                mode="chat",
                triggered_by=triggered_by,
                source_user_email=current_user.email or current_user.username,
                source_agent_name=x_source_agent,
                source_mcp_key_name=x_mcp_key_name,
                model=request.model,
            )
            payload["system_prompt"] = compose_system_prompt(
                execution_context=exec_ctx,
                include_execution_context=is_execution_context_enabled(),
                runtime=agent_runtime,
            )
        except Exception as e:
            logger.warning(f"[Chat] execution context build failed, falling back: {e}")
            payload["system_prompt"] = get_platform_system_prompt(runtime=agent_runtime)
        # Pass execution ID so agent registers process under the same ID (enables termination)
        if task_execution_id:
            payload["execution_id"] = task_execution_id

        # Mark execution dispatched BEFORE calling agent so the cleanup-service
        # no-session sweep doesn't falsely fail long-running executions
        # (mirrors services/task_execution_service.py:401-410, fixes #686 —
        # parallel codepath of #279).
        if task_execution_id:
            try:
                db.mark_execution_dispatched(task_execution_id)
            except Exception as e:
                logger.warning(f"[Chat] Failed to mark execution dispatched: {e}")

        start_time = datetime.utcnow()

        # Use retry helper to handle agent server startup delays
        response = await agent_post_with_retry(
            name,
            "/api/chat",
            payload,
            max_retries=3,
            retry_delay=1.0,
            timeout=chat_timeout + 10  # Add buffer for HTTP overhead
        )
        response.raise_for_status()

        response_data = response.json()

        # Extract metadata for persistence
        execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        metadata = response_data.get("metadata", {})
        session_data = response_data.get("session", {})

        # Serialize tool calls if present
        # Note: Check is not None, not truthiness - empty list [] is valid log
        # execution_log is now raw Claude Code format for UI
        # execution_log_simplified is the old format for activity tracking
        execution_log = response_data.get("execution_log", [])
        execution_log_simplified = response_data.get("execution_log_simplified", execution_log)
        execution_log_json = json.dumps(execution_log) if execution_log is not None else None
        tool_calls_json = json.dumps(execution_log_simplified) if execution_log_simplified is not None else None

        # SECURITY: Sanitize credentials from execution logs and response before persistence
        execution_log_json = sanitize_execution_log(execution_log_json)
        tool_calls_json = sanitize_execution_log(tool_calls_json)
        sanitized_response = sanitize_response(response_data.get("response", ""))

        # Log assistant response to database with observability data
        # SECURITY: Use sanitized response
        assistant_message = db.add_chat_message(
            session_id=session.id,
            agent_name=name,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username,
            role="assistant",
            content=sanitized_response,
            cost=metadata.get("cost_usd"),
            context_used=session_data.get("context_tokens"),
            context_max=session_data.get("context_window"),
            tool_calls=tool_calls_json,
            execution_time_ms=execution_time_ms,
            subscription_id=_chat_subscription_id,
            output_tokens=metadata.get("output_tokens"),
        )

        # Note: Tool calls are stored in chat_messages.tool_calls JSON column
        # Individual tool_call activities were removed (Issue #45) - they were
        # duplicate data that accumulated as orphans (never completed)

        # Track chat completion
        await activity_service.complete_activity(
            activity_id=chat_activity_id,
            status=ActivityState.COMPLETED,
            details={
                "related_chat_message_id": assistant_message.id,
                "context_used": session_data.get("context_tokens"),
                "context_max": session_data.get("context_window"),
                "cost_usd": metadata.get("cost_usd"),
                "execution_time_ms": execution_time_ms,
                "tool_count": len(execution_log_simplified),
                "execution_id": task_execution_id  # Use database execution ID, not queue ID
            }
        )

        # Complete collaboration activity if this was agent-to-agent
        if collaboration_activity_id:
            await activity_service.complete_activity(
                activity_id=collaboration_activity_id,
                status=ActivityState.COMPLETED,
                details={
                    "related_chat_message_id": assistant_message.id,
                    "response_length": len(response_data.get("response", "")),
                    "execution_time_ms": execution_time_ms,
                    "execution_id": task_execution_id  # Use database execution ID, not queue ID
                }
            )

        # Update task execution record with results (#96: all chat types now have execution records)
        # SECURITY: Use sanitized response and execution logs
        if task_execution_id:
            context_used = session_data.get("context_tokens", 0)
            # Persist the real Claude session UUID instead of the 'dispatched'
            # sentinel set by mark_execution_dispatched (#686 UC1 — closes
            # observability gap; falls back to existing sentinel if absent).
            # Defense-in-depth: agent-server emits session IDs as uuid4 strings
            # (docker/base-image/agent_server/services/headless_executor.py:167).
            # Reject malformed values so a buggy/compromised agent can't poison
            # the claude_session_id column — on rejection leave the 'dispatched'
            # sentinel (cleanup sweep stays correct, observability lost for row).
            real_session_id = (
                response_data.get("session_id")
                or session_data.get("session_id")
                or metadata.get("session_id")
            )
            if real_session_id is not None:
                try:
                    uuid.UUID(str(real_session_id))
                except (ValueError, TypeError, AttributeError):
                    logger.warning(
                        f"[Chat] Discarding malformed claude_session_id from agent response "
                        f"(execution_id={task_execution_id})"
                    )
                    real_session_id = None
            db.update_execution_status(
                execution_id=task_execution_id,
                status=TaskExecutionStatus.SUCCESS,
                response=sanitized_response,
                context_used=context_used if context_used > 0 else None,
                context_max=session_data.get("context_window") or 200000,
                cost=metadata.get("cost_usd"),
                tool_calls=tool_calls_json,  # Simplified format for activity tracking
                execution_log=execution_log_json,  # Raw Claude Code format for UI
                claude_session_id=real_session_id,
            )

        execution_success = True

        # Add execution metadata to response
        # Include both IDs for clarity:
        # - id: Queue execution ID (transient, for queue status tracking)
        # - task_execution_id: Database execution ID (permanent, for API queries and navigation)
        response_data["execution"] = {
            "id": execution.id,  # Queue ID (transient)
            "task_execution_id": task_execution_id,  # Database ID (permanent) - use this for navigation
            "queue_status": queue_result,
            "was_queued": is_queued
        }

        # RELIABILITY-006 (#525): store the result so a duplicate Idempotency-Key
        # replays this exact response instead of dispatching a second execution.
        idempotency_service.complete(idem, task_execution_id, response_data)
        idem_done = True

        return response_data
    except BackendAgentCallBudgetExhausted as _budget_e:
        # #904 RC-1: backend agent-call budget exhausted. Translate to a
        # 503 without firing SUB-003 (no Claude work started; the
        # subscription is unrelated to the rejection). Close out the
        # in-flight chat activity + execution row in the FAILED state
        # so the timeline reflects the rejection accurately.
        budget_msg = str(_budget_e)
        await activity_service.complete_activity(
            activity_id=chat_activity_id,
            status=ActivityState.FAILED,
            error=budget_msg,
        )
        if task_execution_id:
            existing = db.get_execution(task_execution_id)
            if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                db.update_execution_status(
                    execution_id=task_execution_id,
                    status=TaskExecutionStatus.FAILED,
                    error=budget_msg,
                )
        if collaboration_activity_id:
            await activity_service.complete_activity(
                activity_id=collaboration_activity_id,
                status=ActivityState.FAILED,
                error=budget_msg,
            )
        raise HTTPException(status_code=503, detail=budget_msg)

    except httpx.HTTPError as e:
        import logging
        # Extract detailed error message from agent response if available
        error_msg = f"HTTP error: {type(e).__name__}"
        agent_status_code = None
        # #678: salvage partial metadata when the agent returned the
        # structured dict body from _classify_empty_result.
        partial_metadata: dict = {}
        if hasattr(e, 'response') and e.response is not None:
            agent_status_code = e.response.status_code
            try:
                error_data = e.response.json()
                detail = error_data.get("detail")
                if isinstance(detail, dict):
                    error_msg = detail.get("message") or str(detail)
                    if isinstance(detail.get("metadata"), dict):
                        partial_metadata = sanitize_dict(detail["metadata"])
                elif "detail" in error_data:
                    error_msg = error_data["detail"]
            except Exception:
                # Try raw text if JSON parsing fails
                if e.response.text:
                    error_msg = e.response.text[:500]
        logging.getLogger("trinity.errors").error(f"Failed to communicate with agent {name}: {error_msg}")

        # Track chat failure
        await activity_service.complete_activity(
            activity_id=chat_activity_id,
            status=ActivityState.FAILED,
            error=error_msg
        )

        # Update task execution record on failure (#96: all chat types now have execution records)
        # #678: salvage cost/context from partial_metadata when the agent
        # captured them before the reader-thread race wedged its stream.
        # Mirror the cancellation-race guard from task_execution_service.py:
        # the SQL WHERE clause in update_execution_status already blocks a
        # FAILED write over a CANCELLED row, but the explicit pre-check
        # keeps the two callers consistent and avoids a wasted UPDATE.
        if task_execution_id:
            existing = db.get_execution(task_execution_id)
            if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                salvage_cost = partial_metadata.get("cost_usd") if partial_metadata else None
                salvage_context = _compute_context_used(partial_metadata) if partial_metadata else None
                salvage_context_max = (
                    (partial_metadata.get("context_window") or 200000)
                    if partial_metadata
                    else None
                )
                db.update_execution_status(
                    execution_id=task_execution_id,
                    status=TaskExecutionStatus.FAILED,
                    error=error_msg,
                    cost=salvage_cost,
                    context_used=salvage_context,
                    context_max=salvage_context_max,
                )

        # Complete collaboration activity on failure (was missing - caused activities to stay in "started" state)
        if collaboration_activity_id:
            await activity_service.complete_activity(
                activity_id=collaboration_activity_id,
                status=ActivityState.FAILED,
                error=error_msg
            )

        # SUB-003 (#441): Auto-switch on rate-limit (429) OR auth-class
        # failures (503 from agent server, or auth indicators in the error).
        from services.subscription_auto_switch import (
            handle_subscription_failure,
            is_auth_failure,
        )

        if agent_status_code == 429:
            try:
                switch_result = await handle_subscription_failure(
                    agent_name=name,
                    error_message=error_msg,
                    failure_kind="rate_limit",
                )
                if switch_result:
                    # Auto-switch happened — inform the caller
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": error_msg,
                            "auto_switch": switch_result,
                            "message": (
                                f"Rate limit hit. Subscription auto-switched to "
                                f"'{switch_result['new_subscription']}'. Please retry."
                            ),
                            "retry_after": 15,
                        }
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[SUB-003] Auto-switch check failed for '{name}': {e}")

            # Preserve 429 from agent so frontend can show clear message
            raise HTTPException(status_code=429, detail=error_msg)

        if agent_status_code == 503 or is_auth_failure(error_msg):
            try:
                switch_result = await handle_subscription_failure(
                    agent_name=name,
                    error_message=error_msg,
                    failure_kind="auth",
                )
                if switch_result:
                    # Auto-switch happened — surface as 503 + retry hint so the
                    # frontend gets the same retry UX as the 429 path.
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "error": error_msg,
                            "auto_switch": switch_result,
                            "message": (
                                f"Authentication failure on subscription. Auto-switched to "
                                f"'{switch_result['new_subscription']}'. Please retry."
                            ),
                            "retry_after": 15,
                        }
                    )
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[SUB-003] Auto-switch check failed for '{name}': {e}")

        raise HTTPException(
            status_code=503,
            detail=f"Failed to communicate with agent: {error_msg}"
        )
    finally:
        # CAPACITY-CONSOLIDATE (#428): single release covers both the
        # SlotService N-ary counter and the in-memory overflow bookkeeping.
        await capacity.release(name, execution.id)
        # RELIABILITY-006 (#525): on any non-success exit, release the in-flight
        # idempotency claim so the caller can legitimately retry (no-op on the
        # success path, where complete() already finalized it).
        if not idem_done:
            idempotency_service.fail(idem)


async def _persist_chat_session(
    agent_name: str,
    request: ParallelTaskRequest,
    result,  # TaskExecutionResult
    user_id: int,
    user_email: str,
    subscription_id: Optional[str] = None,
    execution_time_ms: Optional[int] = None,
):
    """
    Persist a /task execution to the authenticated chat session (THINK-001).

    Shared by the sync and async branches of execute_parallel_task. Only persists
    on SUCCESS — avoids writing empty assistant messages for FAILED/CANCELLED
    executions. Returns the session id (or None on failure).
    """
    if result.status != TaskExecutionStatus.SUCCESS:
        return None

    try:
        if request.create_new_session:
            session = db.create_new_chat_session(
                agent_name=agent_name,
                user_id=user_id,
                user_email=user_email,
                subscription_id=subscription_id,
            )
        elif request.chat_session_id:
            session = db.get_chat_session(request.chat_session_id)
            if not session:
                session = db.get_or_create_chat_session(
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email,
                )
        else:
            session = db.get_or_create_chat_session(
                agent_name=agent_name,
                user_id=user_id,
                user_email=user_email,
            )

        original_user_message = request.user_message or request.message
        db.add_chat_message(
            session_id=session.id,
            agent_name=agent_name,
            user_id=user_id,
            user_email=user_email,
            role="user",
            content=original_user_message,
        )
        db.add_chat_message(
            session_id=session.id,
            agent_name=agent_name,
            user_id=user_id,
            user_email=user_email,
            role="assistant",
            content=result.response or "",
            cost=result.cost,
            context_used=result.context_used,
            context_max=result.context_max,
            execution_time_ms=execution_time_ms,
        )
        logger.debug(f"[Task] Saved to chat session {session.id} for agent '{agent_name}'")
        return session.id
    except Exception as e:
        logger.warning(f"[Task] Failed to save to chat session for agent '{agent_name}': {e}")
        return None


async def _run_async_task_with_persistence(
    agent_name: str,
    request: ParallelTaskRequest,
    execution_id: str,
    collaboration_activity_id: Optional[str],
    x_source_agent: Optional[str],
    user_id: Optional[int] = None,
    user_email: Optional[str] = None,
    subscription_id: Optional[str] = None,
    is_self_task: bool = False,
    self_task_activity_id: Optional[str] = None,
    images: Optional[list] = None,
    dispatch_gate_checked: bool = False,
):
    """
    Async /task background wrapper (issue #95).

    Delegates the full execution lifecycle to TaskExecutionService (single path
    for slot / activity / sanitization / retry / release) and layers on the
    chat-endpoint-specific post-task side effects:
      - authenticated chat_session persistence (THINK-001)
      - chat_response_ready WebSocket broadcast
      - collaboration activity completion (agent-to-agent call)

    Caller (execute_parallel_task async branch) has already pre-acquired the
    capacity slot so that 429-at-capacity is returned synchronously. The
    service will release the slot in its finally block.
    """
    start_time = datetime.utcnow()
    task_service = get_task_execution_service()
    triggered_by = "agent" if x_source_agent else "manual"

    # Outer try/finally so a sync long-poll waiter (issue #498) is always
    # signaled even if the post-task side effects below raise.
    result = None
    chat_session_id = None
    try:
        # Service tracks CHAT_START with parent_activity_id=collaboration_activity_id
        # and merges extra_activity_details (parallel_mode/async_mode) so the Network
        # view filter at src/frontend/src/stores/network.js:255 still includes this
        # execution.
        result = await task_service.execute_task(
            agent_name=agent_name,
            message=request.message,
            triggered_by=triggered_by,
            source_user_id=user_id,
            source_user_email=user_email,
            source_agent_name=x_source_agent,
            model=request.model,
            timeout_seconds=request.timeout_seconds,
            resume_session_id=request.resume_session_id,
            allowed_tools=request.allowed_tools,
            system_prompt=request.system_prompt,
            execution_id=execution_id,
            subscription_id=subscription_id,
            parent_activity_id=collaboration_activity_id,
            extra_activity_details={
                "parallel_mode": True,
                "async_mode": True,
                "model": request.model,
                "timeout_seconds": request.timeout_seconds,
            },
            slot_already_held=True,  # Router pre-acquired to preserve 429-upfront contract
            images=images or [],
            dispatch_gate_checked=dispatch_gate_checked,  # #526: True when router gated at acquire()
        )

        execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)

        # Post-task side effects (each guarded + self-isolating; see helpers).
        chat_session_id = await _persist_and_broadcast_chat_session(
            agent_name=agent_name, request=request, result=result,
            execution_id=execution_id, user_id=user_id, user_email=user_email,
            subscription_id=subscription_id, execution_time_ms=execution_time_ms,
        )
        await _complete_collaboration_activity(
            collaboration_activity_id, result, execution_id, execution_time_ms,
        )
        await _finalize_self_task(
            is_self_task=is_self_task, self_task_activity_id=self_task_activity_id,
            agent_name=agent_name, request=request, result=result,
            execution_id=execution_id, user_id=user_id, user_email=user_email,
            execution_time_ms=execution_time_ms,
        )

        logger.info(
            f"[Task Async] Completed background task for agent '{agent_name}', "
            f"execution_id={execution_id}, status={result.status}"
        )
    finally:
        # Issue #498: signal any sync HTTP caller waiting on this execution.
        # No-op when no waiter is registered (the common async path).
        signal_sync_waiter(execution_id, result, chat_session_id)


def _resolve_deprecated_task_timeout(requested: Optional[int], agent_cap: int) -> tuple:
    """#1068 (demotion PR 1): deprecated per-task timeout override — honor but clamp
    to the agent cap (closing the pre-#1068 unclamped escape around the #929 invariant
    schedules respect). Returns (resolved, warning_or_None); None → pass-through."""
    if requested is None:
        return None, None
    if requested > agent_cap:
        return agent_cap, f"timeout_seconds={requested}s exceeds agent cap {agent_cap}s; clamping (field deprecated, will be removed)."
    return requested, f"timeout_seconds={requested}s deprecated; agent cap ({agent_cap}s) is authoritative."


async def _persist_and_broadcast_chat_session(
    *, agent_name, request, result, execution_id, user_id, user_email,
    subscription_id, execution_time_ms,
):
    """Post-task block 1 (THINK-001): persist the authenticated chat session and
    broadcast chat_response_ready. Returns the chat_session_id (or None when
    persistence isn't applicable) — the caller threads it to signal_sync_waiter.

    The persist call is intentionally un-wrapped (a persistence failure
    propagates, after the caller's finally signals the waiter); only the
    best-effort broadcast is isolated — preserving the pre-refactor behavior.
    """
    if not (request.save_to_session and user_id and user_email):
        return None
    chat_session_id = await _persist_chat_session(
        agent_name=agent_name,
        request=request,
        result=result,
        user_id=user_id,
        user_email=user_email,
        subscription_id=subscription_id,
        execution_time_ms=execution_time_ms,
    )
    if chat_session_id and _websocket_manager:
        try:
            await _websocket_manager.broadcast(json.dumps({
                "type": "chat_response_ready",
                "execution_id": execution_id,
                "agent_name": agent_name,
                "chat_session_id": chat_session_id,
                "timestamp": utc_now_iso(),
            }))
        except Exception as e:
            logger.warning(f"[Task Async] chat_response_ready broadcast failed: {e}")
    return chat_session_id


async def _complete_collaboration_activity(
    collaboration_activity_id, result, execution_id, execution_time_ms,
):
    """Post-task block 2: complete the agent-to-agent collaboration activity.
    No-op when there is no collaboration activity; self-isolating on error."""
    if not collaboration_activity_id:
        return
    try:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            status=(
                ActivityState.COMPLETED
                if result.status == TaskExecutionStatus.SUCCESS
                else ActivityState.FAILED
            ),
            details={
                "response_length": len(result.response or ""),
                "execution_time_ms": execution_time_ms,
                "execution_id": execution_id,
            },
            error=(result.error if result.status == TaskExecutionStatus.FAILED else None),
        )
    except Exception as e:
        logger.warning(f"[Task Async] collaboration activity completion failed: {e}")


async def _finalize_self_task(
    *, is_self_task, self_task_activity_id, agent_name, request, result,
    execution_id, user_id, user_email, execution_time_ms,
):
    """Post-task block 3 (SELF-EXEC-001): complete the self-task activity,
    inject the result into the originating chat session when requested, and
    broadcast the completion event. No-op unless this is a self-task."""
    if not (is_self_task and self_task_activity_id):
        return

    activity_status = (
        ActivityState.COMPLETED
        if result.status == TaskExecutionStatus.SUCCESS
        else ActivityState.FAILED
    )

    # Complete the self-task activity
    try:
        await activity_service.complete_activity(
            activity_id=self_task_activity_id,
            status=activity_status,
            details={
                "response_length": len(result.response or ""),
                "execution_time_ms": execution_time_ms,
                "execution_id": execution_id,
                "inject_result": request.inject_result,
            },
            error=(result.error if result.status == TaskExecutionStatus.FAILED else None),
        )
    except Exception as e:
        logger.warning(f"[Task Async] self-task activity completion failed: {e}")

    # Inject result into chat session if requested
    if request.inject_result and request.chat_session_id and result.status == TaskExecutionStatus.SUCCESS:
        try:
            # Validate session exists and belongs to user
            session = db.get_chat_session(request.chat_session_id)
            if session and session.get("user_id") == user_id:
                # Add self-task result as a chat message
                db.add_chat_message(
                    session_id=request.chat_session_id,
                    agent_name=agent_name,
                    user_id=user_id,
                    user_email=user_email or "",
                    role="assistant",
                    content=result.response or "",
                    cost=result.cost,
                    context_used=result.context_used,
                    context_max=result.context_max,
                    execution_time_ms=execution_time_ms,
                    source="self_task",  # Mark as self-task result
                )
                logger.info(f"[Self-Task] Injected result into chat session {request.chat_session_id}")
            else:
                logger.warning(f"[Self-Task] Cannot inject result: session {request.chat_session_id} not found or not owned by user")
        except Exception as e:
            logger.warning(f"[Self-Task] Failed to inject result into chat session: {e}")

    # Broadcast self-task completion event
    if _websocket_manager:
        try:
            await _websocket_manager.broadcast(json.dumps({
                "type": "agent_activity",
                "agent_name": agent_name,
                "activity_type": "self_task",
                "activity_state": "completed" if result.status == TaskExecutionStatus.SUCCESS else "failed",
                "action": f"Background task completed",
                "timestamp": utc_now_iso(),
                "details": {
                    "execution_id": execution_id,
                    "chat_session_id": request.chat_session_id,
                    "cost_usd": result.cost,
                    "execution_time_ms": execution_time_ms,
                    "response_preview": (result.response or "")[:200],
                    "inject_result": request.inject_result,
                    "result_injected": request.inject_result and request.chat_session_id is not None,
                }
            }))
        except Exception as e:
            logger.warning(f"[Self-Task] WebSocket broadcast failed: {e}")


@router.post("/{name}/task")
async def execute_parallel_task(
    request: ParallelTaskRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
    x_mcp_key_id: Optional[str] = Header(None),
    x_mcp_key_name: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None),
):
    """
    Execute a stateless task in parallel mode (no conversation context).

    Unlike /chat, this endpoint:
    - Does NOT use execution queue (parallel allowed)
    - Does NOT use --continue flag (stateless)
    - Each call is independent and can run concurrently

    Use this for:
    - Agent delegation from orchestrators
    - Batch processing without context pollution
    - Parallel task execution

    Note: Does NOT update conversation history or session state.
    Executions are saved to the database for history tracking.
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    # #1068 (demotion PR 1): normalize the deprecated per-task timeout override once
    # here — in place, so every downstream site (acquire, execute_task, backlog
    # payload) sees the clamped value and the warning fires once. No-override path skipped.
    if request.timeout_seconds is not None:
        _resolved_timeout, _timeout_warning = _resolve_deprecated_task_timeout(
            request.timeout_seconds, db.get_execution_timeout(name)
        )
        if _timeout_warning:
            logger.warning("[#1068] agent '%s': %s", name, _timeout_warning)
            _record_deprecated_task_timeout_use()  # durable signal for the soak gate
        request.timeout_seconds = _resolved_timeout

    # SELF-EXEC-001: Security validation - verify X-Source-Agent matches MCP key's agent scope
    # This prevents header spoofing where a caller claims to be a different agent
    if x_source_agent and current_user.agent_name:
        if x_source_agent != current_user.agent_name:
            raise HTTPException(
                status_code=403,
                detail=f"Source agent header '{x_source_agent}' doesn't match API key scope '{current_user.agent_name}'"
            )

    # SELF-EXEC-001: Detect self-task (agent calling itself)
    is_self_task = (x_source_agent is not None and x_source_agent == name)

    # Determine execution source for logging
    if x_source_agent:
        source = ExecutionSource.AGENT
        triggered_by = "self_task" if is_self_task else "agent"
    elif x_via_mcp:
        source = ExecutionSource.USER
        triggered_by = "mcp"
    else:
        source = ExecutionSource.USER
        triggered_by = "manual"

    # RELIABILITY-006 (#525): idempotency gate. Short-circuit duplicates before
    # any file upload / execution-record creation. Optional header — absent →
    # dedup off, request proceeds normally. Post-dispatch failures intentionally
    # leave the claim in place (a duplicate within the 24h TTL gets a 409 with
    # the original execution_id to poll); only upfront at-capacity rejections
    # release the claim so the caller can retry once capacity frees.
    idem = idempotency_service.begin(
        idempotency_service.make_agent_scope(name), idempotency_key
    )
    if idem.replay:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="idempotent_replay",
            source="mcp" if x_via_mcp else "api",
            actor_user=current_user if not x_source_agent else None,
            actor_agent_name=x_source_agent,
            mcp_key_id=x_mcp_key_id,
            mcp_key_name=x_mcp_key_name,
            target_type="agent",
            target_id=name,
            endpoint=f"/api/agents/{name}/task",
            details={
                "idempotency_key": idempotency_key,
                "execution_id": idem.execution_id,
                "in_flight": idem.in_flight,
            },
        )
        if idem.in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed.",
                    "execution_id": idem.execution_id,
                },
            )
        return JSONResponse(
            content=idem.snapshot
            or {"task_execution_id": idem.execution_id, "async_mode": bool(request.async_mode)},
            headers={"X-Idempotent-Replay": "true"},
        )

    # (#364) File upload processing — done synchronously before the async/sync
    # fork so bytes are decoded and written to the container before we return
    # execution_id (async) or before execute_task runs (sync).
    _upload_dir = None
    _image_data: list = []
    if request.files:
        uploader = current_user.email or current_user.username
        raw_files = [
            {
                "name": f.name,
                "mimetype": f.mimetype,
                "size": f.size,
                "data": decode_web_file(f.dict()),
                "id": f"f{i}",
            }
            for i, f in enumerate(request.files)
        ]
        file_descs, _upload_dir, all_writes_failed, _image_data = await process_file_uploads(
            raw_files=raw_files,
            agent_name=name,
            container=container,
            session_id=str(current_user.id),
            uploader=uploader,
            source="web",
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
        if file_descs:
            file_block = "\n".join(file_descs)
            request.message = f"{request.message}\n\n{file_block}"

    # SUB-004: Look up subscription for usage tracking (best-effort)
    try:
        _task_subscription_id = db.get_agent_subscription_id(name)
    except Exception:
        _task_subscription_id = None

    # Create execution record in database (persisted task history).
    # Issue #95 (E3): pass subscription_id so the pre-created execution record
    # snapshots the subscription at creation time (service only snapshots when
    # it creates the record itself).
    execution = db.create_task_execution(
        agent_name=name,
        message=request.message,
        triggered_by=triggered_by,
        source_user_id=current_user.id,
        source_user_email=current_user.email or current_user.username,
        source_agent_name=x_source_agent,
        source_mcp_key_id=x_mcp_key_id,
        source_mcp_key_name=x_mcp_key_name,
        model_used=request.model,
        subscription_id=_task_subscription_id,
    )
    execution_id = execution.id if execution else None
    idempotency_service.attach_execution(idem, execution_id)

    # Broadcast collaboration event if this is agent-to-agent communication
    # Track collaboration activity FIRST (belongs to source agent) - mirrors /api/chat pattern
    collaboration_activity_id = None
    self_task_activity_id = None

    if x_source_agent:
        if is_self_task:
            # SELF-EXEC-001: Self-task - track as SELF_TASK activity (belongs to the agent itself)
            self_task_activity_id = await activity_service.track_activity(
                agent_name=name,  # Activity belongs to the agent running the self-task
                activity_type=ActivityType.SELF_TASK,
                user_id=current_user.id,
                triggered_by="self_task",
                related_execution_id=execution_id,
                details={
                    "agent_name": name,
                    "action": "self_task",
                    "message_preview": request.message[:100],
                    "execution_id": execution_id,
                    "parallel_mode": True,
                    "inject_result": request.inject_result,
                    "chat_session_id": request.chat_session_id,
                }
            )
            # Broadcast self-task event (distinct from collaboration)
            if _websocket_manager:
                await _websocket_manager.broadcast(json.dumps({
                    "type": "agent_activity",
                    "agent_name": name,
                    "activity_type": "self_task",
                    "activity_state": "started",
                    "action": f"Background task: {request.message[:50]}...",
                    "timestamp": utc_now_iso(),
                    "details": {
                        "execution_id": execution_id,
                        "chat_session_id": request.chat_session_id,
                        "message_preview": request.message[:100],
                        "inject_result": request.inject_result,
                    }
                }))
        else:
            # Regular agent-to-agent collaboration
            await broadcast_collaboration_event(
                source_agent=x_source_agent,
                target_agent=name,
                action="parallel_task"
            )

            # Track agent collaboration activity (belongs to source agent for Dashboard arrows)
            collaboration_activity_id = await activity_service.track_activity(
                agent_name=x_source_agent,  # Activity belongs to source agent (the caller)
                activity_type=ActivityType.AGENT_COLLABORATION,
                user_id=current_user.id,
                triggered_by="agent",
                related_execution_id=execution_id,  # Database execution ID for structured queries
                details={
                    "source_agent": x_source_agent,
                    "target_agent": name,
                    "action": "parallel_task",
                    "message_preview": request.message[:100],
                    "execution_id": execution_id,  # Also in details for WebSocket events
                    "parallel_mode": True
                }
            )

    # Async mode: pre-acquire capacity synchronously so at-capacity returns 429
    # upfront (preserves existing client contract), then delegate the lifecycle
    # to TaskExecutionService via _run_async_task_with_persistence (#95).
    # CAPACITY-CONSOLIDATE (#428): one CapacityManager.acquire call replaces
    # the prior slot_service.acquire_slot + backlog.enqueue dance.
    if request.async_mode:
        capacity = get_capacity_manager()
        max_parallel_tasks = db.get_max_parallel_tasks(name)
        cb_enabled = dispatch_breaker_active(name)  # #526: combined global + per-agent gate
        effective_timeout = request.timeout_seconds
        if effective_timeout is None:
            effective_timeout = db.get_execution_timeout(name)

        try:
            cap_result = await capacity.acquire(
                agent_name=name,
                execution_id=execution_id or f"temp-{datetime.utcnow().timestamp()}",
                max_concurrent=max_parallel_tasks,
                message_preview=request.message[:100] if request.message else "",
                timeout_seconds=effective_timeout,
                overflow_policy="queue_persistent",
                breaker_enabled=cb_enabled,
                overflow_payload=PersistentTaskPayload(
                    request=request,
                    effective_timeout=effective_timeout,
                    user_id=current_user.id,
                    user_email=current_user.email or current_user.username,
                    subscription_id=_task_subscription_id,
                    x_source_agent=x_source_agent,
                    x_mcp_key_id=x_mcp_key_id,
                    x_mcp_key_name=x_mcp_key_name,
                    triggered_by=triggered_by,
                    collaboration_activity_id=collaboration_activity_id,
                    # #496: thread self-task fields so SELF-EXEC-001 (#264)
                    # inject_result still works when a self-task overflows.
                    is_self_task=is_self_task,
                    self_task_activity_id=self_task_activity_id,
                ),
            )
        except CapacityFull as e:
            # Both capacity AND backlog are full — surface 429 with prior shape.
            if execution_id:
                db.update_execution_status(
                    execution_id=execution_id,
                    status=TaskExecutionStatus.FAILED,
                    error=(
                        f"Agent at capacity ({max_parallel_tasks}/{max_parallel_tasks} parallel tasks running) "
                        f"and backlog is full"
                    ),
                )
            idempotency_service.fail(idem)
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Agent '{name}' is at capacity ({max_parallel_tasks} parallel tasks) "
                    f"and its backlog is full. Try again later."
                ),
            ) from e
        except CircuitOpen as e:
            # #526: dispatch breaker open — raised before the queue_persistent
            # enqueue, so nothing was backlogged. Close the pre-created row
            # FAILED(circuit_open) and return 503.
            logger.warning(f"[Task Async] Agent '{name}' dispatch circuit open, rejecting")
            # #946: nothing dispatched — release the idempotency claim so the
            # caller can retry with the same key once the breaker recovers,
            # mirroring /chat (line 242) and the CapacityFull branch above.
            # Without this the in_flight row blocks same-key retries for 24h.
            idempotency_service.fail(idem)
            _raise_circuit_open_503(name, execution_id, e)

        if cap_result.state == "queued_persistent":
            logger.info(
                f"[Task Async] Agent '{name}' at capacity — execution {execution_id} queued to backlog"
            )
            _queued_payload = {
                "status": "queued",
                "execution_id": execution_id,
                "agent_name": name,
                "message": (
                    f"Agent at capacity; task queued. Poll GET "
                    f"/api/agents/{name}/executions/{execution_id} for results."
                ),
                "async_mode": True,
            }
            idempotency_service.complete(idem, execution_id, _queued_payload)
            return _queued_payload
        slot_acquired = True  # admitted — preserved for downstream finally semantics

        # Issue #279: done callback surfaces unhandled BG task exceptions.
        def _on_task_done(task: asyncio.Task):
            if task.cancelled():
                logger.warning(f"[Task Async] Background task cancelled for agent '{name}', execution_id={execution_id}")
            elif exc := task.exception():
                logger.error(f"[Task Async] Unhandled exception in background task for agent '{name}', execution_id={execution_id}: {exc}")

        bg_task = asyncio.create_task(
            _run_async_task_with_persistence(
                agent_name=name,
                request=request,
                execution_id=execution_id,
                collaboration_activity_id=collaboration_activity_id,
                x_source_agent=x_source_agent,
                user_id=current_user.id,
                user_email=current_user.email or current_user.username,
                subscription_id=_task_subscription_id,
                is_self_task=is_self_task,
                self_task_activity_id=self_task_activity_id,
                images=_image_data,
                dispatch_gate_checked=True,  # #526: router already gated at acquire()
            )
        )
        bg_task.add_done_callback(_on_task_done)

        logger.info(f"[Task Async] Started background task for agent '{name}', execution_id={execution_id}")
        _accepted_payload = {
            "status": "accepted",
            "execution_id": execution_id,
            "agent_name": name,
            "message": "Task accepted. Poll GET /api/agents/{name}/executions/{execution_id} for results.",
            "async_mode": True,
        }
        idempotency_service.complete(idem, execution_id, _accepted_payload)
        return _accepted_payload

    # ---- Sync mode: pre-acquire capacity to mirror async branch (issue #498).
    # On success, delegate to TaskExecutionService with slot_already_held=True
    # so service finally still releases. On at-capacity, spill to the same
    # backlog the async path uses and long-poll on this connection until the
    # execution reaches a terminal status.
    # CAPACITY-CONSOLIDATE (#428): single CapacityManager.acquire call.
    capacity = get_capacity_manager()
    sync_max_parallel_tasks = db.get_max_parallel_tasks(name)
    sync_cb_enabled = dispatch_breaker_active(name)  # #526: combined global + per-agent gate
    sync_effective_timeout = request.timeout_seconds
    if sync_effective_timeout is None:
        sync_effective_timeout = db.get_execution_timeout(name)

    try:
        sync_cap_result = await capacity.acquire(
            agent_name=name,
            execution_id=execution_id or f"temp-{datetime.utcnow().timestamp()}",
            max_concurrent=sync_max_parallel_tasks,
            message_preview=request.message[:100] if request.message else "",
            timeout_seconds=sync_effective_timeout,
            overflow_policy="queue_persistent",
            breaker_enabled=sync_cb_enabled,
            overflow_payload=PersistentTaskPayload(
                request=request,
                effective_timeout=sync_effective_timeout,
                user_id=current_user.id,
                user_email=current_user.email or current_user.username,
                subscription_id=_task_subscription_id,
                x_source_agent=x_source_agent,
                x_mcp_key_id=x_mcp_key_id,
                x_mcp_key_name=x_mcp_key_name,
                triggered_by=triggered_by,
                collaboration_activity_id=collaboration_activity_id,
                is_self_task=is_self_task,
                self_task_activity_id=self_task_activity_id,
            ),
        )
    except CapacityFull as e:
        # Both capacity AND backlog are full → preserve terminal-failure semantics.
        if execution_id:
            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=(
                    f"Agent at capacity ({sync_max_parallel_tasks}/{sync_max_parallel_tasks} parallel tasks running) "
                    f"and backlog is full"
                ),
            )
        idempotency_service.fail(idem)
        raise HTTPException(
            status_code=429,
            detail=(
                f"Agent '{name}' is at capacity ({sync_max_parallel_tasks} parallel tasks) "
                f"and its backlog is full. Try again later."
            ),
        ) from e
    except CircuitOpen as e:
        # #526: dispatch breaker open — raised before the queue_persistent
        # enqueue. Close the pre-created row FAILED(circuit_open) and 503.
        logger.warning(f"[Task Sync] Agent '{name}' dispatch circuit open, rejecting")
        # #946: release the idempotency claim so the same-key retry isn't
        # blocked for 24h, mirroring /chat (line 242) and CapacityFull above.
        idempotency_service.fail(idem)
        _raise_circuit_open_503(name, execution_id, e)

    sync_slot_acquired = sync_cap_result.state == "admitted"

    if not sync_slot_acquired:
        # Spilled to backlog — long-poll on the open HTTP connection. The drain
        # callback fires _run_async_task_with_persistence; that helper signals
        # _sync_waiters from its finally so we wake immediately on terminal.

        # Long-poll cap: queue wait + execution time, both bounded individually
        # by effective_timeout via slot TTL and TaskExecutionService internals.
        # Total connection hold ≤ 2 * effective_timeout (Policy B).
        sync_wait_cap = 2 * sync_effective_timeout
        logger.info(
            f"[Task Sync] Agent '{name}' at capacity — execution {execution_id} "
            f"queued to backlog; long-polling up to {sync_wait_cap}s"
        )
        try:
            wait_payload = await wait_for_sync_terminal(
                execution_id, timeout=sync_wait_cap
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Sync task on agent '{name}' did not complete within "
                    f"{sync_wait_cap}s. Execution {execution_id} may still be "
                    f"running; poll GET /api/agents/{name}/executions/{execution_id}."
                ),
            )

        if wait_payload is not None and wait_payload.get("result") is not None:
            # Drain happy path — full TaskExecutionResult is available.
            result = wait_payload["result"]
            sync_chat_session_id = wait_payload.get("chat_session_id")
        else:
            # Polling fallback caught a non-drain terminal flip (corrupt
            # metadata, expire_stale, cleanup recovery). Reconstruct a
            # minimal result from the row so the failure-translation block
            # below still works.
            row = db.get_execution(execution_id)
            if row is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Execution {execution_id} disappeared while waiting",
                )
            from services.task_execution_service import TaskExecutionResult
            result = TaskExecutionResult(
                execution_id=execution_id,
                status=row.status,
                response=row.response or "",
                cost=row.cost,
                context_used=row.context_used,
                context_max=row.context_max,
                session_id=row.claude_session_id,
                error=row.error,
                raw_response={
                    "response": row.response or "",
                    "cost": row.cost,
                    "execution_id": execution_id,
                    "claude_session_id": row.claude_session_id,
                },
            )
            sync_chat_session_id = None

        # Side effects (collaboration activity, chat session persist) were
        # handled by _run_async_task_with_persistence inside the drain — do
        # NOT repeat them. Just translate failure and build the response.
        if result.status == "failed":
            if "at capacity" in (result.error or ""):
                raise HTTPException(
                    status_code=429,
                    detail=f"Agent '{name}' is at capacity. Try again later.",
                )
            elif "timed out" in (result.error or ""):
                raise HTTPException(status_code=504, detail=result.error)
            else:
                raise HTTPException(
                    status_code=503,
                    detail=result.error or "Failed to execute task. The agent may be unavailable.",
                )

        sync_response_data = result.raw_response or {}
        if sync_chat_session_id:
            sync_response_data["chat_session_id"] = sync_chat_session_id
        sync_response_data["task_execution_id"] = execution_id
        idempotency_service.complete(idem, execution_id, sync_response_data)
        return sync_response_data

    # ---- Slot acquired immediately — existing sync path (EXEC-024) ----
    task_execution_service = get_task_execution_service()
    result = await task_execution_service.execute_task(
        agent_name=name,
        message=request.message,
        triggered_by=triggered_by,
        source_user_id=current_user.id,
        source_user_email=current_user.email or current_user.username,
        source_agent_name=x_source_agent,
        source_mcp_key_id=x_mcp_key_id,
        source_mcp_key_name=x_mcp_key_name,
        model=request.model,
        timeout_seconds=request.timeout_seconds,  # TIMEOUT-001: None = use agent's config
        resume_session_id=request.resume_session_id,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        execution_id=execution_id,
        slot_already_held=True,  # Issue #498: router pre-acquired
        images=_image_data,
        dispatch_gate_checked=True,  # #526: router already gated at acquire()
    )

    # Complete collaboration activity based on result
    if collaboration_activity_id:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            status=ActivityState.COMPLETED if result.status == TaskExecutionStatus.SUCCESS else ActivityState.FAILED,
            details={
                "response_length": len(result.response),
                "execution_id": execution_id,
            },
            error=result.error if result.status == TaskExecutionStatus.FAILED else None,
        )

    # Handle failure — translate to HTTP errors
    if result.status == "failed":
        if "at capacity" in (result.error or ""):
            raise HTTPException(
                status_code=429,
                detail=f"Agent '{name}' is at capacity. Try again later."
            )
        elif "timed out" in (result.error or ""):
            raise HTTPException(
                status_code=504,
                detail=result.error
            )
        else:
            raise HTTPException(
                status_code=503,
                detail=result.error or "Failed to execute task. The agent may be unavailable."
            )

    # Build response from service result
    response_data = result.raw_response

    # Persist to chat session if requested (for authenticated Chat tab).
    # Shared helper with the async branch (issue #95): guards on SUCCESS so
    # FAILED/CANCELLED executions don't write empty assistant messages.
    if request.save_to_session:
        chat_session_id = await _persist_chat_session(
            agent_name=name,
            request=request,
            result=result,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username,
            subscription_id=_task_subscription_id,
        )
        if chat_session_id:
            response_data["chat_session_id"] = chat_session_id

    # Add database execution ID to response for frontend tracking
    response_data["task_execution_id"] = execution_id

    idempotency_service.complete(idem, execution_id, response_data)
    return response_data


@router.get("/{name}/chat/history")
async def get_agent_chat_history(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Get agent's conversation history."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent UI not enabled for this agent"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/chat/history",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get chat history for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get chat history"
        )


@router.delete("/{name}/chat/history")
async def reset_agent_chat_history(
    name: str = Depends(get_owned_agent),
    current_user: User = Depends(get_current_user)
):
    """Reset/clear agent's conversation history (start a new session)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=503,
            detail="Agent is not running"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.delete(
                f"http://agent-{name}:8000/api/chat/history",
                timeout=10.0
            )
            # Agent may not implement this endpoint yet
            if response.status_code == 405:
                # Clear activity instead as a fallback
                await client.delete(
                    f"http://agent-{name}:8000/api/activity",
                    timeout=10.0
                )
                return {"status": "reset", "message": "Session activity cleared"}
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to reset chat history for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to reset chat history"
        )


@router.get("/{name}/chat/session")
async def get_agent_chat_session(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Get agent's current session info including context usage."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/chat/session",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get session info for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get session info"
        )


# Activity Monitoring Routes

@router.get("/{name}/activity")
async def get_agent_activity(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Get session activity for real-time monitoring."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": {
                "calls": 0,
                "duration_ms": 0,
                "started_at": None
            }
        }

    try:
        async with agent_httpx_client(name) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/activity",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": {
                "calls": 0,
                "duration_ms": 0,
                "started_at": None
            }
        }


@router.get("/{name}/activity/{tool_id}")
async def get_agent_activity_detail(
    tool_id: str,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Get full details for a specific tool call."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/activity/{tool_id}",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get activity detail for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get activity detail"
        )


@router.delete("/{name}/activity")
async def clear_agent_activity(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Clear session activity (called when starting a new session)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {
            "status": "cleared",
            "message": "Agent is not running - nothing to clear"
        }

    try:
        async with agent_httpx_client(name) as client:
            response = await client.delete(
                f"http://agent-{name}:8000/api/activity",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to clear activity for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to clear activity"
        )


# Model Routes

@router.get("/{name}/model")
async def get_agent_model(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Get agent's current model configuration."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/model",
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to get model info for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to get model info"
        )


@router.put("/{name}/model")
async def set_agent_model(
    request: ModelChangeRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Set agent's model for subsequent messages."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is not running"
        )

    try:
        async with agent_httpx_client(name) as client:
            response = await client.put(
                f"http://agent-{name}:8000/api/model",
                json={"model": request.model},
                timeout=10.0
            )
            response.raise_for_status()

            return response.json()
    except httpx.HTTPError as e:
        import logging
        logging.getLogger("trinity.errors").error(f"Failed to set model for {name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="Failed to set model"
        )


# Persistent Chat History Routes

@router.get("/{name}/chat/history/persistent")
async def get_persistent_chat_history(
    limit: int = 100,
    user_filter: bool = False,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Get persistent chat history from database.

    This returns messages across all sessions, persisted in the database.
    Unlike /chat/history which returns only the current container session.

    Parameters:
    - limit: Maximum number of messages to return (default 100)
    - user_filter: If true, only show current user's messages (default false, shows all users for agent owners)
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Determine if user should see all messages or just their own
    # Agent owners can see all messages, others only see their own
    user_id_filter = None
    if user_filter or current_user.role != "admin":
        # For non-admins, always filter to their own messages unless they're the owner
        # (Owner check would require checking agent_ownership table)
        user_id_filter = current_user.id

    messages = db.get_agent_chat_history(
        agent_name=name,
        user_id=user_id_filter,
        limit=limit
    )

    return {
        "agent_name": name,
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }


@router.get("/{name}/chat/sessions")
async def get_agent_chat_sessions(
    status: str = None,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Get all chat sessions for an agent.

    Returns session metadata including message counts, costs, and timestamps.
    Non-admin users only see their own sessions.

    Parameters:
    - status: Filter by session status ('active' or 'closed')
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Non-admins only see their own sessions
    user_id_filter = None if current_user.role == "admin" else current_user.id

    sessions = db.get_agent_chat_sessions(
        agent_name=name,
        user_id=user_id_filter,
        status=status
    )

    return {
        "agent_name": name,
        "session_count": len(sessions),
        "sessions": [session.model_dump() for session in sessions]
    }


@router.get("/{name}/chat/sessions/{session_id}")
async def get_chat_session_detail(
    session_id: str,
    limit: int = 100,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed information about a specific chat session, including all messages.

    Parameters:
    - limit: Maximum number of messages to return (default 100)
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    # Non-admins can only see their own sessions
    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    messages = db.get_chat_messages(session_id, limit=limit)

    return {
        "session": session.model_dump(),
        "message_count": len(messages),
        "messages": [msg.model_dump() for msg in messages]
    }


@router.post("/{name}/chat/sessions/{session_id}/close")
async def close_chat_session(
    session_id: str,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """Close a chat session (marks it as closed but keeps the history)."""
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    session = db.get_chat_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")

    # Verify session belongs to this agent and user
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")

    if current_user.role != "admin" and session.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this session")

    success = db.close_chat_session(session_id)

    if success:
        return {"status": "closed", "session_id": session_id}
    else:
        raise HTTPException(status_code=500, detail="Failed to close session")


# ============================================================================
# Execution Termination Routes
# ============================================================================

@router.post("/{name}/executions/{execution_id}/terminate")
async def terminate_agent_execution(
    execution_id: str,
    task_execution_id: Optional[str] = None,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Terminate a running execution on an agent.

    Proxies the termination request to the agent container and clears
    the execution queue state if successful.

    Args:
        name: Agent name
        execution_id: The execution ID to terminate (same as database execution ID)
        task_execution_id: Optional override for database execution ID (defaults to execution_id)
    """
    # execution_id is now the database execution ID (passed through to agent process registry)
    # Fall back to using execution_id for DB update if task_execution_id not separately provided
    if not task_execution_id:
        task_execution_id = execution_id

    # BACKLOG-001: If the execution is still queued in the backlog, cancel it
    # directly — no container interaction needed, no slot to release.
    try:
        _exec_row = db.get_execution(task_execution_id)
    except Exception:
        _exec_row = None
    if _exec_row and _exec_row.status == TaskExecutionStatus.QUEUED:
        cancelled = db.cancel_queued_execution(
            task_execution_id, reason="Cancelled by user while queued"
        )
        if cancelled:
            await activity_service.track_activity(
                agent_name=name,
                activity_type=ActivityType.EXECUTION_CANCELLED,
                user_id=current_user.id,
                triggered_by="user",
                related_execution_id=task_execution_id,
                details={
                    "execution_id": execution_id,
                    "task_execution_id": task_execution_id,
                    "status": "cancelled_while_queued",
                },
            )
            return {"status": "cancelled_while_queued", "execution_id": execution_id}
        # Else it transitioned out of queued between our read and update;
        # fall through to the normal terminate path.

    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    try:
        # Proxy termination request to agent container
        async with agent_httpx_client(name, timeout=15.0) as client:
            response = await client.post(
                f"http://agent-{name}:8000/api/executions/{execution_id}/terminate"
            )

        result = response.json()

        # Handle different termination outcomes
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Execution not found in agent")

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=result.get("detail", "Termination failed")
            )

        # Clear capacity state if termination succeeded.
        # CAPACITY-CONSOLIDATE (#428): single force_release covers both the
        # SlotService N-ary counter and the in-memory overflow queue.
        if result.get("status") in ["terminated", "already_finished"]:
            try:
                capacity = get_capacity_manager()
                fr = await capacity.force_release(name)
                logger.info(
                    f"[Terminate] Force-released capacity for agent '{name}' "
                    f"(was_running={fr.was_running}, slots_cleared={fr.slots_cleared})"
                )
            except Exception as e:
                logger.warning(f"[Terminate] Failed to force-release capacity for {name}: {e}")

            # Update database execution record if provided
            if task_execution_id:
                db.update_execution_status(
                    execution_id=task_execution_id,
                    status=TaskExecutionStatus.CANCELLED,
                    error="Execution terminated by user"
                )
                logger.info(f"[Terminate] Updated database execution {task_execution_id} to cancelled")

        # Track termination activity
        await activity_service.track_activity(
            agent_name=name,
            activity_type=ActivityType.EXECUTION_CANCELLED,
            user_id=current_user.id,
            triggered_by="user",
            related_execution_id=task_execution_id,
            details={
                "execution_id": execution_id,
                "task_execution_id": task_execution_id,
                "status": result.get("status"),
                "returncode": result.get("returncode")
            }
        )

        return result

    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to agent '{name}'"
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=f"Timeout connecting to agent '{name}'"
        )


@router.get("/{name}/executions/running")
async def get_agent_running_executions(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Get list of running executions on an agent.

    Returns execution IDs, start times, and metadata for running processes.
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        return {"executions": []}

    try:
        async with agent_httpx_client(name, timeout=10.0) as client:
            response = await client.get(
                f"http://agent-{name}:8000/api/executions/running"
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        return {"executions": []}


# ============================================================================
# Live Execution Streaming Routes
# ============================================================================

@router.get("/{name}/executions/{execution_id}/stream")
async def stream_execution_log(
    execution_id: str,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user)
):
    """
    Stream execution log entries via Server-Sent Events (SSE).

    Proxies the SSE stream from the agent container to the frontend.
    Validates user access before starting the stream.

    SSE Event format:
    - data: JSON-encoded log entry from Claude Code
    - Final message: {"type": "stream_end"}

    Use this endpoint for live monitoring of running executions.
    """
    container = get_agent_container(name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    async def proxy_stream():
        """Proxy SSE stream from agent container with connect timeout and keepalive."""
        agent_url = f"http://agent-{name}:8000/api/executions/{execution_id}/stream"
        try:
            # Connect timeout prevents hanging if agent is unresponsive,
            # but read timeout is None since SSE streams are long-lived
            timeout = httpx.Timeout(connect=10.0, read=None, write=None, pool=None)
            async with agent_httpx_client(name, timeout=timeout) as client:
                async with client.stream("GET", agent_url) as response:
                    if response.status_code == 404:
                        # Execution not found on agent (race condition: task not started yet)
                        yield f"data: {json.dumps({'type': 'error', 'message': 'Execution not yet available on agent', 'retryable': True})}\n\n"
                        yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                        return

                    if response.status_code != 200:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Agent returned {response.status_code}'})}\n\n"
                        yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                        return

                    # Stream through data from agent, adding proxy-level keepalive
                    async for chunk in response.aiter_text():
                        yield chunk
        except httpx.ConnectError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to connect to agent', 'retryable': True})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
        except httpx.ConnectTimeout:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Agent connection timed out', 'retryable': True})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
        except Exception as e:
            logger.error(f"[Stream] Error streaming from agent {name}: {e}")
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
