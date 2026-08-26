"""
Agent chat and activity routes for the Trinity backend.

Includes execution queue integration to prevent parallel execution on the same agent.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import json
import logging
from datetime import datetime
from typing import NoReturn, Optional

from models import User, ChatMessageRequest, ModelChangeRequest, ParallelTaskRequest, TaskExecutionStatus
from dependencies import get_current_user, get_authorized_agent, get_owned_agent, assert_owns_or_admin
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.capacity_manager import (
    CapacityFull,
    CircuitOpen,
    EphemeralBudgetExhausted,
)
from services import dispatch_admission_service
from services import chat_execution_service
from services.chat_signals import ChatAdmissionReplay, ChatDispatchError
from database import db
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/agents", tags=["chat"])

# NOTE (#1483): the chat router no longer holds a WebSocket manager — every
# broadcast (agent_collaboration, self_task, chat_response_ready) moved into
# chat_execution_service / chat_persistence_service, each with its own
# set_websocket_manager wired in main.py (§7: minimize WS globals).

# The #1068 deprecated-timeout usage counter sources its redis client from the
# auth router (RD10: kept at the router boundary so a service never imports a
# router — the exact Invariant #1 smell the split enforces against).
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


def _raise_ephemeral_exhausted_410(agent_name: str, execution_id, exc) -> NoReturn:
    """Map an EphemeralBudgetExhausted to HTTP 410 Gone (trinity-enterprise#69).

    The ghost's budget is spent — it is at end-of-life and will be discarded by
    the terminal hook / GC sweep; 410 (not 429) tells the caller retrying won't
    help. Closes the pre-created execution row when one exists.
    """
    if execution_id:
        try:
            db.update_execution_status(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=f"ephemeral_exhausted: ghost agent budget spent ({exc.reason})",
            )
        except Exception as e:
            logger.warning(
                f"[Chat] Failed to mark execution {execution_id} FAILED on ephemeral exhaustion: {e}"
            )
    raise HTTPException(
        status_code=410,
        detail={
            "error": f"Ephemeral agent '{agent_name}' budget is spent ({exc.reason}); it is being discarded.",
            "code": "ephemeral_budget_exhausted",
        },
    )


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


@router.post("/{name}/chat")
async def chat_with_agent(
    request: ChatMessageRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
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
    # (#526) + capacity acquire (#428) live in dispatch_admission_service now
    # (Invariant #1). The service is HTTP-free: it returns a ChatAdmissionReplay
    # (idempotent replay) or a ChatAdmission, and raises the already-domain
    # CircuitOpen / CapacityFull / EphemeralBudgetExhausted — which this thin
    # handler maps to 503 / 429 / 410 (the FAILED-row-write + raise stay here,
    # RD-E12; /chat holds no pre-created row so execution_id is None).
    try:
        admission = await dispatch_admission_service.admit_chat_request(
            name=name,
            request=request,
            current_user=current_user,
            x_source_agent=x_source_agent,
            x_via_mcp=x_via_mcp,
            idempotency_key=idempotency_key,
        )
    except CircuitOpen as e:
        _raise_circuit_open_503(name, None, e)
    except EphemeralBudgetExhausted as e:
        _raise_ephemeral_exhausted_410(name, None, e)
    except CapacityFull as e:
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
    if isinstance(admission, ChatAdmissionReplay):
        if admission.in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed.",
                    "execution_id": admission.execution_id,
                },
            )
        return JSONResponse(
            content=admission.snapshot
            or {"execution": {"task_execution_id": admission.execution_id}},
            headers={"X-Idempotent-Replay": "true"},
        )
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
    ctx = await chat_execution_service.prepare_chat_execution(
        name=name,
        request=request,
        current_user=current_user,
        x_source_agent=x_source_agent,
        x_via_mcp=x_via_mcp,
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
    # auto-switch. The service is HTTP-free — its failure paths raise
    # ChatDispatchError, which this thin handler maps 1:1 to an HTTPException.
    # The finally in run_chat_turn releases the slot + idem claim.
    try:
        return await chat_execution_service.run_chat_turn(
            name=name,
            request=request,
            current_user=current_user,
            x_source_agent=x_source_agent,
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
    except ChatDispatchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers=e.headers)


def _resolve_deprecated_task_timeout(requested: Optional[int], agent_cap: int) -> tuple:
    """#1068 (demotion PR 1): deprecated per-task timeout override — honor but clamp
    to the agent cap (closing the pre-#1068 unclamped escape around the #929 invariant
    schedules respect). Returns (resolved, warning_or_None); None → pass-through."""
    if requested is None:
        return None, None
    if requested > agent_cap:
        return agent_cap, f"timeout_seconds={requested}s exceeds agent cap {agent_cap}s; clamping (field deprecated, will be removed)."
    return requested, f"timeout_seconds={requested}s deprecated; agent cap ({agent_cap}s) is authoritative."


@router.post("/{name}/task")
async def execute_parallel_task(
    request: ParallelTaskRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),
    idempotency_key: Optional[str] = Header(None),
    x_event_trigger: Optional[str] = Header(None),
    x_internal_secret: Optional[str] = Header(None),
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

    # EXEC-023 (#1672): validate + authorize a resume target before it becomes
    # `claude --resume <id>` (or `codex resume <id>`) in the container.
    # resume_session_id arrives from a user-editable URL query param (ExecutionDetail
    # "Continue as Chat"); untrusted. Two gates:
    #   1. Reject the dispatch sentinels — 'dispatched'/'dispatched_async' land in
    #      claude_session_id before the real session id and stay permanently on
    #      reaper-FAILED async rows (#1083). Such a row IS owned by its triggerer, so
    #      the ownership gate below would pass it — but resuming it runs
    #      `--resume dispatched_async`, which cannot resolve. Reject up front.
    #   2. Authorize ownership — execution rows are AGENT-scoped
    #      (accessible_agent_names), but claude_session_id is a per-user secret
    #      (routers/sessions.py gates the Session tab on it and 404s to avoid leaking
    #      its existence). Without this check, any operator on a *shared* agent could
    #      read a peer's session id from the executions list and resume their private
    #      conversation (IDOR). 404 (not 403) mirrors that enumeration-safe response.
    #
    # The gate is keyed ONLY on resume_session_id being present — deliberately NOT
    # `and not x_source_agent`. x_source_agent is a raw client header; a regular user
    # (current_user.agent_name is None, so the SELF-EXEC-001 spoof-guard below never
    # fires for them) could otherwise set it to skip the gate entirely and keep the
    # IDOR open. Every principal is handled correctly by ownership: a human is checked
    # against their own user id; an agent-scoped key resolves to its owner and is
    # checked against the owner's id; admin bypasses (mirrors the Session tab). No
    # legitimate agent-to-agent path carries a resume id, so gating them costs nothing.
    #
    # Ownership is the real guard, so NO id-shape check is needed: a value that
    # matches a real row's claude_session_id is a system-generated id (a Claude
    # str(uuid4()) OR a Codex thread_id — a shape check would wrongly reject Codex),
    # and a bogus/injection string matches no row → 404.
    if request.resume_session_id:
        rid = request.resume_session_id
        if rid in ("dispatched", "dispatched_async"):
            raise HTTPException(
                status_code=400,
                detail="This execution was never assigned a resumable session.",
            )
        if current_user.role != "admin" and not db.resume_session_belongs_to_user(
            name, rid, current_user.id
        ):
            raise HTTPException(status_code=404, detail="Session not found.")

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

    # Derive → idempotency → file upload → create-row → async/sync dispatch all
    # live in chat_execution_service now (Invariant #1). It is HTTP-free: it
    # raises ChatDispatchError (mapped 1:1 here) and returns a response dict OR a
    # ChatAdmissionReplay (idempotent replay → 409 in-flight / 200 JSONResponse).
    try:
        result = await chat_execution_service.dispatch_parallel_task(
            request=request,
            name=name,
            current_user=current_user,
            container=container,
            x_source_agent=x_source_agent,
            x_via_mcp=x_via_mcp,
            idempotency_key=idempotency_key,
            x_event_trigger=x_event_trigger,
            x_internal_secret=x_internal_secret,
        )
    except ChatDispatchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers=e.headers)
    if isinstance(result, ChatAdmissionReplay):
        if result.in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed.",
                    "execution_id": result.execution_id,
                },
            )
        return JSONResponse(
            content=result.snapshot
            or {"task_execution_id": result.execution_id, "async_mode": bool(request.async_mode)},
            headers={"X-Idempotent-Replay": "true"},
        )
    return result


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
    assert_owns_or_admin(current_user, session.user_id, detail="You don't have access to this session")

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

    assert_owns_or_admin(current_user, session.user_id, detail="You don't have access to this session")

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

    # The termination orchestration (BACKLOG-001 queued-cancel, agent-proxy,
    # capacity force-release, #679 CANCELLED CAS, #1332 dispatch-activity close)
    # lives in chat_execution_service (Invariant #1). HTTP-free — it raises
    # ChatDispatchError (404/503/502/504/non-200), mapped 1:1 here.
    try:
        return await chat_execution_service.terminate_execution(
            name=name,
            execution_id=execution_id,
            task_execution_id=task_execution_id,
            current_user=current_user,
        )
    except ChatDispatchError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail, headers=e.headers)


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
