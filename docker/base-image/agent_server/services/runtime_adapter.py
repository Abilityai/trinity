"""
Runtime Adapter - Abstract interface for agent execution engines.

Allows Trinity to support multiple AI providers (Claude Code, Gemini CLI, etc.)
while maintaining a unified interface for chat, tool execution, and cost tracking.
"""
import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import resolve_context_window

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What a runtime supports, so callers gate on a capability instead of
    branching on the runtime name (#1187).

    ``cost_reporting`` is a string, not a bool: ``"native"`` means the CLI
    reports a real cost (Claude Code), ``"estimated"`` means Trinity derives
    it from token counts (Gemini, Codex).
    """
    chat_continuity: bool = False
    session_tab_resume: bool = False
    mcp_support: bool = False
    cost_reporting: str = "estimated"  # "native" | "estimated"

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class AgentRuntime(ABC):
    """
    Abstract base class for agent execution runtimes.

    Implementations must provide:
    - execute(): Run the agent with a prompt
    - configure_mcp(): Set up MCP tool servers
    - is_available(): Check if runtime is installed
    """

    @abstractmethod
    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        """
        Execute agent with the given prompt.

        Args:
            prompt: User message or task to execute
            model: Model identifier (e.g., "sonnet-4.5", "gemini-2.5-pro")
            continue_session: Whether to continue previous conversation context
            stream: Whether to stream responses (for future use)
            system_prompt: Platform instructions appended via --append-system-prompt
            execution_id: Optional execution ID for process registry (enables termination tracking)

        Returns:
            Tuple of (response_text, execution_log, metadata, raw_messages)
            - execution_log: Simplified ExecutionLogEntry objects for activity tracking
            - raw_messages: Full JSON transcript for execution log viewer
        """
        pass

    @abstractmethod
    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """
        Configure MCP servers for tool access.

        Args:
            mcp_servers: Dict of server configurations from .mcp.json

        Returns:
            True if configuration succeeded, False otherwise
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if this runtime is installed and available.

        Returns:
            True if runtime CLI is installed, False otherwise
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """
        Get the default model for this runtime.

        Returns:
            Model identifier string
        """
        pass

    def get_context_window(self, model: Optional[str] = None) -> int:
        """
        Fallback context window size for a model (#1521).

        Non-abstract by design: a new runtime inherits the shared model catalog
        so it can't ship a silently-wrong 200K denominator by forgetting to
        implement this. Runtimes with a fixed native window (Gemini, Codex)
        override it to resolve against their own default model id. This is a
        FALLBACK — the authoritative value is the runtime-reported window.

        Args:
            model: Optional model identifier (uses default if None)

        Returns:
            Context window size in tokens
        """
        return resolve_context_window(model)

    @abstractmethod
    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
        """
        Execute a stateless task in headless mode (no conversation context).

        Used for:
        - Agent delegation from orchestrators
        - Batch processing without context pollution
        - Parallel task execution
        - Resuming previous sessions (EXEC-023)
        - Session tab turns where the JSONL must persist for the next --resume

        Args:
            prompt: Task description
            model: Model to use
            allowed_tools: List of allowed tool names (None = all tools)
            system_prompt: Custom system prompt
            timeout_seconds: Execution timeout
            max_turns: Maximum agentic turns for runaway prevention (None = unlimited)
            execution_id: Optional execution ID for process registry (enables termination tracking)
            resume_session_id: Optional Claude Code session ID to resume (EXEC-023)
            persist_session: If True, omit ``--no-session-persistence`` so the
                JSONL is written and a future ``--resume`` can reattach.
                Default False preserves stateless headless behavior for all
                existing callers (schedules, MCP, fan-out, webhooks).

        Returns:
            Tuple of (response_text, execution_log, metadata, session_id)
        """
        pass

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        """Declare what this runtime supports.

        Conservative by default (#1187, AC2): a runtime that forgets to
        override this is treated as the least-capable — no Session-tab
        resume, no assumed MCP, estimated cost. Override per runtime to
        declare real support.
        """
        return RuntimeCapabilities()


# Accepted AGENT_RUNTIME values (lowercased). Unknown values fail loudly
# rather than silently selecting Claude (#1187 Phase D).
_CLAUDE_RUNTIMES = frozenset({"claude-code", "claude"})
_GEMINI_RUNTIMES = frozenset({"gemini-cli", "gemini"})
_CODEX_RUNTIMES = frozenset({"codex"})
KNOWN_RUNTIMES = _CLAUDE_RUNTIMES | _GEMINI_RUNTIMES | _CODEX_RUNTIMES


def get_runtime() -> AgentRuntime:
    """
    Factory function to get the appropriate runtime based on configuration.

    Reads AGENT_RUNTIME environment variable to determine which runtime to use.
    Defaults to Claude Code (env unset) for backward compatibility, but an
    explicitly-set UNKNOWN value raises instead of silently falling back to
    Claude — a typo'd runtime should fail loudly, not run the wrong engine
    (#1187 Phase D).

    Returns:
        AgentRuntime instance (ClaudeCodeRuntime, GeminiRuntime, or CodexRuntime)

    Raises:
        ValueError: if AGENT_RUNTIME is set to an unrecognized value.
    """
    runtime_type = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime_type in _GEMINI_RUNTIMES:
        from .gemini_runtime import get_gemini_runtime
        logger.info("Using Gemini CLI runtime")
        return get_gemini_runtime()
    if runtime_type in _CODEX_RUNTIMES:
        from .codex_runtime import get_codex_runtime
        logger.info("Using OpenAI Codex runtime")
        return get_codex_runtime()
    if runtime_type in _CLAUDE_RUNTIMES:
        from .claude_code import get_claude_runtime
        logger.info("Using Claude Code runtime")
        return get_claude_runtime()

    raise ValueError(
        f"Unknown AGENT_RUNTIME={runtime_type!r}. "
        f"Known runtimes: {sorted(KNOWN_RUNTIMES)}. "
        "Refusing to silently fall back to Claude Code."
    )

