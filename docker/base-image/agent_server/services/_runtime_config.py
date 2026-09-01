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
#
# ---------------------------------------------------------------------------
# #2468: the audit. ScheduleWakeup was one member of a family.
# ---------------------------------------------------------------------------
#
# Every entry below was decided from the CLI's OWN tool description, read out
# of a live headless run (claude 2.1.220, `ToolSearch select:...`), not from
# the name. The quoted fragments are verbatim. The test is a single question:
# does this tool promise a future event, a peer session, or work that outlives
# the turn? A Trinity execution is one `claude --print` process that exits when
# the model stops writing, so every one of those promises is false here — and a
# tool result asserting a false fact is worse than a missing tool, because the
# model plans around it and reports success.
#
#   Workflow      "Workflows run in the background — this tool returns
#                 immediately with a task ID, and a <task-notification>
#                 arrives when the workflow completes."  Same shape as
#                 ScheduleWakeup, stated even more plainly.
#   Monitor       "Start a background monitor ... you keep working and
#                 notifications arrive in the chat."  There is no later chat.
#   TaskOutput    "DEPRECATED: ... you receive a <task-notification> with the
#                 same path when the task completes."  Deprecated AND a
#                 promise; its description is itself the false belief.
#   CronCreate    "Schedule a prompt to be enqueued at a future time."  There
#   CronList       is no future turn to enqueue into. CronList reads jobs
#   CronDelete     "scheduled via CronCreate in this session" — a per-session
#                 store that is always empty here — and CronDelete goes with
#                 them. Trinity's own schedules and `set_reminder` are the
#                 primitives that actually fire.
#   SendMessage   "Send a message to another agent ... Teammate by name /
#                 `main` the main conversation."  Those peers are sessions of
#                 an interactive harness; a Trinity container holds one turn
#                 and no teammates. Agent-to-agent here is
#                 `mcp__trinity__chat_with_agent`.
#   PushNotification  "sends a desktop notification in the user's terminal ...
#                 pulls their attention."  There is no terminal and nobody
#                 attached. Trinity has `send_notification` and the operator
#                 queue, which reach a real person.
#   RemoteTrigger "Call the claude.ai remote-trigger API ... the OAuth token is
#                 added automatically."  It creates work on an external control
#                 plane that outlives this turn and is invisible to Trinity's
#                 observability, billing and operator surfaces.
#
# KEPT, with the reason, so a later reader does not have to re-derive it:
#
#   Task, TaskCreate/TaskGet/TaskList/TaskUpdate — the in-turn TASK LIST
#       ("create a structured task list for your current coding session"). The
#       name collides with the background-task registry; the description does
#       not. Pure bookkeeping, no future-event promise.
#   TaskStop — "Stops a running background task by its ID". Acts only within
#       the turn, and denying it would remove the one way to stop a runaway
#       background command the model itself started.
#   Bash — cannot be denied, and its backgrounding result says "You will be
#       notified when it completes" (verbatim, both the `run_in_background`
#       and tool-timeout-promotion variants). Unfixable from here, which is
#       why `platform_prompt_service` carries the counterweight.
#   EnterWorktree/ExitWorktree, DesignSync, ReportFindings, ToolSearch,
#       Skill, Web*, Read/Write/Edit — in-turn, no promise.
#
# NOTED, not acted on: `DesignSync` reaches claude.ai design projects with the
# user's login — the same "external control plane" concern as RemoteTrigger,
# but it makes no future-work promise, so it is outside this issue's frame and
# deliberately left alone rather than swept up.
#
# Tension worth stating rather than hiding: with `Monitor` gone, a model that
# wants to wait for something has only foreground commands inside the execution
# timeout. That is the intended behaviour for a one-shot run and is exactly
# what the prompt now says — but if a future change makes in-turn waiting
# necessary, revisit Monitor here rather than working around it.
PLATFORM_DENIED_TOOLS = (
    "ScheduleWakeup",
    "Workflow",
    "Monitor",
    "TaskOutput",
    "CronCreate",
    "CronList",
    "CronDelete",
    "SendMessage",
    "PushNotification",
    "RemoteTrigger",
)


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
