"""#2468 — nothing in a one-shot run may promise a second turn.

`ScheduleWakeup` (#2454) was one member of a family. Every tool the CLI offers
on Trinity's headless path was read out of a live run by its OWN description
(`ToolSearch select:...`, claude 2.1.220) and judged on one question: does it
promise a future event, a peer session, or work that outlives the turn? A
Trinity execution is one `claude --print` process that exits when the model
stops writing, so each of those promises is false here — and a tool result
asserting a false fact is worse than a missing tool, because the model plans
around it and then reports success.

What is pinned here is the DISCIPLINE, not the membership:

  * every denied tool carries its reason in the source, so the list cannot grow
    by someone adding a name they merely mistrust;
  * the tools deliberately KEPT are named, because over-denying is the way this
    change breaks an agent and it would break it silently;
  * the prompt counterweight exists and says what it must, since the one
    promise Trinity cannot delete is `Bash`'s own "You will be notified when it
    completes" — no tool description can correct a DIFFERENT tool's
    description.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import (
    PLATFORM_INSTRUCTIONS,
    PromptTier,
    render_platform_instructions,
)

_REPO = Path(__file__).resolve().parents[2]
_RUNTIME_CONFIG = (
    _REPO / "docker" / "base-image" / "agent_server" / "services" / "_runtime_config.py"
)
SECTION_HEADING = "Nothing survives the end of your turn"
RUNTIMES = ("claude-code", "codex", "gemini", "")

pytestmark = pytest.mark.unit


def _load_runtime_config():
    """Load the agent-server module by path.

    Importing `agent_server.services...` normally drags in `agent_server`'s
    package __init__, which imports FastAPI — present in the agent image, not
    here. The module itself is stdlib-only, which is why this works.
    """
    spec = importlib.util.spec_from_file_location("_rc_2468", _RUNTIME_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rc():
    return _load_runtime_config()


@pytest.fixture(scope="module")
def source() -> str:
    return _RUNTIME_CONFIG.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The audit is recorded, not remembered
# ---------------------------------------------------------------------------

def _comment_prose(source: str) -> str:
    """Only the comment lines. Deliberately the inverse of every other guard in
    this repo, which strips comments to avoid testing prose: here the prose IS
    the deliverable (AC 4 asks for a recorded reason per tool), so it is what
    gets checked — and the code half is checked separately below, from the
    parsed constant rather than from the text."""
    return "\n".join(
        line for line in source.splitlines() if line.strip().startswith("#")
    )


def test_every_denied_tool_has_a_recorded_reason(rc, source):
    prose = _comment_prose(source)
    missing = [name for name in rc.PLATFORM_DENIED_TOOLS if name not in prose]
    assert missing == [], (
        f"denied with no reason in the source: {missing}. The audit is the "
        "deliverable (#2468 AC 4) — a name added on suspicion alone is how a "
        "deny list grows until it breaks an agent nobody can debug"
    )


def test_the_audit_quotes_the_tools_own_words(source):
    """A reason that paraphrases can be wrong; the CLI's own description
    cannot. These fragments are verbatim from a live headless run."""
    prose = _comment_prose(source)
    for quoted in (
        "<task-notification>",          # Workflow, TaskOutput
        "notifications arrive in the chat",   # Monitor
        "enqueued at a future time",     # CronCreate
        "another agent",                 # SendMessage
        "desktop notification",          # PushNotification
        "remote-trigger API",            # RemoteTrigger
    ):
        assert quoted in prose, f"the audit no longer quotes {quoted!r}"


def test_the_kept_tools_are_named_too(source):
    """Half the audit is what was NOT denied. `TaskCreate` reads like the
    background-task registry and is in fact the in-turn task list — the
    description settles it, and a later reader must not have to re-derive
    that from the name."""
    prose = _comment_prose(source)
    assert "KEPT" in prose
    for kept in ("TaskCreate", "TaskStop", "Bash", "DesignSync"):
        assert kept in prose, f"{kept} is neither denied nor explained"


# ---------------------------------------------------------------------------
# Over-denying is how this change breaks an agent
# ---------------------------------------------------------------------------

CORE_TOOLS = (
    "Bash", "Read", "Write", "Edit", "Task", "TaskCreate", "TaskGet",
    "TaskList", "TaskUpdate", "TaskStop", "Skill", "ToolSearch",
    "WebFetch", "WebSearch", "Glob", "Grep",
)


@pytest.mark.parametrize("tool", CORE_TOOLS)
def test_core_tools_are_never_denied(rc, tool):
    assert tool not in rc.PLATFORM_DENIED_TOOLS, (
        f"{tool} is on the platform deny list — this list may only remove "
        "tools that promise something the runtime cannot deliver, and denying "
        "a working tool fails silently at spawn on every agent (#2468)"
    )


def test_the_family_is_covered(rc):
    """Non-vacuity, and the point of the issue: ScheduleWakeup alone was never
    the fix. If this shrinks back to one entry, the audit was undone."""
    assert "ScheduleWakeup" in rc.PLATFORM_DENIED_TOOLS
    assert len(rc.PLATFORM_DENIED_TOOLS) >= 5


def test_the_deny_list_is_a_tuple_of_plain_names(rc):
    """Plain names only. A rule with a specifier (`Skill(loop)`) is not a
    contract this repo can pin while the base image tracks the latest CLI, and
    an unparseable rule risks the whole --disallowedTools argument on every
    turn — a fleet outage traded for one more layer (#2454)."""
    assert isinstance(rc.PLATFORM_DENIED_TOOLS, tuple)
    for name in rc.PLATFORM_DENIED_TOOLS:
        assert isinstance(name, str) and name.strip() == name and name
        assert "(" not in name and "," not in name


def test_operator_guardrails_still_merge(rc):
    """AC 5 — platform defaults must not clobber per-agent entries."""
    merged = rc.merged_disallowed_tools({"disallowed_tools": ["Bash", "WebFetch"]})
    assert merged[:2] == ["Bash", "WebFetch"]
    for name in rc.PLATFORM_DENIED_TOOLS:
        assert name in merged
    # An operator who already denies one of ours must not get it twice.
    dup = rc.merged_disallowed_tools({"disallowed_tools": ["Monitor"]})
    assert dup.count("Monitor") == 1


# ---------------------------------------------------------------------------
# The counterweight for the promise we cannot delete
# ---------------------------------------------------------------------------

def test_section_registered_and_never_dropped():
    assert SECTION_HEADING in platform_prompt_service._KNOWN_SECTION_HEADINGS
    assert SECTION_HEADING not in platform_prompt_service._MINIMAL_DROP_SECTIONS
    assert SECTION_HEADING in platform_prompt_service._ALWAYS_SECTIONS


@pytest.mark.parametrize("tier", list(PromptTier))
def test_the_counterweight_survives_every_tier(tier):
    """It corrects a DIFFERENT tool's description, so unlike the three
    tool-usage sections there is nowhere for it to fall back to."""
    assert SECTION_HEADING in render_platform_instructions(tier)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_the_counterweight_survives_runtime_adaptation(runtime):
    prompt = platform_prompt_service.get_platform_system_prompt(runtime)
    assert SECTION_HEADING in prompt
    assert "You will be notified" in prompt, (
        "the section no longer quotes the promise it exists to contradict — "
        "an agent that has read the Bash tool result needs to recognise it"
    )
    assert "set_reminder" in prompt, "no working alternative is offered"


def test_it_says_the_work_is_killed_not_merely_unwatched():
    """The distinction is the whole point: 'nobody will look at it' invites
    fire-and-forget, 'it dies' does not."""
    section = PLATFORM_INSTRUCTIONS[PLATFORM_INSTRUCTIONS.index(SECTION_HEADING):]
    section = section[: section.index("\n### ")] if "\n### " in section else section
    assert "killed" in section
    assert "foreground" in section
