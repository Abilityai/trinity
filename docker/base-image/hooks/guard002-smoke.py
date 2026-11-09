#!/usr/bin/env python3
"""Build-time smoke for the GUARD-002 hooks (trinity-enterprise#787).

Runs every hook the managed file registers EXACTLY as that file registers it
(`[command] + args`, read from /etc/claude-code/managed-settings.json, so the
smoke cannot stay green while the registration drifts) against a known input,
and fails the image build unless each one decides as it should. A hook that
cannot import under its registered interpreter and flags exits 1, which
Claude Code treats as "let the tool run": this is where that is caught.

The output scanner is checked on what it prints, not only its exit code
(trinity-enterprise#165): a key on stdout, on stderr, in a persisted output
file, in a Read file and in an owner MCP result must be hidden, the platform's
own MCP server must be left alone, a failing command must be warned about, and
the registration must reach every tool the scanner was written for.

Run by the Dockerfile as root, before /logs is created for the agent user.
It removes the log file, the read-only config, the skill-gate marker and the
persisted-output directory it writes.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MANAGED = "/etc/claude-code/managed-settings.json"
DROP_IN_DIR = "/etc/claude-code/managed-settings.d"
READ_ONLY_CONFIG = "/opt/trinity/read-only-config.json"
LOG_PATH = "/logs/guardrails.jsonl"
SKILL_GATES_MARKER = "/opt/trinity/skill-gates-active"
# Where the persisted-output case puts its file: the scanner accepts only a
# `tool-results` directory under the agent's home.
SMOKE_HOME = "/home/developer"

SECRET = "sk-ant-api03-" + "A" * 93
CASES = {
    "bash-guardrail.py": [
        ({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, 2),
        ({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, 0),
        ({"tool_name": "Bash", "tool_input": {"command": "echo 'K=[REDACTED:sk]' >> .env.local"}}, 2),
    ],
    "file-guardrail.py": [
        ({"tool_name": "Write", "tool_input": {"file_path": "~/.ssh/id_rsa"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "~/.claude.json"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/notes.md"}}, 0),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/.env.local"}}, 2),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/.env.example"}}, 0),
        ({"tool_name": "Write", "tool_input": {"file_path": "/home/developer/cfg.txt",
                                               "content": "K=[REDACTED:sk]"}}, 2),
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


# (event, tool) pairs the output scanner must be registered for, judged by the
# CLI's matcher rule (2.1.281): letters, digits, `_` and `|` only is an exact
# list of names; anything else is an unanchored regex.
SCANNER_COVERAGE = (
    ("PostToolUse", "Bash"), ("PostToolUse", "Read"), ("PostToolUse", "Grep"),
    ("PostToolUse", "WebFetch"), ("PostToolUse", "mcp__owner__fetch"),
    ("PostToolUseFailure", "Bash"), ("PostToolUseFailure", "mcp__owner__fetch"),
)


def _matcher_selects(matcher: str, tool: str) -> bool:
    if not matcher or matcher == "*":
        return True
    if re.fullmatch(r"[a-zA-Z0-9_|]+", matcher):
        return tool in [m.strip() for m in matcher.split("|") if m.strip()]
    return re.search(matcher, tool) is not None


def _check_scanner_coverage(managed: dict) -> list:
    failures = []
    for event, tool in SCANNER_COVERAGE:
        entries = [e for e in managed.get("hooks", {}).get(event, [])
                   if any(h.get("args", [""])[-1].endswith("/output-scanner.py") for h in e["hooks"])]
        if not any(_matcher_selects(e.get("matcher", ""), tool) for e in entries):
            failures.append(f"output-scanner.py: not registered for {tool} on {event}")
    return failures


def _scanner_run(argv: list, payload: dict):
    r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        out = {}
    return r, (out.get("hookSpecificOutput") or {}) if isinstance(out, dict) else {}


def _check_output_scanner(argv: list, failures: list) -> None:
    """trinity-enterprise#165: the scanner must HIDE a key from the model. A
    scanner that only logs exits 0 too, so the exit code proves nothing here."""
    def fail(what, r):
        failures.append(f"output-scanner.py: {what} (exit {r.returncode}): "
                        f"{r.stdout[:200]} {r.stderr.strip()[:200]}")

    base = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "cat x"}}
    shape = {"stdout": "", "stderr": "", "interrupted": False, "isImage": False}
    for field in ("stdout", "stderr"):
        response = {**shape, field: "token=" + SECRET}
        r, out = _scanner_run(argv, {**base, "tool_response": response})
        if (r.returncode != 0 or SECRET in r.stdout or out.get("hookEventName") != "PostToolUse"
                or out.get("updatedToolOutput") != {**response, field: "token=[REDACTED:sk]"}):
            fail(f"a key on {field} was not redacted", r)
    # A failed call reaches PostToolUse with the error as a plain string; only a
    # Bash-shaped object rewrite is accepted by the CLI (2.1.281).
    r, out = _scanner_run(argv, {**base, "tool_response": "Error: Exit code 1\ntoken=" + SECRET})
    if out.get("updatedToolOutput") != {"stdout": "Error: Exit code 1\ntoken=[REDACTED:sk]",
                                        "stderr": "", "interrupted": False} or SECRET in r.stdout:
        fail("a failed call's string output was not rewritten as a Bash object", r)
    # `grep --color=always sk-ant` puts the colour codes INSIDE the key.
    split = {**shape, "stdout": "cfg \x1b[m\x1b[1;31msk-ant\x1b[m" + SECRET[6:] + " end"}
    r, out = _scanner_run(argv, {**base, "tool_response": split})
    if out.get("updatedToolOutput") != {**split, "stdout": "cfg \x1b[m\x1b[1;31m[REDACTED:sk]\x1b[m end"}:
        fail("a key that colour codes split was not redacted whole", r)
    clean = {**shape, "stdout": "pod/task-runner-deployment-7d9f8c6b5-x2z9q Running"}
    r, _ = _scanner_run(argv, {**base, "tool_response": clean})
    if r.returncode != 0 or r.stdout.strip():
        fail("clean output was rewritten", r)
    r, out = _scanner_run(argv, {**base, "hook_event_name": "PostToolUseFailure",
                                 "error": "Exit code 1\ntoken=" + SECRET})
    if (r.returncode != 0 or SECRET in r.stdout or out.get("hookEventName") != "PostToolUseFailure"
            or not out.get("additionalContext")):
        fail("a failing command with a key was not warned about", r)
    read = {"type": "text", "file": {"filePath": "/home/developer/.env", "content": "K=" + SECRET + "\n",
                                     "numLines": 2, "startLine": 1, "totalLines": 2}}
    r, out = _scanner_run(argv, {**base, "tool_name": "Read", "tool_input": {}, "tool_response": read})
    if SECRET in r.stdout or out.get("updatedToolOutput") != {
            **read, "file": {**read["file"], "content": "K=[REDACTED:sk]\n"}}:
        fail("a key in a Read file was not redacted", r)
    blocks = [{"type": "text", "text": "token=" + SECRET}]
    r, out = _scanner_run(argv, {**base, "tool_name": "mcp__owner__fetch", "tool_input": {},
                                 "tool_response": blocks})
    if SECRET in r.stdout or out.get("updatedToolOutput") != [{"type": "text", "text": "token=[REDACTED:sk]"}]:
        fail("a key in an owner MCP result was not redacted", r)
    r, _ = _scanner_run(argv, {**base, "tool_name": "mcp__trinity__list_agents", "tool_input": {},
                               "tool_response": blocks})
    if r.returncode != 0 or r.stdout.strip():
        fail("the platform's own MCP server was scanned", r)
    tmp = tempfile.mkdtemp(dir=SMOKE_HOME, prefix=".guard002-smoke-")
    try:
        path = os.path.join(tmp, "tool-results", "smoke.txt")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("build output\n" * 3000 + "token=" + SECRET + "\n")
        r, _ = _scanner_run(argv, {**base, "tool_response": {
            **shape, "stdout": "build output\n" * 100, "persistedOutputPath": path}})
        with open(path) as f:
            body = f.read()
        if r.returncode != 0 or SECRET in body or "token=[REDACTED:sk]" not in body:
            fail("a key in the persisted output file was not redacted", r)
    finally:
        shutil.rmtree(tmp)


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
    scanned = []
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
            if hook == "output-scanner.py" and argv not in scanned:
                scanned.append(argv)
                _check_output_scanner(argv, failures)
        if "output-scanner.py" in seen:
            failures += _check_scanner_coverage(managed)
            with open(LOG_PATH) as f:
                if '"credential_redacted"' not in f.read():
                    failures.append("output-scanner.py: no credential_redacted line")
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
