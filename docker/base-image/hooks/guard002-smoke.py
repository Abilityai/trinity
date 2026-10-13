#!/usr/bin/env python3
"""Build-time smoke for the GUARD-002 hooks (trinity-enterprise#787).

Runs every hook the managed file registers EXACTLY as that file registers it
(`[command] + args`, read from /etc/claude-code/managed-settings.json, so the
smoke cannot stay green while the registration drifts) against a known input,
and fails the image build unless each one decides as it should. A hook that
cannot import under its registered interpreter and flags exits 1, which
Claude Code treats as "let the tool run": this is where that is caught.

Run by the Dockerfile as root, before /logs is created for the agent user.
It removes the log file and the read-only config it writes.
"""
import json
import os
import subprocess
import sys

MANAGED = "/etc/claude-code/managed-settings.json"
READ_ONLY_CONFIG = "/opt/trinity/read-only-config.json"
LOG_PATH = "/logs/guardrails.jsonl"

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


def main() -> int:
    with open(MANAGED) as f:
        managed = json.load(f)
    entries = [h for phase in managed["hooks"].values() for e in phase for h in e["hooks"]]
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
