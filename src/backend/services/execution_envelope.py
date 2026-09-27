"""Execution result vocabulary (#2314).

The three types every producer and consumer of an execution terminal shares:
the machine-readable error code, the result a caller receives, and the
normalized envelope ``apply_result`` finalizes from.

Carved out of ``task_execution_service`` because they are the one part of that
module with **no collaborators at all** — pure dataclasses and an enum, no
``db``, no HTTP, no capacity manager. That makes this module importable from
anywhere without an import cycle, which is what lets the phase modules beside
it name the same vocabulary instead of importing the service they belong to.

Re-exported from ``services.task_execution_service`` so every existing
``from services.task_execution_service import TaskExecutionResult`` keeps
working unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


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
    SKILL_NOT_FOUND = "skill_not_found"  # Slash-command message didn't resolve to an installed skill (#1410)
    EPHEMERAL_EXHAUSTED = "ephemeral_exhausted"  # Ghost agent budget spent — expired TTL or exec count (trinity-enterprise#69)
    MODEL_UNSUPPORTED = "model_unsupported"  # Claude Code refused the model — CLI too old or unknown id; never a subscription fault (#3012)


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
    # #2638: the SUB-003 switch that happened during this turn, if any — the
    # `handle_subscription_failure` / `ensure_serviceable_subscription` /
    # `fallback_to_api_key` result dict (`switched`, `new_subscription`,
    # `pre_dispatch`, `fallback`). In-memory only, like `dispatched_async`.
    #
    # It exists because "this failed" and "this failed on a subscription the
    # agent is no longer using" are different answers to the caller. Before it,
    # a turn that failed AFTER a successful switch was reported to the Workspace
    # as `retryable=False` — while the agent sat on a fresh subscription and a
    # re-send would very likely have worked (#2638 gap 4).
    subscription_switch: Optional[dict] = None


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


def terminal_from_callback_payload(payload: Any, execution_id: str) -> TerminalEnvelope:
    """Normalize an agent-reported terminal (the #1083 ``ExecutionResultEnvelope``
    shape) into the ``TerminalEnvelope`` ``apply_result`` consumes.

    ONE classification for both readers of that shape: the result-callback
    endpoint (``routers/agents.py``) and, since #2944, the cleanup watchdog
    claiming a terminal the agent retained after the backend lost the held-open
    connection. Two copies of this mapping would be two places for the
    auth-is-never-cancellation rule below to drift.

    #679: 3-way map — ``success``→SUCCESS, ``cancelled``→CANCELLED, everything
    else (incl. unknown forward-compat values) →FAILED.

    Finding 2 (CSO 2026-06-22): an auth/rate terminal must NOT be reclassified
    as a clean cancellation even when the agent labels it "cancelled". The agent
    side already guards this (``result_callback._is_auth_or_rate``), but the
    backend is the trust boundary — a buggy or mixed-version agent that reports
    ``status:"cancelled"`` carrying ``error_code:"auth"`` (or an auth/rate
    ``terminal_reason``) would otherwise silently dodge the AUTH dispatch
    breaker / SUB-003 auto-switch.

    ``payload`` is duck-typed (the Pydantic model or any object with the same
    attributes) so this leaf keeps importing nothing at module level; the
    status enum is resolved lazily for the same reason.
    """
    from models import TaskExecutionStatus  # noqa: WPS433 — leaf stays import-free at module level

    is_auth_or_rate = (
        getattr(payload, "error_code", None) == TaskExecutionErrorCode.AUTH.value
        or getattr(payload, "terminal_reason", None) in ("auth", "rate_limit")
    )
    raw_status = getattr(payload, "status", None)
    if raw_status == "success":
        status = TaskExecutionStatus.SUCCESS
    elif raw_status == "cancelled" and not is_auth_or_rate:
        status = TaskExecutionStatus.CANCELLED
    else:
        status = TaskExecutionStatus.FAILED

    error_code: Optional[TaskExecutionErrorCode] = None
    raw_code = getattr(payload, "error_code", None)
    if raw_code:
        try:
            error_code = TaskExecutionErrorCode(raw_code)
        except ValueError:
            # Unknown codes are non-fatal — apply_result only special-cases AUTH.
            error_code = None

    # #3012: an agent image older than #3012 reports a model the CLI refused as
    # 503 → error_code "auth", which would trip the AUTH dispatch breaker. The
    # error text says what it really was.
    if error_code is not None and error_code.value == TaskExecutionErrorCode.AUTH.value:
        from services.failure_classifier import is_model_rejection  # noqa: WPS433 — leaf, lazy like above

        if is_model_rejection(getattr(payload, "error", None) or ""):
            error_code = TaskExecutionErrorCode.MODEL_UNSUPPORTED

    return TerminalEnvelope(
        execution_id=execution_id,
        status=status,
        response=getattr(payload, "response", None),
        error=getattr(payload, "error", None),
        error_code=error_code,
        metadata=getattr(payload, "metadata", None) or {},
        execution_log=getattr(payload, "execution_log", None),
        session_id=getattr(payload, "session_id", None),
        execution_time_ms=getattr(payload, "execution_time_ms", None),
    )
