"""#2468 — nothing in a one-shot run may promise a second turn.

`ScheduleWakeup` (#2454) was one member of a family. Every tool the CLI offers
on Trinity's headless path was read out of live runs by its OWN description and
judged on one question: does it promise a future event, a peer session, or work
that outlives the turn? A Trinity execution is one `claude --print` process
that exits when the model stops writing, so each of those promises is false
here — and a tool result asserting a false fact is worse than a missing tool,
because the model plans around it and then reports success (#2467 is the
measured incident shape).

Audit lineage: first run on claude 2.1.220 (closed reference PR #2472),
re-measured in full on **2.1.235** — the version the fleet image ships — which
changed one membership (`ListAgents` added) and rewrote two rationales
(`SendMessage`, `Monitor`). `AUDIT_CLI_VERSION` records this as data.

What is pinned here is the DISCIPLINE plus the measured snapshot:

  * every denied tool carries its reason in the source, so the list cannot grow
    by someone adding a name they merely mistrust (weak matcher, by design: any
    comment line naming the tool passes — the pin is "a reason exists where a
    reader will look", not prose quality; each pinned verbatim fragment must
    stay on ONE comment line or `_comment_prose` stops matching it);
  * the tools deliberately KEPT are data (`PLATFORM_KEPT_TOOLS`), because
    over-denying is the way this change breaks an agent and it breaks it
    silently at spawn — and DENIED ∪ KEPT must exactly cover the init list
    measured on `AUDIT_CLI_VERSION`;
  * prompt text and deny list share one vocabulary in BOTH directions (the
    forward direction lives in test_2454_loop_routing; the reverse — every
    name the prompt claims is removed must actually be denied — lives here);
  * the counterweight section exists and says what it must, since the one
    promise Trinity cannot delete is `Bash`'s own "You will be notified when
    it completes" — no tool description can correct a DIFFERENT tool's
    description. It stays truthful where the blunt claim would be false:
    subagents (`local_agent`) ARE waited for, and resumable surfaces do get
    later turns.
"""
from __future__ import annotations

import importlib.util
import re
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
    here. The module itself is stdlib-only, which is why this works. No
    sys.modules writes (sibling suites evict `agent_server*`).
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
    parsed constants rather than from the text."""
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
    cannot. These fragments are verbatim from live headless runs on 2.1.235
    (each cross-checked against the 2.1.220 capture where it existed)."""
    prose = _comment_prose(source)
    for quoted in (
        "<task-notification>",                        # Workflow, TaskOutput
        "notifications arrive in the chat",           # Monitor
        "enqueued at a future time",                  # CronCreate
        "another agent",                              # SendMessage
        "desktop notification",                       # PushNotification
        "remote-trigger API",                         # RemoteTrigger
        "you are re-invoked",                         # ScheduleWakeup (2.1.235)
        "other local Claude sessions on this machine",  # ListAgents (2.1.235)
        "use Monitor with an until-loop",             # Bash routes polling to a denied tool
        "nothing is written to disk",                 # CronCreate's own store admission
    ):
        assert quoted in prose, f"the audit no longer quotes {quoted!r}"


def test_the_kept_tools_are_named_too(rc, source):
    """Half the audit is what was NOT denied. `TaskCreate` reads like the
    background-task registry and is in fact the in-turn task list — the
    description settles it, and a later reader must not have to re-derive
    that from the name."""
    prose = _comment_prose(source)
    assert "KEPT" in prose
    for kept in ("TaskCreate", "TaskStop", "Bash", "DesignSync", "NotebookEdit"):
        assert kept in prose, f"{kept} is neither denied nor explained"
    # And KEPT is DATA, not only prose — the re-audit probe diffs against it.
    for kept in ("Task", "TaskStop", "Bash", "NotebookEdit"):
        assert kept in rc.PLATFORM_KEPT_TOOLS


def test_denied_and_kept_are_disjoint_and_cover_the_measured_offering(rc):
    """DENIED ∪ KEPT must exactly cover the init tool list measured on
    AUDIT_CLI_VERSION — that exhaustiveness is what makes the audit an audit
    and not a sampling. On a CLI bump: re-run
    scripts/dev/audit_headless_tools.py, re-decide any new names, update the
    snapshot below AND AUDIT_CLI_VERSION together."""
    denied, kept = set(rc.PLATFORM_DENIED_TOOLS), set(rc.PLATFORM_KEPT_TOOLS)
    assert not (denied & kept), f"a tool cannot be both denied and kept: {denied & kept}"
    assert rc.AUDIT_CLI_VERSION == "2.1.235", (
        "AUDIT_CLI_VERSION moved — re-measure the init list on the new CLI and "
        "update OFFERED_ON_AUDITED_CLI in this test in the same change"
    )
    OFFERED_ON_AUDITED_CLI = {
        "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "Edit",
        "EnterWorktree", "ExitWorktree", "ListAgents", "Monitor",
        "NotebookEdit", "PushNotification", "Read", "RemoteTrigger",
        "ReportFindings", "ScheduleWakeup", "SendMessage", "Skill", "Task",
        "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
        "TaskUpdate", "ToolSearch", "WebFetch", "WebSearch", "Workflow",
        "Write",
    }
    assert denied | kept == OFFERED_ON_AUDITED_CLI, (
        f"audit does not exactly cover the measured offering — "
        f"unaudited: {sorted(OFFERED_ON_AUDITED_CLI - denied - kept)}, "
        f"phantom: {sorted((denied | kept) - OFFERED_ON_AUDITED_CLI)}"
    )


# ---------------------------------------------------------------------------
# Over-denying is how this change breaks an agent
# ---------------------------------------------------------------------------

CORE_TOOLS = (
    "Bash", "Read", "Write", "Edit", "NotebookEdit", "Task", "TaskCreate",
    "TaskGet", "TaskList", "TaskUpdate", "TaskStop", "Skill", "ToolSearch",
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
    """The audited membership, as a floor: removing any of these un-does a
    recorded decision — if that is intended, the audit prose, the prompt's
    family enumeration, and this list move in the same change."""
    for name in (
        "ScheduleWakeup", "Workflow", "Monitor", "TaskOutput", "CronCreate",
        "CronList", "CronDelete", "SendMessage", "ListAgents",
        "PushNotification", "RemoteTrigger",
    ):
        assert name in rc.PLATFORM_DENIED_TOOLS, f"{name} left the deny list"


def test_the_deny_list_is_a_tuple_of_plain_names(rc):
    """Plain names only. A rule with a specifier (`Skill(loop)`) is not a
    contract this repo can pin while the base image tracks the latest CLI, and
    an unparseable rule risks the whole --disallowedTools argument on every
    turn — a fleet outage traded for one more layer (#2454). An internal space
    survives the strip-check but is a silent unknown-name no-op to the CLI."""
    for tup in (rc.PLATFORM_DENIED_TOOLS, rc.PLATFORM_KEPT_TOOLS):
        assert isinstance(tup, tuple)
        for name in tup:
            assert isinstance(name, str) and name.strip() == name and name
            assert "(" not in name and "," not in name and " " not in name


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
# Prompt and deny list: one vocabulary, both directions
# ---------------------------------------------------------------------------
# Forward (every denied name appears in the prompt) is pinned by
# test_2454_loop_routing.py::test_prompt_and_deny_list_share_one_vocabulary.
# The reverse lives here: every tool the prompt CLAIMS is removed must
# actually be on the deny list, or a tuple shrink leaves the prompt asserting
# a deny that no longer exists (the 2026-08-31 two-vocabularies ledger entry:
# derive the correspondence, never restate the literals).

def test_prompt_family_claims_only_denied_names(rc):
    anchor = "whole family of harness tools"
    assert anchor in PLATFORM_INSTRUCTIONS, (
        "the family-enumeration paragraph moved or was reworded — update this "
        "test's anchor together with it"
    )
    start = PLATFORM_INSTRUCTIONS.index(anchor)
    paragraph = PLATFORM_INSTRUCTIONS[start:].split("\n\n", 1)[0]
    claimed = set(re.findall(r"`([A-Z][A-Za-z]+)`", paragraph))
    assert len(claimed) >= 9, (
        f"family paragraph enumerates only {sorted(claimed)} — extraction "
        "anchor drifted?"
    )
    not_denied = claimed - set(rc.PLATFORM_DENIED_TOOLS)
    assert not_denied == set(), (
        f"the prompt claims these are removed but the deny list disagrees: "
        f"{sorted(not_denied)}"
    )


# ---------------------------------------------------------------------------
# The counterweight for the promise we cannot delete
# ---------------------------------------------------------------------------

def test_section_registered_and_never_dropped():
    assert SECTION_HEADING in platform_prompt_service._KNOWN_SECTION_HEADINGS
    assert SECTION_HEADING not in platform_prompt_service._MINIMAL_DROP_SECTIONS
    # Derived membership (_KNOWN − _MINIMAL_DROP), never hand-listed.
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


def _section_text() -> str:
    section = PLATFORM_INSTRUCTIONS[PLATFORM_INSTRUCTIONS.index(SECTION_HEADING):]
    return section[: section.index("\n### ")] if "\n### " in section else section


def test_it_says_the_work_is_killed_not_merely_unwatched():
    """The distinction is the whole point: 'nobody will look at it' invites
    fire-and-forget, 'it dies' does not."""
    section = _section_text()
    assert "killed" in section
    assert "foreground" in section


def test_it_stays_truthful_about_subagents_and_waiting():
    """Two claims the blunt version would get WRONG, both measured: subagent
    (`local_agent`) tasks ARE waited for by `claude --print` (see
    _NON_WAITED_BG_TASK_TYPES in headless_executor.py), so the kill-claim must
    stay scoped to commands; and Bash both blocks long leading sleeps AND
    routes polling to the denied Monitor tool, so the section must name a wait
    idiom that actually works here."""
    section = _section_text()
    assert "Subagents" in section, "the truthful subagents-are-waited clause is gone"
    assert "until" in section and "timeout" in section, (
        "the sanctioned foreground wait idiom is gone — with long sleeps "
        "blocked and Monitor denied, the prompt must say what DOES work"
    )
