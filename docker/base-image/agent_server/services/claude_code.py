"""
Claude Code execution service — chat path + runtime adapter.

Implements the ``AgentRuntime`` interface for multi-provider support; owns
the interactive chat path (``execute_claude_code``) plus the execution lock
serializing concurrent /api/chat requests. The headless task path lives in
``headless_executor.py``; stream-json parsing in ``stream_parser.py``;
error classification + result recovery in ``error_classifier.py``;
JSONL fallback recovery in ``jsonl_recovery.py``; subprocess teardown in
``subprocess_lifecycle.py``; guardrails config in ``_runtime_config.py``.
Refactored per #122 (split of the original 2137-LOC monolith).
"""
import asyncio
import json
import logging
import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import resolve_context_window
from ..state import agent_state
from ..utils.credential_sanitizer import sanitize_dict, sanitize_subprocess_line, sanitize_text
from ._runtime_config import (
    _DEFAULT_EXECUTION_TIMEOUT_SEC,
    _DEFAULT_MAX_TURNS_CHAT,
    _load_guardrails,
)
from .error_classifier import (
    _classify_signal_exit,
    _diagnose_exit_failure,
    _format_rate_limit_error,
    _is_rate_limit_message,
)
from .headless_executor import _attempt_empty_result_recovery, execute_headless_task
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .stream_parser import process_stream_line
from .subprocess_lifecycle import (
    _capture_pgid,
    _drain_bounded,
    _safe_close_pipes,
    _terminate_process_group,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME

__all__ = [
    "ClaudeCodeRuntime",
    "execute_claude_code",
    "execute_headless_task",
    "get_claude_runtime",
    "get_execution_lock",
]

logger = logging.getLogger(__name__)

# Thread pool for running blocking subprocess operations
# This allows FastAPI to handle other requests (like /api/activity polling) during execution
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
# The platform-level execution queue is the primary protection, but this is defense-in-depth
_execution_lock = asyncio.Lock()


class ClaudeCodeRuntime(AgentRuntime):
    """Claude Code implementation of AgentRuntime interface."""

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        # Claude is the reference runtime: full continuity (--continue), the
        # Session tab's cached-UUID --resume machinery, MCP, and native cost
        # reporting (Claude Code emits total_cost_usd directly). (#1187)
        return RuntimeCapabilities(
            chat_continuity=True,
            session_tab_resume=True,
            mcp_support=True,
            cost_reporting="native",
        )

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
        return "claude-sonnet-4-6"

    def get_context_window(self, model: Optional[str] = None) -> int:
        """Fallback context window for Claude models (#1521).

        Resolves via the shared catalog: ``[1m]`` → 1M, else the 200K safe floor.
        This is a FALLBACK — the authoritative window is the runtime-reported
        ``modelUsage.contextWindow`` captured in ``stream_parser``.
        """
        return resolve_context_window(model)

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

        # Safety-net fallback: backend always resolves model before calling the agent
        # (#831), so this branch should only fire for direct agent-server calls.
        if not model and not agent_state.current_model:
            model = "claude-sonnet-4-6"
            logger.debug("[Chat] No model specified, defaulting to 'claude-sonnet-4-6'")

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
        # Issue #640: track stdout JSON parse failures so operators can see when
        # the wire was corrupted (interleaved/truncated lines that json.loads
        # silently dropped before #639's recovery path could capture them).
        # Mutable single-element box so the reader thread and the main thread
        # share the same state without locking.
        parse_fail_state: Dict[str, object] = {"count": 0, "first_sample": None}
        metadata = ExecutionMetadata()
        # #1521: seed the fallback context window from the model catalog BEFORE
        # the stream runs. The stream parser overwrites this from the runtime's
        # modelUsage.contextWindow (the primary) when present; when it isn't
        # (salvage / partial-metadata paths), this catalog value is the real
        # denominator — NOT the flat 200K Pydantic default (get_context_window
        # is never called for Claude, so this seed is the only fallback hook).
        metadata.context_window = resolve_context_window(model or agent_state.current_model)
        tool_start_times: Dict[str, datetime] = {}
        response_parts: List[str] = []
        # Use provided execution_id if available (enables termination tracking from backend)
        execution_id = execution_id or str(uuid.uuid4())
        # #678: capture turn-start timestamp so JSONL recovery can scope
        # records to this turn (the resumed JSONL accumulates across all
        # turns of the session). Mirror format from headless_executor.py:217.
        task_start_iso = datetime.utcnow().isoformat() + "Z"

        # Mark session as potentially running (will be set to running when first tool starts)
        logger.info(f"Starting Claude Code with streaming: {' '.join(cmd[:5])}...")

        # Use Popen for real-time streaming instead of blocking run().
        # Issue #407: start_new_session=True puts claude (and hook children
        # it spawns) into their own process group so we can reap the whole
        # tree on exit — hook grandchildren can otherwise outlive claude
        # and wedge readline() forever via inherited pipe FDs.
        # Issue #817: TRINITY_EXECUTION_ID env var is inherited by every
        # descendant across fork/exec/setsid/double-fork. Cleanup uses it
        # to identify and kill orphans that escape both the pgid sweep
        # and the FD-based pipe-writer sweep.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            start_new_session=True,
            env={**os.environ, EXECUTION_TAG_NAME: execution_id},
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
                            # Issue #640: track parse failures so the
                            # empty-result path can report them. Capture
                            # only the first sample (sanitized + length-
                            # capped) — flooding logs on a long corrupted
                            # task would harm signal more than help.
                            parse_fail_state["count"] = int(parse_fail_state["count"]) + 1  # type: ignore[arg-type]
                            if parse_fail_state["first_sample"] is None:
                                sample = sanitize_subprocess_line(line).rstrip("\n")
                                if len(sample) > 300:
                                    sample = sample[:299] + "…"
                                parse_fail_state["first_sample"] = sample
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
                _terminate_process_group(process, graceful_timeout=5, pgid=process_pgid, execution_tag=execution_id)
                _drain_bounded(process, stdout_thread, stderr_thread,
                               grace=3, pgid=process_pgid,
                               execution_tag=execution_id)
                raise

            _drain_bounded(process, stdout_thread, stderr_thread,
                           grace=5, pgid=process_pgid,
                           execution_tag=execution_id)

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
                    lambda: _terminate_process_group(process, graceful_timeout=2, pgid=process_pgid, execution_tag=execution_id),
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

            # Stream parser sets ``metadata.error_type = "authentication_failed"``
            # from the assistant message's ``error`` field. Surface it before the
            # generic non-zero-exit-code fallback below so the 503 detail carries
            # the actual claude error message rather than `_diagnose_exit_failure`'s
            # generic wording. Mirrors the symmetric check in headless_executor.
            if metadata.error_type == "authentication_failed":
                auth_msg = sanitize_text(metadata.error_message or "Authentication failed")
                logger.error(f"[Chat] Auth failure (stream-parser signal): {auth_msg[:200]}")
                raise HTTPException(
                    status_code=503,
                    detail=f"Authentication failure: {auth_msg[:300]}. Check subscription token or API key configuration."
                )

            # Check for errors
            if return_code != 0:
                # Issue #906: Signal terminations (SIGKILL/SIGTERM/SIGINT — OOM,
                # timeout, operator cancel) must be classified before the
                # auth-fallback heuristic in `_diagnose_exit_failure`, which
                # would otherwise misread "zero tokens processed" as an
                # expired subscription token. Mirrors the symmetric check
                # in headless_executor (Issue #516).
                signal_exit = _classify_signal_exit(return_code, metadata)
                if signal_exit is not None:
                    status_code, detail = signal_exit
                    logger.warning(f"[Chat] {detail}")
                    raise HTTPException(status_code=status_code, detail=detail)

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

            # #678: empty-result recovery before falling through to the
            # generic 500. Tries JSONL metadata back-fill then text
            # recovery; raises a structured 502 dict body when truly empty.
            # Parity with the async path's _finalize_headless_result.
            parse_fail_count_for_recovery = int(parse_fail_state["count"])  # type: ignore[arg-type]
            parse_fail_sample = parse_fail_state.get("first_sample")  # type: ignore[union-attr]
            hard_failure = _attempt_empty_result_recovery(
                metadata=metadata,
                raw_messages=raw_messages,
                response_parts=response_parts,
                parse_failure_count=parse_fail_count_for_recovery,
                parse_failure_sample=parse_fail_sample if isinstance(parse_fail_sample, str) else None,
                task_start_iso=task_start_iso,
            )
            if hard_failure is not None:
                status_code, body = hard_failure
                logger.error(f"[Chat] {body['message']}")
                raise HTTPException(status_code=status_code, detail=body)

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
            parse_fail_count = int(parse_fail_state["count"])  # type: ignore[arg-type]
            logger.info(f"Claude response: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}, context={metadata.input_tokens}/{metadata.context_window}, raw_messages={len(raw_messages)}, parse_failures={parse_fail_count}, execution_id={execution_id}")
            # Issue #640: surface stream-json parse failures even when the
            # response built successfully. They mean some lines were dropped —
            # the response may be incomplete in subtle ways (e.g. a tool_result
            # missing) and operators should know.
            if parse_fail_count:
                sample = parse_fail_state["first_sample"]
                logger.warning(
                    f"[Chat] {parse_fail_count} stdout line(s) failed JSON parse "
                    f"(first sample: {sample!r}). Likely cause: stdio MCP child "
                    f"interleaved with claude on the agent-server pipe. "
                    f"execution_id={execution_id}"
                )

            return response_text, execution_log, metadata, raw_messages
        finally:
            # Always unregister process when done
            registry.unregister(execution_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Claude Code execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")


def get_execution_lock():
    """Get the execution lock for chat endpoint"""
    return _execution_lock


# Global Claude Code runtime instance
_claude_runtime = None

def get_claude_runtime() -> ClaudeCodeRuntime:
    """Get or create the global Claude Code runtime instance."""
    global _claude_runtime
    if _claude_runtime is None:
        _claude_runtime = ClaudeCodeRuntime()
    return _claude_runtime
