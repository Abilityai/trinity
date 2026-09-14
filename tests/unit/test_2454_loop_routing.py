"""#2454 — "run a loop" must route to Trinity loops, never the harness's own.

A Trinity execution is a one-shot ``claude --print``. The Claude Code ``/loop``
skill paces itself with ``ScheduleWakeup``, which **returns success** in that
process and then never fires — so the agent truthfully narrates a loop that
does not exist, and nothing anywhere reports a failure. Two layers close it and
both are guarded here:

* **Mechanical** — every Claude spawn's ``--disallowedTools`` is built by
  ``merged_disallowed_tools``, which unions the operator's GUARD-003 list with
  ``PLATFORM_DENIED_TOOLS``. The site check is by DISCOVERY (walk the tree,
  find every spawn that passes the flag), not by naming the two files that
  pass it today — a third spawn site is exactly how this regresses.
* **Guidance** — a platform-injected section names the Trinity primitives and
  the harness affordances to avoid, at every prompt tier and every runtime.

The two layers share a vocabulary (the denied tool names), so the last test
derives the prompt's claims from the deny list rather than restating them: a
name added to one and not the other is the class of drift that produced this
bug in the first place.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_server.services._runtime_config import (
    PLATFORM_DENIED_TOOLS,
    merged_disallowed_tools,
)
from services import platform_prompt_service
from services.platform_prompt_service import (
    PLATFORM_INSTRUCTIONS,
    PromptTier,
    render_platform_instructions,
)

_AGENT_SERVER = (
    Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server"
)
SECTION_HEADING = "Repeating Work and Deferred Ticks"
RUNTIMES = ("claude-code", "codex", "gemini", "")


# ---------------------------------------------------------------------------
# Layer 1 — the merge helper
# ---------------------------------------------------------------------------

def test_platform_denials_always_present():
    """Even with no guardrails config at all."""
    for junk in ({}, {"disallowed_tools": None}, {"disallowed_tools": "Bash"}, None, []):
        merged = merged_disallowed_tools(junk)
        for name in PLATFORM_DENIED_TOOLS:
            assert name in merged, f"{name!r} missing for guardrails={junk!r}"


def test_schedule_wakeup_is_denied():
    """The specific mechanism #2454 is about — pinned by name, because the
    whole bug is that this tool answers 'scheduled' in a process that is about
    to exit."""
    assert "ScheduleWakeup" in PLATFORM_DENIED_TOOLS


def test_operator_entries_kept_and_ordered_first():
    merged = merged_disallowed_tools({"disallowed_tools": ["Bash", "WebFetch"]})
    assert merged[:2] == ["Bash", "WebFetch"]
    assert "ScheduleWakeup" in merged


def test_dedupes_without_dropping():
    """An operator who already denies the tool must not produce it twice in the
    CLI argument, and must not lose their other entries to the dedup."""
    merged = merged_disallowed_tools(
        {"disallowed_tools": ["ScheduleWakeup", "Bash", "ScheduleWakeup"]}
    )
    assert merged.count("ScheduleWakeup") == 1
    assert "Bash" in merged


def test_junk_entries_skipped_not_serialized():
    merged = merged_disallowed_tools({"disallowed_tools": ["  ", None, 7, " Bash "]})
    assert merged == ["Bash", *PLATFORM_DENIED_TOOLS]


# ---------------------------------------------------------------------------
# Layer 1 — every spawn site uses the merge (discovered, not enumerated)
# ---------------------------------------------------------------------------

def _functions_passing_the_flag() -> list[tuple[str, str]]:
    """(module path, function name) for every function that builds a
    ``--disallowedTools`` CLI argument anywhere under the agent server."""
    found: list[tuple[str, str]] = []
    for path in sorted(_AGENT_SERVER.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            passes_flag = any(
                isinstance(sub, ast.Constant) and sub.value == "--disallowedTools"
                for sub in ast.walk(node)
            )
            if passes_flag:
                found.append((str(path.relative_to(_AGENT_SERVER)), node.name))
    return found


def test_flag_sites_exist():
    """Guard against a vacuous pass: if the flag is renamed or the spawn sites
    move, the discovery below would silently police nothing."""
    sites = _functions_passing_the_flag()
    assert len(sites) >= 2, f"expected the chat + task spawns, found {sites}"


@pytest.mark.parametrize("rel_path,func_name", _functions_passing_the_flag())
def test_every_spawn_site_merges_platform_denials(rel_path, func_name):
    tree = ast.parse((_AGENT_SERVER / rel_path).read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            calls = {
                sub.func.id
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            }
            assert "merged_disallowed_tools" in calls, (
                f"{rel_path}::{func_name} builds --disallowedTools without "
                "merged_disallowed_tools — the platform denials are dropped for "
                "every turn it spawns"
            )
            return
    pytest.fail(f"{func_name} vanished from {rel_path}")


# ---------------------------------------------------------------------------
# Layer 2 — the injected guidance
# ---------------------------------------------------------------------------

def test_section_registered_and_never_dropped():
    assert SECTION_HEADING in platform_prompt_service._KNOWN_SECTION_HEADINGS
    assert SECTION_HEADING not in platform_prompt_service._MINIMAL_DROP_SECTIONS
    assert SECTION_HEADING in platform_prompt_service._ALWAYS_SECTIONS


@pytest.mark.parametrize("tier", list(PromptTier))
def test_guidance_survives_every_tier(tier):
    """MINIMAL drops the tool-usage sections. This one must not go with them:
    its load-bearing half is a negative rule about tools that are not ours, so
    there is no tool description for it to fall back to."""
    rendered = render_platform_instructions(tier)
    assert SECTION_HEADING in rendered
    assert "run_agent_loop" in rendered
    assert "set_reminder" in rendered


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_negative_rule_survives_runtime_adaptation(runtime):
    """The Codex adapter rewrites mcp__trinity__ tokens; the harness names it
    must leave alone."""
    prompt = platform_prompt_service.get_platform_system_prompt(runtime)
    assert SECTION_HEADING in prompt
    assert "ScheduleWakeup" in prompt
    assert "/loop" in prompt
    assert "run_agent_loop" in prompt
    assert "set_reminder" in prompt


def test_prompt_and_deny_list_share_one_vocabulary():
    """Derived, not restated: a tool added to the deny list without a word in
    the guidance leaves the agent facing a refusal it has no way to interpret,
    and guidance naming a tool the platform does not deny is a rule with no
    backstop. Both directions are drift the section exists to prevent."""
    for name in PLATFORM_DENIED_TOOLS:
        assert name in PLATFORM_INSTRUCTIONS, (
            f"{name!r} is denied at spawn but never explained in the prompt"
        )
