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
import httpx
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException  # only for the defensive SUB-003 re-raise

from models import (
    User,
    ChatMessageRequest,
    ActivityType,
    ActivityState,
    TaskExecutionStatus,
    activity_state_for_terminal,
)
from database import db
from services.activity_service import activity_service
from services.agent_call_limiter import BackendAgentCallBudgetExhausted
from services.model_context import DEFAULT_CONTEXT_WINDOW
from services.task_execution_service import (
    _compute_context_used,
    agent_post_with_retry,
)
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    get_platform_system_prompt,
    is_execution_context_enabled,
)
from services import idempotency_service
from services.chat_signals import ChatExecutionContext, ChatDispatchError
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


def build_chat_payload(
    *, name: str, request: ChatMessageRequest, triggered_by: str, current_user: User,
    x_source_agent: Optional[str], x_mcp_key_name: Optional[str], task_execution_id: object,
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
            x_mcp_key_name=x_mcp_key_name, task_execution_id=task_execution_id,
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
