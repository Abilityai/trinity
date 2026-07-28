"""
Helper utility functions for the agent server.
"""
from datetime import datetime, timezone
from typing import Dict, Any


def iso_z_from_mtime(mtime: float) -> str:
    """Format a filesystem mtime (epoch seconds) as a canonical ISO-Z UTC string.

    Uses an explicit UTC timezone so the result is independent of the container's
    local TZ (Invariant #16: platform timestamps are ISO-Z UTC). A naive
    ``datetime.fromtimestamp(mtime).isoformat()`` renders in local time with no
    zone marker, which downstream consumers (that treat naive ISO as UTC) then
    display hours off. Returns e.g. ``"2026-07-27T08:06:58.549706Z"``.
    """
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def shorten_path(path: str) -> str:
    """Shorten file path for display"""
    if not path:
        return "..."
    parts = path.split('/')
    if len(parts) <= 2:
        return path
    return f".../{'/'.join(parts[-2:])}"


def shorten_url(url: str) -> str:
    """Shorten URL for display"""
    if not url:
        return "..."
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.hostname or url[:30]
    except:
        return url[:30]


def truncate_output(output: str, max_length: int = 500) -> str:
    """Truncate output for summary, preserving beginning"""
    if not output:
        return ""
    if len(output) <= max_length:
        return output
    return output[:max_length] + "..."


def get_tool_name(tool: str, input_data: Dict[str, Any]) -> str:
    """Get display name for tool, adding prefixes for MCP and sub-agents"""
    # Check if it's an MCP tool (usually has specific patterns)
    if tool.startswith("mcp__"):
        # Convert mcp__server__tool to mcp:server
        parts = tool.split("__")
        if len(parts) >= 2:
            return f"mcp:{parts[1]}"
        return f"mcp:{tool}"

    # Check if it's a Task (sub-agent)
    if tool == "Task":
        subagent_type = input_data.get("subagent_type", "")
        if subagent_type:
            return f"Task:{subagent_type}"
        return "Task"

    return tool


def get_input_summary(tool: str, input_data: Dict[str, Any]) -> str:
    """Generate a human-readable summary of tool input"""
    if not input_data:
        return "..."

    if tool == "Read":
        path = input_data.get("file_path", "")
        return shorten_path(path)
    elif tool == "Edit":
        path = input_data.get("file_path", "")
        return shorten_path(path)
    elif tool == "Write":
        path = input_data.get("file_path", "")
        return shorten_path(path)
    elif tool == "Glob":
        return input_data.get("pattern", "...")
    elif tool == "Grep":
        pattern = input_data.get("pattern", "")
        return f'"{pattern[:30]}"' if pattern else "..."
    elif tool == "Bash":
        cmd = input_data.get("command", "")
        return cmd[:50] + ("..." if len(cmd) > 50 else "")
    elif tool == "Task":
        return input_data.get("description", input_data.get("prompt", "...")[:50])
    elif tool == "WebFetch":
        url = input_data.get("url", "")
        return shorten_url(url)
    elif tool == "WebSearch":
        return input_data.get("query", "...")[:40]
    elif tool == "TodoWrite":
        return "Updating todos"
    elif tool == "AskUserQuestion":
        return "Asking question"
    else:
        # For MCP tools or unknown tools, try to get first param
        for key, value in input_data.items():
            if isinstance(value, str) and len(value) < 50:
                return f"{key}: {value[:30]}"
            elif isinstance(value, str):
                return f"{key}: {value[:30]}..."
        return "..."
