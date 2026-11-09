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

# Must equal `_skill_gate.MARKER` (unit-tested).
SKILL_GATES_MARKER = "/opt/trinity/skill-gates-active"


def main() -> None:
    data = read_stdin_json()
    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "")
    if not command:
        allow()

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
                rule=pattern.pattern,
                command_prefix=command[:120],
                code="bash_deny_pattern",
            )

    for literal in extras:
        if isinstance(literal, str) and literal and literal in command:
            deny(
                f"matches per-agent deny rule",
                tool="Bash",
                rule=literal[:80],
                command_prefix=command[:120],
                code="bash_agent_rule",
            )

    allow()


if __name__ == "__main__":
    run_hook(main)
