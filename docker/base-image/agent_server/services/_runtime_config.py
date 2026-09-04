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
# ---------------------------------------------------------------------------
# #2468: the audit — ScheduleWakeup was one member of a family.
# ---------------------------------------------------------------------------
#
# Every entry below was decided from the CLI's OWN tool description, read out
# of a live headless run in a fleet agent container on claude 2.1.235 — the
# version the fleet image actually ships (AUDIT_CLI_VERSION below is data, not
# prose, because the closed reference PR #2472 first ran this audit on 2.1.220
# and re-measuring on 2.1.235 changed one membership and two rationales).
# Method note for the next re-audit: on 2.1.235 `ToolSearch select:` returns
# tool_reference blocks — schemas land in the model's context, not the
# transcript — so descriptions were captured by having the model quote them
# verbatim, cross-checked against the 2.1.220 quotes (every fragment pinned by
# tests/unit/test_2468_headless_tool_audit.py matched byte-identically).
#
# The test is a single question: does this tool promise a future event, a
# peer session, or work that outlives the turn? A Trinity execution is one
# `claude --print` process that exits when the model stops writing, so every
# one of those promises is false here — and a tool result asserting a false
# fact is worse than a missing tool, because the model plans around it and
# reports success (#2467 is the measured incident shape).
#
#   ScheduleWakeup  rationale in the #2454 block above; re-measured on
#                 2.1.235, where its text now also claims "when
#                 harness-tracked work finishes, you are re-invoked
#                 automatically" — the exact false belief behind #2467.
#   Workflow      "Workflows run in the background — this tool returns
#                 immediately with a task ID, and a <task-notification>
#                 arrives when the workflow completes."  Same shape as
#                 ScheduleWakeup, stated even more plainly.
#   Monitor       "you keep working and notifications arrive in the chat."
#                 There is no later chat, and its persistent mode "runs until
#                 you call TaskStop or the session ends" — session end is a
#                 kill, not a wait. Tension, stated rather than hidden: Bash's
#                 own un-editable text routes polling HERE ("use Monitor with
#                 an until-loop"), so this deny leaves a dangling reference —
#                 the prompt counterweight names the working idiom instead (a
#                 plain foreground until-loop under `timeout`). If in-turn
#                 event-stream waiting ever becomes necessary, revisit
#                 Monitor here rather than working around it.
#   TaskOutput    "DEPRECATED: ... you receive a <task-notification> with the
#                 same path when the task completes."  Deprecated AND a
#                 promise; its description is itself the false belief.
#   CronCreate    "Schedule a prompt to be enqueued at a future time."  Its
#   CronList       own text says where that store lives: "Jobs live only in
#   CronDelete     this Claude session — nothing is written to disk, and the
#                 job is gone when Claude exits."  CronList reads jobs
#                 "scheduled via CronCreate in this session" — a store that
#                 is always empty here — and CronDelete removes from "the
#                 in-memory session store". Trinity's own schedules and
#                 `set_reminder` are the primitives that actually fire.
#   SendMessage   "Send a message to another agent."  Denied on DIFFERENT
#   ListAgents    grounds than PR #2472 recorded: 2.1.220's description was
#                 teammate-only ("a Trinity container holds one turn and no
#                 teammates"), but 2.1.235 adds a genuinely WORKING in-turn
#                 facet — continuing a spawned subagent (`local_agent` tasks
#                 are waited by `claude --print`; see
#                 _NON_WAITED_BG_TASK_TYPES in headless_executor.py). The
#                 grounds now: ListAgents advertises, verbatim,
#                 "other local Claude sessions on this machine" — in a
#                 Trinity container those are OTHER CONCURRENT EXECUTIONS of
#                 this agent sharing one HOME (up to max_parallel_tasks,
#                 possibly different callers' turns): an unaudited
#                 cross-execution channel outside Trinity's permissioned
#                 agent-to-agent path (`mcp__trinity__chat_with_agent`).
#                 Denying is the conservative direction; the recorded COST is
#                 the subagent-continuation facet, covered by running
#                 subagents in the foreground. One decision, both tools —
#                 "Lists agents you can SendMessage to" is the discovery
#                 half of the denied verb.
#   PushNotification  "This tool sends a desktop notification in the user's
#                 terminal."  There is no terminal and nobody attached.
#                 Trinity has `send_notification` and the operator queue,
#                 which reach a real person.
#   RemoteTrigger "Call the claude.ai remote-trigger API ... the OAuth token
#                 is added automatically."  It creates work on an external
#                 control plane that outlives this turn and is invisible to
#                 Trinity's observability, billing and operator surfaces.
PLATFORM_DENIED_TOOLS = (
    "ScheduleWakeup",
    "Workflow",
    "Monitor",
    "TaskOutput",
    "CronCreate",
    "CronList",
    "CronDelete",
    "SendMessage",
    "ListAgents",
    "PushNotification",
    "RemoteTrigger",
)

# Tools examined by the same audit and deliberately KEPT — data, not only
# prose, so the committed re-audit probe (scripts/dev/audit_headless_tools.py)
# can diff a live agent's init tool list against DENIED ∪ KEPT at the next CLI
# bump. The audited set has drifted once already (2.1.220 → 2.1.235 surfaced
# ListAgents and NotebookEdit), and `--disallowedTools` filters by NAME with
# an unknown name a SILENT no-op — a CLI rename re-offers a tool with no
# signal, and no PR workflow builds the base image, so CI cannot see it. The
# two name-independent nets are the prompt counterweight
# (platform_prompt_service: "Nothing survives the end of your turn") and
# #2467's turn_integrity flagging. Reasons per kept tool:
#
#   Task — the subagent launcher (model-visible as `Agent` on 2.1.235 while
#       the init array says `Task`; the two diverge). Denying it would remove
#       subagents entirely, and its background default is SAFE here:
#       `local_agent` is a waited type, so "you will be automatically
#       notified when it completes" is TRUE for subagents in this runtime.
#   TaskCreate/TaskGet/TaskList/TaskUpdate — the in-turn TASK LIST, not the
#       background registry (2.1.220 text: "create a structured task list for
#       your current coding session"; the name collides, the description does
#       not). On 2.1.235 they sit in the init array but are not model-visible
#       at all (the in-context deferred list carries only TaskOutput and
#       TaskStop) — kept anyway: costs nothing, correct if re-surfaced.
#   TaskStop — "Stops a running background task by its ID". Acts only within
#       the turn, and denying it would remove the one way to stop a runaway
#       background command the model itself started.
#   Bash — cannot be denied. Two of its 2.1.235 sentences are why the prompt
#       counterweight exists: "you will be notified when it completes" (false
#       here — the task is killed seconds after the turn ends), and "Long
#       leading `sleep` commands are blocked. To poll until a condition is
#       met, use Monitor with an until-loop" (routes to a tool this list
#       removes; the counterweight carries the working idiom).
#   NotebookEdit — single-cell .ipynb editor; pure in-turn, no promise.
#   EnterWorktree/ExitWorktree, DesignSync, ReportFindings, ToolSearch,
#       Skill, WebFetch/WebSearch, Read/Write/Edit — in-turn, no promise.
#
# NOTED, not acted on: `DesignSync` reaches claude.ai design projects with
# the user's login — the same "external control plane" concern as
# RemoteTrigger, but it makes no future-work promise, so it is outside this
# issue's frame and deliberately left alone rather than swept up.
PLATFORM_KEPT_TOOLS = (
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskStop",
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Skill",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "DesignSync",
    "EnterWorktree",
    "ExitWorktree",
    "ReportFindings",
)

# The CLI version the audit above was measured on — bump this ONLY together
# with a fresh probe run (scripts/dev/audit_headless_tools.py) against an
# agent on the new image. DENIED ∪ KEPT covered the init tool list exactly
# (11 + 19 = 30 built-ins) on this version.
AUDIT_CLI_VERSION = "2.1.235"


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
