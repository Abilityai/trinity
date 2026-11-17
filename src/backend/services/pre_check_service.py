"""
Pre-Check Service for Trinity platform (SCHED-COND-001 / #454).

Runs the agent's optional template-supplied pre-check hook
``~/.trinity/pre-check`` via ``docker exec`` and returns a normalized
contract dict. The hook is language-agnostic — interpreter is selected
by the file's shebang, not by Trinity.

Called by ``routers/internal.py`` on behalf of the dedicated scheduler
before each cron-triggered fire. Reuses ``execute_command_in_container``
(the same primitive as ``git_service``, ``ssh_service``,
``agent_service/terminal``, Slack ingest, etc.) — no new HTTP edge from
backend to agent-server, no new long-lived process.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from services.docker_service import (
    execute_command_in_container,
    get_agent_container,
)

logger = logging.getLogger(__name__)


# Convention: language-agnostic executable shipped by the template.
# Interpreter is selected by the shebang line; the file must be marked +x.
HOOK_PATH = "/home/developer/.trinity/pre-check"

# Stdout becomes the chat prompt — 32 KB is plenty even for verbose scan output.
STDOUT_CAP = 32_000
STDERR_CAP = 4_000

EXISTENCE_TIMEOUT_S = 5
EXEC_TIMEOUT_S = 60


class AgentNotFound(Exception):
    """Raised when the target agent has no running container."""


async def run_pre_check(
    agent_name: str,
    *,
    schedule_id: Optional[str] = None,
    schedule_name: Optional[str] = None,
    schedule_message: Optional[str] = None,
) -> Dict:
    """Run the agent's optional pre-check hook.

    The schedule being gated (#3966) is exported to the hook as
    ``TRINITY_SCHEDULE_ID`` / ``TRINITY_SCHEDULE_NAME`` /
    ``TRINITY_SCHEDULE_MESSAGE`` — only the ones the caller passed, verbatim,
    in the exec's environment (not argv). With none passed the hook runs with
    no extra environment, exactly as before.

    Two-step exec:
      1. ``test -f`` (5s) — file presence check. Note ``-f``, not ``-x``:
         a present-but-non-executable file surfaces as exec failure
         (exit 126) so the operator gets a signal, instead of silently
         falling through to the backward-compat "no hook" path.
      2. Run the hook directly (60s). Trinity does not prefix ``python3``
         — the shebang determines interpreter.

    Returns one of:
      ``{"hook_present": False}``
        Template ships no hook. Caller should fire as usual.

      ``{"hook_present": True, "exit_code": int, "stdout": str, "stderr": str}``
        Hook ran. Caller translates per the SCHED-COND-001 contract:
          - exit != 0  → fail-open + log (broken hook must not suppress work)
          - exit 0, empty stdout    → record skip
          - exit 0, non-empty stdout → fire with stdout as override message

    Raises:
        AgentNotFound: if no running container for ``agent_name``.
    """
    if not get_agent_container(agent_name):
        raise AgentNotFound(agent_name)

    container_name = f"agent-{agent_name}"

    exists = await execute_command_in_container(
        container_name=container_name,
        command=f"test -f {HOOK_PATH}",
        timeout=EXISTENCE_TIMEOUT_S,
    )
    if exists.get("exit_code") != 0:
        return {"hook_present": False}

    # #3966: one hook gates every cron schedule on the agent; tell it which
    # one is firing. Only the fields the caller passed are exported.
    schedule_env = {
        name: value
        for name, value in {
            "TRINITY_SCHEDULE_ID": schedule_id,
            "TRINITY_SCHEDULE_NAME": schedule_name,
            "TRINITY_SCHEDULE_MESSAGE": schedule_message,
        }.items()
        if value is not None
    }
    result = await execute_command_in_container(
        container_name=container_name,
        command=HOOK_PATH,
        timeout=EXEC_TIMEOUT_S,
        environment=schedule_env or None,
    )
    # `output` from container_exec_run is the combined stream — keep
    # `stdout`/`stderr` as separate fields for forward-compat.
    output = (result.get("output") or "")[: STDOUT_CAP + STDERR_CAP]
    return {
        "hook_present": True,
        "exit_code": int(result.get("exit_code", 1)),
        "stdout": output[:STDOUT_CAP],
        "stderr": "",
    }
