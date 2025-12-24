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
    success: Optional[bool] = None
    duration_ms: Optional[int] = None
    timestamp: str


class ExecutionMetadata(BaseModel):
    """Metadata about the Claude Code execution"""
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    tool_count: int = 0
    session_id: Optional[str] = None
    # Token tracking for context window
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    context_window: int = 200000  # Default max context


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

class TrinityInjectRequest(BaseModel):
    """Request to inject Trinity meta-prompt"""
    force: bool = False  # If true, re-inject even if already done
    custom_prompt: Optional[str] = None  # System-wide custom prompt to inject into CLAUDE.md


class TrinityInjectResponse(BaseModel):
    """Response from Trinity injection"""
    status: str  # "injected", "already_injected", "error"
    already_injected: bool = False
    files_created: List[str] = []
    directories_created: List[str] = []
    claude_md_updated: bool = False
    error: Optional[str] = None


class TrinityStatusResponse(BaseModel):
    """Response from Trinity status check"""
    injected: bool
    meta_prompt_mounted: bool
    files: Dict[str, bool]
    directories: Dict[str, bool]
    claude_md_has_trinity_section: bool


# ============================================================================
# Parallel Task Execution Models (Headless Mode)
# ============================================================================

class ParallelTaskRequest(BaseModel):
    """Request for parallel task execution (stateless, no conversation context)"""
    message: str  # The task to execute
    model: Optional[str] = None  # Model override: sonnet, opus, haiku, or full model name
    allowed_tools: Optional[List[str]] = None  # Tool restrictions (--allowedTools)
    system_prompt: Optional[str] = None  # Additional instructions (--append-system-prompt)
    timeout_seconds: Optional[int] = 300  # Execution timeout (5 minutes default)


class ParallelTaskResponse(BaseModel):
    """Response from parallel task execution"""
    response: str  # Claude's response
    execution_log: List[ExecutionLogEntry] = []  # Tool calls and results
    metadata: ExecutionMetadata  # Cost, tokens, duration
    session_id: str  # Unique session ID for this task
    timestamp: str  # ISO timestamp
