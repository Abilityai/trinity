"""
Runtime Adapter - Abstract interface for agent execution engines.

Allows Trinity to support multiple AI providers (Claude Code, Gemini CLI, etc.)
while maintaining a unified interface for chat, tool execution, and cost tracking.
"""
import os
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from ..models import ExecutionLogEntry, ExecutionMetadata

logger = logging.getLogger(__name__)


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

    @abstractmethod
    def get_context_window(self, model: Optional[str] = None) -> int:
        """
        Get the context window size for a model.

        Args:
            model: Optional model identifier (uses default if None)

        Returns:
            Context window size in tokens
        """
        pass

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


def get_runtime() -> AgentRuntime:
    """
    Factory function to get the appropriate runtime based on configuration.

    Reads AGENT_RUNTIME environment variable to determine which runtime to use.
    Defaults to Claude Code for backward compatibility.

    Returns:
        AgentRuntime instance (ClaudeCodeRuntime or GeminiRuntime)
    """
    runtime_type = os.getenv("AGENT_RUNTIME", "claude-code").lower()

    if runtime_type == "gemini-cli" or runtime_type == "gemini":
        from .gemini_runtime import get_gemini_runtime
        runtime = get_gemini_runtime()
        logger.info("Using Gemini CLI runtime")
        return runtime
    else:
        # Default to Claude Code
        from .claude_code import get_claude_runtime
        runtime = get_claude_runtime()
        logger.info("Using Claude Code runtime")
        return runtime

