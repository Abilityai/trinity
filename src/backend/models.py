"""
Pydantic models for the Trinity backend API.
"""
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum


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

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class User(BaseModel):
    """Authenticated user."""
    id: int
    username: str
    email: Optional[str] = None
    role: str = "user"


class Token(BaseModel):
    """JWT token response."""
    access_token: str
    token_type: str


class Auth0TokenExchange(BaseModel):
    """Request model for Auth0 token exchange."""
    auth0_token: str


class BulkCredentialImport(BaseModel):
    """Request model for bulk credential import."""
    content: str  # .env-style content: KEY=VALUE pairs


class BulkCredentialResult(BaseModel):
    """Result of bulk credential import."""
    created: int
    skipped: int
    errors: List[str]
    credentials: List[dict]


class HotReloadCredentialsRequest(BaseModel):
    """Request model for hot-reloading credentials."""
    credentials_text: str  # .env-style KEY=VALUE text


class ChatMessageRequest(BaseModel):
    """Request model for chat messages."""
    message: str
    model: Optional[str] = None  # Model alias: sonnet, opus, haiku, or full model name


class ModelChangeRequest(BaseModel):
    """Request model for changing agent's model."""
    model: str  # Model alias: sonnet, opus, haiku, or full model name


class ParallelTaskRequest(BaseModel):
    """Request model for parallel task execution (stateless, no conversation context)."""
    message: str  # The task to execute
    model: Optional[str] = None  # Model override: sonnet, opus, haiku, or full model name
    allowed_tools: Optional[List[str]] = None  # Tool restrictions (--allowedTools)
    system_prompt: Optional[str] = None  # Additional instructions (--append-system-prompt)
    timeout_seconds: Optional[int] = 300  # Execution timeout (5 minutes default)


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


class ExecutionStatus(str, Enum):
    """Status of an execution request."""
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
    status: ExecutionStatus = ExecutionStatus.QUEUED

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
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


class SystemPermissions(BaseModel):
    """Permission configuration for system agents."""
    preset: Optional[str] = None  # "full-mesh", "orchestrator-workers", "none"
    explicit: Optional[Dict[str, List[str]]] = None  # {"orchestrator": ["worker1", "worker2"]}


class SystemManifest(BaseModel):
    """Parsed system manifest from YAML."""
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None


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
    credentials: Optional[Dict[str, str]] = None  # KEY=VALUE pairs
    name: Optional[str] = None  # Override name from template.yaml


class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    credentials_imported: Dict[str, CredentialImportResult] = {}
    credentials_injected: int = 0
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors
