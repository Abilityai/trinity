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
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import httpx

from database import db
from models import ActivityState, ActivityType, TaskExecutionStatus
from services.activity_service import activity_service
from services.capacity_manager import CapacityFull, get_capacity_manager
from utils.credential_sanitizer import sanitize_execution_log, sanitize_response
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
    """
    agent_url = f"http://agent-{agent_name}:8000{endpoint}"

    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(agent_url, json=payload)
                return response
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
        subscription_id: Optional[str] = None,
        parent_activity_id: Optional[str] = None,
        extra_activity_details: Optional[dict] = None,
        slot_already_held: bool = False,
        schedule_context: Optional[dict] = None,
        attempt: Optional[int] = None,
        images: Optional[list] = None,
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
            # ---- 3b. Mark execution as dispatched ---------------------------
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

            # Context-window pressure metric. Anthropic's `usage` object
            # has three non-overlapping input buckets: input_tokens (fresh
            # uncached), cache_creation_tokens (newly cached this turn),
            # cache_read_tokens (re-read from a prior turn's cache).
            #
            # We can't simply sum them. The agent-server in claude_code.py
            # overrides metadata.input_tokens with `modelUsage.inputTokens`
            # (an aggregated total across all internal API calls this turn)
            # whenever the latter is larger — which is virtually always
            # true on tool-call turns. So input_tokens is sometimes the
            # disjoint fresh value and sometimes the aggregated total, and
            # summing it with cache_* double-counts in the second case.
            #
            # Stable approach: rely on cache_read + cache_creation, which
            # are NEVER touched by the override and monotonically track
            # the size of the cached conversation prefix. This is exactly
            # what "context-window fullness" means for a --resume session.
            # Fall back to input_tokens only when caching isn't engaged
            # (cold turns with caching disabled, or the very first turn
            # before the cache breakpoint has fired).
            cache_read = metadata.get("cache_read_tokens") or 0
            cache_create = metadata.get("cache_creation_tokens") or 0
            if cache_read + cache_create > 0:
                context_used = cache_read + cache_create
            else:
                context_used = metadata.get("input_tokens") or 0
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

            # ---- 6. Update execution record ------------------------------
            if execution_id:
                db.update_execution_status(
                    execution_id=execution_id,
                    status=TaskExecutionStatus.SUCCESS,
                    response=sanitized_resp,
                    context_used=context_used if context_used > 0 else None,
                    context_max=metadata.get("context_window") or 200000,
                    cost=metadata.get("cost_usd"),
                    tool_calls=tool_calls_json,
                    execution_log=execution_log_json,
                    claude_session_id=claude_session_id,
                    compact_metadata=compact_metadata_json,
                )

            # ---- 7. Complete activity ------------------------------------
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.COMPLETED,
                    details={
                        "session_id": response_data.get("session_id"),
                        "cost_usd": metadata.get("cost_usd"),
                        "execution_time_ms": execution_time_ms,
                        "tool_count": len(response_data.get("execution_log", [])),
                        # #514: short preview surfaced on dashboard timeline hover
                        "response_preview": (sanitized_resp or "")[:200],
                    },
                )

            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.SUCCESS,
                response=sanitized_resp or "",
                cost=metadata.get("cost_usd"),
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

        except httpx.HTTPError as e:
            error_msg = f"HTTP error: {type(e).__name__}"
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_data = e.response.json()
                    if "detail" in error_data:
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
                error_code=error_code,  # Issue #285: Include auth error code
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
