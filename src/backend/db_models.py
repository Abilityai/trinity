"""
Database entity models for Trinity platform.

These are Pydantic models representing database tables and records.
For API request/response models, see models.py.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, field_validator


# =========================================================================
# User Models
# =========================================================================

class UserCreate(BaseModel):
    username: str
    password: Optional[str] = None  # None for Auth0 users
    role: str = "user"
    auth0_sub: Optional[str] = None
    name: Optional[str] = None
    picture: Optional[str] = None
    email: Optional[str] = None


class User(BaseModel):
    id: int
    username: str
    role: str
    auth0_sub: Optional[str] = None
    name: Optional[str] = None
    picture: Optional[str] = None
    email: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime] = None


# =========================================================================
# Agent Ownership and Sharing Models
# =========================================================================

class AgentOwnership(BaseModel):
    id: int
    agent_name: str
    owner_id: int
    owner_username: str
    created_at: datetime


class AgentShare(BaseModel):
    id: int
    agent_name: str
    shared_with_email: str  # Email of user the agent is shared with (may not exist yet)
    shared_by_id: int
    shared_by_email: str  # Email or username of who shared it
    created_at: datetime
    allow_proactive: bool = False  # Can agent send proactive messages to this user


class AgentShareRequest(BaseModel):
    email: str  # Email of user to share with


# =========================================================================
# MCP API Key Models
# =========================================================================

class McpApiKeyCreate(BaseModel):
    name: str
    description: Optional[str] = None


class McpApiKey(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    usage_count: int = 0
    is_active: bool = True
    user_id: int
    username: str
    user_email: Optional[str] = None
    # Agent collaboration fields (Phase: Agent-to-Agent)
    agent_name: Optional[str] = None  # Which agent owns this key (None for user keys)
    scope: str = "user"  # "user" (external clients) or "agent" (agent-to-agent)


class McpApiKeyWithSecret(McpApiKey):
    """Only returned on creation - includes the full API key"""
    api_key: str


class McpAgentKeyCreate(BaseModel):
    """Request model for creating an agent-scoped MCP API key"""
    agent_name: str
    description: Optional[str] = None


# =========================================================================
# Schedule Models
# =========================================================================

class ScheduleCreate(BaseModel):
    """Request model for creating a schedule."""
    name: str
    cron_expression: str  # e.g., "0 9 * * *" for 9 AM daily
    message: str  # The message/task to send to the agent
    enabled: bool = True
    timezone: str = "UTC"
    description: Optional[str] = None
    timeout_seconds: int = 900  # Default 15 minutes
    allowed_tools: Optional[List[str]] = None  # None = all tools allowed
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
    # Retry configuration (RETRY-001)
    max_retries: int = 1  # 0 = disabled, 1-5 range. Default 1 for resilience.
    retry_delay_seconds: int = 60  # Seconds between retries (30-600 range)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False  # Enable post-execution validation
    validation_prompt: Optional[str] = None  # Custom auditor instructions (None = default prompt)
    validation_timeout_seconds: int = 120  # Timeout for validation task (30-600 range)


class Schedule(BaseModel):
    """Schedule configuration for an agent."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str] = None
    owner_id: int
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    timeout_seconds: int = 900  # Default 15 minutes
    allowed_tools: Optional[List[str]] = None  # None = all tools allowed
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
    # Retry configuration (RETRY-001)
    max_retries: int = 1  # 0 = disabled, 1-5 range
    retry_delay_seconds: int = 60  # Seconds between retries (30-600 range)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False  # Enable post-execution validation
    validation_prompt: Optional[str] = None  # Custom auditor instructions (None = default prompt)
    validation_timeout_seconds: int = 120  # Timeout for validation task (30-600 range)


class ScheduleExecution(BaseModel):
    """Record of a scheduled execution."""
    id: str
    schedule_id: str
    agent_name: str
    status: str  # "pending", "running", "success", "failed", "skipped"
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    message: str
    response: Optional[str] = None
    error: Optional[str] = None
    triggered_by: str  # "schedule", "manual", "mcp", or "agent"
    # Observability fields
    context_used: Optional[int] = None  # Tokens used in context
    context_max: Optional[int] = None   # Max context window
    cost: Optional[float] = None        # Cost in USD
    tool_calls: Optional[str] = None    # JSON array of tool calls
    execution_log: Optional[str] = None # Full Claude Code execution transcript (JSON)
    # Origin tracking fields (AUDIT-001)
    source_user_id: Optional[int] = None       # User who triggered (for manual/mcp)
    source_user_email: Optional[str] = None    # User email (denormalized for queries)
    source_agent_name: Optional[str] = None    # Calling agent (for agent-to-agent)
    source_mcp_key_id: Optional[str] = None    # MCP API key ID (for mcp/agent triggers)
    source_mcp_key_name: Optional[str] = None  # MCP API key name (denormalized)
    # Session resume support (EXEC-023)
    claude_session_id: Optional[str] = None    # Claude Code session ID for --resume
    # Model selection (MODEL-001)
    model_used: Optional[str] = None           # Model used for this execution
    # Fan-out linkage (FANOUT-001)
    fan_out_id: Optional[str] = None           # Parent fan-out operation ID
    # Subscription usage tracking (SUB-004)
    subscription_id: Optional[str] = None      # Subscription active at record time
    # Persistent backlog (BACKLOG-001)
    queued_at: Optional[datetime] = None       # ISO timestamp for FIFO ordering when status=queued
    backlog_metadata: Optional[str] = None     # JSON blob of the full ParallelTaskRequest context
    # Retry tracking (RETRY-001)
    attempt_number: int = 1                    # Which attempt this is (1 = first try)
    retry_of_execution_id: Optional[str] = None  # Links retry to original execution
    retry_scheduled_at: Optional[datetime] = None  # When retry is scheduled (for restart recovery)
    # Validation tracking (VALIDATE-001)
    business_status: Optional[str] = None       # pending_validation, validated, failed_validation, skipped
    validated_at: Optional[datetime] = None     # When validation completed
    validation_execution_id: Optional[str] = None  # FK to the validation execution record
    validates_execution_id: Optional[str] = None   # FK to execution being validated (for validation records)


# =========================================================================
# Git Configuration Models
# =========================================================================

class AgentGitConfig(BaseModel):
    """Git configuration for an agent (GitHub-native agents)."""
    id: str
    agent_name: str
    github_repo: str  # e.g., "Abilityai/agent-ruby"
    working_branch: str  # e.g., "trinity/my-agent/abc123" (legacy) or "main" (source mode)
    instance_id: str  # Unique instance identifier
    source_branch: str = "main"  # Branch to pull from (default: main)
    source_mode: bool = False  # If True, track source_branch directly (no working branch)
    created_at: datetime
    last_sync_at: Optional[datetime] = None
    last_commit_sha: Optional[str] = None
    sync_enabled: bool = True
    sync_paths: Optional[str] = None  # JSON array of paths to sync
    # #389 sync health observability
    auto_sync_enabled: bool = False
    freeze_schedules_if_sync_failing: bool = False


class GitSyncResult(BaseModel):
    """Result of a git sync operation."""
    success: bool
    commit_sha: Optional[str] = None
    message: str
    files_changed: int = 0
    branch: Optional[str] = None
    sync_time: Optional[datetime] = None
    conflict_type: Optional[str] = None  # "push_rejected", "merge_conflict", etc.
    conflict_class: Optional[str] = None  # S5 #386: operator-readable class (AHEAD_ONLY, PARALLEL_HISTORY, ...)


# =========================================================================
# Chat Session Models
# =========================================================================

class ChatSession(BaseModel):
    """Persistent chat session for an agent."""
    id: str
    agent_name: str
    user_id: int
    user_email: str
    started_at: datetime
    last_message_at: datetime
    message_count: int = 0
    total_cost: float = 0.0
    total_context_used: int = 0
    total_context_max: int = 200000
    status: str = "active"  # "active" or "closed"
    subscription_id: Optional[str] = None  # SUB-004: subscription active when session started


class ChatMessage(BaseModel):
    """A single message in a chat session."""
    id: str
    session_id: str
    agent_name: str
    user_id: int
    user_email: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime
    # Observability (only for assistant messages)
    cost: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    tool_calls: Optional[str] = None  # JSON array
    execution_time_ms: Optional[int] = None
    source: Optional[str] = "text"  # "text" or "voice" (VOICE-003)
    subscription_id: Optional[str] = None  # SUB-004: snapshotted at record time
    output_tokens: Optional[int] = None  # SUB-004: output tokens for this message


# =========================================================================
# Agent Permission Models (Phase 9.10: Agent-to-Agent Permissions)
# =========================================================================

class AgentPermission(BaseModel):
    """Permission for one agent to communicate with another."""
    id: int
    source_agent: str  # The agent making calls
    target_agent: str  # The agent being called
    created_at: datetime
    created_by: str  # Username of who set the permission


class AgentPermissionInfo(BaseModel):
    """Agent info for permission display."""
    name: str
    status: str  # "running" or "stopped"
    type: str
    permitted: bool = False


# =========================================================================
# Shared Folder Models (Phase 9.11: Agent Shared Folders)
# =========================================================================

class SharedFolderConfig(BaseModel):
    """Configuration for an agent's shared folder settings."""
    agent_name: str
    expose_enabled: bool = False  # Whether this agent exposes a shared folder
    consume_enabled: bool = False  # Whether this agent mounts other agents' shared folders
    created_at: datetime
    updated_at: datetime


class SharedFolderConfigUpdate(BaseModel):
    """Request model for updating shared folder config."""
    expose_enabled: Optional[bool] = None
    consume_enabled: Optional[bool] = None


class SharedFolderMount(BaseModel):
    """A mounted shared folder from another agent."""
    source_agent: str  # The agent exposing the folder
    mount_path: str    # Where it's mounted in the consuming agent
    access_mode: str = "rw"  # "rw" or "ro"
    currently_mounted: bool = False


class SharedFolderInfo(BaseModel):
    """Response model with full shared folder information."""
    agent_name: str
    expose_enabled: bool
    consume_enabled: bool
    exposed_volume: Optional[str] = None  # Volume name if exposing
    exposed_path: str = "/home/developer/shared-out"
    consumed_folders: List[SharedFolderMount] = []
    restart_required: bool = False  # True if config changed and restart needed


# =========================================================================
# System Settings Models
# =========================================================================

class SystemSetting(BaseModel):
    """A system-wide setting."""
    key: str
    value: str
    updated_at: datetime


class SystemSettingUpdate(BaseModel):
    """Request model for updating a system setting."""
    value: str


# =========================================================================
# Public Agent Link Models (Phase 12.2: Public Agent Links)
# =========================================================================

class PublicLinkCreate(BaseModel):
    """Request model for creating a public link.

    Email verification is driven by the agent-level access policy
    (`agent_ownership.require_email`, #311), not per-link.
    """
    name: Optional[str] = None  # Friendly name for the link
    expires_at: Optional[str] = None  # ISO timestamp for expiration


class PublicLinkUpdate(BaseModel):
    """Request model for updating a public link."""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    expires_at: Optional[str] = None


class PublicLink(BaseModel):
    """A public shareable link for an agent."""
    id: str
    agent_name: str
    token: str
    created_by: str  # User ID who created the link
    created_at: datetime
    expires_at: Optional[datetime] = None
    enabled: bool = True
    name: Optional[str] = None


class PublicLinkWithUrl(PublicLink):
    """Public link with generated URL."""
    url: str  # Internal URL (VPN/tailnet)
    external_url: Optional[str] = None  # External URL (public internet via Funnel/Tunnel)
    usage_stats: Optional[dict] = None


class PublicLinkInfo(BaseModel):
    """Public-facing link information (no sensitive data)."""
    valid: bool
    require_email: bool = False
    agent_available: bool = True
    reason: Optional[str] = None  # "expired", "disabled", "not_found"
    # Agent metadata (only populated when valid)
    agent_display_name: Optional[str] = None
    agent_description: Optional[str] = None
    is_autonomous: bool = False
    is_read_only: bool = False


class VerificationRequest(BaseModel):
    """Request to send a verification code."""
    token: str  # The public link token
    email: str  # Email to verify


class VerificationConfirm(BaseModel):
    """Request to confirm a verification code."""
    token: str  # The public link token
    email: str
    code: str  # 6-digit code


class VerificationResponse(BaseModel):
    """Response after verification confirmation."""
    verified: bool
    session_token: Optional[str] = None
    expires_at: Optional[str] = None
    error: Optional[str] = None


class PublicChatRequest(BaseModel):
    """Request to chat via a public link."""
    message: str
    session_token: Optional[str] = None  # Required if link requires email verification
    session_id: Optional[str] = None  # For anonymous links (stored in localStorage)
    async_mode: bool = False  # When true, return execution_id immediately for SSE streaming


class PublicChatResponse(BaseModel):
    """Response from a public chat."""
    response: str
    session_id: Optional[str] = None  # Returned for anonymous links
    message_count: Optional[int] = None  # Number of messages in this session
    usage: Optional[dict] = None  # {"input_tokens": N, "output_tokens": N}


# =========================================================================
# Public Chat Session Models (Phase 12.2.5: Public Chat Persistence)
# =========================================================================

class PublicChatSession(BaseModel):
    """Persistent public chat session."""
    id: str
    link_id: str
    session_identifier: str  # email (for email links) or anonymous token
    identifier_type: str  # 'email' or 'anonymous'
    created_at: datetime
    last_message_at: datetime
    message_count: int = 0
    total_cost: float = 0.0


class PublicChatMessage(BaseModel):
    """A message in a public chat session."""
    id: str
    session_id: str
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: datetime
    cost: Optional[float] = None


# =========================================================================
# Email Authentication Models (Phase 12.4)
# =========================================================================

class EmailWhitelistEntry(BaseModel):
    """An email address in the whitelist."""
    id: int
    email: str
    added_by: str  # User ID
    added_by_username: Optional[str] = None
    added_at: datetime
    source: str  # "manual", "agent_sharing", "access_request", "cli"
    default_role: str = "user"  # Role assigned on first email login (#314)


class EmailWhitelistAdd(BaseModel):
    """Request to add an email to the whitelist."""
    email: str
    source: str = "manual"
    default_role: str = "user"  # Role assigned on first email login (#314)


class EmailLoginRequest(BaseModel):
    """Request a login code via email."""
    email: str


class EmailLoginVerify(BaseModel):
    """Verify a login code."""
    email: str
    code: str  # 6-digit code


class EmailLoginResponse(BaseModel):
    """Response after successful login."""
    access_token: str
    token_type: str = "bearer"
    user: dict  # User profile


# =========================================================================
# Agent Skills Models (Skills Management System)
# =========================================================================

class AgentSkill(BaseModel):
    """A skill assigned to an agent."""
    id: int
    agent_name: str
    skill_name: str
    assigned_by: str  # Username of who assigned
    assigned_at: datetime


class SkillInfo(BaseModel):
    """Information about a skill from the library."""
    name: str
    description: Optional[str] = None
    path: str  # Relative path in library
    content: Optional[str] = None  # Full SKILL.md content (optional)


class AgentSkillsUpdate(BaseModel):
    """Request model for bulk updating agent skills."""
    skills: List[str]  # List of skill names to assign


class SkillsLibraryStatus(BaseModel):
    """Status of the skills library."""
    configured: bool
    url: Optional[str] = None
    branch: str = "main"
    last_sync: Optional[datetime] = None
    commit_sha: Optional[str] = None
    skill_count: int = 0


# =========================================================================
# Agent Tags Models (ORG-001: Agent Systems & Tags)
# =========================================================================

class AgentTagList(BaseModel):
    """Response model for agent tags."""
    agent_name: str
    tags: List[str]


class AgentTagsUpdate(BaseModel):
    """Request model for setting agent tags."""
    tags: List[str]


class TagWithCount(BaseModel):
    """Tag with agent count."""
    tag: str
    count: int


class AllTagsResponse(BaseModel):
    """Response model for listing all tags."""
    tags: List[TagWithCount]


# =========================================================================
# System Views Models (ORG-001 Phase 2: Agent Systems & Tags)
# =========================================================================

class SystemViewCreate(BaseModel):
    """Request model for creating a system view."""
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None  # Emoji or icon identifier
    color: Optional[str] = None  # Hex color (e.g., "#8B5CF6")
    filter_tags: List[str]  # Tags to filter by (OR logic)
    is_shared: bool = False  # Visible to all users?


class SystemViewUpdate(BaseModel):
    """Request model for updating a system view."""
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    filter_tags: Optional[List[str]] = None
    is_shared: Optional[bool] = None


class SystemView(BaseModel):
    """A saved system view (filter for agents)."""
    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    filter_tags: List[str]
    owner_id: str
    owner_email: Optional[str] = None
    is_shared: bool = False
    agent_count: int = 0  # Number of agents matching the filter
    created_at: str
    updated_at: str


class SystemViewList(BaseModel):
    """Response model for listing system views."""
    views: List[SystemView]


# =========================================================================
# Agent Notification Models (NOTIF-001)
# =========================================================================

class NotificationCreate(BaseModel):
    """Request model for creating a notification."""
    notification_type: str  # alert, info, status, completion, question
    title: str  # Required, max 200 chars
    message: Optional[str] = None
    priority: str = "normal"  # low, normal, high, urgent
    category: Optional[str] = None  # progress, anomaly, health, error, etc.
    metadata: Optional[dict] = None  # Any structured data


class Notification(BaseModel):
    """A notification from an agent."""
    id: str
    agent_name: str
    notification_type: str
    title: str
    message: Optional[str] = None
    priority: str = "normal"
    category: Optional[str] = None
    metadata: Optional[dict] = None
    status: str = "pending"  # pending, acknowledged, dismissed
    created_at: str
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None


class NotificationList(BaseModel):
    """Response model for listing notifications."""
    count: int
    notifications: List[Notification]


class NotificationAcknowledge(BaseModel):
    """Response model for acknowledging a notification."""
    id: str
    status: str
    acknowledged_at: str
    acknowledged_by: str


# =========================================================================
# Subscription Credential Models (SUB-002: Long-Lived Subscription Tokens)
# =========================================================================

class SubscriptionCredentialCreate(BaseModel):
    """Request model for registering a subscription token from `claude setup-token`."""
    name: str  # Unique name for the subscription (e.g., "eugene-max")
    token: str  # Long-lived token from `claude setup-token` (sk-ant-oat01-...)
    subscription_type: Optional[str] = None  # "max", "pro", etc.
    rate_limit_tier: Optional[str] = None  # Rate limit tier if known

    @field_validator('token')
    @classmethod
    def validate_token_prefix(cls, v: str) -> str:
        if not v.startswith('sk-ant-oat01-'):
            raise ValueError("Token must start with 'sk-ant-oat01-' (from `claude setup-token`)")
        return v


class SubscriptionCredential(BaseModel):
    """A registered Claude subscription credential."""
    id: str
    name: str
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    owner_id: int
    owner_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    agent_count: int = 0  # Number of agents using this subscription


class SubscriptionWithAgents(SubscriptionCredential):
    """Subscription with list of assigned agents."""
    agents: List[str] = []


class AgentAuthStatus(BaseModel):
    """Auth status for an agent."""
    agent_name: str
    auth_mode: str  # "subscription", "api_key", "not_configured"
    subscription_name: Optional[str] = None
    subscription_id: Optional[str] = None
    has_api_key: bool = False


class SubscriptionUsageWindow(BaseModel):
    """Usage totals for a rolling time window. (SUB-004)"""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    message_count: int = 0


class SubscriptionUsage(BaseModel):
    """Per-subscription usage across rolling time windows. (SUB-004)"""
    subscription_id: str
    window_5h: SubscriptionUsageWindow
    window_7d: SubscriptionUsageWindow
    agents: List[str] = []  # agents currently assigned to this subscription


# =========================================================================
# Agent Monitoring Models (MON-001: Agent Health Monitoring)
# =========================================================================

class AgentHealthStatus(str, Enum):
    """Health status levels for agents."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class HealthCheckType(str, Enum):
    """Types of health checks."""
    DOCKER = "docker"
    NETWORK = "network"
    BUSINESS = "business"
    AGGREGATE = "aggregate"


class DockerHealthCheck(BaseModel):
    """Docker layer health check result."""
    agent_name: str
    container_status: Optional[str] = None  # running, stopped, paused, restarting
    exit_code: Optional[int] = None
    restart_count: int = 0
    oom_killed: bool = False
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    memory_mb: Optional[float] = None
    checked_at: str


class NetworkHealthCheck(BaseModel):
    """Network layer health check result."""
    agent_name: str
    reachable: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    checked_at: str


class BusinessHealthCheck(BaseModel):
    """Business logic health check result."""
    agent_name: str
    status: str = "healthy"  # healthy, degraded, unhealthy
    runtime_available: Optional[bool] = None
    claude_available: Optional[bool] = None
    context_percent: Optional[float] = None
    active_execution_count: int = 0
    stuck_execution_count: int = 0
    recent_error_rate: float = 0.0  # 0.0 - 1.0
    credential_status: Optional[str] = None  # null, "ok", "missing" (SUB-001/MON-001)
    checked_at: str


class AgentHealthDetail(BaseModel):
    """Detailed health information for a single agent."""
    agent_name: str
    aggregate_status: str
    last_check_at: Optional[str] = None
    docker: Optional[DockerHealthCheck] = None
    network: Optional[NetworkHealthCheck] = None
    business: Optional[BusinessHealthCheck] = None
    issues: List[str] = []
    recent_alerts: List[dict] = []
    uptime_percent_24h: Optional[float] = None
    avg_latency_24h_ms: Optional[float] = None


class AgentHealthSummary(BaseModel):
    """Summary health info for fleet overview."""
    name: str
    status: str
    docker_status: Optional[str] = None
    network_reachable: Optional[bool] = None
    runtime_available: Optional[bool] = None
    last_check_at: Optional[str] = None
    issues: List[str] = []


class FleetHealthSummary(BaseModel):
    """Fleet-wide health summary."""
    total_agents: int = 0
    healthy: int = 0
    degraded: int = 0
    unhealthy: int = 0
    critical: int = 0
    unknown: int = 0


class FleetHealthStatus(BaseModel):
    """Complete fleet health status response."""
    enabled: bool = True
    last_check_at: Optional[str] = None
    summary: FleetHealthSummary
    agents: List[AgentHealthSummary] = []
    circuit_breakers: Optional[Dict[str, Any]] = None


class MonitoringConfig(BaseModel):
    """Monitoring service configuration."""
    enabled: bool = True

    # Check intervals (seconds)
    docker_check_interval: int = 30
    network_check_interval: int = 30
    business_check_interval: int = 60

    # Timeouts
    http_timeout: float = 10.0
    tcp_timeout: float = 5.0

    # Thresholds
    cpu_warning_percent: float = 80.0
    cpu_critical_percent: float = 95.0
    memory_warning_percent: float = 85.0
    memory_critical_percent: float = 95.0
    latency_warning_ms: float = 2000.0
    latency_critical_ms: float = 5000.0
    context_warning_percent: float = 85.0
    context_critical_percent: float = 95.0
    error_rate_warning: float = 0.3
    error_rate_critical: float = 0.5

    # Alert cooldowns (seconds)
    critical_cooldown: int = 300      # 5 min
    unhealthy_cooldown: int = 600     # 10 min
    degraded_cooldown: int = 1800     # 30 min

    # Stuck execution threshold (seconds)
    stuck_execution_threshold: int = 1800  # 30 min


class HealthCheckRecord(BaseModel):
    """Database record for a health check."""
    id: str
    agent_name: str
    check_type: str  # docker, network, business, aggregate
    status: str  # healthy, degraded, unhealthy, critical
    container_status: Optional[str] = None
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    memory_mb: Optional[float] = None
    restart_count: Optional[int] = None
    oom_killed: Optional[bool] = None
    reachable: Optional[bool] = None
    latency_ms: Optional[float] = None
    runtime_available: Optional[bool] = None
    claude_available: Optional[bool] = None
    context_percent: Optional[float] = None
    active_executions: Optional[int] = None
    error_rate: Optional[float] = None
    error_message: Optional[str] = None
    checked_at: str
    created_at: Optional[str] = None


# =========================================================================
# Slack Integration Models (SLACK-001)
# =========================================================================

class SlackConnectionCreate(BaseModel):
    """Request to create a Slack connection (internal, after OAuth)."""
    link_id: str
    slack_team_id: str
    slack_team_name: Optional[str] = None
    slack_bot_token: str
    connected_by: str


class SlackConnection(BaseModel):
    """A Slack workspace connection to a public link."""
    id: str
    link_id: str
    slack_team_id: str
    slack_team_name: Optional[str] = None
    connected_by: str
    connected_at: datetime
    enabled: bool = True


class SlackConnectionStatus(BaseModel):
    """Public status of a Slack connection (no token exposed)."""
    connected: bool
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    connected_at: Optional[str] = None
    connected_by: Optional[str] = None
    enabled: bool = True


class SlackOAuthInitResponse(BaseModel):
    """Response when initiating Slack OAuth flow."""
    oauth_url: str


class SlackUserVerification(BaseModel):
    """A verified Slack user."""
    id: str
    link_id: str
    slack_user_id: str
    slack_team_id: str
    verified_email: str
    verification_method: str  # 'slack_profile' or 'email_code'
    verified_at: datetime


class SlackPendingVerification(BaseModel):
    """An in-progress Slack user verification."""
    id: str
    link_id: str
    slack_user_id: str
    slack_team_id: str
    email: Optional[str] = None
    code: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    state: str = "awaiting_email"  # 'awaiting_email' or 'awaiting_code'


class SlackEvent(BaseModel):
    """Incoming Slack event wrapper."""
    type: str  # 'url_verification' or 'event_callback'
    challenge: Optional[str] = None  # For URL verification
    team_id: Optional[str] = None
    api_app_id: Optional[str] = None
    event: Optional[dict] = None
    event_id: Optional[str] = None
    event_time: Optional[int] = None


class SlackOAuthState(BaseModel):
    """Encrypted OAuth state passed through Slack."""
    link_id: str
    agent_name: str
    user_id: str
    timestamp: int


# =========================================================================
# Parallel Capacity Models (CAPACITY-001)
# =========================================================================

class CapacityUpdate(BaseModel):
    """Request model for updating agent parallel capacity."""
    max_parallel_tasks: int  # Must be 1-10


class SlotInfo(BaseModel):
    """Information about a single execution slot."""
    slot_number: int
    execution_id: str
    started_at: str
    message_preview: str
    duration_seconds: int


class AgentCapacity(BaseModel):
    """Response model for agent capacity status."""
    agent_name: str
    max_parallel_tasks: int
    active_slots: int
    available_slots: int
    slots: List[SlotInfo] = []


class BulkSlotState(BaseModel):
    """Response model for bulk slot state query (Dashboard)."""
    agents: dict  # {agent_name: {"max": N, "active": M}}
    timestamp: str


# =========================================================================
# Nevermined Payment Models (NVM-001: x402 Payment Integration)
# =========================================================================

class NeverminedConfigCreate(BaseModel):
    """Request model for configuring Nevermined payments on an agent."""
    nvm_api_key: str  # Format: "env:jwt" e.g. "sandbox:eyJhbGci..."
    nvm_environment: str = "sandbox"  # sandbox, live, staging_sandbox, staging_live, custom
    nvm_agent_id: str  # Registered agent ID from Nevermined
    nvm_plan_id: str  # Registered plan ID from Nevermined
    credits_per_request: int = 1

    @field_validator('nvm_environment')
    @classmethod
    def validate_environment(cls, v: str) -> str:
        valid = {"sandbox", "live", "staging_sandbox", "staging_live", "custom"}
        if v not in valid:
            raise ValueError(f"nvm_environment must be one of: {', '.join(sorted(valid))}")
        return v

    @field_validator('credits_per_request')
    @classmethod
    def validate_credits(cls, v: int) -> int:
        if v < 1:
            raise ValueError("credits_per_request must be >= 1")
        return v


class NeverminedConfig(BaseModel):
    """Stored Nevermined configuration for an agent (no decrypted key)."""
    id: str
    agent_name: str
    nvm_environment: str
    nvm_agent_id: str
    nvm_plan_id: str
    credits_per_request: int
    enabled: bool
    created_at: str
    updated_at: str


class NeverminedPaymentResult(BaseModel):
    """Result from a verify or settle operation."""
    success: bool
    payer: Optional[str] = None
    agent_request_id: Optional[str] = None
    credits_redeemed: Optional[str] = None
    remaining_balance: Optional[str] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None


class NeverminedPaymentLog(BaseModel):
    """A payment log entry."""
    id: str
    agent_name: str
    execution_id: Optional[str] = None
    action: str  # verify, settle, settle_failed, reject
    subscriber_address: Optional[str] = None
    credits_amount: Optional[int] = None
    tx_hash: Optional[str] = None
    remaining_balance: Optional[int] = None
    success: bool
    error: Optional[str] = None
    created_at: str


# =========================================================================
# Agent Event Subscription Models (EVT-001: Agent Event Pub/Sub)
# =========================================================================

class EventSubscriptionCreate(BaseModel):
    """Request model for creating an event subscription."""
    source_agent: str  # Agent to subscribe to events from
    event_type: str  # Namespaced event type (e.g., "prediction.resolved")
    target_message: str  # Message template with {{payload.field}} placeholders
    enabled: bool = True


class EventSubscriptionUpdate(BaseModel):
    """Request model for updating an event subscription."""
    event_type: Optional[str] = None
    target_message: Optional[str] = None
    enabled: Optional[bool] = None


class EventSubscription(BaseModel):
    """A stored event subscription."""
    id: str
    subscriber_agent: str  # Agent that receives the event
    source_agent: str  # Agent that emits the event
    event_type: str  # Namespaced event type pattern
    target_message: str  # Message template
    enabled: bool = True
    created_at: str
    updated_at: str
    created_by: str


class EventSubscriptionList(BaseModel):
    """Response model for listing event subscriptions."""
    count: int
    subscriptions: List[EventSubscription]


class AgentEvent(BaseModel):
    """A persisted agent event."""
    id: str
    source_agent: str
    event_type: str
    payload: Optional[dict] = None
    subscriptions_triggered: int = 0
    created_at: str


class AgentEventList(BaseModel):
    """Response model for listing agent events."""
    count: int
    events: List[AgentEvent]
