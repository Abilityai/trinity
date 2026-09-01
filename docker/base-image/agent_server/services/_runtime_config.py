"""Runtime guardrails configuration for Claude Code execution.

GUARD-003: CLI budget & scope controls. Guardrails runtime config is written
by startup.sh via /opt/trinity/hooks/write-runtime-config.py and is root-owned
0444 so the agent cannot rewrite it. We read it on every Claude Code
invocation so backend-initiated config updates (via container recreation)
take effect without restarting the agent-server process.

Extracted from `claude_code.py` per #122 (issue split). Kept as a separate
module so both `claude_code.py` (chat path) and `headless_executor.py` (task
path) can import it without a circular dependency.
"""
from __future__ import annotations

import json

_GUARDRAILS_RUNTIME_PATH = "/opt/trinity/guardrails-runtime.json"
_GUARDRAILS_BASELINE_PATH = "/opt/trinity/guardrails-baseline.json"
_DEFAULT_MAX_TURNS_CHAT = 50
_DEFAULT_MAX_TURNS_TASK = 50
_DEFAULT_EXECUTION_TIMEOUT_SEC = 1800  # GUARD-003 (#313): 30 min wall clock for chat


def _load_guardrails() -> dict:
    """Load guardrails config, falling back to baseline, then {}."""
    for path in (_GUARDRAILS_RUNTIME_PATH, _GUARDRAILS_BASELINE_PATH):
        try:
            with open(path) as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            continue
    return {}


# ---------------------------------------------------------------------------
# Platform tool denials (#2454)
# ---------------------------------------------------------------------------

# Tools that CANNOT work in a Trinity turn, denied at every Claude spawn.
#
# A Trinity execution is a one-shot `claude --print`: the process exits at end
# of turn. `ScheduleWakeup` asks a *persistent* harness to re-invoke the agent
# later and returns SUCCESS regardless — so here it is a guaranteed no-op that
# reports it worked, and the model then truthfully narrates a loop that will
# never tick (#2454). The built-in `loop` skill paces itself with exactly that
# tool, so denying the mechanism is what makes the wrong path fail VISIBLY
# instead of silently. The positive half — route repetition to
# `run_agent_loop` / `set_reminder` — is injected as platform guidance by
# `services/platform_prompt_service.py`; this list is the backstop for when
# the model reaches for the harness affordance anyway.
#
# Denied by TOOL NAME only. A `Skill(loop)` rule was considered and rejected:
# the specifier grammar for the Skill tool is not a contract we can pin, the
# base image tracks the latest CLI, and an unparseable rule risks the whole
# `--disallowedTools` argument on every turn — a fleet outage traded for a
# second layer over the mechanism this list already removes.
PLATFORM_DENIED_TOOLS = ("ScheduleWakeup",)


def merged_disallowed_tools(guardrails: dict) -> list:
    """GUARD-003's disallow list ∪ ``PLATFORM_DENIED_TOOLS``, order-stable.

    The single builder for every Claude spawn's ``--disallowedTools`` value, so
    an operator's guardrails edit can neither drop the platform denials nor
    duplicate them into the CLI argument. Operator entries come first (they are
    the ones a reader is looking for in the log line); non-string or blank
    entries are skipped rather than serialized. Never raises — a malformed
    config degrades to the platform denials alone, which is the safe direction:
    the deny list only ever removes capability.
    """
    merged: list = []
    seen = set()
    configured = guardrails.get("disallowed_tools") if isinstance(guardrails, dict) else None
    if not isinstance(configured, (list, tuple)):
        configured = ()
    for name in (*configured, *PLATFORM_DENIED_TOOLS):
        if not isinstance(name, str):
            continue
        cleaned = name.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged
