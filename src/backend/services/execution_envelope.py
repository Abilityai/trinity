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
