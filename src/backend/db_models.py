"""
Database entity models for Trinity platform.

These are Pydantic models representing database tables and records.
For API request/response models, see models.py.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


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


class ScheduleExecution(BaseModel):
    """Record of a scheduled execution."""
    id: str
    schedule_id: str
    agent_name: str
    status: str  # "pending", "running", "success", "failed"
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    message: str
    response: Optional[str] = None
    error: Optional[str] = None
    triggered_by: str  # "schedule" or "manual"
    # Observability fields
    context_used: Optional[int] = None  # Tokens used in context
    context_max: Optional[int] = None   # Max context window
    cost: Optional[float] = None        # Cost in USD
    tool_calls: Optional[str] = None    # JSON array of tool calls


# =========================================================================
# Git Configuration Models
# =========================================================================

class AgentGitConfig(BaseModel):
    """Git configuration for an agent (GitHub-native agents)."""
    id: str
    agent_name: str
    github_repo: str  # e.g., "Abilityai/agent-ruby"
    working_branch: str  # e.g., "trinity/my-agent/abc123"
    instance_id: str  # Unique instance identifier
    created_at: datetime
    last_sync_at: Optional[datetime] = None
    last_commit_sha: Optional[str] = None
    sync_enabled: bool = True
    sync_paths: Optional[str] = None  # JSON array of paths to sync


class GitSyncResult(BaseModel):
    """Result of a git sync operation."""
    success: bool
    commit_sha: Optional[str] = None
    message: str
    files_changed: int = 0
    branch: Optional[str] = None
    sync_time: Optional[datetime] = None


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
    """Request model for creating a public link."""
    name: Optional[str] = None  # Friendly name for the link
    require_email: bool = False  # Whether email verification is required
    expires_at: Optional[str] = None  # ISO timestamp for expiration


class PublicLinkUpdate(BaseModel):
    """Request model for updating a public link."""
    name: Optional[str] = None
    enabled: Optional[bool] = None
    require_email: Optional[bool] = None
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
    require_email: bool = False


class PublicLinkWithUrl(PublicLink):
    """Public link with generated URL."""
    url: str
    usage_stats: Optional[dict] = None


class PublicLinkInfo(BaseModel):
    """Public-facing link information (no sensitive data)."""
    valid: bool
    require_email: bool = False
    agent_available: bool = True
    reason: Optional[str] = None  # "expired", "disabled", "not_found"


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


class PublicChatResponse(BaseModel):
    """Response from a public chat."""
    response: str
    usage: Optional[dict] = None  # {"input_tokens": N, "output_tokens": N}
