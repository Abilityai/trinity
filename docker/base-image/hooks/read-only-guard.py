#!/usr/bin/env python3
"""PreToolUse hook for Write/Edit/NotebookEdit/MultiEdit tools.

Enforces per-agent read-only mode by checking ~/.trinity/read-only-config.json.
Exits 0 (allow) when read-only mode is disabled or the path is on the allow list.
Exits 2 (deny) when read-only mode is enabled and the path matches a blocked pattern.

Input: JSON via stdin (Claude Code hook protocol)
Output: Exit 0 to allow, Exit 2 with stderr message to block

Reference: https://docs.anthropic.com/en/docs/claude-code/hooks
"""
import fnmatch
import os
import sys

sys.path.insert(0, "/opt/trinity/hooks")
from lib import (  # noqa: E402
    allow,
    deny,
    log_event,
    read_stdin_json,
    run_hook,
)

_CONFIG_PATH = os.path.expanduser("~/.trinity/read-only-config.json")


def _load_read_only_config() -> dict | None:
    """Return the read-only config dict if enabled, None if disabled or absent."""
    import json
    if not os.path.exists(_CONFIG_PATH):
        return None
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log_event("read_only_config_load_error", path=_CONFIG_PATH, error=str(e))
        return None
    if not cfg.get("enabled", False):
        return None
    return cfg


def _normalise(path: str) -> str:
    """Resolve to absolute path under /home/developer."""
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join("/home/developer", expanded)
    return os.path.normpath(expanded)


def _matches_any(path: str, patterns: list) -> str:
    """Return the first matching pattern, or empty string."""
    basename = os.path.basename(path)
    for pattern in patterns:
        # Match absolute path
        if fnmatch.fnmatch(path, pattern):
            return pattern
        # Match basename (e.g. "*.py")
        if fnmatch.fnmatch(basename, pattern):
            return pattern
        # Match relative path stripped of /home/developer/ prefix
        rel = path[len("/home/developer/"):] if path.startswith("/home/developer/") else path
        if fnmatch.fnmatch(rel, pattern):
            return pattern
        # Handle directory wildcards like ".claude/*"
        if pattern.endswith("/*"):
            dir_prefix = pattern[:-2]
            if rel.startswith(dir_prefix + "/") or rel == dir_prefix:
                return pattern
    return ""


def _check_path(path: str, cfg: dict) -> None:
    """Deny if path is blocked; allow if it's on the allow list or not blocked."""
    if not path:
        return

    norm = _normalise(path)
    allowed = cfg.get("allowed_patterns", [])
    blocked = cfg.get("blocked_patterns", [])

    if _matches_any(norm, allowed):
        return  # explicitly allowed

    match = _matches_any(norm, blocked)
    if match:
        deny(
            f"read-only mode: cannot modify '{path}' (matches blocked pattern '{match}')",
            tool="Write/Edit/NotebookEdit/MultiEdit",
            path=norm,
            pattern=match,
        )


def main() -> None:
    data = read_stdin_json()
    tool_input = data.get("tool_input") or {}

    cfg = _load_read_only_config()
    if cfg is None:
        allow()  # read-only mode disabled

    # Write / Edit / NotebookEdit: single file_path or notebook_path
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if path:
        _check_path(path, cfg)
        allow()

    # MultiEdit: edits[] array, each with file_path
    edits = tool_input.get("edits") or []
    for edit in edits:
        if isinstance(edit, dict):
            _check_path(edit.get("file_path") or "", cfg)

    allow()


if __name__ == "__main__":
    run_hook(main)
