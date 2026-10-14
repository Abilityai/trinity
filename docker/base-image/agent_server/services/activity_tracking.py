"""
Session activity tracking for real-time monitoring.
"""
from datetime import datetime
import uuid
from typing import Any, Dict, Optional

from ..state import agent_state
from ..utils.helpers import get_tool_name, get_input_summary, truncate_output


def _by_execution() -> Dict[str, Dict[str, Any]]:
    # Older in-memory states (a process that predates the key) get it lazily.
    return agent_state.session_activity.setdefault("by_execution", {})


def execution_activity(execution_id: str) -> Optional[Dict[str, Any]]:
    """The per-execution slot (trinity-enterprise#620): `{tool, input_summary,
    since, tool_id}` while a tool runs, `{tool: None, ...}` between tools
    ("Thinking"), `None` for an execution this process never tracked."""
    return _by_execution().get(execution_id)


def forget_execution(execution_id: str) -> None:
    """Drop the slot once the execution is over — a finished run must never
    keep reporting its last tool (the heartbeat also prunes against the
    process registry, so this is the belt)."""
    _by_execution().pop(execution_id, None)


def start_tool_execution(
    tool_id: str, tool: str, input_data: Dict[str, Any], execution_id: Optional[str] = None
):
    """Record start of a tool execution"""
    now = datetime.now()
    display_name = get_tool_name(tool, input_data)
    input_summary = get_input_summary(tool, input_data)

    # Set session as running
    agent_state.session_activity["status"] = "running"
    agent_state.session_activity["active_tool"] = {
        "name": display_name,
        "input_summary": input_summary,
        "started_at": now.isoformat()
    }
    if execution_id:
        _by_execution()[execution_id] = {
            "tool": display_name,
            "input_summary": input_summary,
            "since": now.isoformat(),
            "tool_id": tool_id,
        }

    # Initialize totals.started_at if this is the first call
    if agent_state.session_activity["totals"]["started_at"] is None:
        agent_state.session_activity["totals"]["started_at"] = now.isoformat()

    # Add to timeline (newest first)
    timeline_entry = {
        # Provider IDs (notably Codex item_0) are only unique within a run.
        # The existing activity/detail API uses this opaque ID for drill-down.
        "id": uuid.uuid4().hex if execution_id else tool_id,
        "provider_tool_id": tool_id,
        "execution_id": execution_id,
        "tool": display_name,
        "input": input_data,
        "input_summary": input_summary,
        "output_summary": None,
        "duration_ms": None,
        "started_at": now.isoformat(),
        "ended_at": None,
        "success": None,
        "status": "running"
    }
    # Insert at beginning (newest first)
    agent_state.session_activity["timeline"].insert(0, timeline_entry)

    # Update tool counts
    if display_name not in agent_state.session_activity["tool_counts"]:
        agent_state.session_activity["tool_counts"][display_name] = 0
    agent_state.session_activity["tool_counts"][display_name] += 1

    # Update totals
    agent_state.session_activity["totals"]["calls"] += 1


def complete_tool_execution(
    tool_id: str, success: bool, output: str = None, execution_id: Optional[str] = None
):
    """Complete only the owning execution's tool, preserving its detail output.

    Legacy callers without an execution ID may resolve an unambiguous tool;
    they must never guess between runs, including already completed runs.
    """
    candidates = [
        entry for entry in agent_state.session_activity["timeline"]
        if entry.get("provider_tool_id", entry["id"]) == tool_id
        and (execution_id is None or entry.get("execution_id") == execution_id)
    ]
    if len(candidates) != 1 or candidates[0]["status"] != "running":
        return
    entry = candidates[0]
    now = datetime.now()
    started_at = datetime.fromisoformat(entry["started_at"])
    duration_ms = int((now - started_at).total_seconds() * 1000)
    entry.update({
        "ended_at": now.isoformat(), "duration_ms": duration_ms,
        "success": success, "status": "completed",
        "output_summary": truncate_output(output) if output else None,
    })
    agent_state.session_activity["totals"]["duration_ms"] += duration_ms
    if output:
        agent_state.tool_outputs[entry["id"]] = output

    owner = entry.get("execution_id")
    slot = _by_execution().get(owner)
    if slot and slot.get("tool_id") == tool_id:
        slot.update({"tool": None, "input_summary": None,
                     "since": now.isoformat(), "tool_id": None})

    # Completing one run must not clear another run's active tool.
    running = next((item for item in agent_state.session_activity["timeline"]
                    if item["status"] == "running"), None)
    agent_state.session_activity["active_tool"] = ({
        "name": running["tool"], "input_summary": running["input_summary"],
        "started_at": running["started_at"],
    } if running else None)
    agent_state.session_activity["status"] = "running" if running else "idle"
