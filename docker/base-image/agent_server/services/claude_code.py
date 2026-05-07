"""
Claude Code execution service.

Now implements AgentRuntime interface for multi-provider support.
"""
import os
import json
import uuid
import asyncio
import subprocess
import logging
import threading
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from ..models import CompactEvent, ExecutionLogEntry, ExecutionMetadata
from ..state import agent_state
from .activity_tracking import start_tool_execution, complete_tool_execution
from .runtime_adapter import AgentRuntime
from .process_registry import get_process_registry
from ..utils.credential_sanitizer import (
    sanitize_text,
    sanitize_dict,
    sanitize_subprocess_line,
)
from ..utils.subprocess_pgroup import (
    capture_pgid as _capture_pgid,
    terminate_process_group as _terminate_process_group,
    safe_close_pipes as _safe_close_pipes,
    drain_reader_threads as _drain_reader_threads,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSONL fallback recovery (stdout pipe race — final safety net)
# ---------------------------------------------------------------------------
#
# Claude Code persists every turn to ~/.claude/projects/<dir>/<uuid>.jsonl
# via a side-channel that's INDEPENDENT of stdout. When a tool subprocess
# (or MCP grandchild) inherits claude's stdout fd and wedges the agent
# server's reader thread, the stream-json result event is lost — but the
# JSONL on disk usually contains the completed turn.
#
# The Phase 5.1 soft-recovery (response_parts != [] → synthesize success)
# only fires when stdout managed to deliver at least one assistant text
# block before the wedge. For races that fire mid-tool-call (zero text
# emitted), response_parts is empty and the soft-recovery falls through
# to a hard 502.
#
# This helper is the next layer down: when stdout failed AND
# response_parts is empty, read the JSONL and pull the assistant text
# emitted during the just-completed turn. The data is authoritative
# (Claude Code's own session record), so when the read succeeds we can
# synthesize a full soft-success response and surface
# `metadata.recovered_from_jsonl = True` for observability.
#
# Recovery is bounded: we only walk forward from the most recent user
# input message (string content, not a tool_result), so prior turns'
# text never leaks into this turn's response.

_JSONL_PROJECTS_DIR = "/home/developer/.claude/projects/-home-developer"
_MAX_JSONL_BYTES_FOR_RECOVERY = 10 * 1024 * 1024  # 10MB cap on read


def _recover_response_from_jsonl(session_id: Optional[str]) -> Optional[str]:
    """Try to recover an assistant text response from a Claude Code JSONL.

    Returns the concatenated text of all assistant.text blocks emitted
    after the most recent user-input message in the JSONL, or None when:
      - session_id is missing
      - the JSONL file doesn't exist or can't be read
      - no user-input boundary is found (shouldn't happen in practice)
      - no assistant text was emitted after the boundary (Claude died
        mid-tool-call before writing any text — genuinely incomplete).

    The boundary uses the shape difference between user inputs (string
    content) and tool_results (list-of-dicts content) — Claude Code
    records them with different types in the JSONL.
    """
    if not session_id:
        return None

    jsonl_path = Path(f"{_JSONL_PROJECTS_DIR}/{session_id}.jsonl")
    if not jsonl_path.exists():
        return None

    try:
        if jsonl_path.stat().st_size > _MAX_JSONL_BYTES_FOR_RECOVERY:
            # Cap read size — turns rarely produce more than a few hundred
            # KB; pathological JSONLs shouldn't hang recovery indefinitely.
            with jsonl_path.open("rb") as f:
                f.seek(-_MAX_JSONL_BYTES_FOR_RECOVERY, os.SEEK_END)
                # Skip the partial first line after seeking mid-file.
                f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        else:
            raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[JSONL Recovery] Failed to read {jsonl_path}: {e}"
        )
        return None

    lines = raw.strip().split("\n")

    # Walk backward to find the boundary: the most recent user-INPUT
    # message (content is a string, not a list). tool_result entries
    # also have type=user but their content is a list of dicts.
    boundary_idx = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            boundary_idx = i
            break

    if boundary_idx is None:
        return None

    # Collect assistant.text blocks emitted after the boundary. Skip
    # tool_use blocks (no user-facing text), thinking blocks (model's
    # internal reasoning, never shown), and any non-list content.
    text_parts: List[str] = []
    for line in lines[boundary_idx + 1:]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    text_parts.append(text)

    if not text_parts:
        return None

    return "\n".join(text_parts)


def _extract_compact_events_from_jsonl(
    session_id: Optional[str], since_iso: Optional[str] = None
) -> List["CompactEvent"]:
    """Read compact_boundary records out of a Claude Code JSONL.

    Claude Code's `--output-format stream-json --verbose` emits
    `compact_boundary` events to stdout but strips the `compactMetadata`
    envelope (we get the event-fired signal but no pre/post/duration
    detail). The JSONL on disk has the canonical shape:

        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger":"auto", "preTokens":175061,
                             "postTokens":5904, "durationMs":73651},
         "timestamp": "2026-05-04T13:01:56.959Z", ...}

    This helper is called AFTER a turn completes to populate
    `metadata.compact_events` with the real detail fields. ``since_iso``
    filters to compact records emitted at or after the given ISO
    timestamp — used to scope the result to the just-completed turn
    when the JSONL has compact records from prior turns.

    Returns an empty list when the session_id is missing, the file
    doesn't exist, or no compact records are present.
    """
    if not session_id:
        return []

    jsonl_path = Path(f"{_JSONL_PROJECTS_DIR}/{session_id}.jsonl")
    if not jsonl_path.exists():
        return []

    try:
        if jsonl_path.stat().st_size > _MAX_JSONL_BYTES_FOR_RECOVERY:
            with jsonl_path.open("rb") as f:
                f.seek(-_MAX_JSONL_BYTES_FOR_RECOVERY, os.SEEK_END)
                f.readline()
                raw = f.read().decode("utf-8", errors="replace")
        else:
            raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[JSONL Compact Extract] Failed to read {jsonl_path}: {e}"
        )
        return []

    events: List["CompactEvent"] = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "system" or entry.get("subtype") != "compact_boundary":
            continue
        ts = entry.get("timestamp")
        if since_iso and isinstance(ts, str) and ts < since_iso:
            continue
        cm = entry.get("compactMetadata") or {}
        if not isinstance(cm, dict):
            cm = {}
        events.append(CompactEvent(
            trigger=cm.get("trigger"),
            pre_tokens=cm.get("preTokens"),
            post_tokens=cm.get("postTokens"),
            duration_ms=cm.get("durationMs"),
            timestamp=ts if isinstance(ts, str) else None,
        ))

    return events


# Thread pool for running blocking subprocess operations
# This allows FastAPI to handle other requests (like /api/activity polling) during execution
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
# The platform-level execution queue is the primary protection, but this is defense-in-depth
_execution_lock = asyncio.Lock()


# GUARD-003: CLI budget & scope controls.
# Guardrails runtime config is written by startup.sh via
# /opt/trinity/hooks/write-runtime-config.py and is root-owned 0444 so the
# agent cannot rewrite it. We read it on every Claude Code invocation so
# backend-initiated config updates (via container recreation) take effect
# without restarting the agent-server process.
_GUARDRAILS_RUNTIME_PATH = "/opt/trinity/guardrails-runtime.json"
_GUARDRAILS_BASELINE_PATH = "/opt/trinity/guardrails-baseline.json"
_DEFAULT_MAX_TURNS_CHAT = 50
_DEFAULT_MAX_TURNS_TASK = 50
_DEFAULT_EXECUTION_TIMEOUT_SEC = 1800  # GUARD-003 (#313): 30 min wall clock for chat


def _load_guardrails() -> dict:
    """Load guardrails config, falling back to baseline, then {}."""
    for path in (_GUARDRAILS_RUNTIME_PATH, _GUARDRAILS_BASELINE_PATH):
        try:
            with open(path) as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            continue
    return {}


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code implementation of AgentRuntime interface."""

    def is_available(self) -> bool:
        """Check if Claude Code CLI is installed."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        """Get default Claude model."""
        return "sonnet"  # Claude Sonnet 4.5

    def get_context_window(self, model: Optional[str] = None) -> int:
        """Get context window for Claude models."""
        # Check for 1M context models
        if model and "[1m]" in model.lower():
            return 1000000
        return 200000  # Standard 200K context

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """
        Configure MCP servers via .mcp.json file.
        Claude Code reads from ~/.mcp.json automatically.
        """
        try:
            mcp_config_path = Path.home() / ".mcp.json"
            config = {"mcpServers": mcp_servers}
            mcp_config_path.write_text(json.dumps(config, indent=2))
            logger.info(f"Configured {len(mcp_servers)} MCP servers for Claude Code")
            return True
        except Exception as e:
            logger.error(f"Failed to configure MCP: {e}")
            return False

    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        """Execute Claude Code with the given prompt.

        Returns: (response_text, execution_log, metadata, raw_messages)
            - execution_log: Simplified ExecutionLogEntry objects for activity tracking
            - raw_messages: Full Claude Code JSON transcript for execution log viewer
        """
        # Note: continue_session is handled internally by agent_state.session_started
        # The execute_claude_code function checks agent_state and uses --continue automatically
        return await execute_claude_code(prompt, stream, model, system_prompt=system_prompt, execution_id=execution_id)

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
        """Execute Claude Code in headless mode for parallel tasks.

        Args:
            resume_session_id: Optional session ID to resume (EXEC-023)
            persist_session: If True, write the JSONL so the next --resume can find it (Session tab)
            images: Optional list of vision images: [{"media_type": str, "data": base64_str}] (#562)
        """
        return await execute_headless_task(
            prompt, model, allowed_tools, system_prompt, timeout_seconds,
            max_turns, execution_id, resume_session_id,
            persist_session=persist_session, images=images,
        )


def parse_stream_json_output(output: str) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata]:
    """
    Parse stream-json output from Claude Code.

    Stream-json format emits one JSON object per line:
    - {"type": "system", "subtype": "init", "session_id": "abc123", ...}
    - {"type": "user", "message": {...}}
    - {"type": "assistant", "message": {"content": [{"type": "tool_use", ...}, ...]}}
    - {"type": "result", "total_cost_usd": 0.003, ...}

    Returns: (response_text, execution_log, metadata)
    """
    execution_log: List[ExecutionLogEntry] = []
    metadata = ExecutionMetadata()
    response_text = ""
    tool_start_times: Dict[str, datetime] = {}  # Track when tools started

    for line in output.strip().split('\n'):
        if not line.strip():
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse line as JSON: {line[:100]}")
            continue

        if not isinstance(msg, dict):
            # stream-json can emit string literals; skip them
            continue

        msg_type = msg.get("type")

        # Claude Code emits {"type": "system", "subtype": "init", ...} for the
        # session-start event; the result event also carries session_id and
        # serves as a fallback when the init line was missed (e.g. truncated
        # stream).
        if msg_type == "system" and msg.get("subtype") == "init":
            metadata.session_id = msg.get("session_id")

        elif msg_type == "system" and msg.get("subtype") == "compact_boundary":
            # Detection-only: stdout's stream-json strips the
            # compactMetadata envelope, so we capture a placeholder event
            # to preserve the count signal. The authoritative pre/post/
            # duration values are filled in post-turn from the JSONL via
            # _extract_compact_events_from_jsonl in execute_headless_task.
            cm = msg.get("compactMetadata", {}) or {}
            metadata.compact_events.append(CompactEvent(
                trigger=cm.get("trigger"),
                pre_tokens=cm.get("preTokens"),
                post_tokens=cm.get("postTokens"),
                duration_ms=cm.get("durationMs"),
                timestamp=msg.get("timestamp"),
            ))

        elif msg_type == "result":
            # Final result message with stats
            metadata.cost_usd = msg.get("total_cost_usd")
            metadata.duration_ms = msg.get("duration_ms")
            metadata.num_turns = msg.get("num_turns")
            response_text = msg.get("result", response_text)
            if not metadata.session_id:
                metadata.session_id = msg.get("session_id")

            # Extract token usage from result.usage
            usage = msg.get("usage", {})
            metadata.input_tokens = usage.get("input_tokens", 0)
            metadata.output_tokens = usage.get("output_tokens", 0)
            metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
            metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0)

            # Extract context window and token counts from modelUsage (preferred source)
            # modelUsage provides per-model breakdown with actual context usage
            model_usage = msg.get("modelUsage", {})
            for model_name, model_data in model_usage.items():
                if "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                # modelUsage.inputTokens is the authoritative context size (includes all turns)
                if "inputTokens" in model_data and model_data["inputTokens"] > metadata.input_tokens:
                    metadata.input_tokens = model_data["inputTokens"]
                if "outputTokens" in model_data and model_data["outputTokens"] > metadata.output_tokens:
                    metadata.output_tokens = model_data["outputTokens"]
                break  # Use first model found

        elif msg_type == "assistant":
            message_content = msg.get("message", {}).get("content", [])

            for content_block in message_content:
                if not isinstance(content_block, dict):
                    continue  # stream-json content arrays can contain plain strings
                block_type = content_block.get("type")

                if block_type == "tool_use":
                    # Tool is being called
                    tool_id = content_block.get("id", str(uuid.uuid4()))
                    tool_name = content_block.get("name", "Unknown")
                    tool_input = content_block.get("input", {})
                    timestamp = datetime.now()

                    tool_start_times[tool_id] = timestamp

                    execution_log.append(ExecutionLogEntry(
                        id=tool_id,
                        type="tool_use",
                        tool=tool_name,
                        input=tool_input,
                        timestamp=timestamp.isoformat()
                    ))

                    # Update session activity
                    start_tool_execution(tool_id, tool_name, tool_input)

                elif block_type == "tool_result":
                    # Tool result returned
                    tool_id = content_block.get("tool_use_id", "")
                    is_error = content_block.get("is_error", False)
                    timestamp = datetime.now()

                    # Extract output content for session activity
                    tool_output = ""
                    result_content = content_block.get("content", [])
                    if isinstance(result_content, list):
                        for item in result_content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                tool_output = item.get("text", "")
                                break
                    elif isinstance(result_content, str):
                        tool_output = result_content

                    # Calculate duration if we have start time
                    duration_ms = None
                    if tool_id in tool_start_times:
                        delta = timestamp - tool_start_times[tool_id]
                        duration_ms = int(delta.total_seconds() * 1000)

                    # Find the corresponding tool_use entry to get tool name
                    tool_name = "Unknown"
                    for entry in execution_log:
                        if entry.id == tool_id and entry.type == "tool_use":
                            tool_name = entry.tool
                            break

                    execution_log.append(ExecutionLogEntry(
                        id=tool_id,
                        type="tool_result",
                        tool=tool_name,
                        success=not is_error,
                        duration_ms=duration_ms,
                        timestamp=timestamp.isoformat()
                    ))

                    # Update session activity
                    complete_tool_execution(tool_id, not is_error, tool_output)

                elif block_type == "text":
                    # Claude's text response - accumulate it
                    text = content_block.get("text", "")
                    if text:
                        if response_text:
                            response_text += "\n" + text
                        else:
                            response_text = text

    # Count unique tools used
    tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
    metadata.tool_count = tool_use_count

    return response_text, execution_log, metadata


def process_stream_line(line: str, execution_log: List[ExecutionLogEntry], metadata: ExecutionMetadata,
                         tool_start_times: Dict[str, datetime], response_parts: List[str]) -> None:
    """
    Process a single line of stream-json output in real-time.
    Updates session activity, execution_log, metadata, and response_parts in place.
    """
    if not line.strip():
        return

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse line as JSON: {line[:100]}")
        return

    if not isinstance(msg, dict):
        # stream-json can emit string literals; skip them
        return

    msg_type = msg.get("type")

    # Claude Code emits {"type": "system", "subtype": "init", ...} for the
    # session-start event; the result event also carries session_id and
    # serves as a fallback when the init line was missed.
    if msg_type == "system" and msg.get("subtype") == "init":
        metadata.session_id = msg.get("session_id")

    elif msg_type == "system" and msg.get("subtype") == "compact_boundary":
        cm = msg.get("compactMetadata", {}) or {}
        event = CompactEvent(
            trigger=cm.get("trigger"),
            pre_tokens=cm.get("preTokens"),
            post_tokens=cm.get("postTokens"),
            duration_ms=cm.get("durationMs"),
            timestamp=msg.get("timestamp"),
        )
        metadata.compact_events.append(event)
        logger.info(
            f"event=session_auto_compact "
            f"claude_session_id={metadata.session_id} "
            f"trigger={event.trigger} "
            f"pre_tokens={event.pre_tokens} "
            f"post_tokens={event.post_tokens} "
            f"duration_ms={event.duration_ms}"
        )

    elif msg_type == "result":
        # Final result message with stats
        metadata.cost_usd = msg.get("total_cost_usd")
        metadata.duration_ms = msg.get("duration_ms")
        metadata.num_turns = msg.get("num_turns")
        result_text = msg.get("result", "")
        if not metadata.session_id:
            metadata.session_id = msg.get("session_id")

        # Detect error results (e.g., max_turns, rate limit, auth failures)
        if msg.get("is_error") and not metadata.error_type:
            # Check for max_turns termination first (Issue #361)
            terminal_reason = msg.get("terminal_reason")
            subtype = msg.get("subtype")
            if terminal_reason == "max_turns" or subtype == "error_max_turns":
                errors = msg.get("errors", [])
                metadata.error_type = "max_turns"
                metadata.error_message = errors[0] if errors else f"Task stopped after {metadata.num_turns} turns"
                logger.warning(f"Claude Code max_turns reached: {metadata.error_message}")
            elif _is_rate_limit_message(result_text):
                metadata.error_type = "rate_limit"
                metadata.error_message = result_text
            else:
                metadata.error_type = "execution_error"
                metadata.error_message = result_text
                logger.warning(f"Claude Code result is_error=true: type={metadata.error_type}, message={result_text}")

        if result_text:
            response_parts.clear()
            response_parts.append(result_text)

        # Pull only model-level facts from the result event (cost, duration,
        # num_turns are already set above). Token counts in result.usage and
        # modelUsage.inputTokens are CUMULATIVE across every internal API
        # call this turn made — for a tool-using turn with 18 iterations,
        # cache_read in result.usage = 18 × per-call cache_read = 1M+ tokens
        # of "billing total", which has nothing to do with the prompt size
        # any single call sent to the model. Overwriting metadata.* with
        # those values previously made our context-window-pressure metric
        # grow far beyond the 200K limit even when no individual call was
        # close to the wall.
        #
        # The per-assistant-message handler below tracks the per-API-call
        # usage; the LATEST assistant message's values represent the FINAL
        # API call's prompt — which is what determines whether the next
        # turn will fit.
        model_usage = msg.get("modelUsage", {})
        for model_name, model_data in model_usage.items():
            if "contextWindow" in model_data:
                metadata.context_window = model_data["contextWindow"]
            break  # Use first model found

        logger.debug(
            f"Result message parsed: cost=${metadata.cost_usd}, "
            f"duration={metadata.duration_ms}ms, num_turns={metadata.num_turns}, "
            f"context_window={metadata.context_window}, "
            f"per-call cache_read={metadata.cache_read_tokens} "
            f"(set by latest assistant message)"
        )

    elif msg_type == "assistant" or msg_type == "user":
        # Detect error classification on assistant messages (e.g., rate_limit, auth errors)
        if msg_type == "assistant" and msg.get("error"):
            metadata.error_type = msg["error"]
            # Extract the error text from content
            error_content = msg.get("message", {}).get("content", [])
            for block in error_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    metadata.error_message = block.get("text", "")
                    break
            logger.warning(f"Claude Code error detected: type={metadata.error_type}, message={metadata.error_message}")

        # Handle both assistant and user message types
        # tool_use appears in assistant messages, tool_result may appear in either
        message = msg.get("message", {})
        message_content = message.get("content", [])

        # Per-API-call token usage. Each assistant message corresponds to ONE
        # Claude API call; usage on it is the per-call breakdown (input,
        # cache_read, cache_creation, output). We OVERWRITE so the LATEST
        # assistant message wins — that's the final API call's prompt size,
        # which determines whether the next user turn will fit. Do NOT use
        # result.usage which is cumulative across all internal calls and
        # produces nonsense values like cache_read=1.26M for tool-heavy
        # turns. (parse_stream_json_output's batch path has the equivalent
        # block at lines 211-215.)
        if msg_type == "assistant":
            usage = message.get("usage", {}) or {}
            if usage:
                metadata.input_tokens = usage.get("input_tokens", 0)
                metadata.output_tokens = usage.get("output_tokens", 0)
                metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0)

        # Log message structure for debugging activity tracking issues
        if message_content:
            logger.debug(f"Processing {msg_type} message with {len(message_content)} content blocks")

        for content_block in message_content:
            if not isinstance(content_block, dict):
                continue  # stream-json content arrays can contain plain strings
            block_type = content_block.get("type")

            if block_type == "tool_use":
                # Tool is being called - update IMMEDIATELY
                tool_id = content_block.get("id", str(uuid.uuid4()))
                tool_name = content_block.get("name", "Unknown")
                tool_input = content_block.get("input", {})
                timestamp = datetime.now()

                tool_start_times[tool_id] = timestamp

                execution_log.append(ExecutionLogEntry(
                    id=tool_id,
                    type="tool_use",
                    tool=tool_name,
                    input=tool_input,
                    timestamp=timestamp.isoformat()
                ))

                # Update session activity in real-time
                start_tool_execution(tool_id, tool_name, tool_input)
                logger.debug(f"Tool started: {tool_name} ({tool_id})")

            elif block_type == "tool_result":
                # Tool result returned - update IMMEDIATELY
                tool_id = content_block.get("tool_use_id", "")
                is_error = content_block.get("is_error", False)
                timestamp = datetime.now()

                # Extract output content for session activity
                tool_output = ""
                result_content = content_block.get("content", [])
                if isinstance(result_content, list):
                    for item in result_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            tool_output = item.get("text", "")
                            break
                elif isinstance(result_content, str):
                    tool_output = result_content

                # Calculate duration if we have start time
                duration_ms = None
                if tool_id in tool_start_times:
                    delta = timestamp - tool_start_times[tool_id]
                    duration_ms = int(delta.total_seconds() * 1000)

                # Find the corresponding tool_use entry to get tool name
                tool_name = "Unknown"
                for entry in execution_log:
                    if entry.id == tool_id and entry.type == "tool_use":
                        tool_name = entry.tool
                        break

                execution_log.append(ExecutionLogEntry(
                    id=tool_id,
                    type="tool_result",
                    tool=tool_name,
                    success=not is_error,
                    duration_ms=duration_ms,
                    timestamp=timestamp.isoformat()
                ))

                # Update session activity in real-time
                complete_tool_execution(tool_id, not is_error, tool_output)
                logger.debug(f"Tool completed: {tool_name} ({tool_id}) - success={not is_error}")

            elif block_type == "text":
                # Claude's text response - accumulate it
                text = content_block.get("text", "")
                if text:
                    response_parts.append(text)


async def execute_claude_code(prompt: str, stream: bool = False, model: Optional[str] = None, system_prompt: Optional[str] = None, execution_id: Optional[str] = None) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
    """
    Execute Claude Code in headless mode with the given prompt.

    Uses streaming subprocess to update session activity in REAL-TIME as tools execute.

    Uses: claude --print --output-format stream-json
    Uses --continue flag for subsequent messages to maintain conversation context
    Uses --model to select Claude model (sonnet, opus, haiku, or full model name)

    Args:
        prompt: User message
        stream: Whether to stream (unused currently)
        model: Model override
        system_prompt: Platform instructions appended via --append-system-prompt

    Returns: (response_text, execution_log, metadata, raw_messages)
        - execution_log: Simplified ExecutionLogEntry objects for activity tracking
        - raw_messages: Full Claude Code JSON transcript for execution log viewer
    """

    if not agent_state.claude_code_available:
        raise HTTPException(
            status_code=503,
            detail="Claude Code is not available in this container"
        )

    try:
        # Note: Claude Code will use whatever authentication is available:
        # 1. OAuth session from /login (Claude Pro/Max subscription) - stored in ~/.claude.json
        # 2. ANTHROPIC_API_KEY environment variable (API billing)
        # We don't require ANTHROPIC_API_KEY since users may be logged in with their subscription.

        # Issue #138: Default to "sonnet" when no model is specified and none is set on state.
        # Same fix as Issue #81 for execute_headless_task() — without --model, Claude Code
        # uses the agent's ~/.claude/settings.json model, which may be incompatible with
        # the assigned subscription (e.g., haiku on Claude Max), causing misleading
        # "token expired" errors.
        if not model and not agent_state.current_model:
            model = "sonnet"
            logger.debug("[Chat] No model specified, defaulting to 'sonnet' for subscription compatibility")

        # Update model if specified (persists for session)
        if model:
            agent_state.current_model = model
            logger.info(f"Model set to: {model}")

        # Build command - use --continue for subsequent messages
        # Use stream-json for detailed execution log (requires --verbose)
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

        # GUARD-003: apply turn cap + disallowed tools from guardrails config.
        guardrails = _load_guardrails()
        max_turns_chat = int(guardrails.get("max_turns_chat") or _DEFAULT_MAX_TURNS_CHAT)
        cmd.extend(["--max-turns", str(max_turns_chat)])
        logger.info(f"[Chat] Limiting to {max_turns_chat} agentic turns")
        disallowed_tools = guardrails.get("disallowed_tools") or []
        if disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])
            logger.info(f"[Chat] Guardrails disallow tools: {disallowed_tools}")
        # GUARD-003 (#313): wall-clock cap so a stuck claude subprocess
        # (billing error, stalled stream, max-turns evasion) doesn't hang
        # the chat session until the container is killed externally.
        timeout_seconds = int(
            guardrails.get("execution_timeout_sec") or _DEFAULT_EXECUTION_TIMEOUT_SEC
        )

        # Add MCP config if .mcp.json exists (for agent-to-agent collaboration via Trinity MCP)
        mcp_config_path = Path.home() / ".mcp.json"
        if mcp_config_path.exists():
            cmd.extend(["--mcp-config", str(mcp_config_path)])

        # Add model selection if set
        if agent_state.current_model:
            cmd.extend(["--model", agent_state.current_model])
            logger.info(f"Using model: {agent_state.current_model}")

        if agent_state.session_started:
            # Continue the existing conversation
            cmd.append("--continue")
            logger.info("Continuing existing conversation session")
        else:
            # First message in session
            agent_state.session_started = True
            logger.info("Starting new conversation session")

        # Add platform system prompt if provided
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
            logger.info(f"Appending system prompt ({len(system_prompt)} chars)")

        # Initialize tracking structures
        execution_log: List[ExecutionLogEntry] = []
        raw_messages: List[Dict] = []  # Capture ALL raw JSON messages for execution log viewer
        metadata = ExecutionMetadata()
        tool_start_times: Dict[str, datetime] = {}
        response_parts: List[str] = []
        # Use provided execution_id if available (enables termination tracking from backend)
        execution_id = execution_id or str(uuid.uuid4())

        # Mark session as potentially running (will be set to running when first tool starts)
        logger.info(f"Starting Claude Code with streaming: {' '.join(cmd[:5])}...")

        # Use Popen for real-time streaming instead of blocking run().
        # Issue #407: start_new_session=True puts claude (and hook children
        # it spawns) into their own process group so we can reap the whole
        # tree on exit — hook grandchildren can otherwise outlive claude
        # and wedge readline() forever via inherited pipe FDs.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            start_new_session=True,
        )
        # Issue #407: capture pgid now — after wait() reaps the parent,
        # the pid is gone and we lose the ability to signal the group.
        process_pgid = _capture_pgid(process)

        # Register process for potential termination
        registry = get_process_registry()
        registry.register(execution_id, process, metadata={
            "type": "chat",
            "message_preview": prompt[:100],
            "pgid": process_pgid,
        })

        # Write prompt to stdin and close it
        process.stdin.write(prompt)
        process.stdin.close()

        stderr_lines: List[str] = []

        def read_stdout():
            """Read stdout (stream-json); parse and publish log entries."""
            try:
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    # Issue #630: per-line try/except so one bad line does
                    # not kill the reader and cost us the result line later
                    # in the stream.
                    try:
                        try:
                            raw_msg = json.loads(line.strip())
                        except json.JSONDecodeError:
                            raw_msg = None

                        if isinstance(raw_msg, dict):
                            # SECURITY: Sanitize credentials from output before storing
                            raw_msg = sanitize_dict(raw_msg)
                            raw_messages.append(raw_msg)
                            try:
                                registry.publish_log_entry(execution_id, raw_msg)
                            except Exception as pub_err:  # noqa: BLE001
                                logger.warning(
                                    f"publish_log_entry failed (continuing): {pub_err}"
                                )
                        sanitized_line = sanitize_subprocess_line(line)
                        process_stream_line(sanitized_line, execution_log, metadata, tool_start_times, response_parts)
                    except Exception as line_err:  # noqa: BLE001
                        logger.warning(
                            f"Per-line stdout processing error (continuing): {line_err}"
                        )
            except Exception as e:
                logger.error(f"Error reading Claude output: {e}")

        def read_stderr():
            """Read stderr line by line (captured for error reporting)."""
            try:
                for line in iter(process.stderr.readline, ''):
                    if not line:
                        break
                    stderr_lines.append(line)
            except Exception as e:
                logger.error(f"Error reading Claude stderr: {e}")

        def read_subprocess_output():
            """Runs in thread pool. Starts stdout/stderr reader threads, waits
            for subprocess (bounded by timeout_seconds — GUARD-003 #313),
            drains readers with process-group cleanup if hook grandchildren
            still hold pipes open (Issue #407)."""
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(
                    f"[Chat] Session {execution_id} timed out after {timeout_seconds}s "
                    f"— killing process group"
                )
                _terminate_process_group(process, graceful_timeout=5, pgid=process_pgid)
                _drain_reader_threads(
                    process, stdout_thread, stderr_thread,
                    grace=3, pgid=process_pgid,
                )
                raise

            _drain_reader_threads(
                process, stdout_thread, stderr_thread,
                grace=5, pgid=process_pgid,
            )

            stderr = ''.join(stderr_lines)
            stderr = sanitize_text(stderr) if stderr else stderr
            return stderr, return_code

        # Run the blocking subprocess reading in a thread pool to allow FastAPI
        # to handle other requests (like /api/activity polling) during execution.
        # Outer asyncio.wait_for is a safety net with a small grace period for
        # drain/cleanup after the inner process.wait() already bounded itself.
        loop = asyncio.get_event_loop()
        try:
            try:
                stderr_output, return_code = await asyncio.wait_for(
                    loop.run_in_executor(_executor, read_subprocess_output),
                    timeout=timeout_seconds + 60
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"[Chat] Outer timeout on session {execution_id} "
                    f"— killing process group as last resort"
                )
                # _terminate_process_group does up to 4s of process.wait() (SIGTERM grace + SIGKILL grace);
                # off-load to the executor so the event loop stays responsive while we tear down.
                await loop.run_in_executor(
                    None,
                    lambda: _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid),
                )
                await loop.run_in_executor(None, _safe_close_pipes, process)
                raise HTTPException(
                    status_code=504,
                    detail=f"Chat execution timed out after {timeout_seconds} seconds"
                )
            except subprocess.TimeoutExpired:
                logger.error(f"[Chat] Session {execution_id} timed out after {timeout_seconds}s")
                raise HTTPException(
                    status_code=504,
                    detail=f"Chat execution timed out after {timeout_seconds} seconds"
                )

            # Check for rate limit detected during stream parsing (takes priority)
            if metadata.error_type == "rate_limit":
                error_detail = _format_rate_limit_error(metadata)
                logger.error(f"Claude Code rate limit: {error_detail}")
                raise HTTPException(
                    status_code=429,
                    detail=error_detail
                )

            # Check for errors
            if return_code != 0:
                error_detail = stderr_output[:500] if stderr_output else ""
                if not error_detail:
                    error_detail = _diagnose_exit_failure(return_code, metadata)
                # Also check if stderr contains a rate limit message
                if _is_rate_limit_message(error_detail) or _is_rate_limit_message(stderr_output):
                    raise HTTPException(
                        status_code=429,
                        detail=f"Subscription usage limit: {error_detail[:300]}"
                    )
                logger.error(f"Claude Code failed (exit {return_code}): {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Claude Code execution failed (exit code {return_code}): {error_detail[:300]}"
                )

            # Build final response text
            response_text = "\n".join(response_parts) if response_parts else ""
            # SECURITY: Sanitize credentials from response text
            response_text = sanitize_text(response_text)

            if not response_text:
                raise HTTPException(
                    status_code=500,
                    detail="Claude Code returned empty response"
                )

            # Count unique tools used
            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count
            metadata.execution_id = execution_id  # Track execution_id in metadata

            # Log metadata for debugging
            # NOTE: input_tokens already includes cached tokens - cache_creation and cache_read are billing subsets, NOT additional
            logger.info(f"Claude response: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}, context={metadata.input_tokens}/{metadata.context_window}, raw_messages={len(raw_messages)}, execution_id={execution_id}")

            return response_text, execution_log, metadata, raw_messages
        finally:
            # Always unregister process when done
            registry.unregister(execution_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Claude Code execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")


def _is_rate_limit_message(text: str) -> bool:
    """Check if a message indicates a subscription usage/rate limit error."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "out of extra usage",
        "out of usage",
        "usage limit",
        "rate limit",
        "rate_limit",
        "resets ",  # "resets 1am (America/New_York)"
        "exceeded your",
        "quota exceeded",
    ])


def _is_model_access_error(text: str) -> bool:
    """Check if a message indicates a model access/subscription tier error."""
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "model is not available",
        "not available on your subscription",
        "don't have access to",
        "model not found",
        "invalid model",
        "model access",
        "cannot access",
        "not supported by your plan",
    ])


def _is_auth_failure_message(text: str) -> bool:
    """Check if a message indicates an authentication/token failure.

    These patterns indicate the subscription token is expired, revoked,
    or otherwise invalid. When detected during execution, we should
    abort immediately rather than waiting for the full timeout.

    Issue #285: Expired tokens can cause Claude Code to hang instead of
    failing fast. Real-time detection in stderr allows early abort.
    """
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "subscription token may be expired",
        "token may be expired",
        "token expired",
        "token revoked",
        "invalid token",
        "authentication failed",
        "auth failed",
        "setup-token",  # "Generate a new one with 'claude setup-token'"
        "oauth token",
        "unauthorized",
        "invalid credentials",
        "credentials expired",
    ])


def _format_rate_limit_error(metadata: 'ExecutionMetadata') -> str:
    """Format a clear, actionable rate limit error message."""
    base_msg = metadata.error_message or "Subscription usage limit reached"
    return (
        f"Subscription usage limit: {base_msg}. "
        f"To resolve: (1) wait for the usage reset, "
        f"(2) set an ANTHROPIC_API_KEY on this agent for pay-per-use billing, "
        f"or (3) assign a different subscription token in Settings → Subscriptions."
    )


def _diagnose_exit_failure(return_code: int, metadata: Optional['ExecutionMetadata'] = None) -> str:
    """Diagnose common Claude Code exit failures when stderr is empty."""
    # Check for rate limit detected during stream parsing
    if metadata and metadata.error_type == "rate_limit":
        return _format_rate_limit_error(metadata)

    # Check for billing errors (e.g., credit balance too low)
    if metadata and metadata.error_type == "billing_error":
        error_msg = metadata.error_message or "Billing error"
        return (
            f"{error_msg}. "
            f"To resolve: (1) add credits to your Anthropic account at console.anthropic.com, "
            f"or (2) assign a subscription token with available usage in Settings → Subscriptions."
        )

    # Check for model access errors detected during stream parsing
    if metadata and metadata.error_message and _is_model_access_error(metadata.error_message):
        return (
            f"Model access error: {metadata.error_message}. "
            f"The agent's configured model may not be available with the current subscription. "
            f"Try using a different model (sonnet, opus) or check subscription settings."
        )

    # Check for missing credentials
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_oauth_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    if not has_api_key and not has_oauth_token:
        return "No authentication configured. Set ANTHROPIC_API_KEY or assign a subscription token."
    if not has_api_key and has_oauth_token:
        # Issue #81: This error message was misleading when the actual issue was
        # model incompatibility. Now that we default to 'sonnet' for headless tasks,
        # this message is more likely to be accurate.
        return "Subscription token may be expired or revoked. Generate a new one with 'claude setup-token'."

    # Exit code hints
    hints = {
        1: "General error. Check agent logs for details.",
        2: "Misuse of command or invalid arguments.",
        126: "Claude Code command found but not executable.",
        127: "Claude Code command not found. Base image may need rebuilding.",
        137: "Process killed (SIGKILL). Likely out of memory — check agent resource limits.",
        139: "Segmentation fault.",
        143: "Process terminated (SIGTERM).",
    }
    return hints.get(return_code, f"Process exited with code {return_code}. Check agent container logs.")


# Signals that indicate external termination of the claude subprocess
# (timeout SIGKILL, OOM-kill, parent SIGTERM, operator cancel).
# Python subprocess returns these as negative numbers; shell wrappers
# may surface them as 128 + signum (130, 137, 143).
_SIGNAL_EXIT_NAMES = {
    2: "SIGINT",
    9: "SIGKILL",
    15: "SIGTERM",
}
_SHELL_ENCODED_SIGNAL_EXITS = {130, 137, 143}


def _classify_signal_exit(
    return_code: int,
    metadata: Optional['ExecutionMetadata'] = None,
) -> Optional[Tuple[int, str]]:
    """Classify a non-zero subprocess exit as an external signal kill.

    Issue #516: When claude is killed by SIGKILL/SIGTERM/SIGINT (schedule
    timeout, OOM-kill, parent cancel), the subprocess never emits its final
    `result` message and `process.wait()` returns a negative or 128+N exit
    code. Without this classification, the downstream auth-fallback heuristic
    misreads "zero tokens processed" as an expired subscription token and
    surfaces a confusing "Generate a new one with claude setup-token" error
    on every cron tick — masking the real cause (timeout/OOM/cancel).

    Mirrors the #361 max-turns special-case pattern: classify *before* the
    auth heuristics get a chance to misclassify.

    Returns ``(status_code, detail)`` for signal exits, or ``None`` if the
    return code is not a recognized signal termination (caller proceeds
    with normal error classification).
    """
    if return_code < 0:
        signum = -return_code
    elif return_code in _SHELL_ENCODED_SIGNAL_EXITS:
        signum = return_code - 128
    else:
        return None

    sig_name = _SIGNAL_EXIT_NAMES.get(signum, f"signal {signum}")
    tool_count = metadata.tool_count if metadata else 0
    num_turns = metadata.num_turns if (metadata and metadata.num_turns) else 0
    detail = (
        f"Execution terminated by {sig_name} after {tool_count} tool calls "
        f"/ {num_turns} turns (exit code {return_code}). "
        f"Likely cause: schedule/agent timeout exceeded, OOM kill, or operator cancel. "
        f"Increase the schedule's timeout_seconds, raise agent memory, "
        f"or split the skill into smaller steps."
    )
    return (504, detail)


def _recover_metadata_from_raw_messages(
    metadata: Optional['ExecutionMetadata'],
    raw_messages: Optional[List[Dict]],
) -> bool:
    """Back-fill ``metadata`` from a ``{"type": "result"}`` entry in
    ``raw_messages`` when ``process_stream_line`` failed to populate it.

    Issue #630: even when the reader thread successfully appends the result
    line to ``raw_messages``, ``process_stream_line`` may not run for that
    line if the reader is interrupted between the append and the parse
    (registry publish raising, permission-validation re-raise, any other
    in-loop exception). In that case ``metadata.cost_usd`` /
    ``duration_ms`` stay ``None`` even though Claude completed cleanly and
    the final stats are sitting in ``raw_messages[-1]``.

    This recovery pass scans ``raw_messages`` from the end (the result line
    is always last) and copies the fields ``process_stream_line`` would
    have set: ``cost_usd``, ``duration_ms``, ``num_turns``, and the token
    counters from ``usage`` / ``modelUsage``. ``error_type`` / response
    text are not back-filled — those drive control flow and would change
    behaviour beyond this defensive recovery.

    Returns ``True`` if recovery populated metadata, ``False`` otherwise.
    Safe to call when metadata is already populated — short-circuits.
    """
    if metadata is None or not raw_messages:
        return False
    if metadata.cost_usd is not None or metadata.duration_ms is not None:
        return False

    for msg in reversed(raw_messages):
        if not isinstance(msg, dict) or msg.get("type") != "result":
            continue

        cost = msg.get("total_cost_usd")
        dur = msg.get("duration_ms")
        if cost is None and dur is None:
            return False  # malformed result entry — nothing to recover

        metadata.cost_usd = cost
        metadata.duration_ms = dur
        metadata.num_turns = msg.get("num_turns")

        usage = msg.get("usage", {}) if isinstance(msg.get("usage"), dict) else {}
        if usage:
            metadata.input_tokens = usage.get("input_tokens", metadata.input_tokens) or metadata.input_tokens
            metadata.output_tokens = usage.get("output_tokens", metadata.output_tokens) or metadata.output_tokens
            metadata.cache_creation_tokens = (
                usage.get("cache_creation_input_tokens", metadata.cache_creation_tokens)
                or metadata.cache_creation_tokens
            )
            metadata.cache_read_tokens = (
                usage.get("cache_read_input_tokens", metadata.cache_read_tokens)
                or metadata.cache_read_tokens
            )

        model_usage = msg.get("modelUsage", {})
        if isinstance(model_usage, dict):
            for _, model_data in model_usage.items():
                if not isinstance(model_data, dict):
                    continue
                if "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                model_in = model_data.get("inputTokens")
                if isinstance(model_in, int) and model_in > metadata.input_tokens:
                    metadata.input_tokens = model_in
                model_out = model_data.get("outputTokens")
                if isinstance(model_out, int) and model_out > metadata.output_tokens:
                    metadata.output_tokens = model_out
                break  # first model wins, mirrors process_stream_line

        return True

    return False


def _classify_empty_result(
    metadata: Optional['ExecutionMetadata'] = None,
    raw_message_count: int = 0,
    raw_messages: Optional[List[Dict]] = None,
) -> Optional[Tuple[int, str]]:
    """Classify a clean (return_code == 0) exit that produced no result message.

    Issue #520: When the claude subprocess exits 0 but the final
    ``{"type":"result"}`` JSON line was dropped before the reader thread
    captured it (typical cause: an MCP tool / child subprocess inherited
    stdout, kept the pipe open past claude's exit, the reader leaked,
    pgroup unwind closed the pipe, the result line went with it), the
    metadata fields populated *only* by the result message — ``cost_usd``
    and ``duration_ms`` — stay ``None``. Returning HTTP 200 here would
    have agent-server log "completed successfully" while backend silently
    reaps the execution as an orphan minutes later, masking the real
    failure with misleading diagnostics.

    Sibling of ``_classify_signal_exit`` (issue #516): both classify
    "subprocess plumbing dropped the result" cases that the success path
    would otherwise mishandle. The two-field check (``cost_usd`` AND
    ``duration_ms`` both ``None``) is conservative — single-field
    nullability could be a Claude format quirk; both-None is a strong
    signal that the terminal ``result`` message never arrived.

    When the result line is lost, metadata.tool_count / num_turns are also
    None (populated only by that line). Derive honest counts from
    raw_messages when available so the 502 detail is accurate. (#531)

    Issue #630: before classifying, attempt
    ``_recover_metadata_from_raw_messages`` — covers the case where the
    result line *was* parsed and appended to raw_messages but
    process_stream_line failed to run for it (reader-thread exit between
    append and parse). When recovery succeeds, metadata is populated and
    the function falls through to the success path.

    Returns ``(status_code, detail)`` for empty-result exits, or ``None``
    if metadata looks well-formed (caller proceeds with the normal
    response-building path).
    """
    if metadata is None:
        return None
    if metadata.cost_usd is not None or metadata.duration_ms is not None:
        return None

    if _recover_metadata_from_raw_messages(metadata, raw_messages):
        logger.warning(
            "[Headless Task] Recovered result metadata from raw_messages "
            "(stream parser missed the result line; cost=%s duration=%sms turns=%s)",
            metadata.cost_usd, metadata.duration_ms, metadata.num_turns,
        )
        return None

    # tool_count is accumulated per-message during parsing (line ~1467), so
    # it's reliable even when the result line is lost. num_turns is populated
    # only by the result line — fall back to counting assistant messages in
    # raw_messages when it's None. (#531)
    tool_count = metadata.tool_count or 0
    if metadata.num_turns is not None:
        num_turns = metadata.num_turns
    elif raw_messages:
        num_turns = sum(1 for m in raw_messages if m.get("type") == "assistant")
    else:
        num_turns = 0

    detail = (
        f"Execution completed without a result message after {tool_count} tool calls "
        f"/ {num_turns} turns (raw_messages={raw_message_count}). "
        f"Likely cause: a tool or child subprocess inherited stdout and prevented "
        f"the claude reader thread from capturing the final result block. "
        f"Check agent-server logs for 'Reader thread(s) stuck after process exit' or "
        f"'I/O operation on closed file' near this execution. "
        f"This is a transient infrastructure failure; retry the task."
    )
    return (502, detail)


def get_execution_lock():
    """Get the execution lock for chat endpoint"""
    return _execution_lock


async def execute_headless_task(
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
) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute Claude Code in headless mode for parallel task execution.

    Unlike execute_claude_code(), this function:
    - Does NOT acquire execution lock (parallel allowed)
    - Does NOT use --continue flag (stateless, no conversation context) by default
    - Each call is independent and can run concurrently
    - Can resume previous sessions via resume_session_id (EXEC-023)
    - Can persist the session JSONL via persist_session=True (Session tab)

    Args:
        prompt: The task to execute
        model: Optional model override (sonnet, opus, haiku). Defaults to "sonnet" if not
               specified to ensure compatibility with all subscription types (Issue #81).
        allowed_tools: Optional list of allowed tools (restricts available tools)
        system_prompt: Optional additional system prompt
        timeout_seconds: Execution timeout (default 5 minutes)
        max_turns: Maximum agentic turns for runaway prevention (None = unlimited)
        execution_id: Optional execution ID to use for process registry (enables termination tracking)
        resume_session_id: Optional Claude Code session ID to resume (EXEC-023)
        persist_session: If True, omit ``--no-session-persistence`` so the
            JSONL is written to ``~/.claude/projects/...`` and a future
            ``--resume`` can reattach. Default False keeps headless tasks
            stateless for all existing callers.

    Returns: (response_text, execution_log, metadata, session_id)
    """
    # Issue #81: Default to "sonnet" when model is not specified.
    # Without this, Claude Code uses the agent's ~/.claude/settings.json model,
    # which may be incompatible with the assigned subscription (e.g., haiku on
    # Claude Max). This causes misleading "token expired" errors.
    # Using --model sonnet ensures compatibility with all subscription types.
    if model is None:
        model = "sonnet"
        logger.debug("[Headless Task] No model specified, defaulting to 'sonnet' for subscription compatibility")

    if not agent_state.claude_code_available:
        raise HTTPException(
            status_code=503,
            detail="Claude Code is not available in this container"
        )

    try:
        # Note: Claude Code will use whatever authentication is available:
        # 1. OAuth session from /login (Claude Pro/Max subscription) - stored in ~/.claude.json
        # 2. ANTHROPIC_API_KEY environment variable (API billing)
        # We don't require ANTHROPIC_API_KEY since users may be logged in with their subscription.

        # Build command - NO --continue flag (stateless) unless resuming
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

        # Add --resume if resuming a previous session (EXEC-023)
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
            logger.info(f"[Headless Task] Resuming session: {resume_session_id}")
        else:
            # Session isolation: prevent headless tasks from writing session files
            # that could collide with interactive /api/chat sessions or other tasks.
            # --no-session-persistence avoids shared state in ~/.claude/projects/
            # --session-id ensures unique namespace per task execution.
            #
            # Session tab opt-in (persist_session=True): the caller wants the
            # JSONL written so the next turn can --resume it. We still pass a
            # unique --session-id so cold turns don't collide on disk.
            if not persist_session:
                cmd.append("--no-session-persistence")
            # Claude Code requires --session-id to be a valid UUID.
            # execution_id is a base64url token (not a UUID), so always generate one.
            cmd.extend(["--session-id", str(uuid.uuid4())])

        # Add MCP config if .mcp.json exists (for agent-to-agent collaboration via Trinity MCP)
        mcp_config_path = Path.home() / ".mcp.json"
        if mcp_config_path.exists():
            cmd.extend(["--mcp-config", str(mcp_config_path)])

        # Add model selection if specified
        if model:
            cmd.extend(["--model", model])
            logger.info(f"[Headless Task] Using model: {model}")

        # Add allowed tools restriction if specified
        if allowed_tools:
            tools_str = ",".join(allowed_tools)
            cmd.extend(["--allowedTools", tools_str])
            logger.info(f"[Headless Task] Restricting tools to: {tools_str}")

        # GUARD-003: merge disallowed tools from guardrails config.
        guardrails = _load_guardrails()
        disallowed_tools = guardrails.get("disallowed_tools") or []
        if disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])
            logger.info(f"[Headless Task] Guardrails disallow tools: {disallowed_tools}")

        # #562: when images are present, use stream-json stdin format so images
        # are delivered as proper vision content blocks, not base64 text strings.
        if images:
            cmd.extend(["--input-format", "stream-json"])
            logger.info(f"[Headless Task] {len(images)} image(s) — switching to stream-json input")

        # Add system prompt if specified
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
            logger.info(f"[Headless Task] Appending system prompt ({len(system_prompt)} chars)")

        # GUARD-003: max-turns limit for runaway prevention.
        # Caller value wins; otherwise fall back to guardrails config, then hardcoded default.
        effective_max_turns = max_turns
        if effective_max_turns is None:
            effective_max_turns = int(guardrails.get("max_turns_task") or _DEFAULT_MAX_TURNS_TASK)
        cmd.extend(["--max-turns", str(effective_max_turns)])
        logger.info(f"[Headless Task] Limiting to {effective_max_turns} agentic turns")

        # Initialize tracking structures
        execution_log: List[ExecutionLogEntry] = []
        raw_messages: List[Dict] = []  # Capture ALL raw JSON messages from Claude Code
        verbose_output_lines: List[str] = []  # Capture verbose text output (stderr)
        metadata = ExecutionMetadata()
        tool_start_times: Dict[str, datetime] = {}
        response_parts: List[str] = []
        permission_mode_validated = False  # Track whether init message confirmed bypassPermissions
        # Use provided execution_id if available (enables termination tracking from backend)
        task_session_id = execution_id or str(uuid.uuid4())

        # Anchor for scoping post-turn JSONL extracts (compact events) to
        # this turn only — the JSONL accumulates across the resumed
        # session's lifetime, so we filter records by timestamp >= start.
        task_start_iso = datetime.utcnow().isoformat() + "Z"

        logger.info(f"[Headless Task] Starting task {task_session_id}: {' '.join(cmd[:5])}...")

        # Use Popen for real-time streaming.
        # Issue #407: start_new_session=True puts claude (and any hooks it
        # spawns) into their own process group so we can reap the whole tree
        # on exit/timeout. Without this, hook grandchildren can outlive
        # claude, keep pipe FDs open, and wedge readline() forever.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            start_new_session=True,
        )
        # Issue #407: capture pgid now — after wait() reaps the parent,
        # the pid is gone and we lose the ability to signal the group.
        process_pgid = _capture_pgid(process)

        # Register process for potential termination
        registry = get_process_registry()
        registry.register(task_session_id, process, metadata={
            "type": "task",
            "message_preview": prompt[:100],
            "pgid": process_pgid,
        })

        # Issue #285: Event to signal auth failure detected in stderr
        # When set, stdout loop should stop and process should be killed
        auth_abort_event = threading.Event()
        auth_abort_reason: List[str] = []  # Capture the auth failure message

        # Issue #407: box for capturing exceptions from the stdout reader
        # thread (e.g. permission-mode RuntimeError) so the main executor
        # function can re-raise after joining.
        stdout_exc: List[BaseException] = []

        def read_stderr():
            """Read stderr line by line; scan for auth failures."""
            try:
                for line in iter(process.stderr.readline, ''):
                    if not line:
                        break
                    stripped = line.rstrip('\n')
                    verbose_output_lines.append(stripped)

                    # Issue #285: detect auth failures in real time
                    if _is_auth_failure_message(stripped):
                        logger.warning(
                            f"[Headless Task] Auth failure detected in stderr: {stripped[:200]}"
                        )
                        auth_abort_reason.append(stripped)
                        auth_abort_event.set()
                        # Kill the whole process group so stdout's readline()
                        # gets EOF and we unwind cleanly (Issue #407).
                        try:
                            _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid)
                        except Exception as kill_err:
                            logger.error(
                                f"[Headless Task] Failed to kill process on auth abort: {kill_err}"
                            )
                        break
            except Exception as e:
                logger.error(f"[Headless Task] Error reading stderr: {e}")

        def read_stdout():
            """Read stdout (stream-json); parse and publish log entries."""
            nonlocal permission_mode_validated
            try:
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    # Issue #285: stderr thread detected auth failure
                    if auth_abort_event.is_set():
                        logger.info(f"[Headless Task] Stdout loop exiting due to auth abort")
                        break

                    # Issue #630: each line is processed inside a per-line
                    # try/except so a single failure (publish_log_entry
                    # raising, process_stream_line tripping on weird input,
                    # any non-RuntimeError exception) does not kill the
                    # reader. If the reader exits early the result line
                    # later in the stream is lost and the execution is
                    # misclassified as "completed without a result message".
                    # RuntimeError (permission-mode failure) is intentional
                    # — keep the existing fast-fail behaviour.
                    try:
                        try:
                            raw_msg = json.loads(line.strip())
                        except json.JSONDecodeError:
                            raw_msg = None

                        if isinstance(raw_msg, dict):
                            # SECURITY: Sanitize credentials from output before storing
                            raw_msg = sanitize_dict(raw_msg)
                            raw_messages.append(raw_msg)
                            # Publish to live streaming subscribers — isolate
                            # so subscriber-side breakage cannot back-pressure
                            # the reader.
                            try:
                                registry.publish_log_entry(task_session_id, raw_msg)
                            except Exception as pub_err:  # noqa: BLE001
                                logger.warning(
                                    f"[Headless Task] publish_log_entry failed (continuing): {pub_err}"
                                )

                            # Validate permissionMode on init message (first message from Claude Code).
                            # If permission bypass isn't active, kill immediately instead of timing out
                            # after hours with zero work completed (all tool calls silently denied).
                            # Claude Code emits {"type": "system", "subtype": "init", ...} — see Appendix B
                            # of docs/planning/SESSION_TAB_2026-04.md.
                            if (
                                raw_msg.get("type") == "system"
                                and raw_msg.get("subtype") == "init"
                                and not permission_mode_validated
                            ):
                                perm_mode = raw_msg.get("permissionMode", "unknown")
                                if perm_mode == "bypassPermissions":
                                    permission_mode_validated = True
                                    logger.info(f"[Headless Task] Permission mode confirmed: {perm_mode}")
                                else:
                                    logger.error(
                                        f"[Headless Task] CRITICAL: Permission bypass not active! "
                                        f"permissionMode={perm_mode} (expected bypassPermissions). "
                                        f"Killing process tree to prevent silent timeout. "
                                        f"Task: {task_session_id}"
                                    )
                                    _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid)
                                    raise RuntimeError(
                                        f"Permission bypass failed: permissionMode={perm_mode}. "
                                        f"This may be caused by a stale Claude Code session process "
                                        f"or project settings overriding the CLI flag. "
                                        f"Try restarting the agent container."
                                    )

                        # SECURITY: Sanitize the line before processing
                        sanitized_line = sanitize_subprocess_line(line)
                        # Process each line for metadata/tool tracking
                        process_stream_line(sanitized_line, execution_log, metadata, tool_start_times, response_parts)
                    except RuntimeError:
                        raise  # Re-raise permission-mode failures
                    except Exception as line_err:  # noqa: BLE001
                        logger.warning(
                            f"[Headless Task] Per-line stdout processing error (continuing): {line_err}"
                        )
            except RuntimeError:
                raise  # Re-raise permission mode failures
            except Exception as e:
                logger.error(f"[Headless Task] Error reading stdout: {e}")

        def _run_stdout():
            try:
                read_stdout()
            except BaseException as e:  # noqa: BLE001 — captured for main thread re-raise
                stdout_exc.append(e)
                # Wake the main thread's process.wait() by killing the group
                try:
                    _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid)
                except Exception:
                    pass

        def read_subprocess_output_with_timeout():
            """Runs in thread pool. Writes stdin, starts reader threads, and
            waits for subprocess with bounded timeout, then drains reader
            threads (killing process-group stragglers if they hold pipes
            open — Issue #407).

            Stdin is written here (not in the async coroutine) so that:
            1. Large payloads (e.g. base64 images) do not block the event loop.
            2. Reader threads are active before the write, preventing pipe-
               buffer deadlock if claude writes stdout before stdin is closed.
            """
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread = threading.Thread(target=_run_stdout, daemon=True)
            stderr_thread.start()
            stdout_thread.start()

            # Build and write stdin payload. For vision tasks use stream-json
            # format so images arrive as proper content blocks (#562).
            if images:
                content_blocks: List[Dict] = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["data"],
                        },
                    }
                    for img in images
                ]
                content_blocks.append({"type": "text", "text": prompt})
                stdin_payload = (
                    json.dumps({"type": "user", "message": {"role": "user", "content": content_blocks}})
                    + "\n"
                )
            else:
                stdin_payload = prompt

            process.stdin.write(stdin_payload)
            process.stdin.close()

            # Bounded wait on the subprocess itself. If claude hangs, we
            # never wedge the executor thread for more than timeout_seconds.
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(
                    f"[Headless Task] Task {task_session_id} timed out after {timeout_seconds}s "
                    f"— killing process group"
                )
                _terminate_process_group(process, graceful_timeout=5, pgid=process_pgid)
                _drain_reader_threads(
                    process, stdout_thread, stderr_thread,
                    grace=3, pgid=process_pgid,
                )
                raise

            # Subprocess exited. Drain readers — if a hook grandchild still
            # holds a pipe, the helper will close the pipe FDs so the
            # reader threads can exit.
            _drain_reader_threads(
                process, stdout_thread, stderr_thread,
                grace=5, pgid=process_pgid,
            )

            # Re-raise permission-mode failure captured by stdout thread
            if stdout_exc:
                raise stdout_exc[0]

            return return_code

        # Run with timeout using asyncio. The inner function already bounds
        # its wait on the subprocess; the outer wait_for is a safety net
        # with a small grace period for drain/cleanup.
        loop = asyncio.get_event_loop()
        try:
            try:
                return_code = await asyncio.wait_for(
                    loop.run_in_executor(None, read_subprocess_output_with_timeout),
                    timeout=timeout_seconds + 60
                )
            except asyncio.TimeoutError:
                # Inner machinery should have raised first; safety net.
                logger.error(
                    f"[Headless Task] Outer timeout on task {task_session_id} "
                    f"— killing process group as last resort"
                )
                # _terminate_process_group does up to 4s of process.wait() (SIGTERM grace + SIGKILL grace);
                # off-load to the executor so the event loop stays responsive while we tear down.
                await loop.run_in_executor(
                    None,
                    lambda: _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid),
                )
                await loop.run_in_executor(None, _safe_close_pipes, process)
                raise HTTPException(
                    status_code=504,
                    detail=f"Task execution timed out after {timeout_seconds} seconds"
                )
            except subprocess.TimeoutExpired:
                # Inner process.wait() timed out; tree has already been killed.
                logger.error(f"[Headless Task] Task {task_session_id} timed out after {timeout_seconds}s")
                raise HTTPException(
                    status_code=504,
                    detail=f"Task execution timed out after {timeout_seconds} seconds"
                )
            except RuntimeError as e:
                # Permission mode validation failure — fast-fail with actionable error
                if "Permission bypass failed" in str(e):
                    raise HTTPException(
                        status_code=503,
                        detail=str(e)
                    )
                raise

            # Build verbose transcript from stderr (the human-readable execution log)
            # SECURITY: Sanitize stderr output
            verbose_output_lines = [sanitize_text(line) for line in verbose_output_lines]
            verbose_transcript = "\n".join(verbose_output_lines)

            # Check for rate limit detected during stream parsing (takes priority)
            if metadata.error_type == "rate_limit":
                error_detail = _format_rate_limit_error(metadata)
                logger.error(f"[Headless Task] Rate limit: {error_detail}")
                raise HTTPException(
                    status_code=429,
                    detail=error_detail
                )

            # Issue #361: Check for max_turns termination (before auth checks to prevent misclassification)
            if metadata.error_type == "max_turns":
                error_msg = metadata.error_message or f"Task stopped after {metadata.num_turns} turns"
                logger.warning(f"[Headless Task] Max turns reached: {error_msg}")
                raise HTTPException(
                    status_code=422,
                    detail=f"Task exceeded turn limit: {error_msg}. Consider increasing max_turns_task in guardrails or breaking into smaller subtasks."
                )

            # Issue #285: Check for auth failure detected in stderr
            # Return 503 (Service Unavailable) so backend can classify as AUTH error
            if auth_abort_event.is_set():
                auth_msg = auth_abort_reason[0] if auth_abort_reason else "Authentication failure detected"
                # SECURITY: Sanitize before logging/returning (auth_abort_reason is captured pre-sanitization)
                auth_msg = sanitize_text(auth_msg)
                logger.error(f"[Headless Task] Auth abort: {auth_msg}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Authentication failure: {auth_msg[:300]}. Check subscription token or API key configuration."
                )

            # Check for errors
            if return_code != 0:
                # Issue #516: Signal terminations (timeout SIGKILL, OOM, parent SIGTERM,
                # operator cancel) must be classified before the auth heuristics, which
                # would otherwise misread "zero tokens processed" as an expired token.
                # Same shape as the #361 max-turns special-case above. The #61 path
                # (backend-driven terminate_execution_on_agent → process_registry's
                # SIGINT→SIGKILL) makes this the common case for any timeout.
                signal_exit = _classify_signal_exit(return_code, metadata)
                if signal_exit is not None:
                    status_code, detail = signal_exit
                    logger.warning(f"[Headless Task] {detail}")
                    raise HTTPException(status_code=status_code, detail=detail)

                error_preview = verbose_transcript[:500] if verbose_transcript else ""
                if not error_preview:
                    # Try to provide a meaningful fallback based on common failure patterns
                    error_preview = _diagnose_exit_failure(return_code, metadata)

                # Issue #285: Check for auth failure patterns in stderr (fallback for cases
                # where real-time detection didn't trigger, e.g., pattern in later lines)
                if _is_auth_failure_message(error_preview) or _is_auth_failure_message(verbose_transcript):
                    logger.error(f"[Headless Task] Auth failure (fallback detection): {error_preview[:200]}")
                    raise HTTPException(
                        status_code=503,
                        detail=f"Authentication failure: {error_preview[:300]}. Check subscription token or API key configuration."
                    )

                # Issue #285: Heuristic fallback — if exit code > 0 AND zero tokens processed,
                # likely an auth failure even if we didn't see the exact pattern.
                # Issue #516: require return_code > 0 — signal exits are handled above and
                # would otherwise produce a false positive here (zero tokens after kill).
                if return_code > 0 and metadata.input_tokens == 0 and metadata.output_tokens == 0:
                    logger.warning(
                        f"[Headless Task] Zero tokens processed with exit code {return_code} — "
                        f"likely auth failure. Stderr: {error_preview[:200]}"
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=f"Execution failed with no output (possible authentication issue): {error_preview[:300]}"
                    )

                # Also check if stderr contains a rate limit message
                if _is_rate_limit_message(error_preview) or _is_rate_limit_message(verbose_transcript):
                    raise HTTPException(
                        status_code=429,
                        detail=f"Subscription usage limit: {error_preview[:300]}"
                    )
                logger.error(f"[Headless Task] Task {task_session_id} failed (exit {return_code}): {error_preview}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Task execution failed (exit code {return_code}): {error_preview[:300]}"
                )

            # Issue #520 + Session-tab pipe race recovery: clean exit
            # (return_code == 0) but the final `result` JSON line never reached
            # the reader thread — typically because a child subprocess inherited
            # stdout. metadata.cost_usd / duration_ms stay None.
            #
            # Recovery: response_parts is appended to as each assistant message's
            # text content arrives (independent of the result event). If we have
            # accumulated text, the assistant DID complete its response — only
            # the closing-stats line was lost. Synthesize a soft success so the
            # user gets their reply instead of a 502 + retry-recommended error.
            # cost/duration stay None in this branch (we don't know what they
            # were); the backend records the execution as success with null
            # cost rather than a misleading FAILED.
            #
            # Hard failure path stays as-is for the truly empty case (no
            # assistant text accumulated → nothing to recover).
            empty_result = _classify_empty_result(metadata, raw_message_count=len(raw_messages), raw_messages=raw_messages)
            if empty_result is not None:
                if response_parts:
                    logger.warning(
                        f"[Headless Task] Result event lost (stdout pipe race) but "
                        f"response_parts has {sum(len(p) for p in response_parts)} chars "
                        f"of assistant content across {len(response_parts)} blocks — "
                        f"recovering as soft success. raw_messages={len(raw_messages)}"
                    )
                else:
                    # Phase 5.1's soft-recovery requires accumulated text from
                    # stdout. When the pipe race fires mid-tool-call, no text
                    # was ever emitted to stdout — but Claude Code's JSONL on
                    # disk usually contains the completed turn. Read it as
                    # the authoritative ground truth before giving up.
                    recovered_text = _recover_response_from_jsonl(metadata.session_id)
                    if recovered_text:
                        logger.warning(
                            f"[Headless Task] Stdout race lost the response "
                            f"(raw_messages={len(raw_messages)}, no text in stream), but "
                            f"recovered {len(recovered_text)} chars from JSONL "
                            f"(session_id={metadata.session_id}) — "
                            f"surfacing as soft success."
                        )
                        response_parts.append(recovered_text)
                        metadata.recovered_from_jsonl = True
                    else:
                        status_code, detail = empty_result
                        logger.error(f"[Headless Task] {detail}")
                        raise HTTPException(status_code=status_code, detail=detail)

            # Build final response text
            response_text = "\n".join(response_parts) if response_parts else ""
            # SECURITY: Sanitize credentials from response text
            response_text = sanitize_text(response_text)

            if not response_text:
                raise HTTPException(
                    status_code=500,
                    detail="Task returned empty response"
                )

            # Count unique tools used
            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count
            metadata.execution_id = task_session_id  # Track execution_id in metadata

            # Use session_id from Claude if available, otherwise use our generated one
            final_session_id = metadata.session_id or task_session_id

            # Authoritative compact_events from the JSONL on disk.
            # Claude Code's stdout stream-json fires the compact_boundary
            # event without the compactMetadata envelope, so the parser
            # branch only learns "a compact happened" but loses the
            # pre/post/duration fields. The JSONL has the canonical shape;
            # read it after the turn completes and override whatever
            # stdout captured. Filtered to records emitted at or after
            # task_start_iso to scope to this turn's compacts only.
            jsonl_compacts = _extract_compact_events_from_jsonl(
                final_session_id, since_iso=task_start_iso
            )
            if jsonl_compacts:
                metadata.compact_events = jsonl_compacts
                for ev in jsonl_compacts:
                    logger.info(
                        f"event=session_auto_compact "
                        f"claude_session_id={final_session_id} "
                        f"trigger={ev.trigger} "
                        f"pre_tokens={ev.pre_tokens} "
                        f"post_tokens={ev.post_tokens} "
                        f"duration_ms={ev.duration_ms}"
                    )

            # Log warning if raw_messages is empty (transcript won't be available in UI)
            if len(raw_messages) == 0:
                logger.warning(f"[Headless Task] Task {final_session_id} completed but raw_messages is empty - execution transcript will be unavailable")
            else:
                logger.info(f"[Headless Task] Task {final_session_id} completed: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}, raw_messages={len(raw_messages)}")

            # Return raw_messages as the execution log (full JSON transcript from Claude Code)
            # Contains: init, assistant (thinking/tool_use), user (tool_result), result
            return response_text, raw_messages, metadata, final_session_id
        finally:
            # Always unregister process when done
            registry.unregister(task_session_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Headless Task] Execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Task execution error: {str(e)}")


# Global Claude Code runtime instance
_claude_runtime = None

def get_claude_runtime() -> ClaudeCodeRuntime:
    """Get or create the global Claude Code runtime instance."""
    global _claude_runtime
    if _claude_runtime is None:
        _claude_runtime = ClaudeCodeRuntime()
    return _claude_runtime
