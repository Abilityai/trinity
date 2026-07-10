"""
Gemini CLI execution service.

Implements AgentRuntime interface for Google's Gemini models.
"""
import os
import json
import uuid
import asyncio
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import resolve_context_window
from ..state import agent_state
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ..utils.orphan_sweep import kill_cgroup_orphans
from .activity_tracking import start_tool_execution, complete_tool_execution
from .runtime_adapter import AgentRuntime, RuntimeCapabilities

logger = logging.getLogger(__name__)


def _sweep_allowlist_pids() -> list:
    """#1501: registry-vouched pids for the post-turn cgroup sweeps below.

    Gemini's sweeps previously ran with NO allowlist (bare
    ``kill_cgroup_orphans()``), so a finishing Gemini turn could SIGKILL a
    concurrent execution's subprocesses (the #912 bug class) or an in-flight
    transient subprocess (a brain-orb hook). Fail-open: on any registry
    error, sweep with an empty allowlist — pre-#1501 behavior. Lazy import
    mirrors orphan_sweeper / subprocess_pgroup.
    """
    try:
        from .process_registry import get_process_registry  # lazy
        return get_process_registry().active_execution_pids()
    except Exception:  # noqa: BLE001
        logger.exception(
            "[Gemini] failed to enumerate sweep allowlist — sweeping bare"
        )
        return []


# Single shared executor for subprocess reading. Mirrors the pattern in
# claude_code.py:63 — one long-lived worker thread instead of a fresh
# ThreadPoolExecutor per call. Per-call executors rely on CPython's weakref
# callback to clean up the worker thread on GC, which is not deterministic
# under load. #333 hardening.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemini-subproc")

# Gemini pricing per 1K tokens (as of Dec 2024, from ai.google.dev/pricing)
# Free tier has limits, but we calculate what it *would* cost
# Note: Prices are for standard context (≤200K tokens)
GEMINI_PRICING = {
    "gemini-3-pro": {
        "input": 0.002,     # $2.00 per 1M = $0.002 per 1K
        "output": 0.012,    # $12.00 per 1M = $0.012 per 1K
    },
    "gemini-3-flash": {
        "input": 0.0005,    # $0.50 per 1M = $0.0005 per 1K
        "output": 0.003,    # $3.00 per 1M = $0.003 per 1K
    },
    "gemini-2.5-pro": {
        "input": 0.00125,   # $1.25 per 1M = $0.00125 per 1K
        "output": 0.01,     # $10.00 per 1M = $0.01 per 1K
    },
    "gemini-2.5-flash": {
        "input": 0.0003,    # $0.30 per 1M = $0.0003 per 1K
        "output": 0.0025,   # $2.50 per 1M = $0.0025 per 1K
    },
    # Historical (retired by Google; kept only so cost calc of older
    # executions still resolves — not offered in the /api/model lists, #1137)
    "gemini-2.0-flash": {
        "input": 0.0001,    # $0.10 per 1M = $0.0001 per 1K
        "output": 0.0004,   # $0.40 per 1M = $0.0004 per 1K
    },
    "gemini-2.0-flash-lite": {
        "input": 0.000075,  # $0.075 per 1M = $0.000075 per 1K
        "output": 0.0003,   # $0.30 per 1M = $0.0003 per 1K
    },
    # Default to 2.5 Pro pricing for unknown models
    "default": {
        "input": 0.00125,
        "output": 0.01,
    }
}


def calculate_gemini_cost(input_tokens: int, output_tokens: int, model: Optional[str] = None) -> float:
    """
    Calculate estimated cost for Gemini API usage.

    Note: Free tier doesn't actually charge, but we calculate
    what it would cost for tracking/comparison purposes.
    """
    # Get pricing for model or use default
    model_key = model.lower() if model else "default"
    pricing = GEMINI_PRICING.get(model_key, GEMINI_PRICING["default"])

    input_cost = (input_tokens / 1000) * pricing["input"]
    output_cost = (output_tokens / 1000) * pricing["output"]

    return round(input_cost + output_cost, 6)


class GeminiRuntime(AgentRuntime):
    """Gemini CLI implementation of AgentRuntime interface."""

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        # Gemini supports chat continuity (--resume) and MCP, but NOT the
        # Session-tab cached-UUID resume (execute_headless ignores
        # resume_session_id), and cost is derived from tokens. (#1187)
        return RuntimeCapabilities(
            chat_continuity=True,
            session_tab_resume=False,
            mcp_support=True,
            cost_reporting="estimated",
        )

    def is_available(self) -> bool:
        """Check if Gemini CLI is installed."""
        try:
            result = subprocess.run(
                ["gemini", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_default_model(self) -> str:
        """Get default Gemini model."""
        return "gemini-3-flash"

    def get_context_window(self, model: Optional[str] = None) -> int:
        """Fallback context window for Gemini models (#1521).

        Resolves via the shared catalog; falls back to the default Gemini model
        id so a None model still resolves to Gemini's native 1M window. The
        runtime-reported ``modelUsage.contextWindow`` remains the primary.
        """
        return resolve_context_window(model or self.get_default_model())

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """
        Configure MCP servers via Gemini CLI commands.

        Gemini uses "gemini mcp add <name> <command> [args...]" instead of .mcp.json.
        Uses the shared implementation from trinity_mcp.py for consistency.
        """
        from .trinity_mcp import _configure_gemini_mcp_servers
        return _configure_gemini_mcp_servers(mcp_servers)

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
        Execute Gemini CLI with the given prompt.

        Uses same output format as Claude Code for compatibility.
        Note: system_prompt is accepted for interface compatibility but not yet
        supported by Gemini CLI.

        Returns: (response_text, execution_log, metadata, raw_messages)
            - execution_log: Simplified ExecutionLogEntry objects for activity tracking
            - raw_messages: Full JSON transcript for execution log viewer
        """
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Gemini CLI is not available in this container"
            )

        try:
            # Get GEMINI_API_KEY from environment
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="GEMINI_API_KEY not configured in agent container"
                )

            # Build command
            cmd = ["gemini", "--output-format", "stream-json", "--yolo"]

            # Add model selection if specified
            if model:
                cmd.extend(["--model", model])
                logger.info(f"Using Gemini model: {model}")

            # Session continuity
            if continue_session and agent_state.session_started:
                cmd.append("--resume")
                logger.info("Resuming existing Gemini session")
            else:
                agent_state.session_started = True
                logger.info("Starting new Gemini session")

            # Initialize tracking structures
            execution_log: List[ExecutionLogEntry] = []
            raw_messages: List[Dict] = []  # Capture ALL raw JSON messages for execution log viewer
            metadata = ExecutionMetadata()
            metadata.context_window = self.get_context_window(model)
            tool_start_times: Dict[str, datetime] = {}
            tool_names: Dict[str, str] = {}  # Map tool_id -> tool_name
            response_parts: List[str] = []

            logger.info(f"Starting Gemini CLI: {' '.join(cmd[:5])}...")

            # Issue #817: ensure execution_id is set so we can tag the
            # Gemini subprocess and any descendants. Tag is inherited at
            # every fork/exec/setsid, lets the post-wait sweep identify
            # and kill orphans the same way the Claude path does.
            execution_id = execution_id or str(uuid.uuid4())

            # Use Popen for real-time streaming
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                env={**os.environ, EXECUTION_TAG_NAME: execution_id},
            )

            # Write prompt to stdin and close it
            process.stdin.write(prompt)
            process.stdin.close()

            # Helper function that reads subprocess output (runs in thread pool)
            def read_subprocess_output():
                """Blocking function to read subprocess output line by line"""
                try:
                    for line in iter(process.stdout.readline, ''):
                        if not line:
                            break
                        # Capture raw JSON for full execution log (same as Claude Code)
                        try:
                            raw_msg = json.loads(line.strip())
                            raw_messages.append(raw_msg)
                        except json.JSONDecodeError:
                            pass
                        # Process each line immediately
                        # Gemini CLI uses same stream-json format as Claude Code
                        self._process_stream_line(line, execution_log, metadata, tool_start_times, tool_names, response_parts, model)
                except Exception as e:
                    logger.error(f"Error reading Gemini output: {e}")

                # Wait for process to complete and get stderr
                stderr = process.stderr.read()
                return_code = process.wait()

                # Issue #817 follow-up: cgroup-walk sweep for Gemini-
                # spawned descendants that escaped via setsid, FD
                # detachment, or env stripping. Best-effort — never
                # fail the response on this path.
                try:
                    killed = kill_cgroup_orphans(extra_pids=_sweep_allowlist_pids())
                    if killed:
                        logger.info(
                            f"[Gemini] Cgroup sweep killed {killed} orphan(s) "
                            f"after execution {execution_id} exit"
                        )
                except Exception:
                    logger.exception(
                        f"[Gemini] cgroup sweep raised after {execution_id} — "
                        "continuing"
                    )

                return stderr, return_code

            # Run the blocking subprocess reading in a thread pool
            loop = asyncio.get_event_loop()
            stderr_output, return_code = await loop.run_in_executor(_executor, read_subprocess_output)

            # Check for errors
            if return_code != 0:
                error_detail = stderr_output[:500] if stderr_output else ""
                if not error_detail:
                    has_key = bool(os.environ.get("GOOGLE_API_KEY"))
                    if not has_key:
                        error_detail = "No GOOGLE_API_KEY configured. Add it via credentials."
                    else:
                        error_detail = f"Process exited with code {return_code}. Check agent container logs."
                logger.error(f"Gemini CLI failed (exit {return_code}): {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Gemini execution failed (exit code {return_code}): {error_detail[:300]}"
                )

            # Build final response text
            logger.info(f"[Stream] Building response from {len(response_parts)} parts")
            response_text = "\n".join(response_parts) if response_parts else ""

            # Count unique tools used
            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count

            # Handle empty response gracefully
            # Sometimes Gemini returns success with no assistant message (tool result IS the response)
            if not response_text:
                logger.warning(f"[Stream] Empty response - parts were: {response_parts[:3] if response_parts else 'EMPTY'}")
                if tool_use_count > 0:
                    # Tools were used but no final message - use tool result as response
                    tool_results = [e for e in execution_log if e.type == "tool_result"]
                    if tool_results and tool_results[-1].output:
                        # Use the last tool result output as the response
                        response_text = tool_results[-1].output
                        logger.info(f"Using tool result as response: {response_text[:100]}...")
                    else:
                        response_text = "(Task completed)"
                        logger.warning("Gemini returned empty response after tool execution")
                else:
                    # No response and no tools - unusual but not necessarily an error
                    response_text = "(No response from model)"
                    logger.warning("Gemini returned empty response with no tool calls")

            # Update session stats
            if metadata.cost_usd:
                agent_state.session_total_cost += metadata.cost_usd
            agent_state.session_total_output_tokens += metadata.output_tokens
            if metadata.input_tokens > agent_state.session_context_tokens:
                agent_state.session_context_tokens = metadata.input_tokens
            agent_state.session_context_window = metadata.context_window

            logger.info(f"Gemini response: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}, context={metadata.input_tokens}/{metadata.context_window}, raw_messages={len(raw_messages)}")

            return response_text, execution_log, metadata, raw_messages

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Gemini execution error: {e}")
            raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")

    def _process_stream_line(
        self,
        line: str,
        execution_log: List[ExecutionLogEntry],
        metadata: ExecutionMetadata,
        tool_start_times: Dict[str, datetime],
        tool_names: Dict[str, str],
        response_parts: List[str],
        current_model: Optional[str] = None
    ) -> None:
        """
        Process a single line of stream-json output from Gemini CLI.

        Gemini CLI uses the same stream-json format as Claude Code, so we can
        reuse the parsing logic with minor adjustments.
        """
        if not line.strip():
            return

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse line as JSON: {line[:100]}")
            return

        msg_type = msg.get("type")

        if msg_type == "init":
            metadata.session_id = msg.get("session_id")

        elif msg_type == "message":
            # Gemini CLI sends response text as {"type":"message","role":"assistant","content":"..."}
            role = msg.get("role")
            content = msg.get("content", "")
            logger.info(f"[Stream] message: role={role}, content_len={len(content) if content else 0}")
            if role == "assistant" and content:
                # Append to response parts (Gemini sends streaming deltas)
                response_parts.append(content)
                logger.info(f"[Stream] Appended assistant content, parts_count={len(response_parts)}")

        elif msg_type == "result":
            # Final result message with stats
            metadata.duration_ms = msg.get("duration_ms")
            metadata.num_turns = msg.get("num_turns")
            result_text = msg.get("result", "")
            if result_text:
                response_parts.clear()
                response_parts.append(result_text)

            # Extract token usage from stats field (Gemini CLI format)
            stats = msg.get("stats", {})
            if stats:
                metadata.input_tokens = stats.get("input_tokens", 0)
                metadata.output_tokens = stats.get("output_tokens", 0)
                metadata.duration_ms = stats.get("duration_ms", metadata.duration_ms)

            # Fallback to usage field if stats not present
            usage = msg.get("usage", {})
            if not stats and usage:
                metadata.input_tokens = usage.get("input_tokens", 0)
                metadata.output_tokens = usage.get("output_tokens", 0)

            # Gemini might use different field names - adapt if needed
            model_usage = msg.get("modelUsage", {})
            detected_model = None
            for model_name, model_data in model_usage.items():
                detected_model = model_name
                if "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                if "inputTokens" in model_data:
                    metadata.input_tokens = model_data["inputTokens"]
                if "outputTokens" in model_data:
                    metadata.output_tokens = model_data["outputTokens"]
                break

            # Calculate cost from tokens (Gemini CLI doesn't report cost directly)
            # Use reported cost if available, otherwise calculate
            reported_cost = msg.get("total_cost_usd", 0)
            if reported_cost and reported_cost > 0:
                metadata.cost_usd = reported_cost
            else:
                # Calculate estimated cost based on token usage
                # Priority: detected_model > current_model (UI selected) > env default
                model_for_pricing = detected_model or current_model or os.getenv("AGENT_RUNTIME_MODEL", "gemini-3-flash")
                metadata.cost_usd = calculate_gemini_cost(
                    metadata.input_tokens,
                    metadata.output_tokens,
                    model_for_pricing
                )

        elif msg_type == "tool_use":
            # Gemini CLI outputs tool_use at top level: {"type":"tool_use","tool_name":"...","tool_id":"...","parameters":{}}
            tool_id = msg.get("tool_id", str(uuid.uuid4()))
            tool_name = msg.get("tool_name") or msg.get("name", "Unknown")
            tool_input = msg.get("parameters") or msg.get("input", {})
            timestamp = datetime.now()

            tool_start_times[tool_id] = timestamp
            tool_names[tool_id] = tool_name  # Store for later lookup

            execution_log.append(ExecutionLogEntry(
                id=tool_id,
                type="tool_use",
                tool=tool_name,
                input=tool_input,
                timestamp=timestamp.isoformat()
            ))

            # Update session activity
            start_tool_execution(tool_id, tool_name, tool_input)
            logger.debug(f"Tool started: {tool_name} ({tool_id})")

        elif msg_type == "tool_result":
            # Gemini CLI outputs tool_result at top level: {"type":"tool_result","tool_id":"...","status":"success","output":"..."}
            tool_id = msg.get("tool_id", "")
            is_error = msg.get("status") == "error"
            tool_output = msg.get("output", "")
            timestamp = datetime.now()

            # Look up tool name from previous tool_use
            tool_name = tool_names.get(tool_id, "Unknown")

            # Calculate duration
            duration_ms = None
            if tool_id in tool_start_times:
                delta = timestamp - tool_start_times[tool_id]
                duration_ms = int(delta.total_seconds() * 1000)

            execution_log.append(ExecutionLogEntry(
                id=tool_id,  # Use same ID for correlation
                type="tool_result",
                tool=tool_name,
                output=tool_output,
                success=not is_error,
                duration_ms=duration_ms,
                timestamp=timestamp.isoformat()
            ))

            # Update session activity
            complete_tool_execution(tool_id, tool_output, is_error)
            logger.debug(f"Tool completed: {tool_name} ({tool_id}) success={not is_error}")

        elif msg_type in ("assistant", "user"):
            # Handle tool_use and tool_result blocks (Claude Code format - nested in message)
            message = msg.get("message", {})
            message_content = message.get("content", [])

            for content_block in message_content:
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
                    logger.debug(f"Tool started: {tool_name} ({tool_id})")

                elif block_type == "tool_result":
                    # Tool result returned
                    tool_id = content_block.get("tool_use_id", "")
                    is_error = content_block.get("is_error", False)
                    timestamp = datetime.now()

                    # Extract output content
                    tool_output = ""
                    result_content = content_block.get("content", [])
                    if isinstance(result_content, list):
                        for item in result_content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                tool_output = item.get("text", "")
                                break
                    elif isinstance(result_content, str):
                        tool_output = result_content

                    # Calculate duration
                    duration_ms = None
                    if tool_id in tool_start_times:
                        delta = timestamp - tool_start_times[tool_id]
                        duration_ms = int(delta.total_seconds() * 1000)

                    # Find tool name from tool_use entry
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
                    logger.debug(f"Tool completed: {tool_name} ({tool_id}) - success={not is_error}")

                elif block_type == "text":
                    # Gemini's text response
                    text = content_block.get("text", "")
                    if text:
                        response_parts.append(text)


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
        Execute Gemini CLI in headless mode for parallel tasks.

        Unlike execute(), this function:
        - Does NOT use --resume (stateless)
        - Each call is independent
        - Supports tool restrictions and custom system prompts
        - Supports max_turns for runaway prevention

        Note: resume_session_id and persist_session are not supported by
        Gemini CLI (accepted for ABC parity, ignored at runtime).
        """
        # Note: resume_session_id and persist_session are ignored — Gemini CLI doesn't support resume
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="Gemini CLI is not available in this container"
            )

        try:
            # Get GEMINI_API_KEY from environment
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise HTTPException(
                    status_code=500,
                    detail="GEMINI_API_KEY not configured in agent container"
                )

            # Use provided execution_id or generate unique session ID for this task
            session_id = execution_id or str(uuid.uuid4())[:8]

            # Build command - stateless (no --resume)
            cmd = ["gemini", "--output-format", "stream-json", "--yolo"]

            # Add model selection if specified
            if model:
                cmd.extend(["--model", model])

            # Add tool restrictions if specified
            if allowed_tools:
                for tool in allowed_tools:
                    cmd.extend(["--allowed-tools", tool])

            # Add system prompt if specified
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])

            # Add max turns limit for runaway prevention
            if max_turns is not None:
                cmd.extend(["--max-turns", str(max_turns)])
                logger.info(f"[Headless Task {session_id}] Limiting to {max_turns} agentic turns")

            # Initialize tracking structures
            execution_log: List[ExecutionLogEntry] = []
            metadata = ExecutionMetadata()
            metadata.context_window = self.get_context_window(model)
            tool_start_times: Dict[str, datetime] = {}
            tool_names: Dict[str, str] = {}  # Map tool_id -> tool_name
            response_parts: List[str] = []

            logger.info(f"[Headless Task {session_id}] Starting Gemini CLI...")

            # Use Popen for real-time streaming with timeout.
            # Issue #817: TRINITY_EXECUTION_ID env var is inherited by every
            # descendant across fork/exec/setsid/double-fork. The post-wait
            # sweep below uses it to identify orphans the same way the Claude
            # path does.
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, EXECUTION_TAG_NAME: session_id},
            )

            # Write prompt to stdin and close it
            process.stdin.write(prompt)
            process.stdin.close()

            # Helper function that reads subprocess output
            def read_subprocess_output():
                """Blocking function to read subprocess output line by line with timeout"""
                import time
                start_time = time.time()
                try:
                    for line in iter(process.stdout.readline, ''):
                        if not line:
                            break
                        if time.time() - start_time > timeout_seconds:
                            process.kill()
                            raise TimeoutError(f"Task exceeded {timeout_seconds}s timeout")
                        self._process_stream_line(line, execution_log, metadata, tool_start_times, tool_names, response_parts, model)
                except Exception as e:
                    logger.error(f"[Headless Task {session_id}] Error: {e}")
                    # Issue #817 follow-up: even on error, run the cgroup
                    # sweep so leaked descendants don't survive the failed
                    # task.
                    try:
                        kill_cgroup_orphans(extra_pids=_sweep_allowlist_pids())
                    except Exception:
                        logger.exception(
                            f"[Headless Task {session_id}] cgroup sweep "
                            "raised on error path — continuing"
                        )
                    raise

                stderr = process.stderr.read()
                return_code = process.wait()

                # Issue #817 follow-up: cgroup sweep catches descendants
                # that escaped via setsid, FD detachment, or env
                # stripping. Best-effort.
                try:
                    killed = kill_cgroup_orphans(extra_pids=_sweep_allowlist_pids())
                    if killed:
                        logger.info(
                            f"[Headless Task {session_id}] Cgroup sweep "
                            f"killed {killed} orphan(s) after Gemini exit"
                        )
                except Exception:
                    logger.exception(
                        f"[Headless Task {session_id}] cgroup sweep raised "
                        "— continuing"
                    )

                return stderr, return_code

            # Run the blocking subprocess reading in a thread pool
            loop = asyncio.get_event_loop()
            stderr_output, return_code = await loop.run_in_executor(_executor, read_subprocess_output)

            # Check for errors
            if return_code != 0:
                error_detail = stderr_output[:500] if stderr_output else ""
                if not error_detail:
                    has_key = bool(os.environ.get("GOOGLE_API_KEY"))
                    if not has_key:
                        error_detail = "No GOOGLE_API_KEY configured. Add it via credentials."
                    else:
                        error_detail = f"Process exited with code {return_code}. Check agent container logs."
                logger.error(f"[Headless Task {session_id}] Gemini CLI failed (exit {return_code}): {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Gemini execution failed (exit code {return_code}): {error_detail[:300]}"
                )

            # Build final response text
            response_text = "\n".join(response_parts) if response_parts else ""

            # Count unique tools used
            tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
            metadata.tool_count = tool_use_count

            # Handle empty response gracefully
            if not response_text:
                if tool_use_count > 0:
                    # Use tool result as response if available
                    tool_results = [e for e in execution_log if e.type == "tool_result"]
                    if tool_results and tool_results[-1].output:
                        response_text = tool_results[-1].output
                        logger.info(f"[Headless Task {session_id}] Using tool result as response")
                    else:
                        response_text = "(Task completed)"
                        logger.warning(f"[Headless Task {session_id}] Gemini returned empty response after tool execution")
                else:
                    response_text = "(No response from model)"
                    logger.warning(f"[Headless Task {session_id}] Gemini returned empty response with no tool calls")

            # Use session_id from metadata if available, otherwise use our generated one
            final_session_id = metadata.session_id or session_id

            logger.info(f"[Headless Task {final_session_id}] Completed: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}")

            return response_text, execution_log, metadata, final_session_id

        except HTTPException:
            raise
        except TimeoutError as e:
            logger.error(f"[Headless Task] Timeout: {e}")
            raise HTTPException(status_code=504, detail=str(e))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            # Subprocess stdin pipe closed before write completed — Gemini CLI
            # child process exited early. Parallel surface to headless_executor.
            # 502 not 503 to avoid SUB-003 auto-switch collision (#474).
            logger.info(
                f"[Headless Task] Subprocess pipe closed before write completed: {pipe_err}. "
                f"This typically means the child Gemini process exited early."
            )
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before task could complete",
            )
        except Exception as e:
            logger.error(f"[Headless Task] Execution error: {e}")
            raise HTTPException(status_code=500, detail=f"Task execution error: {str(e)}")


# Global Gemini runtime instance
_gemini_runtime = None

def get_gemini_runtime() -> GeminiRuntime:
    """Get or create the global Gemini runtime instance."""
    global _gemini_runtime
    if _gemini_runtime is None:
        _gemini_runtime = GeminiRuntime()
    return _gemini_runtime

