"""Derive a tool-call summary from a Claude Code execution log (#1741).

``schedule_executions.tool_calls`` / ``chat_messages.tool_calls`` are meant to
hold a compact list of the tools an execution invoked. The **chat** path gets
that shape for free: ``/api/chat`` returns ``execution_log_simplified`` (a list
of ``ExecutionLogEntry`` — ``{type, tool, input, success, duration_ms, …}``).

The **task** path does not: ``/api/task`` returns only the raw stream-json
transcript, so ``apply_result`` had nothing else to store and stored the whole
transcript into the ``tool_calls`` column verbatim. Consequences (#1741):

* every consumer expects ``{"tool": …}`` / ``{"name": …}`` entries, so the
  Schedules-performance ``tool_call_total`` read **0** forever and the
  execution-detail "Tool Calls" panel rendered **empty** — a confident wrong
  number rather than a blank;
* the column became a second copy of the transcript that ``prune_execution_logs``
  never touched, so it outlived the log-retention window it duplicates.

Deriving the summary here (rather than teaching ``/api/task`` to send it) is
deliberate: it works against **every** agent image immediately, with no base-image
rebuild and no mixed-fleet window. The trade-off is that the two agent-side
fields we cannot reconstruct — ``success`` and ``duration_ms`` — are absent; both
render conditionally in the UI, so they degrade to "not shown" rather than wrong.
Having ``/api/task`` return ``execution_log_simplified`` would restore them and
is a clean follow-up.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A raw stream-json transcript is a list of envelope events; the simplified
# shape is a list of tool entries. These are the envelope `type`s that mark the
# raw shape — used to tell a legacy blob from an already-summarised one.
_RAW_ENVELOPE_TYPES = {"system", "assistant", "user", "result", "rate_limit_event"}

# Bound the summary so one pathological run can't write an unbounded blob.
MAX_TOOL_CALLS = 500
MAX_INPUT_CHARS = 2000


def _truncate_input(value: Any) -> Any:
    """Keep the input renderable but bounded. The UI only shows a one-line
    summary of it, so a giant argument blob buys nothing and costs storage."""
    if value is None:
        return None
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(encoded) <= MAX_INPUT_CHARS:
        return value
    return {"_truncated": encoded[:MAX_INPUT_CHARS]}


def extract_tool_calls(execution_log: Any) -> list[dict]:
    """Return ``[{type: "tool_use", tool, input}, …]`` for a raw transcript.

    Tool uses live one level down from the envelope: each ``assistant`` event
    carries ``message.content``, a list of blocks, and a tool call is a block
    with ``type == "tool_use"``. That nesting is exactly why the old top-level
    ``entry.get("name") or entry.get("tool")`` scan found nothing.

    Never raises — a malformed log yields ``[]`` rather than failing an
    already-billed execution.
    """
    if not isinstance(execution_log, list):
        return []

    calls: list[dict] = []
    for event in execution_log:
        if not isinstance(event, dict):
            continue

        # Already-simplified entries pass through (the /api/chat shape, and any
        # future /api/task that starts sending it).
        if event.get("type") == "tool_use" and (event.get("tool") or event.get("name")):
            calls.append({
                "type": "tool_use",
                "tool": event.get("tool") or event.get("name"),
                "input": _truncate_input(event.get("input")),
                **({"success": event["success"]} if event.get("success") is not None else {}),
                **({"duration_ms": event["duration_ms"]} if event.get("duration_ms") is not None else {}),
            })
            if len(calls) >= MAX_TOOL_CALLS:
                break
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if not name:
                continue
            calls.append({
                "type": "tool_use",
                "tool": name,
                "input": _truncate_input(block.get("input")),
            })
            if len(calls) >= MAX_TOOL_CALLS:
                return calls
    return calls


def looks_like_raw_transcript(parsed: Any) -> bool:
    """True when a stored ``tool_calls`` value is a legacy raw transcript.

    Used by the backfill to tell an unusable legacy blob from an already-correct
    summary, so the pass is idempotent.
    """
    if not isinstance(parsed, list) or not parsed:
        return False
    return any(
        isinstance(e, dict) and e.get("type") in _RAW_ENVELOPE_TYPES
        for e in parsed
    )


def summarize_tool_calls_json(raw_json: Optional[str]) -> Optional[str]:
    """Convert a stored raw-transcript ``tool_calls`` JSON string to the summary
    shape. Returns None when there is nothing usable to store.

    Idempotent: a value that is already a summary is returned unchanged.
    """
    if not raw_json:
        return None
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not looks_like_raw_transcript(parsed):
        return raw_json  # already summarised (or not a transcript) — leave it
    calls = extract_tool_calls(parsed)
    return json.dumps(calls) if calls else None
