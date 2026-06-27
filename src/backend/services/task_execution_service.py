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
from services.agent_auth import agent_httpx_client
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


def _resolve_agent_runtime(agent_name: str) -> str:
    """Best-effort resolve an agent's runtime for the platform prompt (#1187).

    Lazy + guarded import: a top-level ``from services.docker_service import
    get_agent_runtime`` would make a *re-import* of this module fail when a unit
    test has stubbed ``services.docker_service`` with a partial stub that lacks
    the symbol (the conftest pops + re-imports this module between tests). Resolve
    to the Claude default on any failure — never block dispatch.
    """
    try:
        from services.docker_service import get_agent_runtime

        return get_agent_runtime(agent_name)
    except Exception:
        return "claude-code"

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
    RECONCILED = "reconciled"       # Terminal write lost the CAS; row reflects another writer's terminal (#671/H4)
    LEASE_EXPIRED = "lease_expired" # Fire-and-forget lease expired — no callback before slot TTL (#1083)


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
    # #1083: True when the turn was dispatched fire-and-forget (agent ACK'd 202)
    # and will be finalized by the result-callback endpoint. The persisted row
    # stays `running`; this flag is in-memory only so the caller (scheduler
    # async-poll) keeps polling instead of treating the ACK as a terminal.
    dispatched_async: bool = False


@dataclass
class TerminalEnvelope:
    """Normalized, pre-classified terminal contract consumed by ``apply_result``
    (#1083).

    The single input shape for finalizing an execution, whether the terminal is
    produced inline (sync path) or arrives over the result-callback endpoint.
    ``apply_result`` derives every persisted field (cost rollup, context, tool
    calls, compact metadata, salvage) from these raw-ish inputs — it never
    re-runs the error classifier, so ``error_code`` MUST already be set by the
    producer (the substring/status classification stays in ``execute_task``).

    Fields:
        status: ``TaskExecutionStatus.SUCCESS`` or ``.FAILED`` — selects the
            success-style (reconcile-on-lost-CAS) vs failure-style applier.
        response: raw response text (success). Sanitized inside ``apply_result``.
        error: failure message (failure).
        error_code: pre-classified ``TaskExecutionErrorCode`` — only ``AUTH``
            feeds the dispatch breaker (D10).
        metadata: raw agent metadata dict (cost_usd, context_window, tokens,
            compact_events, session_id). Sanitized for the salvage path.
        execution_log: raw transcript list (success) or None.
        session_id: raw ``response_data['session_id']`` (may be None — the
            persisted ``claude_session_id`` falls back to ``metadata['session_id']``).
        retry_count: #678 in-line retry count for the terminal write.
        previous_attempt_cost: #678 R2 — failed-first-attempt cost rolled into
            the terminal cost write.
        execution_time_ms: wall-clock used for the activity-completion detail.
        raw_response: full response dict threaded back via ``TaskExecutionResult``
            (Session router consumes ``compact_events``); empty for callbacks.
    """
    execution_id: Optional[str]
    status: str
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[TaskExecutionErrorCode] = None
    metadata: dict = field(default_factory=dict)
    execution_log: Any = None
    session_id: Optional[str] = None
    retry_count: Optional[int] = None
    previous_attempt_cost: float = 0.0
    execution_time_ms: Optional[int] = None
    raw_response: dict = field(default_factory=dict)


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
# SUB-003 switch-trigger classifier + attempt salvage (#792)
# ---------------------------------------------------------------------------

# Small settle before the post-switch retry so a hot-reloaded token is picked
# up by the NEXT claude subprocess. The retry itself is the readiness probe
# (#792 review): we deliberately do NOT poll the circuit-aware /health endpoint
# (cold-start polling can open the transport breaker and cancel the retry) nor
# trust restart_result's string status (it proves the call returned, not that
# the new token authenticates). A still-failing retry just writes FAILED.
_SWITCH_RETRY_DELAY_S = 3.0


def _extract_agent_error(response: Optional[httpx.Response], fallback: str) -> tuple[str, dict]:
    """Pull a human error string + any #678 structured ``metadata`` from an
    agent error-response body. Shared by the pre-raise switch path (#792) and
    the ``except httpx.HTTPError`` handler so both read the body identically."""
    error_msg = fallback
    partial_metadata: dict = {}
    if response is None:
        return error_msg, partial_metadata
    try:
        error_data = response.json()
        detail = error_data.get("detail")
        if isinstance(detail, dict):
            # #678 structured body
            error_msg = detail.get("message") or str(detail)
            if isinstance(detail.get("metadata"), dict):
                partial_metadata = detail["metadata"]
        elif "detail" in error_data:
            error_msg = error_data["detail"]
    except Exception:
        if response.text:
            error_msg = response.text[:500]
    return error_msg, partial_metadata


def classify_switch_failure(response: httpx.Response) -> Optional[str]:
    """Map an agent response to a SUB-003 switch ``failure_kind``, or None.

    Mirrors the trigger surface SUB-003 uses in the ``except httpx.HTTPError``
    handler so the #792 contract — "any switch-success retries" — is actually
    true (not just 429/503 by status code):

        429                          -> "rate_limit"
        503 / 401 / 403 / 402        -> "auth"
        other 4xx/5xx whose body trips ``is_auth_failure`` -> "auth"
        anything else (incl. 2xx)    -> None
    """
    # Imported here (not at module scope) to match the except-handler import and
    # keep the test patch target `subscription_auto_switch.is_auth_failure` live.
    from services.subscription_auto_switch import is_auth_failure

    code = response.status_code
    if code == 429:
        return "rate_limit"
    if code in (503, 401, 403, 402):
        return "auth"
    if code >= 400:
        error_msg, _ = _extract_agent_error(response, "")
        if is_auth_failure(error_msg):
            return "auth"
    return None


def _salvage_attempt_cost(partial_metadata: dict) -> float:
    """Best-effort first-attempt cost from a salvaged ``metadata`` dict, for the
    #678 R2 ``previous_attempt_cost`` rollup. A mid-run 429/auth can carry
    nonzero cost — "≈$0" is not a contract (#792 review, Codex #3)."""
    raw = partial_metadata.get("cost_usd")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return 0.0


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
                async with agent_httpx_client(agent_name, timeout=timeout) as client:
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
        async with agent_httpx_client(agent_name, timeout=TERMINATE_TIMEOUT) as client:
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


def dispatch_async_eligible(triggered_by: Optional[str]) -> bool:
    """Combined fire-and-forget gate: global ``DISPATCH_ASYNC`` master switch AND
    a trigger in ``ASYNC_DISPATCH_ELIGIBLE_TRIGGERS`` (#1083).

    Single source of truth for "should this turn be dispatched async?", mirroring
    ``dispatch_breaker_active``. The global flag is checked first so a disabled
    fleet pays nothing. Only ``{schedule, webhook}`` are eligible in v1 — the only
    triggers reaching ``execute_task`` with no synchronous result consumer
    (``loop``/``fan_out`` read ``result.response``; ``event`` bypasses
    ``execute_task`` entirely). Fail-safe → False; never raises.

    PULL-MODE GATE (#1084): async/pull dispatch introduces at-least-once
    re-delivery, which would re-emit an agent's outbound side effects (send an
    email twice, charge a payment twice) on a re-run. Effect-scoped idempotency
    (effect_guard, services/idempotency_service.py) makes those effects safe per
    resolved action identity, but turning pull-mode default-ON for any
    side-effect-bearing agent additionally REQUIRES (a) trusted runtime injection
    of ``execution_id`` and (b) fail-closed-when-absent — a BLOCKING prerequisite
    on Epic #1045/#1081, not satisfied here. See
    docs/memory/feature-flows/effect-idempotency.md.
    """
    try:
        from config import ASYNC_DISPATCH_ELIGIBLE_TRIGGERS, DISPATCH_ASYNC
        if not DISPATCH_ASYNC:
            return False
        return triggered_by in ASYNC_DISPATCH_ELIGIBLE_TRIGGERS
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


async def _write_terminal_and_gate(
    execution_id: Optional[str],
    activity_id: Optional[str],
    *,
    status: str,
    activity_status: str,
    error: Optional[str] = None,
    cost: Optional[float] = None,
    context_used: Optional[int] = None,
    context_max: Optional[int] = None,
    retry_count: Optional[int] = None,
) -> bool:
    """Write a non-success terminal through the CAS and gate the activity
    completion on winning it (#671/H4).

    ``db.update_execution_status`` is an atomic compare-and-set: a non-success
    terminal write loses to any already-terminal row (SUCCESS/FAILED/CANCELLED/
    SKIPPED). The activity is completed ONLY when this writer won — a writer
    that lost (e.g. to a user cancel) must not also complete the activity,
    mirroring the SUCCESS-path reconcile. Returns the CAS winner so the caller
    can gate any further side effects (e.g. the dispatch breaker).

    When ``execution_id`` is falsy there is no row to contend for, so the write
    is skipped and the activity completion still runs (``won=True``), preserving
    the prior no-record behaviour. Replaces the old check-then-act guard
    (``get_execution() → status != CANCELLED``), which both raced and only
    blocked CANCELLED — the CAS additionally blocks an already-SUCCESS/FAILED
    row and closes the TOCTOU window.
    """
    won = True
    if execution_id:
        won = db.update_execution_status(
            execution_id=execution_id,
            status=status,
            error=error,
            cost=cost,
            context_used=context_used,
            context_max=context_max,
            retry_count=retry_count,
        )
    if won and activity_id:
        await activity_service.complete_activity(
            activity_id=activity_id,
            status=activity_status,
            error=error,
        )
    return won


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

        # Fire-and-forget dispatch (#1083): eligible triggers ({schedule, webhook})
        # request a 202 ACK + result callback so a wedged turn holds zero backend
        # coroutine/slot beyond its lease. The RUNTIME gate is enforced agent-side
        # (decision 5): a non-Claude / old-image agent ignores `async_result` and
        # returns 200, which falls through to the synchronous handling below. When
        # the agent ACKs 202 we hand the slot lease to the callback and skip the
        # `finally` release. Best-effort read; defaults off.
        async_dispatch = dispatch_async_eligible(triggered_by)
        # Set True once a 202 ACK hands the slot lease to the result callback, so
        # the `finally` does NOT release it (the callback/reaper owns it now).
        async_handoff = False

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
                # #671/H4: route the terminal write through the CAS — the
                # activity is completed only if this writer won (a lost CAS to a
                # cancel/already-terminal row must not also complete it).
                await _write_terminal_and_gate(
                    execution_id,
                    activity_id,
                    status=TaskExecutionStatus.FAILED,
                    activity_status=ActivityState.FAILED,
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
            #
            # #1083: for an async-eligible dispatch write the DURABLE async marker
            # ('dispatched_async') instead — the result-callback endpoint finalizes
            # ONLY rows carrying it (fail-closed cross-path guard). Both sentinels
            # are non-NULL so the no-session sweep treats them identically. If the
            # agent then returns non-202 (non-Claude / old image) the sync terminal
            # write overwrites/ignores the marker — harmless, no callback arrives.
            if execution_id:
                try:
                    db.mark_execution_dispatched(execution_id, async_dispatch=async_dispatch)
                except Exception as e:
                    logger.warning(f"[TaskExecService] Failed to mark execution dispatched: {e}")

            # ---- 4. Call agent with retry --------------------------------
            # Compose platform prompt + execution context (#171) + caller system_prompt.
            # Never let context-building fail the request.
            # Resolve the agent runtime (best-effort, never raises) so the
            # MCP-tool naming matches the harness (#1187 F-MCP).
            agent_runtime = _resolve_agent_runtime(agent_name)
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
                    runtime=agent_runtime,
                )
            except Exception as e:
                logger.warning(
                    f"[TaskExecService] execution context build failed, falling back: {e}"
                )
                platform_prompt = get_platform_system_prompt(runtime=agent_runtime)
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
                # #1083: request a 202 ACK + result callback. Honored ONLY by a
                # Claude-runtime agent on a new base image; everyone else ignores
                # it and runs synchronously (200 → sync fallback below).
                "async_result": async_dispatch,
            }

            effective_timeout = float(timeout_seconds or 600) + 10
            logger.info(f"[TaskExecService] Calling agent {agent_name} /api/task (timeout={effective_timeout}s, tools={allowed_tools}, msg_len={len(message)})")

            # #678 retry bookkeeping. Hoisted ABOVE the first agent call so the
            # except branches can read these without NameError when the first
            # call raises (e.g. ConnectError after agent_post_with_retry's own
            # internal retries are exhausted).
            retry_count = 0
            previous_attempt_cost = 0.0  # accumulator: failed-attempt cost rolled into terminal write
            # #792: one-shot guard for the SUB-003 switch+retry. A dedicated flag
            # (NOT retry_count, which #678's reader-race retry owns) so the two
            # retry reasons never suppress each other. Read by the except handler
            # to skip a cascade double-switch.
            subscription_switch_attempted = False

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

            # #792 SUB-003 switch+retry: a returned 429/auth response is
            # interceptable HERE, before raise_for_status (below) raises it into
            # the except handler — mirroring the #678 502 path above. If the agent
            # rate-limited / auth-failed and SUB-003 successfully switched the
            # subscription, re-issue the turn ONCE with the SAME execution_id so a
            # one-shot trigger (manual / webhook / mcp) recovers instead of landing
            # FAILED. The retry IS the readiness probe (see _SWITCH_RETRY_DELAY_S).
            # Guarded by its own one-shot flag (NOT retry_count, which #678 owns) so
            # the two retry reasons never suppress each other; the except handler
            # reads the flag to skip a cascade double-switch.
            if not subscription_switch_attempted:
                switch_failure_kind = classify_switch_failure(response)
                if switch_failure_kind is not None:
                    subscription_switch_attempted = True
                    switch_error_msg, switch_partial_meta = _extract_agent_error(
                        response, f"HTTP {response.status_code} from agent"
                    )
                    switch_result = None
                    try:
                        from services.subscription_auto_switch import (
                            handle_subscription_failure,
                        )
                        switch_result = await handle_subscription_failure(
                            agent_name=agent_name,
                            error_message=switch_error_msg,
                            failure_kind=switch_failure_kind,
                        )
                    except Exception as switch_err:
                        logger.error(
                            f"[SUB-003] Auto-switch failed for '{agent_name}': {switch_err}"
                        )

                    if switch_result and switch_result.get("switched"):
                        retry_count += 1
                        # #678 R2 rollup: accumulate the failed attempt's cost so it
                        # isn't absorbed by the retry's success replacement.
                        previous_attempt_cost += _salvage_attempt_cost(switch_partial_meta)
                        # Cap the retry to the REMAINING original budget so a 429
                        # after a long run can't balloon wall-clock / slot time.
                        elapsed_s = (datetime.utcnow() - start_time).total_seconds()
                        remaining_s = max(1.0, effective_timeout - elapsed_s)
                        retry_http_timeout = min(remaining_s, _AUTO_RETRY_MAX_TIMEOUT_S)
                        retry_agent_timeout = int(
                            min(float(timeout_seconds or 600), retry_http_timeout)
                        )
                        logger.warning(
                            f"[TaskExecService] SUB-003 switched '{agent_name}' "
                            f"({switch_failure_kind}) -> "
                            f"'{switch_result.get('new_subscription')}' — auto-retry 1/1 "
                            f"(prev_cost=${previous_attempt_cost:.4f})"
                        )
                        # Best-effort audit. phase=initiated documents the retry was queued.
                        try:
                            await platform_audit_service.log(
                                event_type=AuditEventType.EXECUTION,
                                event_action="auto_retry",
                                source="task_execution_service",
                                actor_agent_name=agent_name,
                                target_type="execution",
                                target_id=execution_id,
                                details={
                                    "reason": "subscription_auto_switch",
                                    # retry_count was just incremented above; +1 makes
                                    # this the human attempt number. In the #678→#792
                                    # interplay this is correctly 3 (not a second "2").
                                    "attempt": retry_count + 1,
                                    "phase": "initiated",
                                    "failure_kind": switch_failure_kind,
                                    "new_subscription": switch_result.get("new_subscription"),
                                },
                            )
                        except Exception as audit_err:
                            logger.debug(f"[TaskExecService] audit log failed (non-fatal): {audit_err}")

                        # Small settle so a hot-reloaded token is live for the next
                        # subprocess; the retry call itself probes readiness.
                        await asyncio.sleep(_SWITCH_RETRY_DELAY_S)
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
                            f"[TaskExecService] Agent {agent_name} post-switch retry "
                            f"responded: HTTP {response.status_code} ({execution_time_ms}ms, "
                            f"http_timeout={retry_http_timeout:.0f}s, "
                            f"agent_timeout={retry_agent_timeout}s)"
                        )

            # ---- #1083: fire-and-forget ACK --------------------------------
            # A Claude-runtime agent on a new base image accepts an async turn
            # with 202 and runs it in the background, POSTing the terminal to the
            # result-callback endpoint when done. Hand the slot lease to the
            # callback (skip the `finally` release) and return RUNNING now — no
            # terminal write here; the callback (or the lease reaper) finalizes
            # the row. Any other status (200 success, errors) falls through to
            # today's synchronous handling — the non-202 fallback that keeps mixed
            # image versions and non-Claude runtimes working.
            if async_dispatch and response.status_code == 202:
                async_handoff = True
                logger.info(
                    f"[TaskExecService] Agent {agent_name} ACK'd async dispatch (202) "
                    f"for execution {execution_id}; handing slot lease to result callback"
                )
                return TaskExecutionResult(
                    execution_id=execution_id or "",
                    status=TaskExecutionStatus.RUNNING,
                    response="",
                    dispatched_async=True,
                )

            response.raise_for_status()

            response_data = response.json()
            metadata = response_data.get("metadata", {})

            # ---- #679 cancel cross-validation -----------------------------
            # A cancel-aware agent labels a SIGINT-graceful-exit-0 / SIGKILL→504
            # turn with status:"cancelled" in its 200 reply. The HTTP return code
            # alone can't distinguish that from a genuine success, so trust the
            # label: finalize CANCELLED via the shared applier (never a billable
            # SUCCESS row — defense-in-depth over the #671 CAS). An old agent
            # image omits `status` → .get() is None → the SUCCESS path below is
            # unchanged. release_slot=False: the `finally` owns the sync slot.
            if response_data.get("status") == "cancelled":
                cancelled_envelope = TerminalEnvelope(
                    execution_id=execution_id,
                    status=TaskExecutionStatus.CANCELLED,
                    error="Execution cancelled by user",
                    metadata=metadata,
                    retry_count=retry_count,
                    previous_attempt_cost=previous_attempt_cost,
                )
                return await self.apply_result(
                    agent_name,
                    cancelled_envelope,
                    activity_id=activity_id,
                    breaker_enabled=breaker_enabled,
                    release_slot=False,
                )

            # ---- 5/6/7. Apply the SUCCESS terminal ------------------------
            # The terminal write + side-effects (sanitize, cost rollup, CAS,
            # activity completion, breaker reset) live in apply_result so the
            # sync path and the #1083 result-callback finalize identically.
            # release_slot=False: the `finally` below owns slot release on the
            # sync path (the coroutine holds the slot for the whole turn).
            success_envelope = TerminalEnvelope(
                execution_id=execution_id,
                status=TaskExecutionStatus.SUCCESS,
                response=response_data.get("response"),
                metadata=metadata,
                execution_log=response_data.get("execution_log"),
                session_id=response_data.get("session_id"),
                retry_count=retry_count,
                previous_attempt_cost=previous_attempt_cost,
                execution_time_ms=execution_time_ms,
                raw_response=response_data,
            )
            return await self.apply_result(
                agent_name,
                success_envelope,
                activity_id=activity_id,
                breaker_enabled=breaker_enabled,
                release_slot=False,
            )

        except httpx.TimeoutException:
            elapsed = int((datetime.utcnow() - start_time).total_seconds())
            error_msg = f"Task execution timed out after {timeout_seconds} seconds"
            logger.error(f"[TaskExecService] TIMEOUT on {agent_name} after {elapsed}s (limit={timeout_seconds}s)")

            # Issue #61: Terminate the execution on the agent to prevent orphaned
            # Claude processes from accumulating. Best-effort — watchdog is safety net.
            await terminate_execution_on_agent(agent_name, execution_id)

            # #671/H4: CAS-gate the terminal write (replaces the CANCELLED-only
            # check-then-act guard); complete the activity only if we won.
            await _write_terminal_and_gate(
                execution_id,
                activity_id,
                status=TaskExecutionStatus.FAILED,
                activity_status=ActivityState.FAILED,
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
            # #671/H4: CAS-gate the terminal write; complete the activity only
            # if we won.
            await _write_terminal_and_gate(
                execution_id,
                activity_id,
                status=TaskExecutionStatus.FAILED,
                activity_status=ActivityState.FAILED,
                error=error_msg,
            )
            return TaskExecutionResult(
                execution_id=execution_id or "",
                status=TaskExecutionStatus.FAILED,
                response="",
                error=error_msg,
            )

        except httpx.HTTPError as e:
            # #678: when the agent returns a structured dict detail (from
            # _classify_empty_result), salvage partial metadata onto the
            # failure row instead of writing null-everything. Shared extractor
            # (#792) so this handler and the pre-raise switch path read the body
            # identically.
            error_msg, partial_metadata = _extract_agent_error(
                getattr(e, "response", None), f"HTTP error: {type(e).__name__}"
            )
            logger.error(f"[TaskExecService] Failed to execute task on {agent_name}: {error_msg}")

            # SUB-003 (#441): Auto-switch on rate-limit (429) OR auth-class
            # failures (503 from agent server, or auth indicators in the error
            # text). Fire-and-forget under broad exception handling so a switch
            # error never masks the underlying execution failure.
            # #792 cascade guard: if the pre-raise path already attempted a switch
            # this execution (and its retry still failed into here), do NOT switch
            # again — a second switch would burn another rate-limit event and churn
            # to a third never-used subscription.
            agent_status_code = getattr(getattr(e, "response", None), "status_code", None)
            if not subscription_switch_attempted:
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

            # #678 salvage + terminal write + side-effects live in apply_result.
            # The RAW partial_metadata and the pre-classified error_code are
            # passed through unchanged (classification stays producer-side, here);
            # apply_result sanitizes the metadata, derives salvage cost/context
            # (incl. the #678 R2 previous-attempt rollup), CAS-writes FAILED, and
            # gates the activity completion + AUTH breaker outcome on the win.
            # release_slot=False — the `finally` owns slot release on the sync path.
            failure_envelope = TerminalEnvelope(
                execution_id=execution_id,
                status=TaskExecutionStatus.FAILED,
                error=error_msg,
                error_code=error_code,  # Issue #285: AUTH (503) or None
                metadata=partial_metadata,
                retry_count=retry_count,
                previous_attempt_cost=previous_attempt_cost,
            )
            return await self.apply_result(
                agent_name,
                failure_envelope,
                activity_id=activity_id,
                breaker_enabled=breaker_enabled,
                release_slot=False,
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[TaskExecService] Unexpected error executing task on {agent_name}: {error_msg}")
            # #671/H4: CAS-gate the terminal write; complete the activity only
            # if we won.
            await _write_terminal_and_gate(
                execution_id,
                activity_id,
                status=TaskExecutionStatus.FAILED,
                activity_status=ActivityState.FAILED,
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
            # #1083: on an async 202 handoff the slot lease belongs to the result
            # callback (or the lease reaper) — do NOT release it here, or the turn
            # would run with no capacity reserved and overbook the agent.
            if slot_acquired and not async_handoff:
                await capacity.release(
                    agent_name,
                    execution_id or f"temp-{datetime.utcnow().timestamp()}",
                )

    # -----------------------------------------------------------------------
    # Terminal applier (#1083) — the single point that finalizes an execution
    # -----------------------------------------------------------------------
    async def apply_result(
        self,
        agent_name: str,
        envelope: "TerminalEnvelope",
        *,
        activity_id: Optional[str] = None,
        breaker_enabled: bool = False,
        release_slot: bool = False,
    ) -> TaskExecutionResult:
        """Apply a normalized terminal to an execution row and run its side
        effects (#1083). Shared by the inline sync path and the result-callback
        endpoint, so a sync turn and a fire-and-forget turn finalize identically.

        **CAS-gated side effects (Codex #1/#12):** the terminal write is an
        atomic compare-and-set (``db.update_execution_status`` → bool). EVERY
        side effect — completing the activity, recording the dispatch-breaker
        outcome, and releasing the capacity slot — runs ONLY when this writer won
        the CAS. A CAS-lost write (a replayed/late callback, or a turn superseded
        by a cancel/reaper) does nothing: no double activity close, no breaker
        churn, and critically no double slot release (``slot_service.release_slot``
        fires the BACKLOG-001 drain regardless of ZREM, so a replayed release
        would over-admit past ``max_parallel_tasks``).

        ``release_slot``: True for the callback path (it owns the lease, no
        ``finally`` to fall back on); False for the sync path (``execute_task``'s
        ``finally`` releases unconditionally — the coroutine owns the slot for the
        whole turn). Gating release on the CAS bool is what makes a duplicate
        callback safe.

        The two terminal styles differ on a lost CAS, preserving #671/H4:
        - SUCCESS (success-style): on lost CAS, reconcile to the persisted
          terminal — complete the activity FAILED ("superseded by …") and return
          a RECONCILED result, never report a billable success over a cancel.
        - FAILED (failure-style): on lost CAS, skip ALL side effects and return
          the FAILED result unchanged (the row keeps its real terminal).
        """
        capacity = get_capacity_manager()
        eid = envelope.execution_id
        metadata = envelope.metadata or {}

        if envelope.status == TaskExecutionStatus.SUCCESS:
            # ---- Success-style derivation (moved from execute_task step 5/6) ----
            tool_calls_json = None
            execution_log_json = None
            exec_log = envelope.execution_log
            if isinstance(exec_log, list) and len(exec_log) > 0:
                try:
                    execution_log_json = json.dumps(exec_log)
                    execution_log_json = sanitize_execution_log(execution_log_json)
                    tool_calls_json = execution_log_json
                except Exception as e:
                    logger.error(
                        f"[TaskExecService] Failed to serialize execution_log for {eid}: {e}"
                    )

            context_used = _compute_context_used(metadata) or 0
            sanitized_resp = sanitize_response(envelope.response)
            # claude_session_id falls back to metadata (the persisted column); the
            # activity-detail session_id below uses the raw envelope.session_id to
            # preserve the prior `response_data.get("session_id")` semantics.
            claude_session_id = envelope.session_id or metadata.get("session_id")

            compact_events = metadata.get("compact_events") or []
            compact_metadata_json = json.dumps(compact_events) if compact_events else None

            # #678 R2: roll the failed first attempt's cost into the terminal
            # write. previous_attempt_cost is 0.0 when no retry fired.
            retry_cost = metadata.get("cost_usd")
            if envelope.previous_attempt_cost > 0:
                base = retry_cost if isinstance(retry_cost, (int, float)) else 0.0
                total_cost: Optional[float] = base + envelope.previous_attempt_cost
            else:
                total_cost = retry_cost

            won = True
            if eid:
                won = db.update_execution_status(
                    execution_id=eid,
                    status=TaskExecutionStatus.SUCCESS,
                    response=sanitized_resp,
                    context_used=context_used if context_used > 0 else None,
                    context_max=metadata.get("context_window") or 200000,
                    cost=total_cost,
                    tool_calls=tool_calls_json,
                    execution_log=execution_log_json,
                    claude_session_id=claude_session_id,
                    compact_metadata=compact_metadata_json,
                    retry_count=envelope.retry_count or None,
                )
                if not won:
                    # #671/H4: SUCCESS lost the CAS — only to a CANCELLED row.
                    # Reconcile; never report a billable success over a cancel.
                    reconciled = db.get_execution(eid)
                    reconciled_status = (
                        reconciled.status if reconciled else TaskExecutionStatus.FAILED
                    )
                    logger.warning(
                        "[TaskExecService] SUCCESS write lost CAS for %s — row is "
                        "%s; reconciling, not reporting success",
                        eid,
                        reconciled_status,
                    )
                    if activity_id:
                        await activity_service.complete_activity(
                            activity_id=activity_id,
                            status=ActivityState.FAILED,
                            error=f"superseded by {reconciled_status}",
                        )
                    return TaskExecutionResult(
                        execution_id=eid,
                        status=reconciled_status,
                        response="",
                        error_code=TaskExecutionErrorCode.RECONCILED,
                    )

            # ---- Won (or no row): success side effects ----
            if activity_id:
                await activity_service.complete_activity(
                    activity_id=activity_id,
                    status=ActivityState.COMPLETED,
                    details={
                        "session_id": envelope.session_id,
                        "cost_usd": total_cost,
                        "execution_time_ms": envelope.execution_time_ms,
                        "tool_count": len(exec_log) if isinstance(exec_log, list) else 0,
                        "response_preview": (sanitized_resp or "")[:200],
                    },
                )
            await _record_dispatch_terminal(agent_name, breaker_enabled, None)
            if release_slot and eid:
                await capacity.release(agent_name, eid)

            return TaskExecutionResult(
                execution_id=eid or "",
                status=TaskExecutionStatus.SUCCESS,
                response=sanitized_resp or "",
                cost=total_cost,
                context_used=context_used if context_used > 0 else None,
                context_max=metadata.get("context_window") or 200000,
                session_id=claude_session_id,
                execution_log=execution_log_json,
                raw_response=envelope.raw_response,
            )

        # ---- Failure-style derivation (moved from execute_task httpx branch) ----
        # #678 salvage: surface what telemetry the agent captured before it
        # wedged. Sanitize the partial metadata as defense-in-depth.
        partial_metadata = sanitize_dict(metadata) if metadata else {}
        salvage_cost_raw = partial_metadata.get("cost_usd") if partial_metadata else None
        salvage_context = _compute_context_used(partial_metadata) if partial_metadata else None
        salvage_context_max = (
            (partial_metadata.get("context_window") or 200000) if partial_metadata else None
        )
        if envelope.previous_attempt_cost > 0:
            base = salvage_cost_raw if isinstance(salvage_cost_raw, (int, float)) else 0.0
            salvage_cost: Optional[float] = base + envelope.previous_attempt_cost
        else:
            salvage_cost = salvage_cost_raw

        won = True
        if eid:
            won = db.update_execution_status(
                execution_id=eid,
                # #679: honor envelope.status so this non-success applier also
                # finalizes CANCELLED (and any future non-success terminal). A
                # no-op for every current caller — all still pass FAILED.
                status=envelope.status,
                error=envelope.error,
                cost=salvage_cost,
                context_used=salvage_context,
                context_max=salvage_context_max,
                retry_count=envelope.retry_count or None,
            )
        # #671/H4: complete the activity, record the AUTH breaker outcome, and
        # release the slot ONLY if this writer won the CAS.
        if won and activity_id:
            await activity_service.complete_activity(
                activity_id=activity_id,
                status=ActivityState.FAILED,
                error=envelope.error,
            )
        # #526 D10: AUTH-only counting — a non-auth failure never touches the
        # breaker (None would falsely reset it).
        if won and envelope.error_code == TaskExecutionErrorCode.AUTH:
            await _record_dispatch_terminal(
                agent_name, breaker_enabled, TaskExecutionErrorCode.AUTH
            )
        if won and release_slot and eid:
            await capacity.release(agent_name, eid)

        return TaskExecutionResult(
            execution_id=eid or "",
            # #679: mirror the persisted status (CANCELLED for a cancel terminal,
            # FAILED otherwise) so callers branch on the real outcome.
            status=envelope.status,
            response="",
            error=envelope.error,
            error_code=envelope.error_code,
            cost=salvage_cost,
            context_used=salvage_context,
            context_max=salvage_context_max,
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
