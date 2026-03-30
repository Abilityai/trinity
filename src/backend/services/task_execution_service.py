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
from services.slot_service import get_slot_service
from utils.credential_sanitizer import sanitize_execution_log, sanitize_response
from services.platform_prompt_service import get_platform_system_prompt

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
        triggered_by: str,                      # "manual"|"public"|"schedule"|"agent"|"mcp"
        source_user_id: Optional[int] = None,
        source_user_email: Optional[str] = None,
        source_agent_name: Optional[str] = None,
        source_mcp_key_id: Optional[str] = None,
        source_mcp_key_name: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        resume_session_id: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
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
        slot_service = get_slot_service()
        activity_id: Optional[str] = None
        slot_acquired = False  # Track whether slot was acquired for proper cleanup

        # TIMEOUT-001: Use agent's configured timeout if not explicitly provided
        if timeout_seconds is None:
            timeout_seconds = db.get_execution_timeout(agent_name)

        # ---- 1. Create execution record (if not provided) ----------------
        if not execution_id:
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
            )
            execution_id = execution.id if execution else None

        # Wrap entire execution flow to ensure execution status is updated on any failure.
        # This fixes issue #90 where exceptions during slot acquisition left executions
        # stuck in 'running' status with NULL session_id and duration_ms.
        try:
            # ---- 2. Acquire capacity slot ------------------------------------
            max_parallel_tasks = db.get_max_parallel_tasks(agent_name)
            slot_acquired = await slot_service.acquire_slot(
                agent_name=agent_name,
                execution_id=execution_id or f"temp-{datetime.utcnow().timestamp()}",
                max_parallel_tasks=max_parallel_tasks,
                message_preview=message[:100] if message else "",
                timeout_seconds=timeout_seconds,  # TIMEOUT-001: Pass for dynamic slot TTL
            )

            if not slot_acquired:
                error_msg = f"Agent at capacity ({max_parallel_tasks}/{max_parallel_tasks} parallel tasks running)"
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
            try:
                activity_id = await activity_service.track_activity(
                    agent_name=agent_name,
                    activity_type=ActivityType.CHAT_START,
                    user_id=source_user_id,
                    triggered_by=triggered_by,
                    related_execution_id=execution_id,
                    details={
                        "message_preview": message[:100] if message else "",
                        "source_agent": source_agent_name,
                        "execution_id": execution_id,
                        "triggered_by": triggered_by,
                    },
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
            # Prepend platform instructions to any caller-provided system_prompt
            platform_prompt = get_platform_system_prompt()
            if system_prompt:
                effective_system_prompt = platform_prompt + "\n\n" + system_prompt
            else:
                effective_system_prompt = platform_prompt

            payload = {
                "message": message,
                "model": model,
                "allowed_tools": allowed_tools,
                "system_prompt": effective_system_prompt,
                "timeout_seconds": timeout_seconds,
                "execution_id": execution_id,
                "resume_session_id": resume_session_id,
            }

            start_time = datetime.utcnow()

            response = await agent_post_with_retry(
                agent_name,
                "/api/task",
                payload,
                max_retries=3,
                retry_delay=1.0,
                timeout=float(timeout_seconds or 600) + 10,
            )
            response.raise_for_status()

            response_data = response.json()
            execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
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

            context_used = metadata.get("input_tokens", 0) + metadata.get("output_tokens", 0)
            sanitized_resp = sanitize_response(response_data.get("response"))
            claude_session_id = response_data.get("session_id") or metadata.get("session_id")

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
            error_msg = f"Task execution timed out after {timeout_seconds} seconds"
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

            # SUB-003: Auto-switch subscription on rate-limit errors
            agent_status_code = getattr(getattr(e, "response", None), "status_code", None)
            if agent_status_code == 429:
                try:
                    from services.subscription_auto_switch import handle_rate_limit_error
                    await handle_rate_limit_error(agent_name=agent_name, error_message=error_msg)
                except Exception as switch_err:
                    logger.error(f"[SUB-003] Auto-switch check failed for '{agent_name}': {switch_err}")

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
                await slot_service.release_slot(
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
