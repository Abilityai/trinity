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
from .runtime_names import is_claude_runtime, normalize_runtime

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
        # Backward-compatibility alias: "is the CLAUDE CLI here", which for a
        # Claude agent is the same question runtime_available already answered.
        self.claude_code_available = (
            self.runtime_available
            if is_claude_runtime(self.agent_runtime)
            else self._check_claude_code()
        )
        self.session_started = False  # Track if we've started a conversation
        # #2958: the Claude chat path resumes ONLY its own session. The id is
        # captured after a successful chat turn (never before the subprocess
        # succeeds) and passed as `--resume <id>`; `--continue` took the newest
        # JSONL in the shared project dir, whoever wrote it (a schedule, a
        # Workspace turn). `chat_session_model` is the effective model that
        # session last ran under — a different model starts a fresh session.
        # `chat_session_generation` is bumped by reset_session() so a turn that
        # was in flight across a `DELETE /api/chat/history` drops its capture.
        self.chat_session_id: Optional[str] = None
        self.chat_session_model: Optional[str] = None
        self.chat_session_generation: int = 0
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

    # #2981: one CLI per runtime, table-driven. The previous shape
    # was `if gemini: … else: claude`, so a CODEX agent's availability probe ran
    # `claude --version` — reporting "available" from a binary the runtime never
    # invokes, and (worse) reporting "unavailable" for a perfectly healthy Codex
    # container if the Claude CLI were ever absent. That latent Codex bug is
    # fixed here along with adding opencode; the lookup is keyed on the
    # NORMALIZED name, so `AGENT_RUNTIME=Codex` probes codex (#1187 casing trap).
    _RUNTIME_PROBE_BINARY = {
        "claude-code": "claude",
        "claude": "claude",
        "gemini-cli": "gemini",
        "gemini": "gemini",
        "codex": "codex",
        "opencode": "opencode",
    }

    def _check_runtime_available(self) -> bool:
        """Check whether THIS runtime's CLI is available."""
        binary = self._RUNTIME_PROBE_BINARY.get(
            normalize_runtime(self.agent_runtime)
        )
        if binary is None:
            # An unknown runtime is not silently treated as Claude. get_runtime()
            # raises on it at dispatch; here we simply report it unavailable, so
            # /health and the info endpoint tell the truth instead of probing an
            # unrelated binary.
            logger.error(
                "Unknown AGENT_RUNTIME=%r — no CLI to probe; reporting unavailable",
                self.agent_runtime,
            )
            return False
        return self._check_cli(binary)

    @staticmethod
    def _check_cli(binary: str) -> bool:
        """Does ``<binary> --version`` answer?"""
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"{binary} CLI check failed: {e}")
            return False

    def _check_gemini_cli(self) -> bool:
        """Back-compat alias — kept because tests and callers reference it."""
        return self._check_cli("gemini")

    def _create_empty_activity(self) -> Dict:
        """Create empty session activity structure"""
        return {
            "status": "idle",
            "active_tool": None,
            # trinity-enterprise#620: the active tool PER EXECUTION, keyed by
            # the backend execution id. `active_tool` above is one slot per
            # agent process, so a chat turn and a delegated run on the same
            # agent overwrite each other; this is what the heartbeat reports.
            "by_execution": {},
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
        # #2958: forget the chat's own session (in memory and the reaper's
        # keep-set marker) and invalidate any capture still in flight.
        self.chat_session_id = None
        self.chat_session_model = None
        self.chat_session_generation += 1
        from .services import chat_session_marker  # lazy: avoids an import cycle
        chat_session_marker.clear()
        self.session_total_cost = 0.0
        self.session_total_output_tokens = 0
        self.session_context_tokens = 0
        # Note: current_model is NOT reset - it persists until explicitly changed
        # Reset session activity tracking
        self.session_activity = self._create_empty_activity()
        self.tool_outputs = {}


# Global agent state instance
agent_state = AgentState()
