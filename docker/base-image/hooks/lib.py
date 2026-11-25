"""Shared helpers for Trinity guardrail hooks.

All hooks in /opt/trinity/hooks/ import from this module. It centralises:
- Loading baseline + runtime guardrail config (root-owned, agent cannot rewrite).
- Structured logging to /logs/guardrails.jsonl, the spool the agent server's
  forwarder ships to the platform (trinity-enterprise#165).
- Safe stdin JSON parsing with a fail-closed default.

Hook protocol (Claude Code):
- stdin: JSON with tool_input, tool_name, hook_event_name, ...
- exit 0 with no stdout: allow
- exit 2 with stderr message: deny (message shown to model)
- exit 1 or uncaught exception: treated as non-blocking error by Claude
  -> we fail-closed by catching and exiting 2 ourselves.
"""
import json
import os
import re
import sys
import time
import traceback
import uuid
from typing import Any, Optional

RUNTIME_CONFIG_PATH = "/opt/trinity/guardrails-runtime.json"
BASELINE_CONFIG_PATH = "/opt/trinity/guardrails-baseline.json"
LOG_PATH = "/logs/guardrails.jsonl"
PROC_ROOT = "/proc"
ENVIRON_MAX_BYTES = 1 << 20
MAX_ID_LEN = 128

# The spool record (trinity-enterprise#165 §6.1). `kind` and `hook` are closed
# sets owned here; the backend keeps its own copy, parity-tested there. Both are
# DERIVED below, never passed by a caller, so a new hook event needs no edit to
# this file as long as its legacy name is in the table.
SPOOL_SCHEMA_VERSION = 1
EVENT_KINDS = (
    "deny",
    "credential_redacted",
    "credential_unredacted",
    "redaction_writeback_refused",
    "scanner_error",
    "hook_error",
    "config_error",
)
EVENT_HOOKS = (
    "bash_guardrail",
    "file_guardrail",
    "read_only_guard",
    "turn_tier_guard",
    "skill_gate",
    "output_scanner",
)
_HOOK_BY_SCRIPT = {
    "bash-guardrail.py": "bash_guardrail",
    "file-guardrail.py": "file_guardrail",
    "read-only-guard.py": "read_only_guard",
    "turn-tier-guard.py": "turn_tier_guard",
    "skill-gate.py": "skill_gate",
    "output-scanner.py": "output_scanner",
}
# Legacy event name -> kind. Anything absent (skill_gate_allow, turn_tier_owner,
# credential_pattern_in_output, ...) is kind null: kept in the spool for local
# debugging, never forwarded.
_KIND_BY_EVENT = {
    "deny": "deny",
    "credential_redacted": "credential_redacted",
    "credential_unredacted": "credential_unredacted",
    "scanner_error": "scanner_error",
    "scanner_oversize": "scanner_error",
    "hook_error": "hook_error",
    "hook_stdin_parse_error": "hook_error",
    "config_load_error": "config_error",
    "config_missing": "config_error",
    "bad_baseline_regex": "config_error",
    "read_only_config_load_error": "config_error",
    "skill_gate_deny": "deny",
}
# A deny whose code names a kind of its own.
_KIND_BY_DENY_CODE = {"redaction_writeback_refused": "redaction_writeback_refused"}

# The hook input's ids, kept by read_stdin_json for the records this process writes.
_HOOK_INPUT_IDS: dict = {}
_UNREAD = object()
_execution_id: Any = _UNREAD


def _event_kind(event: str, fields: dict) -> Optional[str]:
    if event == "deny":
        return _KIND_BY_DENY_CODE.get(fields.get("code"), "deny")
    if event == "skill_gate_unknown":
        # No verdict: a refusal only when the marker made it one.
        return "deny" if fields.get("marker") is True else None
    return _KIND_BY_EVENT.get(event)


def _hook_name() -> Optional[str]:
    script = sys.argv[0] if sys.argv else ""
    return _HOOK_BY_SCRIPT.get(os.path.basename(script or ""))


def _read_execution_id() -> Optional[str]:
    """`TRINITY_EXECUTION_ID` from the claude process's launch environment, the
    source the turn-tier guard trusts (never this hook's own env, which the
    registration clears). Attribution, not authority: a nested claude can set
    any value, so consumers filter by the authenticated agent."""
    try:
        with open(os.path.join(PROC_ROOT, str(os.getppid()), "environ"), "rb") as f:
            raw = f.read(ENVIRON_MAX_BYTES)
    except OSError:
        return None
    for entry in raw.split(b"\0"):
        name, sep, value = entry.partition(b"=")
        if sep and name == b"TRINITY_EXECUTION_ID":
            text = value.decode("utf-8", "replace")
            return text if 0 < len(text) <= MAX_ID_LEN else None
    return None


def _execution_id_once() -> Optional[str]:
    global _execution_id
    if _execution_id is _UNREAD:
        _execution_id = _read_execution_id()
    return _execution_id


def _derived_fields(event: str, fields: dict) -> dict:
    """The contract fields. They win over a caller's value of the same name."""
    return {
        "v": SPOOL_SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "kind": _event_kind(event, fields),
        "hook": _hook_name(),
        "execution_id": _execution_id_once(),
    }


def log_event(event: str, **fields: Any) -> None:
    """Append a structured JSON event to the guardrails log. Never raises.

    A fault computing the contract fields still writes the legacy record (no
    `v`, so the forwarder skips it): the spool fails open."""
    try:
        record = {"ts": time.time(), "event": event, **fields}
        try:
            derived = _derived_fields(event, fields)
        except Exception:
            derived = None
        if derived is not None:
            record.update(derived)
            record.setdefault("code", event)
            for name in ("session_id", "tool_use_id"):
                record.setdefault(name, _HOOK_INPUT_IDS.get(name))
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def load_config() -> dict:
    """Load runtime config, falling back to baseline, then hardcoded defaults.

    The runtime file is written by startup.sh (root-owned 0444) merging the
    image-baked baseline with the AGENT_GUARDRAILS env var.
    """
    for path in (RUNTIME_CONFIG_PATH, BASELINE_CONFIG_PATH):
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                log_event("config_load_error", path=path, error=str(e))
    log_event("config_missing")
    return {}


def read_stdin_json() -> dict:
    """Parse Claude Code hook JSON from stdin. Fail-closed on parse error."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        log_event("hook_stdin_parse_error", error=str(e))
        print("Guardrail: malformed hook input", file=sys.stderr)
        sys.exit(2)
    if isinstance(data, dict):
        for name in ("session_id", "tool_use_id"):
            value = data.get(name)
            if isinstance(value, str) and 0 < len(value) <= MAX_ID_LEN:
                _HOOK_INPUT_IDS[name] = value
    return data


def compile_patterns(patterns: list) -> list:
    """Compile regex patterns. Skips (and logs) any that fail to compile."""
    compiled = []
    for entry in patterns:
        if isinstance(entry, dict):
            pattern, reason = entry.get("pattern", ""), entry.get("reason", "denied")
        else:
            pattern, reason = entry, "denied"
        try:
            compiled.append((re.compile(pattern), reason))
        except re.error as e:
            log_event("bad_baseline_regex", pattern=pattern, error=str(e))
    return compiled


def deny(message: str, **log_fields: Any) -> None:
    """Emit deny decision to Claude Code and log it."""
    log_event("deny", reason=message, **log_fields)
    print(f"Guardrail blocked: {message}", file=sys.stderr)
    sys.exit(2)


def allow() -> None:
    """Allow the tool call."""
    sys.exit(0)


def run_hook(main_fn) -> None:
    """Wrap a hook's main() with fail-closed error handling."""
    try:
        main_fn()
    except SystemExit:
        raise
    except Exception:
        log_event("hook_error", traceback=traceback.format_exc())
        print("Guardrail: internal error (fail-closed)", file=sys.stderr)
        sys.exit(2)
