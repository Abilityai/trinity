"""
Task Execution Service — Unified execution path for all task callers (EXEC-024).

Extracts execution orchestration from routers/chat.py into a shared service so
that all callers (authenticated tasks, public link chat, scheduled executions)
use a single code path for execution tracking, activity tracking, slot management,
and response processing.

Lifecycle:
    1. create execution record
    2. acquire capacity slot
    3. track activity start
    4. call agent (with retry)
    5. sanitize + persist result
    6. track activity completion
    7. release slot (finally)
"""

import asyncio
import json
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import httpx

from database import db
from models import ActivityState, ActivityType, TaskExecutionStatus
from services.activity_service import activity_service
from services.agent_call_limiter import (
    BackendAgentCallBudgetExhausted,
    acquire_agent_call_slot,
)
from services.agent_client import CircuitState
from services.capacity_manager import CapacityFull, CircuitOpen, get_capacity_manager
from services.dispatch_breaker import DispatchBreaker
from services.platform_audit_service import AuditEventType, platform_audit_service
from services.settings_service import settings_service
from utils.credential_sanitizer import sanitize_dict, sanitize_execution_log, sanitize_response, sanitize_text
from services.platform_prompt_service import (
    ExecutionContext,
    compose_system_prompt,
    get_platform_system_prompt,
    is_execution_context_enabled,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TaskExecutionErrorCode(str, Enum):
    """Machine-readable error codes for task execution failures.

    Used by callers (message router, chat, etc.) to match error types
    without parsing human-readable error strings.
    """
    TIMEOUT = "timeout"              # Execution exceeded timeout_seconds
    CAPACITY = "capacity"            # All parallel slots in use
    AUTH = "auth"                    # No API key or subscription configured
    BILLING = "billing"             # Rate limit, credit, or billing issue
    AGENT_ERROR = "agent_error"     # Agent returned non-zero exit code
    NETWORK = "network"             # HTTP/connection error to agent container
    CIRCUIT_OPEN = "circuit_open"   # Circuit breaker open — agent known unhealthy (#767)


@dataclass
class TaskExecutionResult:
    """Result of a task execution."""
    execution_id: str
    status: str                         # TaskExecutionStatus value
    response: str                       # Sanitized response text
    cost: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    session_id: Optional[str] = None    # Claude Code session ID
    execution_log: Optional[str] = None # Sanitized JSON transcript
    raw_response: dict = field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[TaskExecutionErrorCode] = None


# ---------------------------------------------------------------------------
# Context-window helper (shared between success and HTTPError salvage paths)
# ---------------------------------------------------------------------------


def _compute_context_used(metadata: dict) -> Optional[int]:
    """Derive context-window pressure (tokens used) from an
    ``ExecutionMetadata.model_dump()`` dict.

    Mirrors the success-path logic at the original call site: cache_read
    + cache_creation is the stable signal (monotonic across a resumed
    session), fall back to input_tokens when caching isn't engaged.
    Returns None when no token signal is present.

    Shared between the success path and the #678 HTTPError salvage so
    both compute context_used the same way.
    """
    if not metadata:
        return None
    cache_read = metadata.get("cache_read_tokens") or 0
    cache_create = metadata.get("cache_creation_tokens") or 0
    if cache_read + cache_create > 0:
        return cache_read + cache_create
    input_tokens = metadata.get("input_tokens") or 0
    return input_tokens if input_tokens > 0 else None


# ---------------------------------------------------------------------------
# Reader-race signature (Issue #678 auto-retry)
# ---------------------------------------------------------------------------

# Conservative gating: only retry when the original turn was cheap and
# the agent-server's classifier marked it as a reader-race (not a real
# claude failure). num_turns < 5 keeps a 24-min execution like the
# original #678 from being silently re-burned.
_AUTO_RETRY_MAX_TURNS = 5

# The retry must not silently double the operator's timeout budget. Reader
# races fire fast; 5 min is plenty. We pass `min(effective_timeout, this)`
# to the retry so a 30-min task that ate 28 min before failing doesn't get
# another 30 min on top.
_AUTO_RETRY_MAX_TIMEOUT_S = 300.0


def _is_reader_race_signature(detail) -> bool:
    """True when a 502 detail body matches the stdout reader-race
    signature and the original turn was cheap enough to retry.

    The structured body comes from
    ``error_classifier._classify_empty_result`` (Issue #678):

        {
            "message": "Execution completed without a result message ...",
            "metadata": {...},
            "raw_message_count": N,
            "parse_failure_count": N,
            "recovery_attempted": True,
        }

    Gating: raw_message_count == 0 (reader thread emitted nothing —
    distinct from a partial stream), num_turns < 5 (cheap to retry),
    parse_failure_count == 0 (no wire corruption).
    """
    if not isinstance(detail, dict):
        return False
    if not detail.get("recovery_attempted"):
        return False
    if detail.get("raw_message_count", 0) != 0:
        return False
    if detail.get("parse_failure_count", 0) != 0:
        return False
    meta = detail.get("metadata") or {}
    num_turns = meta.get("num_turns") or 0
    if num_turns >= _AUTO_RETRY_MAX_TURNS:
        return False
    msg = (detail.get("message") or "").lower()
    return "result message" in msg


# ---------------------------------------------------------------------------
# Agent HTTP helper (moved from routers/chat.py)
# ---------------------------------------------------------------------------

async def agent_post_with_retry(
    agent_name: str,
    endpoint: str,
    payload: dict,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    timeout: float = 600.0,
) -> httpx.Response:
    """
    POST to an agent container with exponential-backoff retry.

    Handles the case where a container is running but its internal HTTP
    server is not yet ready.

    #904 RC-1: gated by the backend agent-call semaphore in
    ``services.agent_call_limiter`` — limits concurrent outbound calls
    per agent (to the agent's ``max_parallel_tasks``) and globally (to
    ``BACKEND_AGENT_CALL_LIMIT``, default 8). Prevents one misbehaving
    agent's long-running HTTP call from saturating the backend's event
    loop and stalling the dashboard / healthcheck. The gate wraps each
    connect-retry attempt independently — a `httpx.ConnectError` that
    triggers a retry briefly releases the slot so other callers aren't
    blocked while we sleep before the next attempt.
    """
    agent_url = f"http://agent-{agent_name}:8000{endpoint}"

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            async with acquire_agent_call_slot(agent_name):
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(agent_url, json=payload)
                    return response
        except BackendAgentCallBudgetExhausted:
            # Translate to a synthetic 503 ``httpx.HTTPStatusError`` so
            # the caller's existing `httpx.HTTPError` except branch
            # handles slot release, execution-row FAILED write, and
            # SUB-003 short-circuit (the new error string contains the
            # SIGKILL/OOM markers added by #907, so `is_auth_failure`
            # rejects it). Carrying the exception detail through a
            # `Request`-less synthetic Response keeps the caller's
            # status-code branching code path identical.
            raise
        except httpx.ConnectError as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt)
                logger.debug(
                    f"Agent {agent_name} connection failed (attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    f"Agent {agent_name} connection failed after {max_retries} attempts: {e}"
                )

    raise last_error or httpx.ConnectError(f"Failed to connect to agent {agent_name}")


# ---------------------------------------------------------------------------
# Terminate helper (Issue #61)
# ---------------------------------------------------------------------------

TERMINATE_TIMEOUT = 5.0  # Short timeout for terminate call — don't block failure path


async def terminate_execution_on_agent(
    agent_name: str,
    execution_id: str,
) -> bool:
    """
    Terminate an execution on an agent container (Issue #61).

    Calls POST /api/executions/{id}/terminate on the agent to kill the
    running Claude process. This prevents orphaned processes from
    accumulating when the backend times out waiting for a response.

    Best-effort: failures are logged but don't raise exceptions.
    The cleanup service watchdog provides a safety net.

    Args:
        agent_name: The agent container name.
        execution_id: The execution to terminate.

    Returns:
        True if termination succeeded or process already finished,
        False if termination failed (agent unreachable, etc.).
    """
    if not execution_id:
        return False

    agent_url = f"http://agent-{agent_name}:8000/api/executions/{execution_id}/terminate"

    try:
        async with httpx.AsyncClient(timeout=TERMINATE_TIMEOUT) as client:
            response = await client.post(agent_url)

            if response.status_code < 300:
                result = response.json()
                status = result.get("status", "unknown")
                if status == "terminated":
                    logger.info(
                        f"[TaskExecService] Terminated execution {execution_id} "
                        f"on agent '{agent_name}'"
                    )
                elif status == "already_finished":
                    logger.debug(
                        f"[TaskExecService] Execution {execution_id} already finished "
                        f"on agent '{agent_name}'"
                    )
                return True

            elif response.status_code == 404:
                # Execution not found in agent's registry — may have finished
                # between timeout and terminate call
                logger.debug(
                    f"[TaskExecService] Execution {execution_id} not found on "
                    f"agent '{agent_name}' (may have finished)"
                )
                return True

            else:
                logger.warning(
                    f"[TaskExecService] Terminate returned {response.status_code} "
                    f"for execution {execution_id} on agent '{agent_name}'"
                )
                return False

    except httpx.TimeoutException:
        logger.warning(
            f"[TaskExecService] Terminate timed out for execution {execution_id} "
            f"on agent '{agent_name}' — watchdog will clean up"
        )
        return False

    except httpx.ConnectError:
        logger.warning(
            f"[TaskExecService] Could not reach agent '{agent_name}' to terminate "
            f"execution {execution_id} — watchdog will clean up"
        )
        return False

    except Exception as e:
        logger.warning(
            f"[TaskExecService] Error terminating execution {execution_id} "
            f"on agent '{agent_name}': {e}"
        )
        return False


# ---------------------------------------------------------------------------
# Dispatch circuit breaker helpers (#526, RELIABILITY-007)
# ---------------------------------------------------------------------------


def dispatch_breaker_active(agent_name: str) -> bool:
    """Combined dispatch-breaker gate: global master switch AND per-agent opt-in.

    Single source of truth for "is the dispatch breaker engaged for this agent?",
    shared by the routers (chat / task) and ``execute_task``. The global
    ``DISPATCH_BREAKER_ENABLED`` master switch is checked first, so when the
    feature is off fleet-wide the per-agent ``circuit_breaker_enabled`` SELECT is
    short-circuited and a disabled fleet pays nothing on the dispatch hot path
    (#526 D7). Fail-safe → False; never raises.
    """
    try:
        from config import DISPATCH_BREAKER_ENABLED
        if not DISPATCH_BREAKER_ENABLED:
            return False
        return bool(db.get_circuit_breaker_enabled(agent_name))
    except Exception:
        return False


# Strong references to fire-and-forget breaker tasks. asyncio's event loop holds
# only a WEAK reference to a bare ``create_task`` result, so an un-referenced task
# can be garbage-collected mid-flight (the backlog drain would silently vanish).
# Holding the task here until it completes closes that window; the done-callback
# discards it so the set never grows unbounded.
_background_breaker_tasks: "set[asyncio.Task[Any]]" = set()


def _spawn_bg(coro: "Coroutine[Any, Any, None]") -> None:
    """Schedule a fire-and-forget breaker task with a strong reference held until
    it finishes — prevents the asyncio weak-ref GC footgun (#526)."""
    task = asyncio.create_task(coro)
    _background_breaker_tasks.add(task)
    task.add_done_callback(_background_breaker_tasks.discard)


async def _audit_circuit_transition(agent_name: str, transition: str) -> None:
    """Audit a dispatch-breaker state transition (open / closed). Best-effort."""
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action=f"circuit_breaker_{transition}",
            source="system",
            target_type="agent",
            target_id=agent_name,
            details={"breaker": "dispatch", "transition": transition},
        )
    except Exception as e:
        logger.warning(
            "[DispatchBreaker] audit %s for %s failed: %s", transition, agent_name, e
        )


async def _fail_backlog_and_audit(agent_name: str) -> None:
    """Drain-on-trip (#526 D3): on the breaker →open transition, fail the doomed
    persistent backlog, clear the in-memory overflow, and audit the transition.

    Best-effort and never raises — backgrounded by the caller via ``_spawn_bg``
    (a strong reference is held so the task can't be GC'd mid-flight). If this
    task is still lost or ``fail_queued_for_agent`` throws, the breaker-aware
    sweep in ``CapacityManager.run_maintenance`` (60s loop) re-fails the queued
    backlog for any agent whose dispatch breaker is still open — so the worst
    case is a ~60s delay, not the 24h generic ``expire_stale`` window.
    """
    try:
        failed = db.fail_queued_for_agent(
            agent_name, reason="circuit_open: dispatch breaker open"
        )
        if failed:
            logger.warning(
                "[DispatchBreaker] failed %d queued backlog row(s) for %s on circuit open",
                failed,
                agent_name,
            )
    except Exception as e:
        logger.error(
            "[DispatchBreaker] fail_queued_for_agent(%s) failed: %s", agent_name, e
        )
    try:
        await get_capacity_manager().clear_in_memory_queue(agent_name)
    except Exception as e:
        logger.warning(
            "[DispatchBreaker] clear_in_memory_queue(%s) failed: %s", agent_name, e
        )
    await _audit_circuit_transition(agent_name, "open")


async def _record_dispatch_terminal(
    agent_name: str, breaker_enabled: bool, error_code: Optional["TaskExecutionErrorCode"]
) -> None:
    """Record a dispatch-breaker outcome at an execution terminal (#526 D10).

    ``error_code``: ``None`` at a SUCCESS terminal; ``TaskExecutionErrorCode.AUTH``
    at the auth-failure terminal. Callers MUST NOT pass a non-AUTH failure's
    ``None`` here (it would read as a success and reset the counter — see
    ``DispatchBreaker.record_outcome``); the AUTH terminal gates on
    ``error_code == AUTH`` before calling.

    On the →open transition the backlog drain + audit are backgrounded so a slow
    Redis/DB write never blocks the response. Best-effort; never raises.
    """
    if not breaker_enabled:
        return
    try:
        t = DispatchBreaker(agent_name).record_outcome(error_code)
    except Exception as e:  # pragma: no cover - fail-open is internal to the breaker
        logger.warning("[DispatchBreaker] record_outcome(%s) failed: %s", agent_name, e)
        return
    if t.opened:
        _spawn_bg(_fail_backlog_and_audit(agent_name))
    elif t.closed:
        _spawn_bg(_audit_circuit_transition(agent_name, "closed"))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TaskExecutionService:
    """
    Stateless service encapsulating the full task-execution lifecycle.

    All callers (authenticated /task, public chat, scheduler) delegate here
    so that execution tracking, slot management, activity tracking, and
    credential sanitisation are applied consistently.
    """

    async def execute_task(
        self,
        agent_name: str,
        message: str,
        triggered_by: str,                      # "manual"|"public"|"schedule"|"agent"|"mcp"|"fan_out"
        source_user_id: Optional[int] = None,
        source_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        allowed_tools: Optional[list] = None,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
        fan_out_id: Optional[str] = None,
        loop_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        parent_activity_id: Optional[str] = None,
        extra_activity_details: Optional[dict] = None,
        slot_already_held: bool = False,
        schedule_context: Optional[dict] = None,
        attempt: Optional[int] = None,
        images: Optional[list] = None,
        dispatch_gate_checked: bool = False,
    ) -> TaskExecutionResult:
        """
        Execute a task on an agent container with full lifecycle management.

        If *execution_id* is provided the caller has already created the
        execution record (e.g. the authenticated /task endpoint creates it
        early for async-mode support). Otherwise a new record is created here.

        Args:
            timeout_seconds: Execution timeout. If None, uses agent's configured
                timeout (TIMEOUT-001). Default agent timeout is 900s (15 minutes).

        Returns a :class:`TaskExecutionResult` on both success and failure
        (never raises for agent-level errors — callers inspect ``result.status``).

        Raises:
            HTTPException-style errors are intentionally **not** raised here;
            callers are responsible for translating ``result.status == "failed"``
            into the appropriate HTTP response.
        """
        capacity = get_capacity_manager()
        activity_id: Optional[str] = None
        # If caller already acquired the slot (async /task path preserves 429-upfront
        # contract by pre-flighting capacity), we still own releasing it in finally.
        slot_acquired = slot_already_held

        # TIMEOUT-001: Use agent's configured timeout if not explicitly provided
        if timeout_seconds is None:
            timeout_seconds = db.get_execution_timeout(agent_name)

        # #831: Resolve null model → platform default so the agent always receives
        # a concrete model string. Avoids the stale "sonnet" hardcode in base-image.
        if model is None:
            model = settings_service.get_platform_default_model()

        # Dispatch circuit breaker (#526): combined global master-switch AND
        # per-agent opt-in. Drives the acquire() gate (this path), the
        # slot_already_held drain-path guard at 3b, and outcome recording at the
        # terminals. Best-effort read; defaults off so disabled agents pay nothing.
        breaker_enabled = dispatch_breaker_active(agent_name)

        # ---- 1. Create execution record (if not provided) ----------------
        if not execution_id:
            # Snapshot subscription at record time (best-effort) for usage tracking (SUB-004)
            _exec_sub_id = subscription_id
            if _exec_sub_id is None:
                try:
                    _exec_sub_id = db.get_agent_subscription_id(agent_name)
                except Exception:
                    _exec_sub_id = None
            execution = db.create_task_execution(
                agent_name=agent_name,
                message=message,
                triggered_by=triggered_by,
                source_user_id=source_user_id,
                source_user_email=source_user_email,
                source_agent_name=source_agent_name,
                source_mcp_key_id=source_mcp_key_id,
                source_mcp_key_name=source_mcp_key_name,
                model_used=model,
                fan_out_id=fan_out_id,
                loop_id=loop_id,
                subscription_id=_exec_sub_id,
            )
            execution_id = execution.id if execution else None

        start_time = datetime.utcnow()

        # Wrap entire execution flow to ensure execution status is updated on any failure.
        # This fixes issue #90 where exceptions during slot acquisition left executions
        # stuck in 'running' status with NULL session_id and duration_ms.
        try:
            # ---- 2. Acquire capacity slot ------------------------------------
            # CAPACITY-CONSOLIDATE (#428): policy=reject preserves prior
            # behaviour — TaskExecutionService is invoked when the caller
            # already decided this execution is admitted (router pre-acquires)
            # OR is invoked from internal contexts where overflow isn't wanted
            # (scheduler, fan-out). In both cases we want a hard rejection on
            # capacity, not a backlog spill.
            if not slot_already_held:
                max_parallel_tasks = db.get_max_parallel_tasks(agent_name)
                try:
                    cap_result = await capacity.acquire(
                        agent_name=agent_name,
                        execution_id=execution_id or f"temp-{datetime.utcnow().timestamp()}",
                        max_concurrent=max_parallel_tasks,
                        message_preview=message[:100] if message else "",
                        timeout_seconds=timeout_seconds,
                        overflow_policy="reject",
                        breaker_enabled=breaker_enabled,
                    )
                    slot_acquired = cap_result.state == "admitted"
                except CapacityFull:
                    error_msg = (
                        f"Agent at capacity ({max_parallel_tasks}/{max_parallel_tasks} "
                        f"parallel tasks running)"
                    )
                    if execution_id:
                        db.update_execution_status(
                            execution_id=execution_id,
                            status=TaskExecutionStatus.FAILED,
                            error=error_msg,
                        )
                    return TaskExecutionResult(
                        execution_id=execution_id or "",
                        status=TaskExecutionStatus.FAILED,
                        response="",
                        error=error_msg,
                    )
                except CircuitOpen as e:
                    # #526: dispatch breaker open — fast-fail before any agent
                    # call. The slot was never acquired and nothing was enqueued
                    # (acquire raised before the overflow branch). Close the row
                    # FAILED(circuit_open) so it reads as a failed execution.
                    error_msg = "circuit_open: agent unhealthy (dispatch breaker open)"
                    logger.warning(
                        f"[TaskExecService] Dispatch breaker OPEN for {agent_name}; "
                        f"fast-failing execution {execution_id} "
                        f"(retry_after={e.retry_after_seconds}s)"
                    )
                    if execution_id:
                        db.update_execution_status(
                            execution_id=execution_id,
                            status=TaskExecutionStatus.FAILED,
                            error=error_msg,
                        )
                    return TaskExecutionResult(
                        execution_id=execution_id or "",
                        status=TaskExecutionStatus.FAILED,
                        response="",
                        error=error_msg,
                        error_code=TaskExecutionErrorCode.CIRCUIT_OPEN,
                    )

            # ---- 3. Track activity start -------------------------------------
            activity_details = {
                "message_preview": message[:100] if message else "",
                "source_agent": source_agent_name,
                "execution_id": execution_id,
                "triggered_by": triggered_by,
            }
            if extra_activity_details:
                activity_details.update(extra_activity_details)
            try:
                activity_id = await activity_service.track_activity(
                    agent_name=agent_name,
                    activity_type=ActivityType.CHAT_START,
                    user_id=source_user_id,
                    triggered_by=triggered_by,
                    parent_activity_id=parent_activity_id,
                    related_execution_id=execution_id,
                    details=activity_details,
                )
            except Exception as e:
                logger.warning(f"[TaskExecService] Failed to track activity start: {e}")
            # ---- 3b. Circuit breaker fast-fail ------------------------------
            # Check the per-agent circuit breakers before marking dispatched.
            # If a CB is open the agent is known-unhealthy; close the record
            # immediately rather than letting it hang until cleanup (120 min).
            #
            # Transport breaker (#631): always consulted.
            # Dispatch breaker (#526 D2): consulted ONLY on the slot_already_held
            # DRAIN path where no upstream dispatch gate ran (the
            # not-slot_already_held path already gated at acquire(); router
            # pre-acquire sets dispatch_gate_checked=True). A pure state read —
            # NOT allow_dispatch() — so it never consumes the half-open probe and
            # cannot block a probe an upstream gate already admitted.
            circuit = CircuitState(agent_name)
            transport_open = not circuit.allow_request()
            dispatch_open = False
            if breaker_enabled and slot_already_held and not dispatch_gate_checked:
                dispatch_open = (
                    DispatchBreaker(agent_name).to_dict().get("state") == "open"
                )
            if transport_open or dispatch_open:
                error_msg = "Agent circuit breaker open — agent is unhealthy"
                logger.warning(f"[TaskExecService] CB open, fast-failing execution {execution_id} for {agent_name}")
                if execution_id:
                    existing = db.get_execution(execution_id)
                    if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                        db.update_execution_status(
                            execution_id=execution_id,
                            status=TaskExecutionStatus.FAILED,
                            error=error_msg,
                        )
                if activity_id:
                    await activity_service.complete_activity(
                        activity_id=activity_id,
                        status=ActivityState.FAILED,
                        error=error_msg,
                    )
                return TaskExecutionResult(
                    execution_id=execution_id or "",
                    status=TaskExecutionStatus.FAILED,
                    response="",
                    error=error_msg,
                    error_code=TaskExecutionErrorCode.CIRCUIT_OPEN,
                )

            # ---- 3c. Mark execution as dispatched ---------------------------
            # Set claude_session_id='dispatched' BEFORE calling the agent so
            # the no-session cleanup doesn't falsely mark long-running executions
            # as "Silent launch failure". Only truly orphaned executions (where
            # the backend died before reaching this point) will be caught.
            if execution_id:
                try:
                    db.mark_execution_dispatched(execution_id)
                except Exception as e:
                    logger.warning(f"[TaskExecService] Failed to mark execution dispatched: {e}")

            # ---- 4. Call agent with retry --------------------------------
            # Compose platform prompt + execution context (#171) + caller system_prompt.
            # Never let context-building fail the request.
            try:
                exec_ctx = ExecutionContext(
                    agent_name=agent_name,
                    mode=ExecutionContext.derive_mode(triggered_by),
                    triggered_by=triggered_by,
                    source_user_email=source_user_email,
                    source_agent_name=source_agent_name,
                    source_mcp_key_name=source_mcp_key_name,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    attempt=attempt,
                    schedule_name=(schedule_context or {}).get("name"),
                    schedule_cron=(schedule_context or {}).get("cron"),
                    schedule_next_run=(schedule_context or {}).get("next_run"),
                    execution_id=execution_id,
                )
                effective_system_prompt = compose_system_prompt(
                    execution_context=exec_ctx,
                    caller_prompt=system_prompt,
                    include_execution_context=is_execution_context_enabled(),
                )
            except Exception as e:
                logger.warning(
                    f"[TaskExecService] execution context build failed, falling back: {e}"
                )
                platform_prompt = get_platform_system_prompt()
                effective_system_prompt = (
                    platform_prompt + "\n\n" + system_prompt if system_prompt else platform_prompt
                )

            payload = {
                "message": message,
                "model": model,
                "allowed_tools": allowed_tools,
                "system_prompt": effective_system_prompt,
                "timeout_seconds": timeout_seconds,
                "execution_id": execution_id,
                "resume_session_id": resume_session_id,
                "persist_session": persist_session,
                "images": images or None,
            }

            effective_timeout = float(timeout_seconds or 600) + 10
            logger.info(f"[TaskExecService] Calling agent {agent_name} /api/task (timeout={effective_timeout}s, tools={allowed_tools}, msg_len={len(message)})")

            # #678 retry bookkeeping. Hoisted ABOVE the first agent call so the
            # except branches can read these without NameError when the first
            # call raises (e.g. ConnectError after agent_post_with_retry's own
            # internal retries are exhausted).
            retry_count = 0
            previous_attempt_cost = 0.0  # accumulator: failed-attempt cost rolled into terminal write

            response = await agent_post_with_retry(
                agent_name,
                "/api/task",
                payload,
                max_retries=3,
                retry_delay=1.0,
                timeout=effective_timeout,
            )

            execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            logger.info(f"[TaskExecService] Agent {agent_name} responded: HTTP {response.status_code} ({execution_time_ms}ms)")

            # #678 auto-retry: when the agent server returned a 502 with the
            # reader-race signature AND the original turn was cheap to retry,
            # re-issue the request once with the same execution_id. The
            # agent-server side reuses the row, so this is a true in-line
            # retry (not a new execution).
            if response.status_code == 502:
                try:
                    body = response.json()
                except Exception:
                    body = {}
                inner_detail = body.get("detail") if isinstance(body, dict) else None
                if _is_reader_race_signature(inner_detail):
                    # #678 R1: cap the retry's timeout so we don't silently
                    # double the operator's wallclock budget. The reader
                    # race fires fast; 5 min is plenty. Cap the agent-side
                    # timeout too — otherwise the agent runs to the original
                    # 3600s while the backend gives up at 300s, wasting
                    # the slot and a Claude subprocess.
                    retry_agent_timeout = int(
                        min(float(timeout_seconds or 600), _AUTO_RETRY_MAX_TIMEOUT_S)
                    )
                    retry_http_timeout = min(effective_timeout, _AUTO_RETRY_MAX_TIMEOUT_S)

                    # CB re-check: if the agent went unhealthy between the
                    # first 502 and now, fast-fail the retry the same way
                    # the original call would have been fast-failed above.
                    if not circuit.allow_request():
                        logger.warning(
                            f"[TaskExecService] CB opened between first call and "
                            f"retry on {agent_name} — skipping auto-retry"
                        )
                    else:
                        retry_count = 1
                        prev_meta = inner_detail.get("metadata") or {}
                        num_turns_before = prev_meta.get("num_turns") or 0
                        # #678 R2: carry the failed attempt's cost into the
                        # terminal cost write so the spend isn't silently
                        # absorbed by the retry's $0-or-success replacement.
                        prev_cost_raw = prev_meta.get("cost_usd")
                        if isinstance(prev_cost_raw, (int, float)) and prev_cost_raw > 0:
                            previous_attempt_cost = float(prev_cost_raw)
                        logger.warning(
                            f"[TaskExecService] Reader-race signature on {agent_name} "
                            f"(num_turns={num_turns_before}, prev_cost=${previous_attempt_cost:.4f}) "
                            f"— auto-retry 1/1"
                        )
                        # Fire-and-forget audit log. Best-effort; never blocks retry.
                        # `phase=initiated` documents that this row attests the
                        # retry was queued — a wire-level ConnectError after
                        # this point would still leave the row in place.
                        try:
                            await platform_audit_service.log(
                                event_type=AuditEventType.EXECUTION,
                                event_action="auto_retry",
                                source="task_execution_service",
                                actor_agent_name=agent_name,
                                target_type="execution",
                                target_id=execution_id,
                                details={
                                    "reason": "reader_race_signature",
                                    "attempt": 2,
                                    "phase": "initiated",
                                    "previous_num_turns": num_turns_before,
                                    "previous_message": sanitize_text(
                                        (inner_detail.get("message") or "")[:300]
                                    ),
                                },
                            )
                        except Exception as audit_err:
                            logger.debug(f"[TaskExecService] audit log failed (non-fatal): {audit_err}")

                        retry_payload = {**payload, "timeout_seconds": retry_agent_timeout}
                        start_time = datetime.utcnow()
                        response = await agent_post_with_retry(
                            agent_name,
                            "/api/task",
                            retry_payload,
                            max_retries=3,
                            retry_delay=1.0,
                            timeout=retry_http_timeout,
                        )
                        execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                        logger.info(
                            f"[TaskExecService] Agent {agent_name} retry responded: "
                            f"HTTP {response.status_code} ({execution_time_ms}ms, "
                            f"http_timeout={retry_http_timeout}s, "
                            f"agent_timeout={retry_agent_timeout}s)"
                        )

            response.raise_for_status()

            response_data = response.json()
            metadata = response_data.get("metadata", {})

            # ---- 5. Sanitize + persist -----------------------------------
            tool_calls_json = None
            execution_log_json = None

            if "execution_log" in response_data and response_data["execution_log"] is not None:
                execution_log = response_data["execution_log"]
                if isinstance(execution_log, list) and len(execution_log) > 0:
                    try:
                        execution_log_json = json.dumps(execution_log)
                        execution_log_json = sanitize_execution_log(execution_log_json)
                        tool_calls_json = execution_log_json
                    except Exception as e:
                        logger.error(f"[TaskExecService] Failed to serialize execution_log for {execution_id}: {e}")

            # Context-window pressure metric. See ``_compute_context_used``
            # for the cache_read + cache_creation invariant — shared with
            # the #678 HTTPError salvage path so success and failure rows
            # record context_used the same way.
            context_used = _compute_context_used(metadata) or 0
            sanitized_resp = sanitize_response(response_data.get("response"))
            claude_session_id = response_data.get("session_id") or metadata.get("session_id")

            # Auto-compact events captured by the agent server's stream parser
            # (Bundle B observability). Serialised once here, persisted on the
            # execution row + threaded back to the Session router via raw_response
            # so it can also land on agent_session_messages.
            compact_events = metadata.get("compact_events") or []
            compact_metadata_json = (
                json.dumps(compact_events) if compact_events else None
            )

            # #678 R2: roll the failed first attempt's cost into the
            # terminal write so spend tracking reflects what we actually
            # burnt, not just the retry. previous_attempt_cost is 0.0
            # when no retry fired, so the no-retry path is unchanged.
            retry_cost = metadata.get("cost_usd")
            if previous_attempt_cost > 0:
                base = retry_cost if isinstance(retry_cost, (int, float)) else 0.0
                total_cost: Optional[float] = base + previous_attempt_cost
            else:
                total_cost = retry_cost

            # ---- 6. Update execution record ------------------------------
            if execution_id:
                db.update_execution_status(
                    execution_id=execution_id,
                    status=TaskExecutionStatus.SUCCESS,
                    response=sanitized_resp,
                    context_used=context_used if context_used > 0 else None,
                    context_max=metadata.get("context_window") or 200000,
                    cost=total_cost,
                    tool_calls=tool_calls_json,
                    execution_log=execution_log_json,
                    claude_session_id=claude_session_id,
                    compact_metadata=compact_metadata_json,
                    retry_count=retry_count or None,
                )

            # ---- 7. Complete activity ------------------------------------
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.COMPLETED,
                    details={
                        "session_id": response_data.get("session_id"),
                        "cost_usd": total_cost,
                        "execution_time_ms": execution_time_ms,
                        "tool_count": len(response_data.get("execution_log", [])),
                        # #514: short preview surfaced on dashboard timeline hover
                        "response_preview": (sanitized_resp or "")[:200],
                    },
                )

            # #526: success terminal — reset the dispatch breaker (consecutive-
            # failure model; any success closes a recovering breaker / clears the
            # counter). Backgrounds the recovery audit if this closed an open one.
            await _record_dispatch_terminal(agent_name, breaker_enabled, None)

            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.SUCCESS,
                response=sanitized_resp or "",
                cost=total_cost,
                context_used=context_used if context_used > 0 else None,
                context_max=metadata.get("context_window") or 200000,
                session_id=claude_session_id,
                execution_log=execution_log_json,
                raw_response=response_data,
            )

        except httpx.TimeoutException:
            elapsed = int((datetime.utcnow() - start_time).total_seconds())
            error_msg = f"Task execution timed out after {timeout_seconds} seconds"
            logger.error(f"[TaskExecService] TIMEOUT on {agent_name} after {elapsed}s (limit={timeout_seconds}s)")

            # Issue #61: Terminate the execution on the agent to prevent orphaned
            # Claude processes from accumulating. Best-effort — watchdog is safety net.
            await terminate_execution_on_agent(agent_name, execution_id)

            # Don't overwrite cancelled executions
            if execution_id:
                existing = db.get_execution(execution_id)
                if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                    db.update_execution_status(
                        execution_id=execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                    )
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.FAILED,
                    error=error_msg,
                )
            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.FAILED,
                response="",
                error=error_msg,
                error_code=TaskExecutionErrorCode.TIMEOUT,
            )

        except BackendAgentCallBudgetExhausted as e:
            # #904 RC-1: backend agent-call budget exhausted. Different
            # from a normal `httpx.HTTPError` because no Claude work
            # started — the rejection happened entirely inside the
            # backend's semaphore wait. SUB-003 must NOT fire (the
            # agent's subscription is irrelevant here), the execution
            # row should be marked FAILED with a clear message, and
            # the slot will be released by the outer `finally`.
            error_msg = str(e)
            logger.warning(
                f"[TaskExecService] Rejecting task on {agent_name} — backend "
                f"call budget exhausted: {error_msg}"
            )
            if execution_id:
                existing = db.get_execution(execution_id)
                if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                    db.update_execution_status(
                        execution_id=execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                    )
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.FAILED,
                    error=error_msg,
                )
            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.FAILED,
                response="",
                error=error_msg,
            )

        except httpx.HTTPError as e:
            error_msg = f"HTTP error: {type(e).__name__}"
            # #678: when the agent returns a structured dict detail (from
            # _classify_empty_result), salvage partial metadata onto the
            # failure row instead of writing null-everything.
            partial_metadata: dict = {}
            error_data = None
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_data = e.response.json()
                    detail = error_data.get("detail")
                    if isinstance(detail, dict):
                        # #678 structured body
                        error_msg = detail.get("message") or str(detail)
                        if isinstance(detail.get("metadata"), dict):
                            partial_metadata = detail["metadata"]
                    elif "detail" in error_data:
                        error_msg = error_data["detail"]
                except Exception:
                    if e.response.text:
                        error_msg = e.response.text[:500]
            logger.error(f"[TaskExecService] Failed to execute task on {agent_name}: {error_msg}")

            # SUB-003 (#441): Auto-switch on rate-limit (429) OR auth-class
            # failures (503 from agent server, or auth indicators in the error
            # text). Fire-and-forget under broad exception handling so a switch
            # error never masks the underlying execution failure.
            agent_status_code = getattr(getattr(e, "response", None), "status_code", None)
            try:
                from services.subscription_auto_switch import (
                    handle_subscription_failure,
                    is_auth_failure,
                )
                if agent_status_code == 429:
                    await handle_subscription_failure(
                        agent_name=agent_name,
                        error_message=error_msg,
                        failure_kind="rate_limit",
                    )
                elif agent_status_code == 503 or is_auth_failure(error_msg):
                    await handle_subscription_failure(
                        agent_name=agent_name,
                        error_message=error_msg,
                        failure_kind="auth",
                    )
            except Exception as switch_err:
                logger.error(f"[SUB-003] Auto-switch check failed for '{agent_name}': {switch_err}")

            # Issue #285: Detect auth failures (HTTP 503 from agent server)
            # Return structured error code so callers can handle appropriately
            error_code = None
            if agent_status_code == 503:
                logger.warning(f"[TaskExecService] Auth failure detected on {agent_name}: {error_msg[:200]}")
                error_code = TaskExecutionErrorCode.AUTH

            # #678 salvage: surface what telemetry the agent did capture
            # before its stdout reader thread wedged. Sanitize the partial
            # metadata once more here as defense-in-depth — the agent-server
            # side already sanitized, but error_message can carry tokens
            # from claude output (stream_parser.py:281).
            if partial_metadata:
                partial_metadata = sanitize_dict(partial_metadata)
            salvage_cost_raw = partial_metadata.get("cost_usd") if partial_metadata else None
            salvage_context = _compute_context_used(partial_metadata) if partial_metadata else None
            salvage_context_max = (
                (partial_metadata.get("context_window") or 200000)
                if partial_metadata
                else None
            )

            # #678 R2: when the retry ALSO failed, the first attempt's cost
            # lives in previous_attempt_cost and the retry's cost lives in
            # salvage_cost_raw (from the retry-failure body). Sum so the
            # FAILED row reflects total burn, not just the retry slice.
            if previous_attempt_cost > 0:
                base = salvage_cost_raw if isinstance(salvage_cost_raw, (int, float)) else 0.0
                salvage_cost: Optional[float] = base + previous_attempt_cost
            else:
                salvage_cost = salvage_cost_raw

            if execution_id:
                existing = db.get_execution(execution_id)
                if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                    db.update_execution_status(
                        execution_id=execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                        cost=salvage_cost,
                        context_used=salvage_context,
                        context_max=salvage_context_max,
                        retry_count=retry_count or None,
                    )
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.FAILED,
                    error=error_msg,
                )
            # #526 D10: AUTH-only counting. Gate on error_code == AUTH so a
            # non-auth HTTP failure (error_code is None) is NEVER passed to
            # record_outcome — which would read None as a success and reset the
            # counter. On the →open transition this backgrounds the backlog
            # drain + audit.
            if error_code == TaskExecutionErrorCode.AUTH:
                await _record_dispatch_terminal(agent_name, breaker_enabled, error_code)
            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.FAILED,
                response="",
                error=error_msg,
                error_code=error_code,  # Issue #285: Include auth error code
                cost=salvage_cost,
                context_used=salvage_context,
                context_max=salvage_context_max,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[TaskExecService] Unexpected error executing task on {agent_name}: {error_msg}")
            if execution_id:
                existing = db.get_execution(execution_id)
                if not existing or existing.status != TaskExecutionStatus.CANCELLED:
                    db.update_execution_status(
                        execution_id=execution_id,
                        status=TaskExecutionStatus.FAILED,
                        error=error_msg,
                    )
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.FAILED,
                    error=error_msg,
                )
            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.FAILED,
                response="",
                error=error_msg,
            )

        except asyncio.CancelledError:
            # Python 3.11+: CancelledError is BaseException, bypasses except Exception.
            # On backend shutdown, background tasks are cancelled; close the record
            # immediately so cleanup_service doesn't inflate duration (#767).
            if execution_id:
                try:
                    existing = db.get_execution(execution_id)
                    if existing and existing.status not in (
                        TaskExecutionStatus.SUCCESS,
                        TaskExecutionStatus.FAILED,
                        TaskExecutionStatus.CANCELLED,
                    ):
                        db.update_execution_status(
                            execution_id=execution_id,
                            status=TaskExecutionStatus.FAILED,
                            error="Execution cancelled (backend shutdown)",
                        )
                except Exception:
                    pass
            raise

        finally:
            # ---- 8. Release slot (only if acquired) ----------------------
            if slot_acquired:
                await capacity.release(
                    agent_name,
                    execution_id or f"temp-{datetime.utcnow().timestamp()}",
                )


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_task_execution_service: Optional[TaskExecutionService] = None


def get_task_execution_service() -> TaskExecutionService:
    """Get the global TaskExecutionService instance."""
    global _task_execution_service
    if _task_execution_service is None:
        _task_execution_service = TaskExecutionService()
    return _task_execution_service
