"""
Agent state management for the agent server.
"""
import os
import subprocess
import logging
import threading
from typing import List, Dict, Optional
from datetime import datetime, timezone

from .models import ChatMessage
from .model_context import resolve_context_window

logger = logging.getLogger(__name__)

# Cap for in-memory conversation_history. Without it the list grows unbounded
# across days-long agent uptime — persistent chat history lives in the backend
# DB; this in-memory copy is only used for the agent-server API (history,
# session info). Override via AGENT_HISTORY_LIMIT env var. #333 hardening.
_DEFAULT_HISTORY_LIMIT = 1000


def _resolve_history_limit() -> int:
    raw = os.getenv("AGENT_HISTORY_LIMIT")
    if not raw:
        return _DEFAULT_HISTORY_LIMIT
    try:
        value = int(raw)
        return value if value > 0 else _DEFAULT_HISTORY_LIMIT
    except ValueError:
        logger.warning("AGENT_HISTORY_LIMIT=%r is not an int; using default", raw)
        return _DEFAULT_HISTORY_LIMIT


class AgentState:
    """
    Manages the state of the agent including conversation history,
    session tracking, and real-time activity monitoring.
    """

    def __init__(self):
        self.conversation_history: List[ChatMessage] = []
        self.history_limit: int = _resolve_history_limit()
        self.agent_name = os.getenv("AGENT_NAME", "unknown")
        self.agent_runtime = os.getenv("AGENT_RUNTIME", "claude-code")
        # Check if the configured runtime is available
        self.runtime_available = self._check_runtime_available()
        # Backward compatibility alias
        self.claude_code_available = self.runtime_available if self.agent_runtime == "claude-code" else self._check_claude_code()
        self.session_started = False  # Track if we've started a conversation
        # Session-level token tracking
        self.session_total_cost: float = 0.0
        self.session_total_output_tokens: int = 0
        self.session_context_tokens: int = 0  # Latest context size
        # Model selection (persists across session). Set BEFORE the context
        # window so the [1m]-aware catalog resolver can see it at init (#1521);
        # otherwise a 1M ([1m]) agent would report a 200K window until its first
        # turn overwrites session_context_window from the runtime value.
        self.current_model: Optional[str] = os.getenv("AGENT_RUNTIME_MODEL", None) or os.getenv("CLAUDE_MODEL", None)
        self.session_context_window: int = self._get_default_context_window()
        # Session activity tracking (for real-time monitoring)
        self.session_activity = self._create_empty_activity()
        # Store full tool outputs for drill-down (separate from timeline summaries)
        self.tool_outputs: Dict[str, str] = {}

        # Richer /health signal (#1020). Tracked across both execution paths
        # (/api/chat and /api/task) so the platform gets a real health gauge,
        # not just {status: ok}. Guarded by a lock because tasks run
        # concurrently. `mailbox_depth` is intentionally NOT tracked here —
        # there is no agent-side mailbox until the actor model lands (#945);
        # the backend derives queue depth from CapacityManager.
        self._health_lock = threading.Lock()
        self.active_task_count: int = 0
        self.last_task_at: Optional[str] = None
        self.consecutive_failures: int = 0

    def record_task_start(self) -> None:
        """Mark an execution as started (either chat or headless task)."""
        with self._health_lock:
            self.active_task_count += 1
            self.last_task_at = datetime.now(timezone.utc).isoformat()

    def record_task_finish(self, success: Optional[bool]) -> None:
        """Mark an execution as finished. Resets the consecutive-failure counter
        on success, increments it on failure — this is the signal the dispatch
        circuit breaker (#526) consumes.

        #679 (F4): ``success=None`` is a NEUTRAL finish (a user cancel) — the
        task still finishes (decrement the active count, stamp last_task_at) but
        the failure counter is left untouched. A cancel is neither a failure (it
        must not push a healthy agent toward an open breaker) nor a success (it
        proves nothing about agent health), so it must not reset OR increment."""
        with self._health_lock:
            if self.active_task_count > 0:
                self.active_task_count -= 1
            self.last_task_at = datetime.now(timezone.utc).isoformat()
            if success is None:
                return
            if success:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1

    def _get_default_context_window(self) -> int:
        """Fallback context window when the runtime hasn't reported one yet.

        Resolves via the shared model catalog (#1521), keyed on the selected
        model (so an ``[1m]`` agent gets 1M) and falling back to the runtime name
        for the pre-first-turn / None-model case (``gemini-cli`` → 1M, else the
        200K safe floor). The authoritative value is the runtime-reported window,
        which overwrites ``session_context_window`` after each turn.
        """
        return resolve_context_window(self.current_model or self.agent_runtime)

    def _check_runtime_available(self) -> bool:
        """Check if the configured runtime CLI is available"""
        if self.agent_runtime == "gemini-cli" or self.agent_runtime == "gemini":
            return self._check_gemini_cli()
        return self._check_claude_code()

    def _check_gemini_cli(self) -> bool:
        """Check if Gemini CLI is available"""
        try:
            result = subprocess.run(
                ["gemini", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Gemini CLI check failed: {e}")
            return False

    def _create_empty_activity(self) -> Dict:
        """Create empty session activity structure"""
        return {
            "status": "idle",
            "active_tool": None,
            "tool_counts": {},
            "timeline": [],
            "totals": {
                "calls": 0,
                "duration_ms": 0,
                "started_at": None
            }
        }

    def _check_claude_code(self) -> bool:
        """Check if Claude Code CLI is available"""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Claude Code check failed: {e}")
            return False

    def add_message(self, role: str, content: str):
        """Add message to conversation history"""
        self.conversation_history.append(
            ChatMessage(
                role=role,
                content=content,
                timestamp=datetime.now()
            )
        )
        # FIFO trim once over the cap. Persistent history is in the backend DB;
        # the in-memory list is only for /api/chat/history + session counts.
        overflow = len(self.conversation_history) - self.history_limit
        if overflow > 0:
            del self.conversation_history[:overflow]

    def reset_session(self):
        """Reset conversation state and token tracking"""
        self.conversation_history = []
        self.session_started = False
        self.session_total_cost = 0.0
        self.session_total_output_tokens = 0
        self.session_context_tokens = 0
        # Note: current_model is NOT reset - it persists until explicitly changed
        # Reset session activity tracking
        self.session_activity = self._create_empty_activity()
        self.tool_outputs = {}


# Global agent state instance
agent_state = AgentState()
