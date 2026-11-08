#!/usr/bin/env python3
"""PreToolUse hook for the Bash tool.

Denies commands matching baseline regex patterns plus any per-agent literal
substrings configured via extra_bash_deny. Fail-closed.

An agent with gated skills also gets the gated_bash_deny rows
(trinity-enterprise#1002). The root-owned marker says which agents those are.
"""
import os
import sys

sys.path.insert(0, "/opt/trinity/hooks")
from lib import (  # noqa: E402
    allow,
    compile_patterns,
    deny,
    load_config,
    read_stdin_json,
    run_hook,
)

# The output scanner's placeholder (output-scanner.py PLACEHOLDER). A command
# carrying it would write it where the real value was (trinity-enterprise#165).
REDACTION_MARK = "[REDACTED:"
WRITEBACK_REFUSED = (
    "this holds `[REDACTED:`, the placeholder Trinity puts where it hid a credential from you; "
    "a command that writes it would replace the real value. To change a file that holds one, change only "
    "the lines you need (the Edit tool, or sed on those lines) and leave the hidden value's line as it is; "
    "do not rewrite the whole file, which would lose the value. If the value itself must change, ask the "
    "owner to set it")

# Must equal `_skill_gate.MARKER` (unit-tested).
SKILL_GATES_MARKER = "/opt/trinity/skill-gates-active"


def main() -> None:
    data = read_stdin_json()
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "")
    if not command:
        allow()
    if REDACTION_MARK in command:
        deny(WRITEBACK_REFUSED, tool="Bash", code="redaction_writeback_refused")

    config = load_config()
    rows = list(config.get("bash_deny", []))
    # Fails open on purpose: with no marker the rows do not apply, because an
    # agent with no gates keeps its CLIs. A gated agent whose marker write failed
    # is covered again at its next start or skill check.
    if os.path.lexists(SKILL_GATES_MARKER):
        rows += config.get("gated_bash_deny", [])
    baseline = compile_patterns(rows)
    extras = config.get("extra_bash_deny", []) or []

    for pattern, reason in baseline:
        if pattern.search(command):
            deny(
                reason,
                tool="Bash",
                pattern=pattern.pattern,
                command_prefix=command[:120],
            )

    for literal in extras:
        if isinstance(literal, str) and literal and literal in command:
            deny(
                f"matches per-agent deny rule",
                tool="Bash",
                rule=literal[:80],
                command_prefix=command[:120],
            )

    allow()


if __name__ == "__main__":
    run_hook(main)
