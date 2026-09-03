"""
Pydantic models for the scheduler service.

These are standalone models that mirror the main app's models
but are independent for the scheduler service.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


class ExecutionStatus(str, Enum):
    """Status of a schedule execution."""
    # #2391: a dispatch the backend handed to the durable pull queue instead of
    # pushing (pull-pilot agents only). NON-terminal — the agent's worker claims
    # the row back to `running` — so `_poll_execution_completion` must keep
    # polling through it, exactly as it does through `running`.
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"  # Added for Issue #46 - records when execution was dropped
    PENDING_RETRY = "pending_retry"  # Added for RETRY-001 - retry scheduled but not yet fired


class TriggerSource(str, Enum):
    """What triggered the execution."""
    SCHEDULE = "schedule"
    MANUAL = "manual"
    API = "api"
    RETRY = "retry"  # Added for RETRY-001 - automatic retry of failed execution


@dataclass
class Schedule:
    """A scheduled task definition."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str]
    owner_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    # #913: None = inherit from agent_ownership.execution_timeout_seconds.
    # Scheduler must round-trip None through to /api/internal/execute-task so
    # the backend's TaskExecutionService falls back to the per-agent value.
    # Treating NULL as 900 was the bug.
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None  # None = all tools allowed
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
    # Retry configuration (RETRY-001). 0 = disabled (default, #476), 1-5 opt-in.
    max_retries: int = 0
    retry_delay_seconds: int = 60  # Seconds between retries (30-600 range)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False  # Enable post-execution validation
    validation_prompt: Optional[str] = None  # Custom auditor instructions
    validation_timeout_seconds: int = 120  # Timeout for validation task


@dataclass
class ScheduleExecution:
    """A record of a schedule execution."""
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    message: str
    triggered_by: str
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    response: Optional[str] = None
    error: Optional[str] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None
    # Origin tracking fields (AUDIT-001)
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Retry tracking (RETRY-001)
    attempt_number: int = 1  # Which attempt this is (1 = first try)
    retry_of_execution_id: Optional[str] = None  # Links retry to original execution
    retry_scheduled_at: Optional[datetime] = None  # When retry is scheduled (for restart recovery)
    # Validation tracking (VALIDATE-001)
    business_status: Optional[str] = None  # pending_validation, validated, failed_validation, skipped
    validated_at: Optional[datetime] = None  # When validation completed
    validation_execution_id: Optional[str] = None  # FK to validation execution
    validates_execution_id: Optional[str] = None  # FK to execution being validated


# SQLite stores INTEGER as a signed 64-bit value and RAISES on anything wider
# rather than truncating, so this is the real bound on `source_user_id` — a
# parser that only type-checks lets a payload field fail the INSERT (#1970,
# found by /edge-cases). PostgreSQL `integer`/`bigint` are narrower or equal, so
# clamping here is safe on both backends.
_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1


@dataclass
class ExecutionOrigin:
    """Who initiated an execution (AUDIT-001, #1970).

    One value object instead of five parallel parameters threaded through
    ``_trigger_handler → _execute_manual_trigger → _execute_schedule_with_lock
    → create_execution`` — five positional siblings at four call depths is how
    one of them silently stops being forwarded.

    Attribution only; **nothing authorizes on these fields.** An all-``None``
    origin is the correct, honest record for a cron tick, which has no caller.
    """
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    agent_name: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None

    def is_empty(self) -> bool:
        # Compared against None, NOT truthiness. `user_id=0` is a populated
        # origin, but `not any((0, ...))` reported it empty. Found by
        # /edge-cases, Hypothesis-shrunk to `{"source_user_id": 0}`. Nothing in
        # `src/` gated on this yet — which is exactly why it was worth fixing
        # before the first caller inherited a helper that lies about a valid
        # value.
        return all(
            value is None
            for value in (self.user_id, self.user_email, self.agent_name,
                          self.mcp_key_id, self.mcp_key_name)
        )

    @classmethod
    def from_payload(cls, body: object) -> "ExecutionOrigin":
        """Build from an untrusted JSON body (the scheduler's trigger endpoint).

        Validates at the boundary: wrong types are dropped rather than coerced,
        wrong RANGES likewise, and strings are length-capped — so a malformed or
        oversized payload cannot write junk into an append-only-in-spirit audit
        column, nor break the write. A caller that lies about its identity is
        not a new exposure — ``triggered_by`` has been caller-supplied on this
        same endpoint all along, and the scheduler is reachable only from the
        platform network.
        """
        if not isinstance(body, dict):
            return cls()

        def _str(key: str) -> Optional[str]:
            value = body.get(key)
            if not isinstance(value, str):
                return None
            value = value.strip()
            return value[:255] or None

        user_id = body.get("source_user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            user_id = None
        elif not (_SQLITE_INT_MIN <= user_id <= _SQLITE_INT_MAX):
            # Found by /edge-cases. The type check alone was not enough: the
            # column is INTEGER and SQLite raises `OverflowError: Python int too
            # large to convert to SQLite INTEGER` rather than truncating, so an
            # out-of-range value from this untrusted body escaped into the
            # INSERT and took the whole dispatch with it — silently losing the
            # run on the #1970 path (the exception lands in a blanket `except`
            # AFTER the endpoint already answered "triggered"). Dropped to None
            # like every other unusable input here: attribution is never worth
            # a failed run.
            user_id = None

        return cls(
            user_id=user_id,
            user_email=_str("source_user_email"),
            agent_name=_str("source_agent_name"),
            mcp_key_id=_str("source_mcp_key_id"),
            mcp_key_name=_str("source_mcp_key_name"),
        )


@dataclass
class Reminder:
    """A durable one-shot agent self-reminder (#1296).

    ``fire_at``/``firing_at`` are **naive UTC** datetimes (parsed via
    ``parse_scheduler_ts``) so a ``DateTrigger(run_date=fire_at)`` under the
    scheduler's ``timezone=pytz.UTC`` is interpreted correctly, and the
    ``fire_at < datetime.utcnow()`` past-due comparison never hits the
    offset-aware-vs-naive ``TypeError`` (#1472/#1474).
    """
    id: str
    agent_name: str
    message: str
    fire_at: datetime
    status: str  # pending | firing | fired | cancelled | failed
    fire_attempts: int = 0
    firing_at: Optional[datetime] = None
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    execution_id: Optional[str] = None
    error: Optional[str] = None
    # Provenance captured when the reminder was set (#1296). Carried onto the
    # execution row the reminder fires, so a reminder-triggered run is
    # attributable instead of anonymous (AUDIT-001, #1970).
    owner_id: Optional[int] = None
    created_by_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None


@dataclass
class AgentTaskMetrics:
    """Metrics extracted from agent task response."""
    context_used: int = 0
    context_max: int = 200000
    context_percent: float = 0.0
    cost_usd: Optional[float] = None
    tool_calls_json: Optional[str] = None
    execution_log_json: Optional[str] = None
    session_id: Optional[str] = None  # Claude Code session ID for --resume (EXEC-023)


@dataclass
class AgentTaskResponse:
    """Parsed response from agent task endpoint."""
    response_text: str
    metrics: AgentTaskMetrics
    raw_response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulerStatus:
    """Current status of the scheduler service."""
    running: bool
    jobs_count: int
    last_check: datetime
    uptime_seconds: float
    jobs: List[Dict[str, Any]] = field(default_factory=list)


# =============================================================================
# Process Scheduling Models
# =============================================================================


@dataclass
class ProcessSchedule:
    """
    A scheduled process trigger definition.

    Represents a schedule trigger defined in a process definition.
    When the cron fires, the scheduler executes the process.
    """
    id: str  # Unique schedule ID
    process_id: str  # Process definition ID
    process_name: str  # Process name (denormalized for display)
    trigger_id: str  # Trigger ID from process definition
    cron_expression: str
    enabled: bool
    timezone: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None


@dataclass
class ProcessScheduleExecution:
    """A record of a process schedule execution."""
    id: str
    schedule_id: str
    process_id: str
    process_name: str
    execution_id: Optional[str]  # Process execution ID returned by backend
    status: str
    started_at: datetime
    triggered_by: str
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
