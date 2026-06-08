"""Stream-json parser for Claude Code output.

Extracted from `claude_code.py` per #122 (issue split). Two parsing surfaces:

- ``parse_stream_json_output`` — batch parser; consumes a fully-collected
  output buffer after the subprocess exits. Now used only by tests.
- ``process_stream_line`` — streaming parser; called once per stdout line
  while the subprocess is still running so activity events fire in real time.

Token-accounting invariant
==========================

Token counts in ``result.usage`` and ``modelUsage.inputTokens`` are
CUMULATIVE across every internal API call this turn made — for a tool-using
turn with 18 iterations, ``cache_read`` in ``result.usage`` is 18× the
per-call ``cache_read`` (1M+ tokens of "billing total"), which has nothing
to do with the prompt size any single call sent to the model. Overwriting
``metadata.*`` with those values would make context-window-pressure metrics
grow far beyond the 200K limit even when no individual call was close to
the wall.

The per-API-call ``usage`` block on each ``assistant`` message tracks the
correct values; the LATEST assistant message's values represent the FINAL
API call's prompt — which is what determines whether the next turn will
fit. Both parsers extract ``cost_usd`` / ``duration_ms`` / ``num_turns`` /
``context_window`` from the result event but rely on the assistant-message
loop for token counts.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List

from ..models import CompactEvent, ExecutionLogEntry, ExecutionMetadata
from .activity_tracking import complete_tool_execution, start_tool_execution
from .error_classifier import _is_rate_limit_message

logger = logging.getLogger(__name__)


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
            # Final result message with stats. Pull only model-level facts
            # (cost / duration / num_turns / contextWindow). Token counts in
            # result.usage and modelUsage.inputTokens are CUMULATIVE — see the
            # module docstring's "Token-accounting invariant" section. Tokens
            # come from per-call usage on the assistant branch below.
            metadata.cost_usd = msg.get("total_cost_usd")
            metadata.duration_ms = msg.get("duration_ms")
            metadata.num_turns = msg.get("num_turns")
            response_text = msg.get("result", response_text)
            if not metadata.session_id:
                metadata.session_id = msg.get("session_id")

            model_usage = msg.get("modelUsage", {})
            for _, model_data in model_usage.items():
                if "contextWindow" in model_data:
                    metadata.context_window = model_data["contextWindow"]
                break  # Use first model found

        elif msg_type == "assistant":
            message = msg.get("message", {})
            message_content = message.get("content", [])

            # Per-API-call token usage. Each assistant message corresponds to
            # ONE Claude API call; usage on it is the per-call breakdown
            # (input, cache_read, cache_creation, output). We OVERWRITE so
            # the LATEST assistant message wins — that's the final API call's
            # prompt size, which determines whether the next user turn will
            # fit. (Mirrors process_stream_line below.)
            usage = message.get("usage", {}) or {}
            if usage:
                metadata.input_tokens = usage.get("input_tokens", 0)
                metadata.output_tokens = usage.get("output_tokens", 0)
                metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0)

            # #678: capture the actual model from the latest assistant message.
            # Useful when the trailing result line is lost and we need to record
            # what model ran on the failure row.
            model_id = message.get("model")
            if isinstance(model_id, str) and model_id:
                metadata.model_name = model_id

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
        for _, model_data in model_usage.items():
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
        # block in its assistant branch.)
        if msg_type == "assistant":
            usage = message.get("usage", {}) or {}
            if usage:
                metadata.input_tokens = usage.get("input_tokens", 0)
                metadata.output_tokens = usage.get("output_tokens", 0)
                metadata.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                metadata.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            # #678: capture actual model id from the latest assistant message
            # so the metadata survives the reader-race even when the trailing
            # result line is lost.
            model_id = message.get("model")
            if isinstance(model_id, str) and model_id:
                metadata.model_name = model_id

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
