"""
Claude Code execution service.
"""
import os
import json
import uuid
import asyncio
import subprocess
import logging
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..state import agent_state
from .activity_tracking import start_tool_execution, complete_tool_execution

logger = logging.getLogger(__name__)

# Thread pool for running blocking subprocess operations
# This allows FastAPI to handle other requests (like /api/activity polling) during execution
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
# The platform-level execution queue is the primary protection, but this is defense-in-depth
_execution_lock = asyncio.Lock()


def parse_stream_json_output(output: str) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata]:
    """
    Parse stream-json output from Claude Code.

    Stream-json format emits one JSON object per line:
    - {"type": "init", "session_id": "abc123", ...}
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

        msg_type = msg.get("type")

        if msg_type == "init":
            metadata.session_id = msg.get("session_id")

        elif msg_type == "result":
            # Final result message with stats
            metadata.cost_usd = msg.get("total_cost_usd")
            metadata.duration_ms = msg.get("duration_ms")
            metadata.num_turns = msg.get("num_turns")
            response_text = msg.get("result", response_text)

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

    msg_type = msg.get("type")

    if msg_type == "init":
        metadata.session_id = msg.get("session_id")

    elif msg_type == "result":
        # Final result message with stats
        metadata.cost_usd = msg.get("total_cost_usd")
        metadata.duration_ms = msg.get("duration_ms")
        metadata.num_turns = msg.get("num_turns")
        result_text = msg.get("result", "")
        if result_text:
            response_parts.clear()
            response_parts.append(result_text)

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

        logger.debug(f"Result message parsed: usage={usage}, modelUsage={model_usage}, input_tokens={metadata.input_tokens}")

    elif msg_type == "assistant" or msg_type == "user":
        # Handle both assistant and user message types
        # tool_use appears in assistant messages, tool_result may appear in either
        message = msg.get("message", {})
        message_content = message.get("content", [])

        # Log message structure for debugging activity tracking issues
        if message_content:
            logger.debug(f"Processing {msg_type} message with {len(message_content)} content blocks")

        for content_block in message_content:
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


async def execute_claude_code(prompt: str, stream: bool = False, model: Optional[str] = None) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata]:
    """
    Execute Claude Code in headless mode with the given prompt.

    Uses streaming subprocess to update session activity in REAL-TIME as tools execute.

    Uses: claude --print --output-format stream-json
    Uses --continue flag for subsequent messages to maintain conversation context
    Uses --model to select Claude model (sonnet, opus, haiku, or full model name)

    Returns: (response_text, execution_log, metadata)
    """

    if not agent_state.claude_code_available:
        raise HTTPException(
            status_code=503,
            detail="Claude Code is not available in this container"
        )

    try:
        # Get ANTHROPIC_API_KEY from environment
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="ANTHROPIC_API_KEY not configured in agent container"
            )

        # Update model if specified (persists for session)
        if model:
            agent_state.current_model = model
            logger.info(f"Model set to: {model}")

        # Build command - use --continue for subsequent messages
        # Use stream-json for detailed execution log (requires --verbose)
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

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

        # Initialize tracking structures
        execution_log: List[ExecutionLogEntry] = []
        metadata = ExecutionMetadata()
        tool_start_times: Dict[str, datetime] = {}
        response_parts: List[str] = []

        # Mark session as potentially running (will be set to running when first tool starts)
        logger.info(f"Starting Claude Code with streaming: {' '.join(cmd[:5])}...")

        # Use Popen for real-time streaming instead of blocking run()
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
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
                    # Process each line immediately - updates session_activity in real-time
                    process_stream_line(line, execution_log, metadata, tool_start_times, response_parts)
            except Exception as e:
                logger.error(f"Error reading Claude output: {e}")

            # Wait for process to complete and get stderr
            stderr = process.stderr.read()
            return_code = process.wait()
            return stderr, return_code

        # Run the blocking subprocess reading in a thread pool to allow FastAPI
        # to handle other requests (like /api/activity polling) during execution
        loop = asyncio.get_event_loop()
        stderr_output, return_code = await loop.run_in_executor(_executor, read_subprocess_output)

        # Check for errors
        if return_code != 0:
            logger.error(f"Claude Code failed (exit {return_code}): {stderr_output[:500]}")
            raise HTTPException(
                status_code=500,
                detail=f"Claude Code execution failed: {stderr_output[:200] if stderr_output else 'Unknown error'}"
            )

        # Build final response text
        response_text = "\n".join(response_parts) if response_parts else ""

        if not response_text:
            raise HTTPException(
                status_code=500,
                detail="Claude Code returned empty response"
            )

        # Count unique tools used
        tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
        metadata.tool_count = tool_use_count

        # Log metadata for debugging
        # NOTE: input_tokens already includes cached tokens - cache_creation and cache_read are billing subsets, NOT additional
        logger.info(f"Claude response: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}, context={metadata.input_tokens}/{metadata.context_window}")

        return response_text, execution_log, metadata

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Claude Code execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Execution error: {str(e)}")


def get_execution_lock():
    """Get the execution lock for chat endpoint"""
    return _execution_lock


async def execute_headless_task(
    prompt: str,
    model: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    timeout_seconds: int = 300
) -> tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
    """
    Execute Claude Code in headless mode for parallel task execution.

    Unlike execute_claude_code(), this function:
    - Does NOT acquire execution lock (parallel allowed)
    - Does NOT use --continue flag (stateless, no conversation context)
    - Each call is independent and can run concurrently

    Args:
        prompt: The task to execute
        model: Optional model override (sonnet, opus, haiku)
        allowed_tools: Optional list of allowed tools (restricts available tools)
        system_prompt: Optional additional system prompt
        timeout_seconds: Execution timeout (default 5 minutes)

    Returns: (response_text, execution_log, metadata, session_id)
    """
    import signal

    if not agent_state.claude_code_available:
        raise HTTPException(
            status_code=503,
            detail="Claude Code is not available in this container"
        )

    try:
        # Get ANTHROPIC_API_KEY from environment
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="ANTHROPIC_API_KEY not configured in agent container"
            )

        # Build command - NO --continue flag (stateless)
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

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

        # Add system prompt if specified
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
            logger.info(f"[Headless Task] Appending system prompt ({len(system_prompt)} chars)")

        # Initialize tracking structures
        execution_log: List[ExecutionLogEntry] = []
        metadata = ExecutionMetadata()
        tool_start_times: Dict[str, datetime] = {}
        response_parts: List[str] = []
        task_session_id = str(uuid.uuid4())

        logger.info(f"[Headless Task] Starting task {task_session_id}: {' '.join(cmd[:5])}...")

        # Use Popen for real-time streaming
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )

        # Write prompt to stdin and close it
        process.stdin.write(prompt)
        process.stdin.close()

        # Helper function that reads subprocess output (runs in thread pool)
        def read_subprocess_output_with_timeout():
            """Blocking function to read subprocess output line by line with timeout"""
            try:
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                    # Process each line immediately
                    process_stream_line(line, execution_log, metadata, tool_start_times, response_parts)
            except Exception as e:
                logger.error(f"[Headless Task] Error reading output: {e}")

            # Wait for process to complete and get stderr
            stderr = process.stderr.read()
            return_code = process.wait()
            return stderr, return_code

        # Run with timeout using asyncio
        loop = asyncio.get_event_loop()
        try:
            stderr_output, return_code = await asyncio.wait_for(
                loop.run_in_executor(None, read_subprocess_output_with_timeout),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            # Kill the process on timeout
            process.kill()
            process.wait()
            logger.error(f"[Headless Task] Task {task_session_id} timed out after {timeout_seconds}s")
            raise HTTPException(
                status_code=504,
                detail=f"Task execution timed out after {timeout_seconds} seconds"
            )

        # Check for errors
        if return_code != 0:
            logger.error(f"[Headless Task] Task {task_session_id} failed (exit {return_code}): {stderr_output[:500]}")
            raise HTTPException(
                status_code=500,
                detail=f"Task execution failed: {stderr_output[:200] if stderr_output else 'Unknown error'}"
            )

        # Build final response text
        response_text = "\n".join(response_parts) if response_parts else ""

        if not response_text:
            raise HTTPException(
                status_code=500,
                detail="Task returned empty response"
            )

        # Count unique tools used
        tool_use_count = len([e for e in execution_log if e.type == "tool_use"])
        metadata.tool_count = tool_use_count

        # Use session_id from Claude if available, otherwise use our generated one
        final_session_id = metadata.session_id or task_session_id

        logger.info(f"[Headless Task] Task {final_session_id} completed: cost=${metadata.cost_usd}, duration={metadata.duration_ms}ms, tools={metadata.tool_count}")

        return response_text, execution_log, metadata, final_session_id

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Headless Task] Execution error: {e}")
        raise HTTPException(status_code=500, detail=f"Task execution error: {str(e)}")
