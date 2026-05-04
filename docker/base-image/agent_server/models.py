"""
Pydantic models for the agent server.
"""
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime


# ============================================================================
# Chat Models
# ============================================================================

class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = None


class ChatRequest(BaseModel):
    message: str
    stream: bool = False
    model: Optional[str] = None  # Model to use: sonnet, opus, haiku, or full model name
    system_prompt: Optional[str] = None  # Platform instructions (--append-system-prompt)
    execution_id: Optional[str] = None  # Database execution ID (enables termination tracking)


class ChatResponse(BaseModel):
    """Enhanced chat response with execution log"""
    response: str
    execution_log: List["ExecutionLogEntry"] = []
    metadata: "ExecutionMetadata"
    timestamp: str


class ModelRequest(BaseModel):
    model: str  # Model alias: sonnet, opus, haiku, or full model name


# ============================================================================
# Credential Models
# ============================================================================

class CredentialUpdateRequest(BaseModel):
    credentials: dict  # {"VAR_NAME": "value", ...}
    mcp_config: Optional[str] = None  # Pre-generated .mcp.json content (if provided)
    files: Optional[Dict[str, str]] = None  # File-type credentials: {"path": "content", ...}


# ============================================================================
# Agent Info Models
# ============================================================================

class AgentInfo(BaseModel):
    name: str
    status: str
    claude_version: Optional[str] = None
    mcp_servers: List[str] = []
    uptime: Optional[str] = None


# ============================================================================
# Execution Log Models
# ============================================================================

class ExecutionLogEntry(BaseModel):
    """A single entry in the execution log (tool_use or tool_result)"""
    id: str
    type: str  # "tool_use" or "tool_result"
    tool: str
    input: Optional[Dict[str, Any]] = None
    output: Optional[str] = None  # Tool output for tool_result entries
    success: Optional[bool] = None
    duration_ms: Optional[int] = None
    timestamp: str


class CompactEvent(BaseModel):
    """One Claude Code auto-compact event observed in the stream-json output.

    Claude Code emits ``{"type":"system","subtype":"compact_boundary"}`` mid-turn
    when its conversation history reaches ~85% of the model window. The compact
    summarizes ~170k tokens of history into a ~10k summary in place. Capturing
    these events gives users honest signal about session state beyond the
    silent latency cost.
    """
    trigger: Optional[str] = None       # "auto" | "manual"
    pre_tokens: Optional[int] = None
    post_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    timestamp: Optional[str] = None


class ExecutionMetadata(BaseModel):
    """Metadata about the Claude Code execution"""
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    tool_count: int = 0
    session_id: Optional[str] = None
    execution_id: Optional[str] = None  # Unique ID for process registry (termination)
    # Token tracking for context window
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    context_window: int = 200000  # Default max context
    error_type: Optional[str] = None  # Error classification from Claude Code (e.g., "rate_limit")
    error_message: Optional[str] = None  # Human-readable error message from Claude Code
    compact_events: List[CompactEvent] = []  # Auto-compact events observed mid-turn
    recovered_from_jsonl: bool = False  # Stdout race + JSONL fallback fired (response from disk, not stream)


# ============================================================================
# Session Activity Models (for real-time monitoring)
# ============================================================================

class TimelineEntry(BaseModel):
    """A single entry in the session activity timeline"""
    id: str
    tool: str
    input: Optional[Dict[str, Any]] = None
    input_summary: str
    output_summary: Optional[str] = None
    duration_ms: Optional[int] = None
    started_at: str
    ended_at: Optional[str] = None
    success: Optional[bool] = None
    status: str  # "running" | "completed"


class ActiveTool(BaseModel):
    """Currently running tool"""
    name: str
    input_summary: str
    started_at: str


class SessionTotals(BaseModel):
    """Aggregate totals for the session"""
    calls: int = 0
    duration_ms: int = 0
    started_at: Optional[str] = None


class SessionActivity(BaseModel):
    """Session-wide activity tracking"""
    status: str = "idle"  # "running" | "idle"
    active_tool: Optional[ActiveTool] = None
    tool_counts: Dict[str, int] = {}
    timeline: List[TimelineEntry] = []
    totals: SessionTotals = SessionTotals()


class ToolCallDetail(BaseModel):
    """Full detail for a single tool call (for drill-down)"""
    id: str
    tool: str
    input: Optional[Dict[str, Any]] = None
    output: Optional[str] = None
    duration_ms: Optional[int] = None
    started_at: str
    ended_at: Optional[str] = None
    success: Optional[bool] = None


# ============================================================================
# Git Sync Models
# ============================================================================

class GitSyncRequest(BaseModel):
    """Request for git sync operation"""
    message: Optional[str] = None  # Custom commit message
    paths: Optional[List[str]] = None  # Specific paths to sync (default: all)
    strategy: Optional[str] = "normal"  # "normal", "pull_first", "force_push"


class GitPullRequest(BaseModel):
    """Request for git pull operation"""
    strategy: Optional[str] = "clean"  # "clean", "stash_reapply", "force_reset"


class GitCommitInfo(BaseModel):
    """Git commit information"""
    sha: str
    short_sha: str
    message: str
    author: str
    date: str


# ============================================================================
# File Browser Models
# ============================================================================

class FileInfo(BaseModel):
    """File or directory information"""
    name: str
    path: str
    type: str  # "file" or "directory"
    size: int
    modified: str


# ============================================================================
# Trinity Injection Models
# ============================================================================

class TrinityStatusResponse(BaseModel):
    """Response from Trinity status check"""
    injected: bool  # Always true — runtime injection via --append-system-prompt


# ============================================================================
# Parallel Task Execution Models (Headless Mode)
# ============================================================================

class ParallelTaskRequest(BaseModel):
    """Request for parallel task execution (stateless, no conversation context)"""
    message: str  # The task to execute
    model: Optional[str] = None  # Model override: sonnet, opus, haiku, or full model name
    allowed_tools: Optional[List[str]] = None  # Tool restrictions (--allowedTools)
    system_prompt: Optional[str] = None  # Additional instructions (--append-system-prompt)
    timeout_seconds: Optional[int] = 900  # Execution timeout (15 minutes default)
    max_turns: Optional[int] = None  # Maximum agentic turns (--max-turns) for runaway prevention
    execution_id: Optional[str] = None  # Database execution ID (used for process registry if provided)
    resume_session_id: Optional[str] = None  # Claude Code session ID for --resume (EXEC-023)
    persist_session: Optional[bool] = False  # Session tab: write the JSONL so future --resume works
    images: Optional[List[Dict[str, str]]] = None  # Vision images: [{"media_type": "image/jpeg", "data": "<base64>"}]


class ParallelTaskResponse(BaseModel):
    """Response from parallel task execution"""
    response: str  # Claude's response
    execution_log: List[ExecutionLogEntry] = []  # Tool calls and results
    metadata: ExecutionMetadata  # Cost, tokens, duration
    session_id: str  # Unique session ID for this task
    timestamp: str  # ISO timestamp


# ============================================================================
# New Credential Models (CRED-002: Simplified Credential System)
# ============================================================================

class CredentialReadRequest(BaseModel):
    """Request to read credential files from workspace"""
    paths: List[str]  # List of file paths to read


class CredentialReadResponse(BaseModel):
    """Response with credential file contents"""
    files: Dict[str, str]  # {path: content} for files that exist


class CredentialInjectRequest(BaseModel):
    """Request to inject credential files into workspace"""
    files: Dict[str, str]  # {path: content} to write


class CredentialInjectResponse(BaseModel):
    """Response from credential injection"""
    status: str  # "success"
    files_written: List[str]
