"""
Chat execution lifecycle for the ``/chat`` (and ``/task`` post-processing)
endpoints (#1483, Invariant #1).

Owns the business logic the router used to carry inline: the ``/chat`` execution
setup (``prepare_chat_execution``), the sync-**chat** execute+finalize applier
(``run_chat_turn`` — decomposed per plan §3a), the agent-to-agent collaboration
broadcast, and (added by the /task move) the async ``/task`` post-processing +
dispatch orchestration.

**Scope boundary (RD1, declared TRANSITIONAL — RD15):** ``run_chat_turn`` is the
ONE divergent terminal-writing applier this service owns; it exists because
sync-chat does its own ``chat_sessions`` persistence + collaboration-activity
completion + ``mode="chat"`` prompt that ``task_execution_service.execute_task``
does not. This service is **never** a ``/task`` terminal applier or a parallel
task coordinator — ``/task`` keeps delegating to
``task_execution_service.execute_task`` / ``apply_result`` (the single applier the
pull migration converges on, architecture.md). Converging ``run_chat_turn`` onto
``execute_task`` is a behavior change tracked as a follow-up.

**HTTP-free** (Invariant #1): failure paths raise ``ChatDispatchError`` carrying
the exact (status_code, detail, headers) the thin router maps 1:1. The lone
``fastapi`` touch is ``except HTTPException: raise`` in ``_apply_sub003_autoswitch``
— a defensive *re-raise* of an exception ``subscription_auto_switch`` itself
raises (propagate-unchanged), never HTTP construction.
"""
import asyncio
import httpx
import json
import logging
import uuid
from collections import namedtuple
from datetime import datetime
from typing import Optional

from fastapi import HTTPException  # only for the defensive SUB-003 re-raise

from models import (
    User,
    ChatMessageRequest,
    ParallelTaskRequest,
    ActivityType,
    ActivityState,
    TaskExecutionStatus,
    activity_state_for_terminal,
)
from database import db
from services.activity_service import activity_service
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.agent_call_limiter import BackendAgentCallBudgetExhausted
from services.model_context import DEFAULT_CONTEXT_WINDOW
from services.task_execution_service import (
    _compute_context_used,
    agent_post_with_retry,
    get_task_execution_service,
    dispatch_breaker_active,
)
from services.capacity_manager import (
    CapacityFull,
    CircuitOpen,
    EphemeralBudgetExhausted,
    PersistentTaskPayload,
    get_capacity_manager,
)
from services.upload_service import (
    process_file_uploads,
    decode_web_file,
    WEB_MAX_FILES,
    WEB_MAX_FILE_SIZE,
    WEB_MAX_IMAGE_SIZE,
    WEB_MAX_TOTAL_IMAGE_SIZE,
)
from services.sync_waiter import signal_sync_waiter, wait_for_sync_terminal
from services.event_dispatch_service import (
    RESERVED_EVENT_TRIGGER,
    RESERVED_EVENT_TRIGGER_HEADER_VALUE,
    verify_internal_dispatch_secret,
)
from services import idempotency_service
from services import dispatch_admission_service
from services import chat_persistence_service
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    get_platform_system_prompt,
    is_execution_context_enabled,
)
from services.chat_signals import ChatExecutionContext, ChatAdmissionReplay, ChatDispatchError
from utils.credential_sanitizer import sanitize_dict, sanitize_execution_log, sanitize_response
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# WebSocket manager (injected from main.py). Feeds broadcast_collaboration_event
# (per-service setter — the established activity_service/report_service pattern).
_websocket_manager = None


def set_websocket_manager(manager):
    """Set the WebSocket manager for collaboration broadcasts."""
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


async def prepare_chat_execution(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
    x_via_mcp: Optional[str],
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
        source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
        source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
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


def build_chat_payload(
    *, name: str, request: ChatMessageRequest, triggered_by: str, current_user: User,
    x_source_agent: Optional[str], task_execution_id: object,
) -> dict:
    """Build the agent-server /api/chat payload: message + model + the
    runtime-aware platform/execution-context system prompt (MEM-001, #1187), and
    mark the execution dispatched (#686) so the no-session sweep doesn't falsely
    fail a long turn."""
    payload = {"message": request.message, "stream": False}
    if request.model:
        payload["model"] = request.model
    # Resolve the agent runtime (best-effort, never raises) so the MCP-tool
    # naming in the platform prompt matches the harness (#1187 F-MCP). Lazy +
    # guarded so a re-import under a stubbed services.docker_service can't break
    # dispatch; Claude default on any failure.
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
            source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
            model=request.model,
        )
        payload["system_prompt"] = compose_system_prompt(
            execution_context=exec_ctx,
            include_execution_context=is_execution_context_enabled(),
            runtime=agent_runtime,
        )
    except Exception as e:
        logger.warning(f"[Chat] execution context build failed, falling back: {e}")
        # ent#243: pass the model here too — a context-build failure must not
        # silently swap the prompt tier as well as the context block.
        payload["system_prompt"] = get_platform_system_prompt(
            runtime=agent_runtime, model=request.model
        )
    # Pass execution ID so agent registers process under the same ID (enables termination)
    if task_execution_id:
        payload["execution_id"] = task_execution_id

    # Mark execution dispatched BEFORE calling agent so the cleanup-service
    # no-session sweep doesn't falsely fail long-running executions
    # (mirrors services/task_execution_service.py, fixes #686).
    if task_execution_id:
        try:
            db.mark_execution_dispatched(task_execution_id)
        except Exception as e:
            logger.warning(f"[Chat] Failed to mark execution dispatched: {e}")
    return payload


async def _finalize_chat_success(
    *, name, response, start_time, session, current_user, chat_activity_id,
    collaboration_activity_id, task_execution_id, _chat_subscription_id,
    execution, queue_result, is_queued, idem,
) -> dict:
    """Success finalizer: persist the assistant message + observability, complete
    the chat/collaboration activities, write the terminal SUCCESS row (with a
    UUID-validated claude_session_id), build response["execution"], and store the
    idempotency snapshot. Returns the response body."""
    response_data = response.json()
    execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
    metadata = response_data.get("metadata", {})
    session_data = response_data.get("session", {})

    execution_log = response_data.get("execution_log", [])
    execution_log_simplified = response_data.get("execution_log_simplified", execution_log)
    execution_log_json = json.dumps(execution_log) if execution_log is not None else None
    tool_calls_json = json.dumps(execution_log_simplified) if execution_log_simplified is not None else None

    # SECURITY: Sanitize credentials from execution logs and response before persistence
    execution_log_json = sanitize_execution_log(execution_log_json)
    tool_calls_json = sanitize_execution_log(tool_calls_json)
    sanitized_response = sanitize_response(response_data.get("response", ""))

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
            "execution_id": task_execution_id
        }
    )

    if collaboration_activity_id:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            status=ActivityState.COMPLETED,
            details={
                "related_chat_message_id": assistant_message.id,
                "response_length": len(response_data.get("response", "")),
                "execution_time_ms": execution_time_ms,
                "execution_id": task_execution_id
            }
        )

    if task_execution_id:
        context_used = session_data.get("context_tokens", 0)
        # Persist the real Claude session UUID instead of the 'dispatched'
        # sentinel (#686 UC1). Reject malformed values so a buggy/compromised
        # agent can't poison the claude_session_id column — on rejection leave
        # the 'dispatched' sentinel (cleanup sweep stays correct).
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
            context_max=session_data.get("context_window") or DEFAULT_CONTEXT_WINDOW,
            cost=metadata.get("cost_usd"),
            tool_calls=tool_calls_json,
            execution_log=execution_log_json,
            claude_session_id=real_session_id,
        )

    # Add execution metadata to response
    response_data["execution"] = {
        "id": execution.id,  # Queue ID (transient)
        "task_execution_id": task_execution_id,  # Database ID (permanent)
        "queue_status": queue_result,
        "was_queued": is_queued
    }

    # RELIABILITY-006 (#525): store the result so a duplicate Idempotency-Key
    # replays this exact response instead of dispatching a second execution.
    idempotency_service.complete(idem, task_execution_id, response_data)
    return response_data


async def _finalize_budget_exhausted(
    *, budget_exc, task_execution_id, chat_activity_id, collaboration_activity_id,
):
    """#904 RC-1: backend agent-call budget exhausted → 503 without firing
    SUB-003. #1332: mirror a raced CANCELLED terminal onto the activities instead
    of stamping FAILED over a cancel. Always raises ChatDispatchError(503)."""
    budget_msg = str(budget_exc)
    existing = db.get_execution(task_execution_id) if task_execution_id else None
    budget_close_state = (
        activity_state_for_terminal(existing.status) if existing else ActivityState.FAILED
    )
    budget_close_error = budget_msg if budget_close_state == ActivityState.FAILED else None
    await activity_service.complete_activity(
        activity_id=chat_activity_id,
        status=budget_close_state,
        error=budget_close_error,
    )
    if task_execution_id and (not existing or existing.status != TaskExecutionStatus.CANCELLED):
        db.update_execution_status(
            execution_id=task_execution_id,
            status=TaskExecutionStatus.FAILED,
            error=budget_msg,
        )
    if collaboration_activity_id:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            status=budget_close_state,
            error=budget_close_error,
        )
    raise ChatDispatchError(503, budget_msg)


def _parse_agent_http_error(e, name: str):
    """Extract (error_msg, agent_status_code, partial_metadata) from an httpx
    error, salvaging the structured #678 metadata dict when present."""
    error_msg = f"HTTP error: {type(e).__name__}"
    agent_status_code = None
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
            if e.response.text:
                error_msg = e.response.text[:500]
    return error_msg, agent_status_code, partial_metadata


async def _apply_sub003_autoswitch(name: str, error_msg: str, agent_status_code):
    """SUB-003 (#441): auto-switch on rate-limit (429) OR auth-class failures.
    ALWAYS raises: ChatDispatchError (switch/plain) OR the HTTPException that
    handle_subscription_failure itself raised (propagate-unchanged — the original
    ``except HTTPException: raise`` semantics; preserved by the char tests)."""
    from services.subscription_auto_switch import (
        handle_subscription_failure,
        is_auth_failure,
    )

    if agent_status_code == 429:
        try:
            switch_result = await handle_subscription_failure(
                agent_name=name, error_message=error_msg, failure_kind="rate_limit",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[SUB-003] Auto-switch check failed for '{name}': {e}")
            switch_result = None
        if switch_result:
            raise ChatDispatchError(
                429,
                {
                    "error": error_msg,
                    "auto_switch": switch_result,
                    "message": (
                        f"Rate limit hit. Subscription auto-switched to "
                        f"'{switch_result['new_subscription']}'. Please retry."
                    ),
                    "retry_after": 15,
                },
            )
        raise ChatDispatchError(429, error_msg)

    if agent_status_code == 503 or is_auth_failure(error_msg):
        try:
            switch_result = await handle_subscription_failure(
                agent_name=name, error_message=error_msg, failure_kind="auth",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[SUB-003] Auto-switch check failed for '{name}': {e}")
            switch_result = None
        if switch_result:
            raise ChatDispatchError(
                503,
                {
                    "error": error_msg,
                    "auto_switch": switch_result,
                    "message": (
                        f"Authentication failure on subscription. Auto-switched to "
                        f"'{switch_result['new_subscription']}'. Please retry."
                    ),
                    "retry_after": 15,
                },
            )
        raise ChatDispatchError(503, f"Failed to communicate with agent: {error_msg}")

    raise ChatDispatchError(503, f"Failed to communicate with agent: {error_msg}")


async def _finalize_http_failure(
    *, name, e, task_execution_id, chat_activity_id, collaboration_activity_id,
):
    """httpx failure finalizer: #1332 read-before-close mirroring + #678 salvage
    onto the FAILED row + SUB-003 auto-switch. Always raises."""
    error_msg, agent_status_code, partial_metadata = _parse_agent_http_error(e, name)
    logging.getLogger("trinity.errors").error(
        f"Failed to communicate with agent {name}: {error_msg}"
    )

    # #1332: read the row state BEFORE closing activities so a row already
    # CANCELLED (operator terminate raced this HTTP error) closes as CANCELLED.
    existing = db.get_execution(task_execution_id) if task_execution_id else None
    http_close_state = (
        activity_state_for_terminal(existing.status) if existing else ActivityState.FAILED
    )
    http_close_error = error_msg if http_close_state == ActivityState.FAILED else None

    await activity_service.complete_activity(
        activity_id=chat_activity_id,
        status=http_close_state,
        error=http_close_error,
    )

    # #678: salvage cost/context from partial_metadata when the agent captured
    # them before the reader-thread race wedged its stream. The CANCELLED guard
    # mirrors task_execution_service.
    if task_execution_id and (not existing or existing.status != TaskExecutionStatus.CANCELLED):
        salvage_cost = partial_metadata.get("cost_usd") if partial_metadata else None
        salvage_context = _compute_context_used(partial_metadata) if partial_metadata else None
        salvage_context_max = (
            (partial_metadata.get("context_window") or DEFAULT_CONTEXT_WINDOW)
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

    if collaboration_activity_id:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            status=http_close_state,
            error=http_close_error,
        )

    await _apply_sub003_autoswitch(name, error_msg, agent_status_code)


async def run_chat_turn(
    *,
    name: str,
    request: ChatMessageRequest,
    current_user: User,
    x_source_agent: Optional[str],
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
    """Execute the chat against the agent and finalize (#1026 slice 3;
    **transitional** sync-chat applier, RD15).

    Dispatches to the agent server, then on success finalizes (persist + activity
    completion + terminal SUCCESS row + idempotency snapshot); on the agent-call
    error paths runs the budget / httpx+SUB-003 finalizers, which raise
    ``ChatDispatchError`` (mapped to HTTP by the router). The ``finally`` always
    releases the capacity slot and any still-in-flight idempotency claim.
    """
    idem_done = False
    try:
        payload = build_chat_payload(
            name=name, request=request, triggered_by=triggered_by,
            current_user=current_user, x_source_agent=x_source_agent,
            task_execution_id=task_execution_id,
        )
        start_time = datetime.utcnow()
        response = await agent_post_with_retry(
            name,
            "/api/chat",
            payload,
            max_retries=3,
            retry_delay=1.0,
            timeout=chat_timeout + 10  # Add buffer for HTTP overhead
        )
        response.raise_for_status()

        response_data = await _finalize_chat_success(
            name=name, response=response, start_time=start_time, session=session,
            current_user=current_user, chat_activity_id=chat_activity_id,
            collaboration_activity_id=collaboration_activity_id,
            task_execution_id=task_execution_id, _chat_subscription_id=_chat_subscription_id,
            execution=execution, queue_result=queue_result, is_queued=is_queued, idem=idem,
        )
        idem_done = True
        return response_data
    except BackendAgentCallBudgetExhausted as _budget_e:
        await _finalize_budget_exhausted(
            budget_exc=_budget_e, task_execution_id=task_execution_id,
            chat_activity_id=chat_activity_id, collaboration_activity_id=collaboration_activity_id,
        )
    except httpx.HTTPError as e:
        await _finalize_http_failure(
            name=name, e=e, task_execution_id=task_execution_id,
            chat_activity_id=chat_activity_id, collaboration_activity_id=collaboration_activity_id,
        )
    finally:
        # CAPACITY-CONSOLIDATE (#428): single release covers both the SlotService
        # counter and the in-memory overflow bookkeeping.
        await capacity.release(name, execution.id)
        # RELIABILITY-006 (#525): on any non-success exit, release the in-flight
        # idempotency claim so the caller can legitimately retry (no-op on the
        # success path, where complete() already finalized it).
        if not idem_done:
            idempotency_service.fail(idem)


# ===========================================================================
# /task dispatch lifecycle (#1483). NOTE: this service is NEVER a /task terminal
# applier — the sync-immediate and backlog paths delegate to
# task_execution_service.execute_task (the single applier). This section owns
# only the endpoint normalization + dispatch orchestration + chat-specific
# post-processing (RD1).
# ===========================================================================

_TaskDerivation = namedtuple(
    "_TaskDerivation", ["triggered_by", "is_self_task", "reserved_event_dispatch"]
)



_NO_INHERITED_CONTEXT = (None, None, None, None, None)


def _inherited_channel_context(request, *, current_user=None, x_source_agent=None) -> tuple:
    """ent#224/ent#265: resolve the originating channel/thread + binding agent
    from the CALLER's execution — consumed at ROW CREATION time (D0).

    When agent A (answering in Slack/Telegram) delegates to agent B, B's row
    would carry no channel context and B's completion would have nowhere to go —
    that is exactly the reported failure. If the caller passes its own
    ``parent_execution_id`` we copy the destination down onto the CHILD ROW
    ITSELF, so B's terminal can report into A's thread. (The former
    ``run_async_task`` → ``execute_task(source_channel=…)`` threading was dead:
    ``execute_task`` writes channel columns only in its no-``execution_id``
    creation branch, and the /task path always pre-creates the row.)

    The 4th element is ``source_channel_agent`` (ent#265 D1): the agent whose
    channel binding owns the context — the parent's own binding agent when set,
    else the parent's agent name. Transitive across A→B→C, so the completion
    always delivers through the bot the user actually addressed. Returned only
    when the parent actually HAS ``source_channel`` — never a dangling agent
    pointer on a channel-less child.

    Provenance guard (ent#265 security): ``db.get_execution(parent_id)`` is a
    global lookup with no ownership check, and inheriting the context hands the
    child's terminal an outbound destination (someone else's chat) plus the bot
    token that reaches it — so the caller must own the parent context:
      * **agent-scoped principal** (``current_user.agent_name``) must BE the
        parent's executing agent;
      * **human principal** must be the parent agent's OWNER (or an admin);
      * **connector principal** (consumption-only, ent#46) never inherits.
    A failed guard means NO inheritance (info log) — fail-open to no-context,
    never to someone else's chat.

    Two deliberate choices, both load-bearing:

    * **The arm is selected by the AUTHENTICATED PRINCIPAL, never by the raw
      ``X-Source-Agent`` header.** The SELF-EXEC-001 spoof guard in
      ``derive_source_and_trigger`` only fires when ``current_user.agent_name``
      is set, so for a human caller the header is unvalidated client input
      (``routers/chat.py`` documents the identical trap for the resume-session
      IDOR). Keying the agent arm off the header let any human satisfy it by
      naming the parent's own agent — the row itself tells you that name — which
      turned the human arm into a no-op. ``x_source_agent`` is therefore
      logged, never trusted.
    * **The human arm is owner-or-admin (``can_user_share_agent``), not any
      accessor.** Posting into a channel chat is a proactive-send capability,
      and every other proactive surface is owner-gated (`OwnedAgentByName` for
      group sends) or per-recipient-consented (#321). A share recipient can
      already read the owner's execution ids (`GET /api/executions` is
      accessor-scoped), so an accessor arm would let them route a report into
      the owner's Telegram DM or group.

    Fail-open: any miss returns (None, None, None, None) and behaviour is
    unchanged.
    """
    parent_id = getattr(request, "parent_execution_id", None)
    if not parent_id:
        return _NO_INHERITED_CONTEXT
    try:
        from database import db
        parent = db.get_execution(parent_id)
        if parent is None:
            return _NO_INHERITED_CONTEXT
        parent_agent = getattr(parent, "agent_name", None)
        # --- Provenance guard (principal-selected arms) -------------------
        if current_user is None:
            logger.info(
                "[ent#265] channel-context inheritance refused: no caller "
                "principal to evaluate against parent execution %s", parent_id,
            )
            return _NO_INHERITED_CONTEXT
        if getattr(current_user, "connector_agent", None):
            logger.info(
                "[ent#265] channel-context inheritance refused: connector keys "
                "are consumption-only (parent execution %s)", parent_id,
            )
            return _NO_INHERITED_CONTEXT
        agent_principal = getattr(current_user, "agent_name", None)
        if agent_principal:
            if parent_agent != agent_principal:
                logger.info(
                    "[ent#265] channel-context inheritance refused: parent "
                    "execution %s belongs to '%s', caller agent is '%s' "
                    "(header claimed '%s')",
                    parent_id, parent_agent, agent_principal, x_source_agent,
                )
                return _NO_INHERITED_CONTEXT
        elif not db.can_user_share_agent(current_user.username, parent_agent):
            logger.info(
                "[ent#265] channel-context inheritance refused: caller "
                "'%s' does not own parent agent '%s' (execution %s, header "
                "claimed '%s')",
                current_user.username, parent_agent, parent_id, x_source_agent,
            )
            return _NO_INHERITED_CONTEXT
        # --- The parent must be work that is STILL HAPPENING (ent#457 review) --
        #
        # Everything inherited below is read off `parent`, which the CALLER
        # names. So any check that compares two inherited values is a tautology:
        # the first version of this guard compared the inherited client against
        # the inherited session and could not fail, because both came from this
        # same row. That is the defect it was written to fix, one level up.
        #
        # The only thing here the caller does not choose is TIME. ent#265's
        # premise is that A delegates *during* a turn it is currently serving,
        # so a parent that has already finished is not that. Requiring `running`
        # removes "any historical execution of any client of this agent" from
        # the attack surface, which is what made the portal case reachable: an
        # agent shared with clients X and Y could cite any of X's past portal
        # turns while serving Y and file a report into X's thread.
        #
        # HONEST RESIDUAL: this does not make the portal leg airtight. If X has
        # a turn genuinely in flight at the same moment, A can still name it.
        # The window shrinks from "all history" to "a concurrent live turn",
        # which is a real narrowing and not a proof. Closing it properly needs
        # the child to learn its own client from something other than the
        # caller's argument — a trusted runtime injection of the executing
        # turn's identity, which is the #1084 `execution_id` work and is not
        # this change.
        parent_status = str(getattr(parent, "status", "") or "").lower()
        if parent_status != "running":
            logger.info(
                "[ent#457] channel-context inheritance refused: parent "
                "execution %s is '%s', not running — inheritance is for work "
                "delegated DURING a live turn, and an already-finished parent "
                "is how a past turn's destination gets reused",
                parent_id, parent_status or "unknown",
            )
            return _NO_INHERITED_CONTEXT

        src_channel = getattr(parent, "source_channel", None)
        if not src_channel:
            return _NO_INHERITED_CONTEXT
        return (
            src_channel,
            getattr(parent, "source_channel_chat_id", None),
            getattr(parent, "source_channel_thread", None),
            getattr(parent, "source_channel_agent", None) or parent_agent,
            # ent#457 review: the client the context belongs to, carried down
            # with it. The guard above establishes that the CALLER owns the
            # parent agent — it cannot establish which of that agent's clients
            # the work is for, and for the portal leg the destination is a
            # per-client thread. Falling back to the parent's own
            # `source_user_email` covers the root turn, whose row predates the
            # column being set by anything but the portal creation sites.
            getattr(parent, "source_channel_client", None)
            or getattr(parent, "source_user_email", None),
        )
    except Exception:  # noqa: BLE001 — never fail a dispatch over provenance
        return _NO_INHERITED_CONTEXT


async def run_async_task(
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
    triggered_by_override: Optional[str] = None,
):
    """
    Async /task background wrapper (issue #95).

    Delegates the full execution lifecycle to TaskExecutionService (single path
    for slot / activity / sanitization / retry / release) and layers on the
    chat-endpoint-specific post-task side effects:
      - authenticated chat_session persistence (THINK-001)
      - chat_response_ready WebSocket broadcast
      - collaboration activity completion (agent-to-agent call)

    Caller (the async branch) has already pre-acquired the capacity slot so that
    429-at-capacity is returned synchronously. The service releases the slot in
    its finally block.
    """
    start_time = datetime.utcnow()
    task_service = get_task_execution_service()
    # #1578: a reserved agent.task.* dispatch persists triggered_by="event" (the
    # recursion-break sentinel); every other async task keeps the derived value.
    triggered_by = triggered_by_override or ("agent" if x_source_agent else "manual")

    # Outer try/finally so a sync long-poll waiter (issue #498) is always
    # signaled even if the post-task side effects below raise.
    result = None
    chat_session_id = None
    # ent#265 (D0): inherited channel context is persisted at ROW CREATION
    # (create_task_execution_and_activities) — threading it into execute_task
    # here was dead code, because the pre-created execution_id skips the
    # creation branch that writes the channel columns.
    try:
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
        chat_session_id = await chat_persistence_service.persist_and_broadcast_chat_session(
            agent_name=agent_name, request=request, result=result,
            execution_id=execution_id, user_id=user_id, user_email=user_email,
            subscription_id=subscription_id, execution_time_ms=execution_time_ms,
        )
        await complete_collaboration_activity(
            collaboration_activity_id, result, execution_id, execution_time_ms,
        )
        await finalize_self_task(
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


async def complete_collaboration_activity(
    collaboration_activity_id, result, execution_id, execution_time_ms,
):
    """Post-task block 2: complete the agent-to-agent collaboration activity.
    No-op when there is no collaboration activity; self-isolating on error."""
    if not collaboration_activity_id:
        return
    try:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            # #1332: a cancelled collaboration turn reads as CANCELLED, not FAILED.
            status=activity_state_for_terminal(result.status),
            details={
                "response_length": len(result.response or ""),
                "execution_time_ms": execution_time_ms,
                "execution_id": execution_id,
            },
            error=(result.error if result.status == TaskExecutionStatus.FAILED else None),
        )
    except Exception as e:
        logger.warning(f"[Task Async] collaboration activity completion failed: {e}")


async def finalize_self_task(
    *, is_self_task, self_task_activity_id, agent_name, request, result,
    execution_id, user_id, user_email, execution_time_ms,
):
    """Post-task block 3 (SELF-EXEC-001): complete the self-task activity,
    inject the result into the originating chat session when requested, and
    broadcast the completion event. No-op unless this is a self-task. Kept WHOLE
    (RD9): its shared activity_status/result must not fracture across modules."""
    if not (is_self_task and self_task_activity_id):
        return

    # #1332: a cancelled self-task turn reads as CANCELLED, not FAILED.
    activity_status = activity_state_for_terminal(result.status)

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
            if session and session.user_id == user_id:
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
                # #1332: mirror the activity DB state (cancelled stays cancelled)
                # so the WS event and the persisted row never disagree.
                "activity_state": activity_status.value,
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


def derive_source_and_trigger(
    *, name, x_source_agent, x_via_mcp, x_event_trigger, x_internal_secret, current_user
) -> "_TaskDerivation":
    """SELF-EXEC-001 spoof guard (403), self-task detection, triggered_by
    derivation, and the #1578 reserved-event tag (internal-secret gated, C-003).
    HTTP-free — the spoof guard raises ChatDispatchError(403)."""
    # SELF-EXEC-001: verify X-Source-Agent matches the MCP key's agent scope.
    if x_source_agent and current_user.agent_name:
        if x_source_agent != current_user.agent_name:
            raise ChatDispatchError(
                403,
                f"Source agent header '{x_source_agent}' doesn't match API key scope '{current_user.agent_name}'",
            )

    is_self_task = (x_source_agent is not None and x_source_agent == name)

    if x_source_agent:
        triggered_by = "self_task" if is_self_task else "agent"
    elif x_via_mcp:
        triggered_by = "mcp"
    else:
        triggered_by = "manual"

    # #1578 recursion-break: honored ONLY with a valid backend-internal secret.
    reserved_event_dispatch = (
        x_event_trigger == RESERVED_EVENT_TRIGGER_HEADER_VALUE
        and verify_internal_dispatch_secret(x_internal_secret)
    )
    if reserved_event_dispatch:
        triggered_by = RESERVED_EVENT_TRIGGER

    return _TaskDerivation(
        triggered_by=triggered_by,
        is_self_task=is_self_task,
        reserved_event_dispatch=reserved_event_dispatch,
    )


async def process_task_file_uploads(*, request, name, container, current_user) -> list:
    """(#364) File upload processing before the async/sync fork. Mutates
    request.message with the file block; returns the decoded image data. Raises
    ChatDispatchError(502) when ALL writes fail — and, matching the original,
    does NOT touch the idempotency claim on that path (RD11; the caller has
    already begun the claim, which is intentionally left in place)."""
    image_data: list = []
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
        file_descs, _upload_dir, all_writes_failed, image_data = await process_file_uploads(
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
            raise ChatDispatchError(
                502, "File upload failed: could not write to agent workspace."
            )
        if file_descs:
            file_block = "\n".join(file_descs)
            request.message = f"{request.message}\n\n{file_block}"
    return image_data


async def create_task_execution_and_activities(
    *, request, name, current_user, x_source_agent,
    triggered_by, is_self_task, idem,
):
    """Create the execution record (#95/#96), attach the idempotency claim, and
    track the collaboration / self-task activity (mirrors the /chat pattern).
    Returns (execution_id, subscription_id, collaboration_activity_id,
    self_task_activity_id)."""
    # SUB-004: subscription snapshot at creation time (best-effort).
    try:
        subscription_id = db.get_agent_subscription_id(name)
    except Exception:
        subscription_id = None

    # ent#224/ent#265 (D0): resolve inherited channel context HERE, at the
    # single row-creation point both the async and sync /task branches route
    # through, and persist it on the row itself. The provenance guard evaluates
    # the AUTHENTICATED principal (current_user) — x_source_agent is passed for
    # logging only, because for a human caller it is unvalidated client input.
    src_channel, src_chat_id, src_thread, src_channel_agent, src_channel_client = _inherited_channel_context(
        request, current_user=current_user, x_source_agent=x_source_agent,
    )

    execution = db.create_task_execution(
        agent_name=name,
        message=request.message,
        triggered_by=triggered_by,
        source_user_id=current_user.id,
        source_user_email=current_user.email or current_user.username,
        source_agent_name=x_source_agent,
        source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
        source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
        model_used=request.model,
        subscription_id=subscription_id,
        source_channel=src_channel,
        source_channel_chat_id=src_chat_id,
        source_channel_thread=src_thread,
        source_channel_agent=src_channel_agent,
        source_channel_client=src_channel_client,
    )
    execution_id = execution.id if execution else None
    idempotency_service.attach_execution(idem, execution_id)

    collaboration_activity_id = None
    self_task_activity_id = None
    if x_source_agent:
        if is_self_task:
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
            await broadcast_collaboration_event(
                source_agent=x_source_agent,
                target_agent=name,
                action="parallel_task"
            )
            collaboration_activity_id = await activity_service.track_activity(
                agent_name=x_source_agent,  # Activity belongs to source agent (the caller)
                activity_type=ActivityType.AGENT_COLLABORATION,
                user_id=current_user.id,
                triggered_by="agent",
                related_execution_id=execution_id,
                details={
                    "source_agent": x_source_agent,
                    "target_agent": name,
                    "action": "parallel_task",
                    "message_preview": request.message[:100],
                    "execution_id": execution_id,
                    "parallel_mode": True
                }
            )

    return execution_id, subscription_id, collaboration_activity_id, self_task_activity_id


def _circuit_open_dispatch_error(name, execution_id, exc) -> ChatDispatchError:
    """Byte-identical mirror of routers.chat._raise_circuit_open_503, but returns
    the domain error instead of raising HTTP — closes the pre-created row FAILED
    (the /task path has a row) then builds the 503 + X-Circuit-Open/Retry-After.
    The router's _raise_circuit_open_503 is used ONLY on /chat (no row), so the
    FAILED-write is never double-written (RD-E12)."""
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
    return ChatDispatchError(
        503,
        {"error": "circuit_open", "retry_after_seconds": retry_after},
        headers={"X-Circuit-Open": "true", "Retry-After": str(retry_after)},
    )


def _ephemeral_dispatch_error(name, execution_id, exc) -> ChatDispatchError:
    """Byte-identical mirror of routers.chat._raise_ephemeral_exhausted_410 (with
    the /task row FAILED-write)."""
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
    return ChatDispatchError(
        410,
        {
            "error": f"Ephemeral agent '{name}' budget is spent ({exc.reason}); it is being discarded.",
            "code": "ephemeral_budget_exhausted",
        },
    )


async def _acquire_task_capacity(
    *, mode_label, name, request, execution_id, triggered_by, collaboration_activity_id,
    is_self_task, self_task_activity_id, user_id, user_email, subscription_id,
    x_source_agent, idem,
):
    """Pre-acquire the /task capacity slot (queue_persistent overflow) shared by
    the async and sync branches. On a deny it releases the idempotency claim and
    raises a ChatDispatchError (429 capacity-full / 503 circuit / 410 ephemeral).
    Returns (cap_result, effective_timeout)."""
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
                user_id=user_id,
                user_email=user_email,
                subscription_id=subscription_id,
                x_source_agent=x_source_agent,
                triggered_by=triggered_by,
                collaboration_activity_id=collaboration_activity_id,
                is_self_task=is_self_task,
                self_task_activity_id=self_task_activity_id,
            ),
        )
    except CapacityFull:
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
        raise ChatDispatchError(
            429,
            (
                f"Agent '{name}' is at capacity ({max_parallel_tasks} parallel tasks) "
                f"and its backlog is full. Try again later."
            ),
        )
    except CircuitOpen as e:
        # #526: dispatch breaker open — raised before the queue_persistent enqueue.
        logger.warning(f"[{mode_label}] Agent '{name}' dispatch circuit open, rejecting")
        idempotency_service.fail(idem)
        raise _circuit_open_dispatch_error(name, execution_id, e)
    except EphemeralBudgetExhausted as e:
        # trinity-enterprise#69: ghost budget spent — 410, no enqueue.
        idempotency_service.fail(idem)
        raise _ephemeral_dispatch_error(name, execution_id, e)

    return cap_result, effective_timeout


def _map_task_failure(name, result):
    """Shared /task failure translation (#679): a non-success terminal maps to
    429 (at-capacity) / 504 (timed out) / 503. Raises ChatDispatchError."""
    if result.status in ("failed", "cancelled"):
        if "at capacity" in (result.error or ""):
            raise ChatDispatchError(429, f"Agent '{name}' is at capacity. Try again later.")
        elif "timed out" in (result.error or ""):
            raise ChatDispatchError(504, result.error)
        else:
            raise ChatDispatchError(
                503, result.error or "Failed to execute task. The agent may be unavailable."
            )


async def _dispatch_async(
    *, request, name, current_user, execution_id, subscription_id,
    collaboration_activity_id, self_task_activity_id, is_self_task, triggered_by,
    reserved_event_dispatch, image_data, idem, x_source_agent,
):
    """Async branch (#95): pre-acquire capacity, then either report queued-202 or
    spawn the background task and report accepted-202."""
    cap_result, _effective_timeout = await _acquire_task_capacity(
        mode_label="Task Async", name=name, request=request, execution_id=execution_id,
        triggered_by=triggered_by, collaboration_activity_id=collaboration_activity_id,
        is_self_task=is_self_task, self_task_activity_id=self_task_activity_id,
        user_id=current_user.id, user_email=current_user.email or current_user.username,
        subscription_id=subscription_id, x_source_agent=x_source_agent, idem=idem,
    )

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

    # Issue #279: done callback surfaces unhandled BG task exceptions.
    def _on_task_done(task: asyncio.Task):
        if task.cancelled():
            logger.warning(f"[Task Async] Background task cancelled for agent '{name}', execution_id={execution_id}")
        elif exc := task.exception():
            logger.error(f"[Task Async] Unhandled exception in background task for agent '{name}', execution_id={execution_id}: {exc}")

    bg_task = asyncio.create_task(
        run_async_task(
            agent_name=name,
            request=request,
            execution_id=execution_id,
            collaboration_activity_id=collaboration_activity_id,
            x_source_agent=x_source_agent,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username,
            subscription_id=subscription_id,
            is_self_task=is_self_task,
            self_task_activity_id=self_task_activity_id,
            images=image_data,
            dispatch_gate_checked=True,  # #526: router already gated at acquire()
            triggered_by_override=(
                RESERVED_EVENT_TRIGGER if reserved_event_dispatch else None
            ),  # #1578 recursion-break
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


async def _dispatch_sync_backlog(*, name, execution_id, sync_effective_timeout, idem):
    """Sync backlog long-poll (#498): wait for the drain terminal, or reconstruct
    a minimal result from the row (non-drain terminal flip), translate failure,
    and build the response. Side effects were handled inside the drain — not repeated."""
    sync_wait_cap = 2 * sync_effective_timeout
    logger.info(
        f"[Task Sync] Agent '{name}' at capacity — execution {execution_id} "
        f"queued to backlog; long-polling up to {sync_wait_cap}s"
    )
    try:
        wait_payload = await wait_for_sync_terminal(execution_id, timeout=sync_wait_cap)
    except asyncio.TimeoutError:
        raise ChatDispatchError(
            504,
            (
                f"Sync task on agent '{name}' did not complete within "
                f"{sync_wait_cap}s. Execution {execution_id} may still be "
                f"running; poll GET /api/agents/{name}/executions/{execution_id}."
            ),
        )

    if wait_payload is not None and wait_payload.get("result") is not None:
        result = wait_payload["result"]
        sync_chat_session_id = wait_payload.get("chat_session_id")
    else:
        row = db.get_execution(execution_id)
        if row is None:
            raise ChatDispatchError(503, f"Execution {execution_id} disappeared while waiting")
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

    _map_task_failure(name, result)

    sync_response_data = result.raw_response or {}
    if sync_chat_session_id:
        sync_response_data["chat_session_id"] = sync_chat_session_id
    sync_response_data["task_execution_id"] = execution_id
    idempotency_service.complete(idem, execution_id, sync_response_data)
    return sync_response_data


async def _dispatch_sync_immediate(
    *, request, name, current_user, execution_id, subscription_id, triggered_by,
    collaboration_activity_id, image_data, idem, x_source_agent,
):
    """Sync immediate path (EXEC-024): delegate to the single applier
    (task_execution_service.execute_task), complete the collaboration activity,
    translate failure, persist to the chat session (#1444), and build the response."""
    task_execution_service = get_task_execution_service()
    result = await task_execution_service.execute_task(
        agent_name=name,
        message=request.message,
        triggered_by=triggered_by,
        source_user_id=current_user.id,
        source_user_email=current_user.email or current_user.username,
        source_agent_name=x_source_agent,
        source_mcp_key_id=getattr(current_user, "mcp_key_id", None),
        source_mcp_key_name=getattr(current_user, "mcp_key_name", None),
        model=request.model,
        timeout_seconds=request.timeout_seconds,  # TIMEOUT-001: None = use agent's config
        resume_session_id=request.resume_session_id,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        execution_id=execution_id,
        slot_already_held=True,  # Issue #498: router pre-acquired
        images=image_data,
        dispatch_gate_checked=True,  # #526: router already gated at acquire()
    )

    if collaboration_activity_id:
        await activity_service.complete_activity(
            activity_id=collaboration_activity_id,
            # #1332: a cancelled collaboration turn reads as CANCELLED, not FAILED.
            status=activity_state_for_terminal(result.status),
            details={
                "response_length": len(result.response),
                "execution_id": execution_id,
            },
            error=result.error if result.status == TaskExecutionStatus.FAILED else None,
        )

    _map_task_failure(name, result)

    response_data = result.raw_response

    # Persist to chat session if requested (#1444: guarded on SUCCESS).
    if request.save_to_session:
        chat_session_id = await chat_persistence_service.persist_chat_session(
            agent_name=name,
            request=request,
            result=result,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username,
            subscription_id=subscription_id,
        )
        if chat_session_id:
            response_data["chat_session_id"] = chat_session_id
        elif result.status == TaskExecutionStatus.SUCCESS:
            # Fail-loud (#1444): never 500 a completed, billed turn.
            response_data["chat_persist_failed"] = True

    response_data["task_execution_id"] = execution_id
    idempotency_service.complete(idem, execution_id, response_data)
    return response_data


async def _dispatch_sync(
    *, request, name, current_user, execution_id, subscription_id,
    collaboration_activity_id, self_task_activity_id, is_self_task, triggered_by,
    image_data, idem, x_source_agent,
):
    """Sync branch (#498): pre-acquire; on immediate admit run the single
    applier, else long-poll the backlog drain."""
    cap_result, sync_effective_timeout = await _acquire_task_capacity(
        mode_label="Task Sync", name=name, request=request, execution_id=execution_id,
        triggered_by=triggered_by, collaboration_activity_id=collaboration_activity_id,
        is_self_task=is_self_task, self_task_activity_id=self_task_activity_id,
        user_id=current_user.id, user_email=current_user.email or current_user.username,
        subscription_id=subscription_id, x_source_agent=x_source_agent, idem=idem,
    )

    if cap_result.state != "admitted":
        return await _dispatch_sync_backlog(
            name=name, execution_id=execution_id,
            sync_effective_timeout=sync_effective_timeout, idem=idem,
        )
    return await _dispatch_sync_immediate(
        request=request, name=name, current_user=current_user, execution_id=execution_id,
        subscription_id=subscription_id, triggered_by=triggered_by,
        collaboration_activity_id=collaboration_activity_id, image_data=image_data,
        idem=idem, x_source_agent=x_source_agent,
    )


async def dispatch_parallel_task(
    *, request, name, current_user, container, x_source_agent, x_via_mcp,
    idempotency_key, x_event_trigger, x_internal_secret,
):
    """The /task dispatch orchestrator (Invariant #1). Owns derive → idempotency
    (via dispatch_admission_service) → file upload → create-row+activities →
    async/sync fork. Returns a response dict OR a ChatAdmissionReplay (router maps
    to 200/409); raises ChatDispatchError for the HTTP error cases. The router
    keeps the resume-validation + #1068 timeout guards ahead of this call (their
    order + the redis-via-router #1068 helper are boundary concerns — RD10)."""
    derivation = derive_source_and_trigger(
        name=name, x_source_agent=x_source_agent, x_via_mcp=x_via_mcp,
        x_event_trigger=x_event_trigger, x_internal_secret=x_internal_secret,
        current_user=current_user,
    )

    # RELIABILITY-006 (#525): idempotency begin/replay (shared with /chat, RD2).
    idem, replay = dispatch_admission_service.begin_task_idempotency(
        name=name, idempotency_key=idempotency_key,
    )
    if replay is not None:
        await dispatch_admission_service.audit_idempotent_replay(
            name=name, endpoint=f"/api/agents/{name}/task", x_via_mcp=x_via_mcp,
            x_source_agent=x_source_agent, current_user=current_user,
            idempotency_key=idempotency_key, idem=idem,
        )
        return replay

    image_data = await process_task_file_uploads(
        request=request, name=name, container=container, current_user=current_user,
    )

    (
        execution_id, subscription_id, collaboration_activity_id, self_task_activity_id,
    ) = await create_task_execution_and_activities(
        request=request, name=name, current_user=current_user,
        x_source_agent=x_source_agent, triggered_by=derivation.triggered_by,
        is_self_task=derivation.is_self_task, idem=idem,
    )

    if request.async_mode:
        return await _dispatch_async(
            request=request, name=name, current_user=current_user, execution_id=execution_id,
            subscription_id=subscription_id, collaboration_activity_id=collaboration_activity_id,
            self_task_activity_id=self_task_activity_id, is_self_task=derivation.is_self_task,
            triggered_by=derivation.triggered_by, reserved_event_dispatch=derivation.reserved_event_dispatch,
            image_data=image_data, idem=idem, x_source_agent=x_source_agent,
        )
    return await _dispatch_sync(
        request=request, name=name, current_user=current_user, execution_id=execution_id,
        subscription_id=subscription_id, collaboration_activity_id=collaboration_activity_id,
        self_task_activity_id=self_task_activity_id, is_self_task=derivation.is_self_task,
        triggered_by=derivation.triggered_by, image_data=image_data, idem=idem,
        x_source_agent=x_source_agent,
    )


# ===========================================================================
# Execution termination (#1483, RD5). Decomposed off the CC-22 router handler.
# The agent-proxy stays inline (NOT task_execution_service.terminate_execution_on_agent,
# which returns a bool and swallows connect/timeout — reusing it would drop the
# router's 502/504/404 surfacing, a behavior change). HTTP-free: raises
# ChatDispatchError, mapped 1:1 by the thin router handler.
# ===========================================================================


async def _cancel_queued_if_queued(name, execution_id, task_execution_id, current_user,
                                  actor_kind="operator"):
    """BACKLOG-001: if the execution is still queued in the backlog, cancel it
    directly (no container interaction, no slot to release) and return the
    cancelled-while-queued payload. Returns None if not queued (fall through to
    the normal terminate path) OR if it transitioned out of queued between the
    read and the update."""
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
                user_id=getattr(current_user, "id", None),
                triggered_by="user",
                related_execution_id=task_execution_id,
                details={
                    "execution_id": execution_id,
                    "task_execution_id": task_execution_id,
                    "status": "cancelled_while_queued",
                    # ent#155: a public-link visitor and a Workspace client are
                    # both real people cancelling their own turn, and neither
                    # has a `users` row — so `user_id` is legitimately NULL and
                    # this is what says who acted.
                    "actor_kind": actor_kind,
                },
            )
            return {"status": "cancelled_while_queued", "execution_id": execution_id}
    return None


async def _close_dispatch_activity_cancelled(task_execution_id, cancel_won):
    """#1332 (Path B): close the still-open dispatch activity as CANCELLED
    immediately (operator-terminate writes the CANCELLED row first and never
    reaches apply_result). Gate on the CAS result so the activity never disagrees
    with the row. Best-effort — a close failure never fails the terminate.

    #1804: folded onto the shared ``close_execution_activity`` owner — this was
    the fifth hand-rolled copy of the lookup-then-close idiom, and a hand-rolled
    copy is invisible to the parity guard. Behaviour is unchanged: the terminal
    it passes is CANCELLED on a won CAS and the row's REAL terminal on a lost
    one, the helper applies the same ``activity_state_for_terminal`` mapping,
    and the error is still dropped for a COMPLETED close.
    """
    try:
        if cancel_won:
            close_status = TaskExecutionStatus.CANCELLED
        else:
            reconciled = db.get_execution(task_execution_id)
            close_status = (
                reconciled.status if reconciled else TaskExecutionStatus.CANCELLED
            )
        close_error = (
            None if close_status == TaskExecutionStatus.SUCCESS
            else "Execution terminated by user"
        )
        await activity_service.close_execution_activity(
            task_execution_id, close_status, error=close_error
        )
    except Exception as e:
        # The helper is already fail-open; this keeps the reconcile read
        # (db.get_execution) inside the same guarantee.
        logger.warning(
            f"[Terminate] Failed to close dispatch activity for "
            f"{task_execution_id} as cancelled: {e}"
        )


async def _proxy_terminate_and_finalize(name, execution_id, task_execution_id, current_user,
                                       actor_kind="operator"):
    """Proxy the terminate to the agent container, force-release capacity on a
    terminal outcome, write the #679 CANCELLED CAS (only when we actually
    terminated a running turn), close the #1332 dispatch activity, and track the
    termination activity. Raises ChatDispatchError (404/non-200/502/504)."""
    try:
        async with agent_httpx_client(name, timeout=15.0) as client:
            response = await client.post(
                f"http://agent-{name}:8000/api/executions/{execution_id}/terminate"
            )

        result = response.json()

        if response.status_code == 404:
            raise ChatDispatchError(404, "Execution not found in agent")
        if response.status_code != 200:
            raise ChatDispatchError(
                response.status_code, result.get("detail", "Termination failed")
            )

        # Release capacity for THIS execution (CAPACITY-CONSOLIDATE #428).
        #
        # ent#155 review (N1): this was `force_release(name)`, documented in
        # `capacity_manager` as "Emergency: clear all running slots and the
        # in-memory queue" — it DELs `agent:slots:{name}` wholesale, every
        # per-slot metadata key, and the overflow LIST. That was tolerable while
        # the only caller was an operator terminating from Agent Detail. ent#155
        # gives the same code path to a public-link visitor and a Workspace
        # client, so on an agent with `max_parallel_tasks > 1` one person
        # stopping THEIR OWN turn dropped slot accounting for every other
        # in-flight execution on that agent and discarded the queued overflow.
        #
        # `release_if_matches` is the per-execution form, and TOCTOU-safe: the
        # ZSET model is keyed by execution_id, so it is a no-op if this turn no
        # longer holds the slot. Nothing else's accounting is touched.
        #
        # `already_finished` no longer releases at all: nothing was cancelled,
        # the agent's own terminal already ran its own release, and firing an
        # emergency clear on a no-op branch was the part with no defensible
        # reading at all.
        if result.get("status") == "terminated" and task_execution_id:
            try:
                capacity = get_capacity_manager()
                released = await capacity.release_if_matches(name, task_execution_id)
                logger.info(
                    "[Terminate] Released capacity for execution %s on '%s' (released=%s)",
                    task_execution_id, name, released,
                )
            except Exception as e:
                logger.warning(f"[Terminate] Failed to release capacity for {name}: {e}")

            # #679: write CANCELLED only when we actually terminated a running
            # turn. On `already_finished` the agent's genuine terminal already
            # stands — leave it. Capacity is force-released in both cases.
            if task_execution_id and result.get("status") == "terminated":
                cancel_won = db.update_execution_status(
                    execution_id=task_execution_id,
                    status=TaskExecutionStatus.CANCELLED,
                    error="Execution terminated by user"
                )
                if cancel_won:
                    logger.info(f"[Terminate] Updated database execution {task_execution_id} to cancelled")
                else:
                    logger.info(
                        f"[Terminate] CANCELLED write for {task_execution_id} lost the CAS — "
                        f"row already terminal; leaving it and closing the activity in its real state"
                    )
                await _close_dispatch_activity_cancelled(task_execution_id, cancel_won)

        await activity_service.track_activity(
            agent_name=name,
            activity_type=ActivityType.EXECUTION_CANCELLED,
            user_id=getattr(current_user, "id", None),
            triggered_by="user",
            related_execution_id=task_execution_id,
            details={
                "execution_id": execution_id,
                "task_execution_id": task_execution_id,
                "status": result.get("status"),
                "returncode": result.get("returncode"),
                "actor_kind": actor_kind,
            }
        )
        return result

    except httpx.ConnectError:
        raise ChatDispatchError(502, f"Failed to connect to agent '{name}'")
    except httpx.TimeoutException:
        raise ChatDispatchError(504, f"Timeout connecting to agent '{name}'")


async def terminate_execution(*, name, execution_id, task_execution_id, current_user=None,
                              actor_kind="operator"):
    """Terminate a running execution: cancel-if-queued (BACKLOG-001), else
    container-gate then proxy-terminate + finalize. Returns the result dict
    (or the cancelled-while-queued payload); raises ChatDispatchError.

    ent#155: `current_user` is OPTIONAL because the cancel trigger is no longer
    operator-only. A public-link visitor and a Workspace client are both people
    stopping a turn they themselves started, and neither has a `users` row — so
    the caller-identity gate belongs to the ROUTE (the public link token, or the
    portal roster + started-by-this-caller check), and this function only needs
    to know that someone authorised got here. It records `actor_kind` on the
    activity so a NULL `user_id` is legible rather than mysterious.

    The cancel SEMANTICS are unchanged and deliberately so: CANCELLED, not
    FAILED (#679/#1332), neutral for the dispatch breaker, and CAS-guarded — a
    cancel that lands after the row is already terminal loses and leaves the
    real terminal alone.
    """
    queued = await _cancel_queued_if_queued(name, execution_id, task_execution_id, current_user,
                                            actor_kind=actor_kind)
    if queued is not None:
        return queued

    container = get_agent_container(name)
    if not container:
        raise ChatDispatchError(404, "Agent not found")
    if container.status != "running":
        raise ChatDispatchError(503, "Agent is not running")

    return await _proxy_terminate_and_finalize(name, execution_id, task_execution_id, current_user,
                                               actor_kind=actor_kind)
