"""
Pydantic models for the Trinity backend API.
"""
import re

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Dict, List, Literal, Optional
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
    # For connector-scoped MCP keys, the single agent this key may consume.
    # Set ⇒ the principal is consumption-only: it may read/chat ONLY this agent
    # and may NOT perform owner or role-gated operations, even though it
    # resolves to the owner user. Edition-agnostic enforcement primitive — the
    # key itself is minted by an entitled module (core-primitive + enterprise-
    # knob, same shape as users.suspended_at #995).
    connector_agent: Optional[str] = None


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
    CANCELLED = "cancelled"  # #1332: user-cancelled terminal, distinct from FAILED


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
# Agent Reports (#918) — agent-published structured telemetry / domain reports
# ============================================================================

# Max serialized-JSON byte length of a report payload. Enforced at the router
# (oversize → HTTP 413) to bound SQLite growth and list-response weight.
REPORT_PAYLOAD_MAX_BYTES = 256 * 1024  # 256 KiB

# Renderer hints the frontend understands; an unknown/absent hint falls back to
# the report_type prefix map, then the JSON viewer.
ReportDisplayHint = Literal["table", "kpi", "markdown", "timeline", "json"]

_REPORT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def _validate_iso8601(value: Optional[str]) -> Optional[str]:
    """Reject a non-ISO-8601 period bound (None passes through)."""
    if value is None:
        return value
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("must be an ISO-8601 timestamp")
    return value


class ReportCreate(BaseModel):
    """Request body for an agent publishing a structured report (#918).

    The agent + author are resolved server-side from the MCP/JWT auth context —
    never from this body — so a report cannot be attributed to another agent.
    """
    report_type: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=300)
    payload: Dict  # byte-capped at the router (REPORT_PAYLOAD_MAX_BYTES → 413)
    display_hint: Optional[ReportDisplayHint] = None
    schema_version: int = Field(1, ge=1, le=1000)
    period_start: Optional[str] = None
    period_end: Optional[str] = None

    @field_validator("report_type")
    @classmethod
    def _check_report_type(cls, v: str) -> str:
        if not _REPORT_TYPE_RE.match(v):
            raise ValueError(
                "report_type must be namespaced lower_snake segments joined by "
                "'.', e.g. 'recon.weekly_summary'"
            )
        return v

    @field_validator("period_start", "period_end")
    @classmethod
    def _check_period_iso(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso8601(v)

    @model_validator(mode="after")
    def _check_period_order(self) -> "ReportCreate":
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start must be <= period_end")
        return self


class ReportSummary(BaseModel):
    """List-response model — metadata only, never carries ``payload`` (#918)."""
    id: str
    agent_name: str
    report_type: str
    title: str
    display_hint: Optional[str] = None
    schema_version: int = 1
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True


class Report(ReportSummary):
    """Detail-response model — full row including the decoded ``payload``."""
    user_id: Optional[int] = None
    payload: Dict


class FleetReportStats(BaseModel):
    """Aggregate stat-card data for the fleet Reports view (#918)."""
    total: int
    by_type: Dict[str, int]
    agents: int


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


def activity_state_for_terminal(status) -> "ActivityState":
    """Map a terminal execution status to the activity state that closes its
    dispatch activity (#1332).

    SUCCESS → COMPLETED, CANCELLED → CANCELLED, everything else → FAILED.

    A cancelled execution must read as a distinct, non-failure terminal so
    activity-derived views (collaboration timeline, replay, needs-attention)
    don't collapse it into FAILED. ``status`` accepts a ``TaskExecutionStatus``
    or its bare string value (both compare equal — str-backed enum). The
    explicit ``else`` keeps a future status (e.g. PENDING_RETRY) deterministic.
    """
    if status == TaskExecutionStatus.SUCCESS:
        return ActivityState.COMPLETED
    if status == TaskExecutionStatus.CANCELLED:
        return ActivityState.CANCELLED
    return ActivityState.FAILED


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
    # Effect-scoped idempotency (#1084): a re-run of the same turn sharing the
    # same file replays the original signed URL instead of minting a new token.
    execution_id: Optional[str] = Field(default=None, max_length=200)
    dedup_label: str = Field(default="", max_length=200)


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


class ClientRosterEntry(BaseModel):
    """One external channel client in the Sharing-tab roster (#20).

    An outside person (no Trinity account) who has messaged the agent through a
    channel. `identity` is the channel-native handle (Telegram @username or
    numeric id, WhatsApp phone). `display_name`/`verified_email` are null when
    unknown; `last_active` is null for a row that has never recorded activity.
    Channel-extensible: Slack/VoIP slot in without a contract change.
    """
    channel: str
    identity: str
    display_name: Optional[str] = None
    verified_email: Optional[str] = None
    message_count: int = 0
    last_active: Optional[str] = None


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


class MaxParallelTasksCeilingUpdate(BaseModel):
    """Body for PUT /api/settings/max-parallel-tasks-ceiling (#506).

    Range (1–32) is enforced in the router so an out-of-range value returns a
    400 with a descriptive message rather than a generic 422.
    """
    value: int


class AgentCapacityUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/capacity (CAPACITY-001, #506)."""
    max_parallel_tasks: int


# Max length for the public/channel custom-instructions fragment (#1205).
PUBLIC_CHANNEL_PROMPT_MAX_LEN = 4000


class PublicChannelPrompt(BaseModel):
    """Per-agent custom instructions for public & channel chats (#1205).

    Response for GET, and the stored value echoed back by PUT. `null`/empty
    means unset — a strict no-op for the agent's behavior.
    """
    public_channel_system_prompt: Optional[str] = None


class PublicChannelPromptUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/public-prompt (#1205)."""
    public_channel_system_prompt: Optional[str] = Field(
        default=None, max_length=PUBLIC_CHANNEL_PROMPT_MAX_LEN
    )


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


class McpExposedUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/mcp-exposed (#846).

    Per-agent opt-in. When enabled, the Trinity MCP server dynamically registers
    a dedicated ``chat_with_<slug>`` tool for the agent. Execution still runs the
    same access gate — this only publishes a surface.
    """
    enabled: bool


class VoiceRepliesUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/voice-replies (epic #24 / #25).

    Shared agent-level outbound-voice config: when ``enabled``, channel adapters
    speak the agent's reply via the shared TTS service using ``voice_id``
    (an ElevenLabs voice id). ``voice_id`` is required when enabling.
    """
    enabled: bool
    voice_id: Optional[str] = None

    @field_validator("voice_id")
    @classmethod
    def _strip_voice_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class PublicChannelModelUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/public-channel-model (#894).

    ``model`` is the Claude model id to use for public-facing channels (public
    link, Slack/Telegram/WhatsApp, x402). ``None`` or empty string clears the
    override so the agent inherits the platform default.
    """
    model: Optional[str] = None


class ExecutionResultEnvelope(BaseModel):
    """Body for POST /api/agents/{name}/executions/{id}/result (#1083).

    The typed terminal an agent POSTs back after a fire-and-forget turn. The
    backend does NOT re-classify — ``status``/``error_code`` are authoritative
    and flow straight into ``TaskExecutionService.apply_result``. ``metadata``
    carries the same shape the synchronous ``/api/task`` response does (cost_usd,
    context_window, token counts, compact_events, session_id).

    ``status`` is free-form ``str`` (not an enum) for forward-compatibility: the
    backend maps ``success``→SUCCESS, ``cancelled``→CANCELLED (#679), and every
    other value (incl. an unknown future status) →FAILED.

    Field caps bound abuse from a buggy/compromised agent while staying well
    above a legitimate large transcript (the sync path already accepts these):
    enforced in the router after parse so the failure is a clean 413, not a
    Pydantic 422 (the agent's retry logic special-cases status codes).
    """
    status: str = Field(..., description="'success', 'failed', or 'cancelled'")
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    terminal_reason: Optional[str] = None  # completed|max_duration|stall_no_output|auth|empty_result|cancelled
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


# =============================================================================
# Router-relocated request/response models (#654, INV-14)
# Each section below was moved verbatim from its router so Pydantic models
# live in one place (Architectural Invariant #14). One exception remains in
# routers/canary.py (RunCycleRequest) — see test_models_centralized.py.
# =============================================================================


# =============================================================================
# Agent Files Models (routers/agent_files.py)
# =============================================================================


class FileUpdateRequest(BaseModel):
    """Request body for file updates."""
    content: str


class CreateFolderRequest(BaseModel):
    """Request body for folder creation."""
    path: str


# =============================================================================
# Agent Rename Models (routers/agent_rename.py)
# =============================================================================


class RenameAgentRequest(BaseModel):
    """Request body for agent rename."""
    new_name: str


# =============================================================================
# Agent Ssh Models (routers/agent_ssh.py)
# =============================================================================


class SshAccessRequest(BaseModel):
    """Request body for SSH access."""
    ttl_hours: float = 4.0
    auth_method: str = "key"  # "key" for SSH key, "password" for ephemeral password
    public_key: Optional[str] = None  # Required for key auth — client-supplied OpenSSH public key


# =============================================================================
# Agents Models (routers/agents.py)
# =============================================================================


class HeartbeatPayload(BaseModel):
    """Lightweight liveness payload POSTed by the agent every ~5s."""
    memory_mb: Optional[float] = None
    active_executions: Optional[int] = None
    uptime_s: Optional[float] = None


# =============================================================================
# Audit Log Models (routers/audit_log.py)
# =============================================================================


class AuditLogEntry(BaseModel):
    """Single audit log row as returned to API clients."""

    id: int
    event_id: str
    event_type: str
    event_action: str
    actor_type: str
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    actor_ip: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    mcp_scope: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: str
    details: Optional[dict] = None
    request_id: Optional[str] = None
    source: str
    endpoint: Optional[str] = None
    previous_hash: Optional[str] = None
    entry_hash: Optional[str] = None
    created_at: Optional[str] = None


class AuditLogListResponse(BaseModel):
    """Paginated list response."""

    entries: List[AuditLogEntry]
    total: int
    limit: int
    offset: int


class AuditLogStatsResponse(BaseModel):
    """Aggregate counts."""

    total: int
    by_event_type: dict = Field(default_factory=dict)
    by_actor_type: dict = Field(default_factory=dict)


class AuditHeatmapCell(BaseModel):
    """Single populated bucket in the 7×24 dow×hour heatmap."""

    dow: int = Field(..., ge=0, le=6, description="Weekday (0=Sunday)")
    hour: int = Field(..., ge=0, le=23, description="Hour 0–23 UTC")
    count: int = Field(..., ge=0)


class AuditHeatmapResponse(BaseModel):
    """Sparse 7×24 dow×hour heatmap. Zero-count cells omitted."""

    cells: List[AuditHeatmapCell]
    total: int
    max_count: int


class AuditCalendarDay(BaseModel):
    """Single populated day in the calendar heatmap."""

    date: str = Field(..., description="UTC date, ISO 'YYYY-MM-DD'")
    count: int = Field(..., ge=0)


class AuditCalendarResponse(BaseModel):
    """Sparse per-day calendar heatmap (GitHub-style). Quiet days omitted."""

    days: List[AuditCalendarDay]
    total: int
    max_count: int


class AuditVerifyResponse(BaseModel):
    """Hash chain verification result."""

    valid: bool
    checked: int
    first_invalid_id: Optional[int] = None


# =============================================================================
# Avatar Models (routers/avatar.py)
# =============================================================================


class AvatarGenerateRequest(BaseModel):
    identity_prompt: str


# =============================================================================
# Canary Models (routers/canary.py)
# =============================================================================


class CanaryViolation(BaseModel):
    """Single canary_violations row as returned to API clients."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict = Field(default_factory=dict)
    signal_query: Optional[str] = None
    created_at: Optional[str] = None


class CanaryViolationListResponse(BaseModel):
    """Paginated list response."""

    violations: List[CanaryViolation]
    total: int
    limit: int
    offset: int


class CanaryStatsResponse(BaseModel):
    """Aggregate violation counts for dashboard tiles."""

    total: int
    by_invariant: dict = Field(default_factory=dict)
    by_severity: dict = Field(default_factory=dict)


class CycleViolation(BaseModel):
    """One violation persisted during a run-cycle call."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict
    signal_query: Optional[str] = None


class CycleTransition(BaseModel):
    """A green→red transition detected this cycle.

    `CanaryService` posts exactly one Slack webhook message per entry,
    mapping severity to the message styling. Surfaced here so the run-cycle
    response mirrors what the service actually emitted.
    """

    invariant_id: str
    severity: str
    violations_in_cycle: int
    previous_violation_at: Optional[str] = Field(
        None,
        description=(
            "snapshot_time of the most recent prior violation for this "
            "invariant; null if the invariant has never violated before."
        ),
    )


class RunCycleResponse(BaseModel):
    """Result of one canary cycle."""

    snapshot_time: str
    cycle_duration_ms: int
    # Invariants this cycle attempted (= the request's `invariants` filter,
    # or all registered ids if unfiltered). Whether each one *fired* is
    # surfaced via `violations` and `transitions`. Sources that were down
    # this cycle are listed in `sources_unavailable` — invariants that
    # depend on them returned no violations regardless of state.
    checks_run: List[str]
    sources_unavailable: List[str]
    violations: List[CycleViolation]
    transitions: List[CycleTransition]


# =============================================================================
# Event Subscriptions Models (routers/event_subscriptions.py)
# =============================================================================


class EmitEventRequest(BaseModel):
    """Request body for emitting an event."""
    event_type: str  # Namespaced event type (e.g., "prediction.resolved")
    payload: Optional[dict] = None  # Structured data


# =============================================================================
# Fan Out Models (routers/fan_out.py)
# =============================================================================


TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


MAX_TASKS = 50


MAX_CONCURRENCY = 10


class FanOutTask(BaseModel):
    """A single task in a fan-out request."""
    id: str
    message: str = Field(..., min_length=1, max_length=100_000)

    @field_validator("id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(
                f"Task ID must be 1-64 alphanumeric characters, hyphens, or underscores: '{v}'"
            )
        return v


class FanOutRequest(BaseModel):
    """Request model for fan-out parallel task execution."""
    tasks: List[FanOutTask]
    agent: str = "self"
    # Optional overall fan-out deadline. When None, no outer deadline is
    # applied — each sub-task is still bounded by the target agent's
    # configured execution_timeout_seconds (TIMEOUT-001).
    timeout_seconds: Optional[int] = None
    max_concurrency: int = 3
    policy: str = "best-effort"
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("tasks")
    @classmethod
    def validate_tasks(cls, v: List[FanOutTask]) -> List[FanOutTask]:
        if len(v) == 0:
            raise ValueError("At least one task is required")
        if len(v) > MAX_TASKS:
            raise ValueError(f"Maximum {MAX_TASKS} tasks per fan-out")
        # Check for duplicate IDs
        ids = [t.id for t in v]
        if len(ids) != len(set(ids)):
            dupes = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"Duplicate task IDs: {set(dupes)}")
        return v

    @field_validator("max_concurrency")
    @classmethod
    def validate_concurrency(cls, v: int) -> int:
        if v < 1 or v > MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 10 or v > 3600:
            raise ValueError("timeout_seconds must be between 10 and 3600")
        return v

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, v: str) -> str:
        if v != "best-effort":
            raise ValueError("Only 'best-effort' policy is supported")
        return v


class FanOutTaskResponse(BaseModel):
    """Result of a single fan-out subtask."""
    id: str
    status: str
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_id: Optional[str] = None
    cost: Optional[float] = None
    context_used: Optional[int] = None
    duration_ms: Optional[int] = None


class FanOutResponse(BaseModel):
    """Aggregated fan-out result."""
    fan_out_id: str
    status: str
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResponse]


# =============================================================================
# Git Models (routers/git.py)
# =============================================================================


class GitSyncRequest(BaseModel):
    """Request body for git sync operation."""
    message: Optional[str] = None  # Custom commit message
    paths: Optional[List[str]] = None  # Specific paths to sync
    strategy: Optional[str] = "normal"  # "normal", "pull_first", "force_push"


class GitPullRequest(BaseModel):
    """Request body for git pull operation."""
    strategy: Optional[str] = "clean"  # "clean", "stash_reapply", "force_reset"


class GitInitializeRequest(BaseModel):
    """Request body for git initialization."""
    repo_owner: str  # GitHub username or organization
    repo_name: str  # Repository name
    create_repo: bool = True  # Whether to create the repository if it doesn't exist
    private: bool = True  # Whether the new repository should be private
    description: Optional[str] = None  # Repository description


class GitHubPATRequest(BaseModel):
    """Request body for setting agent GitHub PAT."""
    pat: str


class AutoSyncToggle(BaseModel):
    enabled: bool


class FreezeSchedulesToggle(BaseModel):
    enabled: bool


# =============================================================================
# Image Generation Models (routers/image_generation.py)
# =============================================================================


class ImageGenerateRequest(BaseModel):
    prompt: str
    use_case: Optional[str] = "general"
    aspect_ratio: Optional[str] = "1:1"
    refine_prompt: Optional[bool] = True


# =============================================================================
# Internal Models (routers/internal.py)
# =============================================================================


class ActivityTrackRequest(BaseModel):
    """Request model for tracking activity start."""
    agent_name: str
    activity_type: str  # e.g., "schedule_start"
    user_id: Optional[int] = None
    triggered_by: str = "schedule"  # schedule, manual, user, agent, system
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None


class ActivityCompleteRequest(BaseModel):
    """Request model for completing an activity."""
    status: str = ActivityState.COMPLETED  # ActivityState: completed, failed, cancelled
    details: Optional[Dict] = None
    error: Optional[str] = None


class InternalTaskExecutionRequest(BaseModel):
    """Request model for internal task execution via TaskExecutionService."""
    agent_name: str
    message: str
    triggered_by: str = "schedule"
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None  # TIMEOUT-001: None = use agent's config (default 15 min)
    allowed_tools: Optional[List[str]] = None
    execution_id: Optional[str] = None
    async_mode: bool = False
    # #171: optional schedule metadata surfaced in the agent's execution context block.
    schedule_name: Optional[str] = None
    schedule_cron: Optional[str] = None
    schedule_next_run: Optional[str] = None
    attempt: Optional[int] = None


class ValidateExecutionRequest(BaseModel):
    """Request model for triggering execution validation."""
    execution_id: str
    agent_name: str
    schedule_id: str
    original_message: str
    execution_response: str
    custom_prompt: Optional[str] = None
    timeout_seconds: int = 120


class InternalAuditRequest(BaseModel):
    """Request model for audit log entries from MCP server."""
    event_type: str          # AuditEventType value
    event_action: str        # e.g. "tool_call"
    source: str = "mcp"      # Always "mcp" for MCP server calls
    # MCP auth context
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    mcp_scope: Optional[str] = None
    actor_agent_name: Optional[str] = None
    # Target
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    # Request correlation (#905): lets an MCP `mcp_operation` row be joined to
    # the backend `git_operation`/etc. row it triggered, when the MCP tool
    # forwards the same X-Request-ID it sends on the proxied backend call.
    request_id: Optional[str] = None
    # Details
    details: Optional[Dict] = None


# =============================================================================
# Logs Models (routers/logs.py)
# =============================================================================


class RetentionConfig(BaseModel):
    """Retention configuration."""
    retention_days: int = Field(..., ge=1, le=3650, description="Days to retain logs")
    archive_enabled: bool = Field(..., description="Whether archival is enabled")
    cleanup_hour: int = Field(..., ge=0, le=23, description="Hour (UTC) to run nightly archival")


class ArchiveRequest(BaseModel):
    """Manual archive request."""
    retention_days: Optional[int] = Field(None, ge=1, le=3650, description="Override retention days")
    delete_after_archive: bool = Field(True, description="Delete originals after archiving")


# =============================================================================
# Loops Models (routers/loops.py)
# =============================================================================


MAX_RUNS_LIMIT = 100


MAX_MESSAGE_LEN = 100_000


MAX_DELAY_SECONDS = 3600


MAX_TIMEOUT_PER_RUN = 7200


MAX_STOP_SIGNAL_LEN = 200


MAX_DURATION_SECONDS = 604_800  # 7 days — hard ceiling on the wall-clock deadline


class StartLoopRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    max_runs: int = Field(..., ge=1, le=MAX_RUNS_LIMIT)
    stop_signal: Optional[str] = Field(default=None, max_length=MAX_STOP_SIGNAL_LEN)
    delay_seconds: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)
    timeout_per_run: Optional[int] = Field(default=None, ge=10, le=MAX_TIMEOUT_PER_RUN)
    # #1156: optional loop-level wall-clock deadline. NULL = unbounded
    # (max_runs is still the hard stop). Lower bound vs the per-run timeout
    # is validated in the endpoint (needs the agent's configured timeout).
    max_duration_seconds: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION_SECONDS)
    # #1155: optional per-loop USD cost budget. NULL = no limit (max_runs is
    # still the hard stop). Enforced as an iteration-boundary gate — the loop
    # stops before the next run once accumulated cost meets/exceeds the budget
    # (stop_reason='budget_exhausted'). The current run always finishes, so one
    # run (including the first) can overshoot. No upper cap — allow sub-cent.
    max_cost_usd: Optional[float] = Field(default=None, gt=0)
    # #1157: doom-loop detection. Stop the loop after K consecutive runs whose
    # response fingerprint (SHA-256 of normalized text) is identical. 0 disables;
    # default 3. 1 is nonsensical ("repeated identical" needs ≥2) → rejected.
    no_progress_threshold: Optional[int] = Field(default=3, ge=0)
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("stop_signal")
    @classmethod
    def _normalize_stop_signal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None  # empty after strip → fixed mode

    @field_validator("no_progress_threshold")
    @classmethod
    def _validate_no_progress_threshold(cls, v: Optional[int]) -> Optional[int]:
        if v == 1:
            raise ValueError(
                "no_progress_threshold must be 0 (disabled) or >= 2; "
                "1 would stop after the first success"
            )
        return v


class StartLoopResponse(BaseModel):
    loop_id: str
    status: str
    agent_name: str
    max_runs: int


class LoopRunResponse(BaseModel):
    run_number: int
    execution_id: Optional[str] = None
    status: str
    response_preview: Optional[str] = None
    cost: Optional[float] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None


class LoopStatusResponse(BaseModel):
    loop_id: str
    agent_name: str
    status: str
    max_runs: int
    runs_completed: int
    stop_reason: Optional[str] = None
    last_response: Optional[str] = None
    error: Optional[str] = None
    runs: List[LoopRunResponse]
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # #1156: wall-clock deadline (NULL = unbounded) + elapsed since started_at
    # (frozen at completed_at once terminal). Both NULL before the loop runs.
    max_duration_seconds: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    # #1155: cost budget (NULL = unbounded) + total_cost computed on read as
    # the sum of agent_loop_runs.cost (NULL→0). total_cost is always a float,
    # 0.0 for a zero-run loop.
    max_cost_usd: Optional[float] = None
    total_cost: float = 0.0
    # #1157: no-progress threshold (NULL = disabled / legacy loop).
    no_progress_threshold: Optional[int] = None


class StopLoopResponse(BaseModel):
    loop_id: str
    status: str  # "stopping" | "already_done"


# =============================================================================
# Messages Models (routers/messages.py)
# =============================================================================


class SendMessageRequest(BaseModel):
    """Request to send a proactive message to a user."""
    recipient_email: EmailStr = Field(
        ...,
        description="Verified email of the recipient. Must be in agent_sharing with allow_proactive=1."
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Message content (max 4096 characters)"
    )
    channel: Literal["auto", "telegram", "slack", "web"] = Field(
        default="auto",
        description="Target channel. 'auto' tries channels in order: telegram -> slack -> web"
    )
    reply_to_thread: bool = Field(
        default=False,
        description="Continue in last thread if one exists (channel-dependent)"
    )
    execution_id: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "The execution this send belongs to (effect-scoped idempotency, #1084). "
            "A re-delivery of the same turn dedupes to one send per (recipient, channel). "
            "Fail-open when absent."
        ),
    )
    dedup_label: str = Field(
        default="",
        max_length=200,
        description=(
            "Optional discriminator (#1084) to intentionally send two distinct "
            "messages to the same recipient in one turn. Default → at-most-one."
        ),
    )


class SendMessageResponse(BaseModel):
    """Response from sending a proactive message."""
    success: bool
    channel: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class ProactiveShareUpdate(BaseModel):
    """Request to update allow_proactive flag for a share."""
    email: EmailStr
    allow_proactive: bool


class ProactiveSharesResponse(BaseModel):
    """List of emails with proactive messaging enabled."""
    agent_name: str
    emails: list[str]


# =============================================================================
# Notifications Models (routers/notifications.py)
# =============================================================================


class DismissAllRequest(BaseModel):
    """Body for bulk-dismissing notifications (#1017)."""
    agent_name: Optional[str] = None


# =============================================================================
# Operator Queue Models (routers/operator_queue.py)
# =============================================================================


class OperatorResponse(BaseModel):
    """Body for responding to a queue item."""
    response: str
    response_text: Optional[str] = None


class BulkCancelRequest(BaseModel):
    """Body for bulk-cancelling pending queue items (#1017).

    The client sends the ids it actually rendered, so a sync-loop race can
    never cancel items the operator never saw.
    """
    ids: List[str] = Field(..., min_length=1, max_length=500)


class ClearResolvedRequest(BaseModel):
    """Body for clearing the Resolved tab (#1017)."""
    agent_name: Optional[str] = None


# =============================================================================
# Paid Models (routers/paid.py)
# =============================================================================


class PaidChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


# =============================================================================
# Public Models (routers/public.py)
# =============================================================================


class PublicChatHistoryResponse(BaseModel):
    """Response model for chat history endpoint."""
    messages: List[dict]
    session_id: str
    message_count: int


class ClearSessionResponse(BaseModel):
    """Response model for clear session endpoint."""
    cleared: bool
    new_session_id: Optional[str] = None


# =============================================================================
# Public Memory Models (routers/public_memory.py)
# =============================================================================


class WriteUserMemoryRequest(BaseModel):
    execution_id: str = Field(..., min_length=1, max_length=200)
    memory_text: str = Field(..., max_length=8000)


# =============================================================================
# Schedules Models (routers/schedules.py)
# =============================================================================


class ScheduleUpdateRequest(BaseModel):
    """Request model for updating a schedule."""
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    message: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    description: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Retry configuration (RETRY-001)
    max_retries: Optional[int] = None
    retry_delay_seconds: Optional[int] = None
    # Validation configuration (VALIDATE-001)
    validation_enabled: Optional[bool] = None
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: Optional[int] = None


class ScheduleResponse(BaseModel):
    """Response model for schedule data."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]
    # #913: null means "inherit from agent_ownership.execution_timeout_seconds".
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: int = 120

    class Config:
        from_attributes = True


class ExecutionSummary(BaseModel):
    """Lightweight execution response for list views - excludes large text fields.

    Used by GET /api/agents/{name}/executions for fast list loading.
    Full details available via GET /api/agents/{name}/executions/{id}.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    triggered_by: str
    # Observability fields (small)
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    # Origin tracking (small) - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume (small) - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection (small) - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage (small) - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking (small) - VALIDATE-001
    business_status: Optional[str] = None  # pending_validation, validated, failed_validation, skipped
    validation_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B) - small JSON list
    compact_metadata: Optional[str] = None

    # EXCLUDED (large fields - fetch via /executions/{id}):
    # - response: Optional[str]      # Full response text
    # - error: Optional[str]         # Full error text
    # - tool_calls: Optional[str]    # JSON array of tool calls
    # - execution_log: Optional[str] # Full Claude Code transcript

    class Config:
        from_attributes = True


class ExecutionResponse(BaseModel):
    """Full response model for execution data - includes all fields.

    Used by GET /api/agents/{name}/executions/{id} for single execution details.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    response: Optional[str]
    error: Optional[str]
    triggered_by: str
    # Observability fields
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None  # Full Claude Code execution transcript (JSON)
    # Origin tracking - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking - VALIDATE-001
    business_status: Optional[str] = None
    validated_at: Optional[datetime] = None
    validation_execution_id: Optional[str] = None
    validates_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B)
    compact_metadata: Optional[str] = None

    class Config:
        from_attributes = True


class WebhookStatusResponse(BaseModel):
    """Webhook configuration for a schedule."""
    schedule_id: str
    has_token: bool
    webhook_enabled: bool
    webhook_url: Optional[str] = None


# =============================================================================
# Sessions Models (routers/sessions.py)
# =============================================================================


class CreateSessionRequest(BaseModel):
    """Optional body for POST /session. All fields optional."""

    subscription_id: Optional[str] = None


class SessionMessageRequest(BaseModel):
    """Body for the turn endpoint."""

    message: str = Field(..., min_length=1)
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    # File attachments — same shape as ParallelTaskRequest.files (#364).
    # Images become vision blocks for the model; non-images are written
    # into the agent workspace and a "[File uploaded by X]: name (size)
    # saved to path" line is appended to the prompt so the agent can
    # `Read` them. (Phase 5.2 file-upload parity with Chat.)
    files: Optional[list] = None


# =============================================================================
# Settings Models (routers/settings.py)
# =============================================================================


class ApiKeyUpdate(BaseModel):
    """Request body for updating an API key."""
    api_key: str


class ApiKeyTest(BaseModel):
    """Request body for testing an API key."""
    api_key: str


class OpsSettingsUpdate(BaseModel):
    """Request body for updating ops settings."""
    settings: Dict[str, str]


class SlackSettingsUpdate(BaseModel):
    """Request body for updating Slack settings."""
    client_id: str = None
    client_secret: str = None
    signing_secret: str = None


class SlackConnectRequest(BaseModel):
    """Request body for connecting Slack transport."""
    app_token: Optional[str] = None  # xapp-... for Socket Mode
    transport_mode: Optional[str] = None  # "socket" or "webhook"


class GitHubTemplateEntry(BaseModel):
    """A single GitHub template entry."""
    github_repo: str
    display_name: str = ""
    description: str = ""


class GitHubTemplatesUpdate(BaseModel):
    """Request body for updating GitHub templates."""
    templates: List[GitHubTemplateEntry]


class McpUrlUpdate(BaseModel):
    """Request body for updating the MCP server URL."""
    url: str


class AgentQuotaUpdate(BaseModel):
    """Request body for updating per-role agent quotas."""
    max_agents_creator: Optional[str] = None
    max_agents_operator: Optional[str] = None
    max_agents_user: Optional[str] = None


# =============================================================================
# Setup Models (routers/setup.py)
# =============================================================================


class SetAdminPasswordRequest(BaseModel):
    """Request body for creating the admin account at first-time setup.

    `email` is **required** (trinity-enterprise#49): it becomes the admin's
    sign-in identity (login with email + password instead of the fixed 'admin')
    and is the contact used for the optional operator intake. The remaining
    operator-profile fields (company/name/role/use_case) stay optional and are
    only forwarded to the hosted intake endpoint when `consent_updates` is true.
    """
    password: str = Field(..., max_length=128)
    confirm_password: str = Field(..., max_length=128)
    # Required admin email — sign-in identity. Shape validated in the handler so
    # a typo / blank value yields a clean 400 (a missing field yields a 422).
    email: str = Field(..., max_length=254)
    # Optional operator profile — all skippable; setup completes without them.
    company: Optional[str] = Field(None, max_length=200)
    name: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    use_case: Optional[str] = Field(None, max_length=500)
    # Affirmative, opt-in consent to occasionally receive security & product
    # updates. ONLY when true is anything submitted to the hosted intake.
    consent_updates: bool = False


# =============================================================================
# Sharing Models (routers/sharing.py)
# =============================================================================


class AccessPolicy(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessPolicyUpdate(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessRequest(BaseModel):
    id: str
    agent_name: str
    email: str
    channel: str | None = None
    requested_at: str
    status: str


class AccessRequestDecision(BaseModel):
    approve: bool


# =============================================================================
# Slack Models (routers/slack.py)
# =============================================================================


class SlackEventResponse(BaseModel):
    """Response to Slack events (always return 200)."""
    ok: bool = True
    challenge: Optional[str] = None


# =============================================================================
# Telegram Models (routers/telegram.py)
# =============================================================================


class TelegramWebhookResponse(BaseModel):
    ok: bool = True


class TelegramBindingResponse(BaseModel):
    agent_name: str
    bot_username: Optional[str] = None
    bot_id: Optional[str] = None
    webhook_url: Optional[str] = None
    bot_link: Optional[str] = None
    configured: bool = False
    group_count: int = 0
    warning: Optional[str] = None


class TelegramConfigureRequest(BaseModel):
    bot_token: str


class TelegramTestRequest(BaseModel):
    chat_id: Optional[str] = None
    message: str = "Hello from Trinity! Your Telegram bot is configured correctly."


class TelegramGroupConfigResponse(BaseModel):
    id: int
    chat_id: str
    chat_title: Optional[str] = None
    chat_type: str = "group"
    trigger_mode: str = "mention"
    welcome_enabled: bool = False
    welcome_text: Optional[str] = None
    is_active: bool = True


class TelegramGroupConfigUpdateRequest(BaseModel):
    trigger_mode: Optional[str] = None
    welcome_enabled: Optional[bool] = None
    welcome_text: Optional[str] = None


class TelegramGroupMessageRequest(BaseModel):
    """Request model for proactive group messaging (Issue #349)."""
    message: str


class SlackChannelMessageRequest(BaseModel):
    """Request model for proactive Slack channel messaging (#350)."""
    message: str
    thread_ts: Optional[str] = None  # optionally reply in an existing thread


# =============================================================================
# Users Models (routers/users.py)
# =============================================================================


class UserRoleUpdate(BaseModel):
    role: str


class UpdateMyEmailRequest(BaseModel):
    email: str


# =============================================================================
# Voice Models (routers/voice.py)
# =============================================================================


class VoiceStartRequest(BaseModel):
    session_id: Optional[str] = None  # Existing chat session to continue
    voice_name: Optional[str] = None  # Gemini voice name (e.g. "Kore", "Puck")
    workspace_mode: bool = False       # Enable canvas panel tools


class VoiceStartResponse(BaseModel):
    voice_session_id: str
    websocket_url: str
    chat_session_id: str


class VoiceStopRequest(BaseModel):
    voice_session_id: str


class VoiceStopResponse(BaseModel):
    transcript: list
    messages_saved: int
    duration_seconds: float


# =============================================================================
# Voip Models (routers/voip.py)
# =============================================================================


class VoipConfigureRequest(BaseModel):
    account_sid: str
    auth_token: str
    from_number: str
    daily_call_cap: Optional[int] = None


class VoipBindingResponse(BaseModel):
    agent_name: str
    configured: bool
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    daily_call_cap: Optional[int] = None
    display_name: Optional[str] = None
    enabled: Optional[bool] = None


class VoipCallRequest(BaseModel):
    to_number: str
    context: Optional[str] = None
    process_transcript: bool = True
    # Effect-scoped idempotency (#1084): a re-delivery of the same turn replays
    # the original call instead of placing a second PSTN call. Fail-open absent.
    execution_id: Optional[str] = Field(default=None, max_length=200)
    dedup_label: str = Field(default="", max_length=200)


class VoipEnabledRequest(BaseModel):
    enabled: bool


# =============================================================================
# Webhooks Models (routers/webhooks.py)
# =============================================================================


CONTEXT_MAX_CHARS = 4000


class WebhookTriggerRequest(BaseModel):
    """Optional body for a webhook trigger call."""
    context: Optional[str] = Field(
        default=None,
        description="Additional context appended to the schedule message.",
        max_length=CONTEXT_MAX_CHARS,
    )
    metadata: Optional[dict] = Field(
        default=None,
        description="Arbitrary key/value metadata stored on the execution record.",
    )


# =============================================================================
# Whatsapp Models (routers/whatsapp.py)
# =============================================================================


class WhatsAppBindingResponse(BaseModel):
    agent_name: str
    configured: bool = False
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    messaging_service_sid: Optional[str] = None
    display_name: Optional[str] = None
    is_sandbox: bool = False
    webhook_url: Optional[str] = None
    warning: Optional[str] = None


class WhatsAppConfigureRequest(BaseModel):
    account_sid: str
    auth_token: str
    from_number: str
    messaging_service_sid: Optional[str] = None


class WhatsAppTestRequest(BaseModel):
    to_number: Optional[str] = None
    message: str = "Hello from Trinity! Your WhatsApp integration is configured correctly."
