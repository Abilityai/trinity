#!/usr/bin/env python3
"""PreToolUse hook for Write/Edit/NotebookEdit tools.

Denies writes to credential files, SSH/AWS/cloud config, and Trinity's own
settings directories. Per-agent extra_path_deny adds literal substring rules.
Also refuses new content holding the output scanner's `[REDACTED:` placeholder
(trinity-enterprise#165). Fail-closed.
"""
import fnmatch
import os
import sys

sys.path.insert(0, "/opt/trinity/hooks")
from lib import (  # noqa: E402
    allow,
    deny,
    load_config,
    read_stdin_json,
    run_hook,
)


def _normalise(path: str) -> str:
    """Normalise to absolute path for consistent matching."""
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join("/home/developer", expanded)
    # normpath keeps exactly two leading slashes (POSIX leaves `//` implementation-
    # defined); the filesystem reads them as one, so `//home/developer/...` must
    # match the same patterns (trinity-enterprise#792).
    return os.path.normpath("/" + expanded.lstrip("/"))


def _matches_glob(path: str, patterns: list, allow: list = ()) -> str:
    """Return matching pattern or empty string.

    A basename in `allow` (`.env.example`, #3868: names only, required by the
    compatibility check) skips the slash-free patterns, which are the basename
    credential rules such as `.env.*`. Absolute patterns still apply, and a
    symlink never gets the exception (it could point at a real `.env`).
    """
    basename = os.path.basename(path)
    allowed = basename in allow and not os.path.islink(path)
    for pattern in patterns:
        if allowed and "/" not in pattern:
            continue
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            return pattern
    return ""


# The output scanner's placeholder (output-scanner.py PLACEHOLDER). Written
# back, it would replace a real credential in the file (trinity-enterprise#165).
REDACTION_MARK = "[REDACTED:"
WRITEBACK_REFUSED = (
    "this holds `[REDACTED:`, the placeholder Trinity puts where it hid a credential from you; "
    "writing it would replace the real value. To change a file that holds one, use Edit on only the lines "
    "you need, keeping the hidden value's line out of old_string and new_string; do not rewrite the whole "
    "file, which would lose the value. If the value itself must change, ask the owner to set it")


def _new_content(tool_input: dict) -> list:
    """The text a Write/Edit/MultiEdit/NotebookEdit call would put in the file."""
    parts = [tool_input.get(k) for k in ("content", "new_string", "new_source")]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        parts += [e.get("new_string") for e in edits if isinstance(e, dict)]
    return [p for p in parts if isinstance(p, str)]


def main() -> None:
    data = read_stdin_json()
    tool_input = data.get("tool_input") or {}
    tool_name = data.get("tool_name", "")
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw_path:
        allow()

    path = _normalise(raw_path)
    config = load_config()
    baseline = config.get("path_deny", []) or []
    extras = config.get("extra_path_deny", []) or []
    path_allow = config.get("path_allow", []) or []

    match = _matches_glob(path, baseline, path_allow)
    if match:
        deny(
            f"protected path ({match})",
            tool=tool_name,
            path=path,
            pattern=match,
        )

    for literal in extras:
        if isinstance(literal, str) and literal and literal in path:
            deny(
                "matches per-agent path rule",
                tool=tool_name,
                path=path,
                rule=literal[:80],
            )

    if any(REDACTION_MARK in part for part in _new_content(tool_input)):
        deny(WRITEBACK_REFUSED, tool=tool_name, path=path, code="redaction_writeback_refused")

    allow()


if __name__ == "__main__":
    run_hook(main)
