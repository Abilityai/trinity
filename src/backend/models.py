"""
Pydantic models for the Trinity backend API.
"""
from pydantic import BaseModel, Field
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum

from utils.helpers import to_utc_iso
from db_models import WebFileUpload  # noqa: F401 — re-exported for router imports


class AgentConfig(BaseModel):
    """Configuration for creating a new agent."""
    name: str
    type: Optional[str] = "business-assistant"
    base_image: str = "trinity-agent-base:latest"
    resources: Optional[dict] = {"cpu": "2", "memory": "4g"}
    tools: Optional[List[str]] = ["filesystem", "web_search"]
    mcp_servers: Optional[List[str]] = []
    custom_instructions: Optional[str] = None
    port: Optional[int] = None  # SSH port (auto-assigned if None)
    template: Optional[str] = None  # Template to initialize agent from
    # GitHub-native agent support
    github_repo: Optional[str] = None  # GitHub repo (e.g., "Abilityai/agent-ruby")
    github_credential_id: Optional[str] = None  # Credential ID for GitHub PAT
    # GitHub source mode (unidirectional pull from a branch)
    source_branch: Optional[str] = "main"  # Branch to pull updates from
    source_mode: Optional[bool] = True  # True = track source branch (pull only), False = create working branch
    # Multi-runtime support
    runtime: Optional[str] = "claude-code"  # "claude-code" or "gemini-cli"
    runtime_model: Optional[str] = None  # Model override (e.g., "sonnet-4.5", "gemini-2.5-pro")
    # Security options
    full_capabilities: Optional[bool] = False  # True = Docker default caps (apt-get works), False = restricted (secure default)


class AgentStatus(BaseModel):
    """Status of an agent container."""
    name: str
    type: str
    status: str
    port: int  # SSH port only - UI no longer exposed externally
    created: datetime
    resources: dict
    container_id: Optional[str] = None
    template: Optional[str] = None
    runtime: Optional[str] = "claude-code"  # "claude-code" or "gemini-cli"
    base_image_version: Optional[str] = None  # Version of trinity-agent-base image

    class Config:
        json_encoders = {
            # Use to_utc_iso to ensure 'Z' suffix for frontend compatibility
            datetime: lambda v: to_utc_iso(v) if v else None
        }


class User(BaseModel):
    """Authenticated user."""
    id: int
    username: str
    email: Optional[str] = None
    role: str = "user"
    # For agent-scoped MCP API keys, this is the agent name
    agent_name: Optional[str] = None


class Token(BaseModel):
    """JWT token response.

    Normally carries ``access_token``. When enterprise 2FA (#5) requires a
    second factor, the login endpoint instead returns ``mfa_required`` +
    ``challenge_token`` and no ``access_token`` — the client completes the
    flow at ``/api/enterprise/2fa/login/*`` to obtain the real token. The 2FA
    fields are always absent in OSS-only builds.
    """
    access_token: Optional[str] = None
    token_type: str = "bearer"
    mfa_required: Optional[bool] = None
    mfa_enrolled: Optional[bool] = None
    enrollment_required: Optional[bool] = None
    challenge_token: Optional[str] = None


class ChatMessageRequest(BaseModel):
    """Request model for chat messages."""
    message: str
    model: Optional[str] = None  # Model alias: sonnet, opus, haiku, or full model name


class ModelChangeRequest(BaseModel):
    """Request model for changing agent's model."""
    model: str  # Model alias: sonnet, opus, haiku, or full model name


class ParallelTaskRequest(BaseModel):
    """Request model for parallel task execution (stateless, no conversation context)."""
    message: str  # The task to execute (may include context prompt with history)
    model: Optional[str] = None  # Model override: sonnet, opus, haiku, or full model name
    allowed_tools: Optional[List[str]] = None  # Tool restrictions (--allowedTools)
    system_prompt: Optional[str] = None  # Additional instructions (--append-system-prompt)
    timeout_seconds: Optional[int] = None  # DEPRECATED (#1068, demotion PR 1): per-task override. Agent execution_timeout_seconds (#665) / schedule cap (#913) is authoritative; honored-but-clamped to the agent cap for now, to be removed after one release of soak. None = use agent's config.
    max_turns: Optional[int] = None  # Maximum agentic turns (--max-turns) for runaway prevention
    async_mode: Optional[bool] = False  # If true, return immediately with execution_id (fire-and-forget)
    save_to_session: Optional[bool] = False  # If true, persist messages to chat_sessions (for authenticated Chat tab)
    user_message: Optional[str] = None  # Original user message (without context), used when save_to_session=True
    create_new_session: Optional[bool] = False  # If true, close existing active sessions and create a new one
    chat_session_id: Optional[str] = None  # Explicit chat session ID to save messages to (for continuing existing sessions)
    resume_session_id: Optional[str] = None  # Claude Code session ID to resume (EXEC-023)
    inject_result: Optional[bool] = False  # If true and self-task, inject result as message in originating chat session (SELF-EXEC-001)
    files: Optional[List[WebFileUpload]] = None  # File attachments (#364)


# ============================================================================
# Activity Stream Models
# ============================================================================

class ActivityType(str, Enum):
    """Types of activities that can be tracked."""
    # Chat activities
    CHAT_START = "chat_start"
    CHAT_END = "chat_end"
    TOOL_CALL = "tool_call"

    # Schedule activities
    SCHEDULE_START = "schedule_start"
    SCHEDULE_END = "schedule_end"

    # Collaboration activities
    AGENT_COLLABORATION = "agent_collaboration"

    # Self-execute activities (agent runs background task on itself during chat)
    SELF_TASK = "self_task"

    # Execution control activities
    EXECUTION_CANCELLED = "execution_cancelled"

    # Future activity types (not yet implemented)
    FILE_ACCESS = "file_access"
    MODEL_CHANGE = "model_change"
    CREDENTIAL_RELOAD = "credential_reload"
    GIT_SYNC = "git_sync"


class ActivityState(str, Enum):
    """State of an activity."""
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class ActivityCreate(BaseModel):
    """Request model for creating a new activity."""
    agent_name: str
    activity_type: ActivityType
    activity_state: ActivityState = ActivityState.STARTED
    parent_activity_id: Optional[str] = None
    user_id: Optional[int] = None
    triggered_by: str = "user"  # user, schedule, agent, system
    related_chat_message_id: Optional[str] = None
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None
    error: Optional[str] = None


class Activity(BaseModel):
    """Activity record from database."""
    id: str
    agent_name: str
    activity_type: str
    activity_state: str
    parent_activity_id: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    user_id: Optional[int] = None
    triggered_by: str
    related_chat_message_id: Optional[str] = None
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None
    error: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True


# ============================================================================
# Execution Queue Models (Parallel Execution Prevention)
# ============================================================================

class ExecutionSource(str, Enum):
    """Source of an execution request."""
    USER = "user"       # User chat via UI
    SCHEDULE = "schedule"  # Scheduled task
    AGENT = "agent"     # Agent-to-agent via MCP


class TaskExecutionStatus(str, Enum):
    """
    Canonical status values for task/schedule executions (RELIABILITY-005).

    State machine — allowed transitions and authorized writers:

        [create]  → QUEUED       writer: TaskExecutionService / BacklogService
        QUEUED    → RUNNING      writer: BacklogService (drain) / TaskExecutionService
        RUNNING   → SUCCESS      writer: TaskExecutionService (agent HTTP response — always wins)
        RUNNING   → FAILED       writer: TaskExecutionService / CleanupService (guarded: no overwrite of terminal)
        RUNNING   → CANCELLED    writer: terminate handler (guarded)
        RUNNING   → PENDING_RETRY writer: scheduler retry handler (#271)
        PENDING_RETRY → RUNNING  writer: scheduler retry dispatch
        any       → SKIPPED      writer: TaskExecutionService (capacity overflow path)

    CAS invariant (db/schedules.py update_execution_status): SUCCESS writes are
    unconditional; all other terminal writes are blocked if the row is already
    in a terminal state, preventing cleanup paths from overwriting a real completion.
    """
    QUEUED = "queued"          # Persisted async task waiting for a free slot (BACKLOG-001)
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    PENDING_RETRY = "pending_retry"  # Awaiting retry dispatch (#271)


class BusinessStatus(str, Enum):
    """
    Business validation status for task executions (VALIDATE-001).

    Separate from technical TaskExecutionStatus — an execution can complete
    successfully (technical status) but fail business validation.
    """
    PENDING_VALIDATION = "pending_validation"  # Execution completed, awaiting validation
    VALIDATED = "validated"                     # Validation passed
    FAILED_VALIDATION = "failed_validation"    # Validation found incomplete/incorrect work
    SKIPPED = "skipped"                        # Validation not configured for this schedule


class QueueItemStatus(str, Enum):
    """Status of an execution request in the in-memory/Redis execution queue."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class Execution(BaseModel):
    """
    Represents an execution request in the agent queue.

    Used to track and serialize requests for platform-level queuing.
    Only one execution can run per agent at a time.
    """
    id: str                                    # UUID
    agent_name: str
    source: ExecutionSource
    source_agent: Optional[str] = None         # If source == AGENT
    source_user_id: Optional[str] = None       # User who triggered
    source_user_email: Optional[str] = None    # User email for tracking
    message: str                               # The chat message
    queued_at: datetime
    started_at: Optional[datetime] = None
    status: QueueItemStatus = QueueItemStatus.QUEUED

    class Config:
        json_encoders = {
            # Use to_utc_iso to ensure 'Z' suffix for frontend compatibility
            datetime: lambda v: to_utc_iso(v) if v else None
        }


class QueueStatus(BaseModel):
    """Status of an agent's execution queue."""
    agent_name: str
    is_busy: bool
    current_execution: Optional[Execution] = None
    queue_length: int
    queued_executions: List[Execution] = []


# ============================================================================
# System Manifest Models (Recipe-based Multi-Agent Deployment)
# ============================================================================

class SystemAgentConfig(BaseModel):
    """Configuration for a single agent in a system manifest."""
    template: str  # e.g., "github:Org/repo" or "local:business-assistant"
    resources: Optional[dict] = None  # {"cpu": "2", "memory": "4g"}
    folders: Optional[dict] = None  # {"expose": bool, "consume": bool}
    schedules: Optional[List[dict]] = None  # [{name, cron, message, ...}]
    tags: Optional[List[str]] = None  # Additional tags for this agent (ORG-001 Phase 4)


class SystemPermissions(BaseModel):
    """Permission configuration for system agents."""
    preset: Optional[str] = None  # "full-mesh", "orchestrator-workers", "none"
    explicit: Optional[Dict[str, List[str]]] = None  # {"orchestrator": ["worker1", "worker2"]}


class SystemViewConfig(BaseModel):
    """Configuration for auto-creating a System View on deploy (ORG-001 Phase 4)."""
    name: str  # Display name for the view
    icon: Optional[str] = None  # Emoji icon
    color: Optional[str] = None  # Hex color
    shared: bool = True  # Visible to all users?


class SystemManifest(BaseModel):
    """Parsed system manifest from YAML."""
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None
    # ORG-001 Phase 4: Tags and System View support
    default_tags: Optional[List[str]] = None  # Applied to all agents in manifest
    system_view: Optional[SystemViewConfig] = None  # Auto-create System View on deploy


class SystemDeployRequest(BaseModel):
    """Request to deploy a system from YAML manifest."""
    manifest: str  # Raw YAML string
    dry_run: bool = False


class SystemDeployResponse(BaseModel):
    """Response from system deployment."""
    status: str  # "deployed" or "valid" (for dry_run)
    system_name: str
    agents_created: List[str]  # Final agent names created
    agents_to_create: Optional[List[dict]] = None  # For dry_run: [{name, template}]
    prompt_updated: bool
    permissions_configured: int = 0
    schedules_created: int = 0
    tags_configured: int = 0  # ORG-001 Phase 4: Number of tags applied
    system_view_created: Optional[str] = None  # ORG-001 Phase 4: View ID if created
    warnings: List[str] = []


# ============================================================================
# Local Agent Deployment Models
# ============================================================================

class CredentialImportResult(BaseModel):
    """Result of importing a single credential."""
    status: str  # "created", "reused", "renamed"
    name: str
    original: Optional[str] = None  # Original name if renamed


class VersioningInfo(BaseModel):
    """Versioning information for local agent deployment."""
    base_name: str
    previous_version: Optional[str] = None
    previous_version_stopped: bool = False
    new_version: str


class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz
    name: Optional[str] = None  # Override name from template.yaml
    credentials: Optional[Dict[str, str]] = None  # Optional credentials to inject {KEY: value}


# Maximum credentials allowed per deploy-local request
MAX_DEPLOY_CREDENTIALS = 100


class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    credentials_imported: Optional[Dict[str, str]] = None  # Files found in archive
    credentials_injected: Optional[int] = None  # Count of credentials injected
    warnings: List[str] = []  # Advisory deploy-time warnings (e.g. MCP credential gaps)
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors


# ============================================================================
# Credential Injection Models (CRED-002: Simplified Credential System)
# ============================================================================

class CredentialInjectRequest(BaseModel):
    """Request to inject credential files directly into an agent."""
    files: Dict[str, str] = {}       # text files: {".env": "KEY=value\n...", ".mcp.json": "{}"}
    files_b64: Dict[str, str] = {}   # binary files: {path: base64(content)} (#11 — .p12/.pfx/DER)


class CredentialInjectResponse(BaseModel):
    """Response from credential injection."""
    status: str  # "success"
    files_written: List[str]
    message: str


class CredentialExportResponse(BaseModel):
    """Response from exporting credentials to encrypted file."""
    status: str  # "success"
    encrypted_file: str  # Path to .credentials.enc
    files_exported: int


class CredentialImportResponse(BaseModel):
    """Response from importing credentials from encrypted file."""
    status: str  # "success"
    files_imported: List[str]
    message: str


class InternalDecryptInjectRequest(BaseModel):
    """Request for internal decrypt-and-inject (startup.sh)."""
    agent_name: str


# ============================================================================
# GitHub PAT Propagation Models (#211)
# ============================================================================

class AgentPropagationStatus(BaseModel):
    """Per-agent result when propagating the global GitHub PAT."""
    agent_name: str
    # "updated", "skipped_per_agent_pat", "skipped_no_pat", "failed"
    status: str
    error: Optional[str] = None


class GithubPatPropagationResult(BaseModel):
    """Aggregate result of a GitHub PAT propagation run."""
    total_running: int
    updated: List[str]
    skipped: List[AgentPropagationStatus]
    failed: List[AgentPropagationStatus]


# =============================================================================
# Outbound File Sharing (FILES-001)
# =============================================================================

class ShareFileRequest(BaseModel):
    """Body for POST /api/internal/agent-files/share (internal, agent-server path)."""
    agent_name: str = Field(..., max_length=128)
    filename: str = Field(..., min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    expires_in: Optional[int] = None
    # NOTE: `one_time` is deferred — the schema retains the columns
    # so we can re-enable it later without a migration.


class ShareFileMcpRequest(BaseModel):
    """Body for POST /api/agents/{agent_name}/shared-files (MCP path).

    The agent_name lives in the URL, so the body only needs the
    per-share parameters.
    """
    filename: str = Field(..., min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    expires_in: Optional[int] = None


class ShareFileResponse(BaseModel):
    """Response payload for a successful share."""
    file_id: str
    url: str
    expires_at: str
    size_bytes: int
    mime_type: Optional[str] = None


class SharedFileInfo(BaseModel):
    """One row in the owner's file-sharing panel."""
    file_id: str
    filename: str
    size_bytes: int
    mime_type: Optional[str] = None
    url: str
    created_at: str
    expires_at: str
    download_count: int
    last_downloaded_at: Optional[str] = None


class SharedFilesList(BaseModel):
    """Response for GET /api/agents/{name}/shared-files."""
    agent_name: str
    files: List[SharedFileInfo]
    total_bytes: int
    quota_bytes: int


class AgentDataImportResponse(BaseModel):
    """Response for POST /api/agents/{name}/data/import (#1169).

    `restored`/`skipped` come straight from the agent-server restore
    primitive (`restore_from_tar`); `skipped` entries fell outside the
    `data/**` allowlist or tripped a path-traversal guard.
    """
    agent_name: str
    restored: List[str]
    skipped: List[str]
    bytes_received: int


class AgentDefaultResourcesUpdate(BaseModel):
    """Body for PUT /api/settings/agent-defaults/resources (RES-001)."""
    cpu: Optional[str] = None
    memory: Optional[str] = None


class AgentDefaultAccessPolicyUpdate(BaseModel):
    """Body for PUT /api/settings/agent-defaults/access-policy (#1129)."""
    require_email: Optional[bool] = None


# ---------------------------------------------------------------------------
# Fleet Executions (EXEC-022 / Issue #18)
# ---------------------------------------------------------------------------

class FleetExecutionSummary(BaseModel):
    """Lightweight execution row for the Unified Executions Dashboard list.

    Excludes large fields (response, tool_calls, execution_log).
    error_summary is a 200-char truncation for failed-row one-liners.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    message: str
    triggered_by: str
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    error_summary: Optional[str] = None
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    model_used: Optional[str] = None
    fan_out_id: Optional[str] = None
    business_status: Optional[str] = None
    validation_execution_id: Optional[str] = None
    queued_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: to_utc_iso(v) if v else None}


class FleetExecutionStats(BaseModel):
    """Aggregate stats for the Unified Executions Dashboard stat cards."""
    total: int
    success_count: int
    failed_count: int
    running_count: int
    queued_count: int
    total_cost: float
    success_rate: float
    hours: int  # 0 = all-time


class CircuitBreakerConfigUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/circuit-breaker (RELIABILITY-007, #526).

    Per-agent opt-in for the dispatch breaker. Gated again by the global
    DISPATCH_BREAKER_ENABLED master switch — both must be on to engage.
    """
    enabled: bool


class ExecutionResultEnvelope(BaseModel):
    """Body for POST /api/agents/{name}/executions/{id}/result (#1083).

    The typed terminal an agent POSTs back after a fire-and-forget turn. The
    backend does NOT re-classify — ``status``/``error_code`` are authoritative
    and flow straight into ``TaskExecutionService.apply_result``. ``metadata``
    carries the same shape the synchronous ``/api/task`` response does (cost_usd,
    context_window, token counts, compact_events, session_id).

    Field caps bound abuse from a buggy/compromised agent while staying well
    above a legitimate large transcript (the sync path already accepts these):
    enforced in the router after parse so the failure is a clean 413, not a
    Pydantic 422 (the agent's retry logic special-cases status codes).
    """
    status: str = Field(..., description="'success' or 'failed'")
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    terminal_reason: Optional[str] = None  # completed|max_duration|stall_no_output|auth|empty_result
    metadata: Optional[Dict] = None
    execution_log: Optional[List] = None
    session_id: Optional[str] = None
    execution_time_ms: Optional[int] = None


# =============================================================================
# Soft-Delete Admin Recovery (#834 Phase 1c)
# =============================================================================

class SoftDeletedAgent(BaseModel):
    """Response item for GET /api/admin/soft-deleted/agents."""
    agent_name: str
    owner_id: int
    created_at: str
    deleted_at: str
    # When the retention sweep would hard-purge this row (None when
    # the retention setting is 0 = disabled).
    purge_eta: Optional[str]


class SoftDeletedSchedule(BaseModel):
    """Response item for GET /api/admin/soft-deleted/schedules."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    owner_id: int
    enabled: bool
    deleted_at: str
    purge_eta: Optional[str]


# =============================================================================
# Schedule Analytics (#868)
# =============================================================================
#
# Per-schedule distributions over `schedule_executions`. Per-agent rollup
# and per-chat-session analytics deferred to #18 and a follow-up issue
# respectively — see #868 issue body "Out of Scope" section for the
# decision context.


class DurationPercentiles(BaseModel):
    """Duration percentiles in milliseconds. All null when the schedule
    has fewer than 1 successful execution in the window."""
    p50: Optional[int] = None
    p95: Optional[int] = None
    p99: Optional[int] = None


class CostTotals(BaseModel):
    """Cost totals in USD for the analytics window."""
    total: float = 0.0


class ToolCallEntry(BaseModel):
    """One row of the top-N tool-call distribution."""
    name: str
    total_duration_ms: int


class ToolCallSummary(BaseModel):
    """Tool-call distribution weighted by total wall time per tool.

    Top-N is intentionally weighted by `sum(duration_ms)` rather than
    raw count — raw count is dominated by `Read` / `Bash` on every
    agent and has low signal-to-noise. Locked by /autoplan strategy
    finding #6.
    """
    top: List[ToolCallEntry] = []
    total_calls: int = 0


class TimelineEntry(BaseModel):
    """One UTC-day bucket on the analytics timeline. Zero-filled for
    days that had no executions (Python-side gap fill) so chart
    libraries render a continuous x-axis."""
    date: str
    success: int
    failed: int
    cost: float


class ScheduleAnalyticsResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/schedules/{schedule_id}/analytics.

    `sampled` reports whether the percentile / tool-call pool was
    capped (currently 5000 newest success rows). Counts and timeline
    are always unsampled. UTC day boundaries.
    """
    window_hours: int
    total_executions: int
    success_count: int
    failed_count: int
    cancelled_count: int
    success_rate: float
    duration_ms: DurationPercentiles
    cost: CostTotals
    tool_calls: ToolCallSummary
    timeline: List[TimelineEntry]
    sampled: bool = False
    sample_size: int = 0


# ---------------------------------------------------------------------------
# Agent-scoped Overview analytics (#1107) — generalises the #868 per-schedule
# analytics to agent scope with a `triggered_by` type breakdown. Backs the
# Agent Detail "Overview" trend charts.
# ---------------------------------------------------------------------------


class DurationStats(BaseModel):
    """Overall duration stats for the window (milliseconds). `avg` is the
    SQL mean over the *full* success rowset; `p95` is computed over the
    newest capped pool. Both null when the agent has no successful runs
    with a duration in the window."""
    avg: Optional[int] = None
    p95: Optional[int] = None


class AgentTypeTotal(BaseModel):
    """Per-bucket execution total for the window. `bucket` is a user-facing
    grouping of the raw `triggered_by` values (Chat/Tasks, MCP, Channels,
    Public, Scheduled, Agent-to-agent, Voice, Other)."""
    bucket: str
    total: int


class AgentAnalyticsTimelinePoint(BaseModel):
    """One UTC-day bucket for the Overview charts. `success_rate`,
    `duration_avg_ms`, and `context_avg` are null on days with no
    qualifying rows so the chart renders a gap rather than a false zero.
    `by_type` maps present buckets → that day's count (drives the stacked
    bars)."""
    date: str
    total: int
    success: int
    failed: int
    success_rate: Optional[float] = None
    duration_avg_ms: Optional[int] = None
    context_avg: Optional[int] = None
    by_type: Dict[str, int] = {}


class AgentAnalyticsResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/analytics (#1107).

    Deterministic, DB-sourced agent activity over a rolling window.
    `by_type` groups raw `triggered_by` into user-facing buckets (with an
    "Other" catch-all so a new trigger type never silently vanishes);
    `buckets` is the ordered legend / stack order for the chart.
    `success_rate` is terminal-based (success / (success + failed)).
    `sampled` reports whether the p95 pool was capped — `avg` is always
    full-set, never sampled. UTC day boundaries.
    """
    window_hours: int
    total_executions: int
    success_count: int
    failed_count: int
    success_rate: float
    duration_ms: DurationStats
    context_avg: Optional[int] = None
    by_type: List[AgentTypeTotal] = []
    buckets: List[str] = []
    timeline: List[AgentAnalyticsTimelinePoint] = []
    sampled: bool = False
    sample_size: int = 0


class ScheduleSummaryRow(BaseModel):
    """One per-schedule performance rollup (#1115).

    `success_rate` is terminal-based (success / (success + failed [incl.
    `error`])) and `None` when there were zero terminal runs in the window —
    the UI renders `—`, not a false 0%. `avg_duration_ms` / `context_avg` are
    `None` when nothing measurable ran. A zero-run schedule still appears
    (all counts 0, rates `None`).
    """
    schedule_id: str
    name: str
    command: str = ""
    cron_expression: str
    enabled: bool
    total_executions: int
    success_count: int
    failed_count: int
    cancelled_count: int
    success_rate: Optional[float] = None
    avg_duration_ms: Optional[int] = None
    cost_total: float
    context_avg: Optional[int] = None
    tool_call_total: int
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None


class AgentSchedulesSummaryResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/schedules/analytics-summary (#1115).

    One compact rollup row per non-deleted schedule for the window — consumed
    by BOTH the Overview "Schedules performance" section and the Schedules-tab
    inline stats from a single call (no N per-schedule round-trips).
    `tool_calls_sampled` flags when the agent-wide tool-call parse pool was
    capped. UTC window via `iso_cutoff`.
    """
    window_hours: int
    schedule_count: int
    tool_calls_sampled: bool = False
    schedules: List[ScheduleSummaryRow] = []


# =============================================================================
# Agent Compatibility Validation (#668)
# =============================================================================

class CompatibilityCheck(BaseModel):
    """Result of a single compatibility check (one row from the spec catalog).

    `status` is the check outcome: "pass" (compliant), "fail" (issue found), or
    "skipped" (not evaluated — e.g. AI checks with no API key, or a check that
    doesn't apply to this agent's runtime). `severity` is the catalog severity
    (hard | soft | info); AI-evaluated checks are capped at SOFT since their
    verdict is non-deterministic (HARD is reserved for deterministic STATIC
    checks). `detail` carries safe, redacted specifics (line numbers, patterns)
    — never a secret value.
    """
    check_id: str  # "F-001", "S-003", "C-002", ...
    category: str  # human-readable category name
    severity: str  # "hard" | "soft" | "info"
    type: str  # "static" | "ai"
    status: str  # "pass" | "fail" | "skipped"
    message: str
    auto_fixable: bool = False
    explanation: Optional[str] = None  # AI rationale / extra context (markdown)
    confidence: Optional[float] = None  # AI confidence 0..1 (None for STATIC)
    detail: Optional[Dict] = None  # redacted specifics (location, pattern)
    skip_reason: Optional[str] = None  # why a check was skipped


class CompatibilityReport(BaseModel):
    """Aggregate compatibility report for one agent (#668).

    `overall_status`: "compatible" (no hard/soft failures), "issues" (≥1
    hard/soft failure), or "unavailable" (couldn't read the workspace — e.g.
    agent stopped, collector failure). `container_running` distinguishes the
    degraded-stopped case from a genuine clean result. `ai_ran_at` is the
    timestamp of the last AI evaluation (None if never run) so the UI can show
    staleness and a re-run affordance.
    """
    agent_name: str
    container_running: bool
    overall_status: str  # "compatible" | "issues" | "unavailable"
    runtime: Optional[str] = None  # agent runtime (claude | gemini | codex)
    checks: List[CompatibilityCheck] = []
    hard_count: int = 0
    soft_count: int = 0
    info_count: int = 0
    ai_ran_at: Optional[str] = None
    static_ran_at: Optional[str] = None
    message: Optional[str] = None  # human note for the unavailable case


class CompatibilityFixRequest(BaseModel):
    """Request to auto-fix a single correctable compatibility check (#668)."""
    check_id: str


class CompatibilityFixResponse(BaseModel):
    """Result of an auto-fix attempt (#668).

    `uncommitted` is always true on success: the fix edits the in-container
    `.gitignore` only — committing/pushing is the agent's own git-sync job, so
    the change is not yet on GitHub until the next sync.
    """
    check_id: str
    fixed: bool
    message: str
    uncommitted: bool = True
