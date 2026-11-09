#!/usr/bin/env python3
"""Build-time smoke for the GUARD-002 hooks (trinity-enterprise#787).

Runs every hook the managed file registers EXACTLY as that file registers it
(`[command] + args`, read from /etc/claude-code/managed-settings.json, so the
smoke cannot stay green while the registration drifts) against a known input,
and fails the image build unless each one decides as it should. A hook that
cannot import under its registered interpreter and flags exits 1, which
Claude Code treats as "let the tool run": this is where that is caught.

Run by the Dockerfile as root, before /logs is created for the agent user.
It removes the log file, the read-only config and the skill-gate marker it writes.
"""
import json
import os
import subprocess
import sys

MANAGED = "/etc/claude-code/managed-settings.json"
DROP_IN_DIR = "/etc/claude-code/managed-settings.d"
READ_ONLY_CONFIG = "/opt/trinity/read-only-config.json"
LOG_PATH = "/logs/guardrails.jsonl"
SKILL_GATES_MARKER = "/opt/trinity/skill-gates-active"

SECRET = "sk-ant-api03-" + "A" * 93
CASES = {
    "bash-guardrail.py": [
        ({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, 2),
        ({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, 0),
    ],
    "file-guardrail.py": [
        ({"tool_name": "Write", "tool_input": {"file_path": "~/.ssh/id_rsa"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "~/.claude.json"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/notes.md"}}, 0),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/.env.local"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/.env.example"}}, 0),
    ],
    # No TRINITY_EXECUTION_ID at build time = no platform turn: a protected path
    # passes, an ordinary one passes. The tier lookup itself is covered by unit tests.
    "turn-tier-guard.py": [
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/notes.md"}}, 0),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/CLAUDE.md"}}, 0),
        ({"tool_name": "Read", "tool_input": {"file_path": "/home/developer/notes.md"}}, 0),
        # The shell is owner tier in a platform turn (ent#926); outside one it runs.
        ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, 0),
        # Likewise a search with no path and an owner-added MCP server's tool.
        ({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, 0),
        ({"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": "/etc/hosts"}}, 0),
    ],
    "read-only-guard.py": [
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/app.py"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/notes.txt"}}, 0),
    ],
    "output-scanner.py": [
        ({"tool_name": "Bash", "tool_input": {"command": "cat x"},
          "tool_response": {"stdout": "token=" + SECRET}}, 0),
    ],
}


def _check_gated_rows(argv: list, failures: list) -> None:
    """trinity-enterprise#1002: `gated_bash_deny` applies with the marker only.
    The marker never survives this: it would make the skill-gate hook fail
    closed on every agent built from the image."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "claude -p hi"}})
    for gated, want in ((False, 0), (True, 2)):
        if gated:
            open(SKILL_GATES_MARKER, "w").close()
        try:
            r = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=30)
        finally:
            if gated:
                os.remove(SKILL_GATES_MARKER)
        if r.returncode != want:
            failures.append(f"gated rows, marker={gated}: exit {r.returncode}, want {want}: "
                            f"{r.stderr.strip()[:200]}")


def _check_deny_record(failures: list) -> None:
    """trinity-enterprise#165: a deny record carries the spool contract the
    agent server's forwarder ships (`v`, `event_id`, a derived `kind` and
    `hook`, a `code`). Read right after the bash-guardrail rows ran."""
    try:
        with open(LOG_PATH) as f:
            records = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        failures.append(f"spool: unreadable after a deny: {e}")
        return
    if not any(r.get("event") == "deny" and r.get("v") == 1 and isinstance(r.get("event_id"), str)
               and r.get("kind") == "deny" and isinstance(r.get("hook"), str)
               and isinstance(r.get("code"), str) for r in records):
        failures.append("spool: no deny record with v/event_id/kind/hook/code")


def main() -> int:
    with open(MANAGED) as f:
        managed = json.load(f)
    entries = [h for phase in managed["hooks"].values() for e in phase for h in e["hooks"]]
    # Drop-ins are validated by the CLI one file at a time (a bad one voids only
    # itself), so a hook registered there is run exactly as registered too.
    for name in sorted(os.listdir(DROP_IN_DIR)) if os.path.isdir(DROP_IN_DIR) else []:
        with open(os.path.join(DROP_IN_DIR, name)) as f:
            extra = json.load(f)
        entries += [h for phase in extra.get("hooks", {}).values() for e in phase for h in e["hooks"]]
    seen = set()
    failures = []
    logs_existed = os.path.exists(os.path.dirname(LOG_PATH))
    with open(READ_ONLY_CONFIG, "w") as f:
        json.dump({"enabled": True, "blocked_patterns": ["*.py"], "allowed_patterns": ["*.txt"]}, f)
    try:
        for entry in entries:
            argv = [entry["command"], *entry.get("args", [])]
            hook = os.path.basename(argv[-1])
            seen.add(hook)
            for payload, want in CASES.get(hook, []):
                r = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                                   text=True, timeout=30)
                if r.returncode != want:
                    failures.append(f"{hook}: exit {r.returncode}, want {want}: {r.stderr.strip()[:200]}")
            if hook == "bash-guardrail.py":
                _check_gated_rows(argv, failures)
                _check_deny_record(failures)
        if "output-scanner.py" in seen:
            with open(LOG_PATH) as f:
                if '"credential_pattern_in_output"' not in f.read():
                    failures.append("output-scanner.py: no credential_pattern_in_output line")
    finally:
        os.remove(READ_ONLY_CONFIG)
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
        if not logs_existed and os.path.isdir(os.path.dirname(LOG_PATH)):
            os.rmdir(os.path.dirname(LOG_PATH))
    missing = set(CASES) - seen
    if missing:
        failures.append(f"not registered: {sorted(missing)}")
    for line in failures:
        print(f"GUARD-002 smoke FAILED — {line}", file=sys.stderr)
    if not failures:
        print(f"GUARD-002 smoke passed: {sorted(seen)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
