"""#1402 — async operator human-gate contract sentinel tests.

The MAX_REDELIVERY cap + poison-park *mechanism* shipped in Phase 3 (#1550,
``services/lease_reaper_service.py``); #1402's remaining deliverable is the
*authored contract* agents receive: fire-and-park (never block a turn waiting
on a human), ask-before-irreversible-actions, and collision-safe derived
request ids. That contract lives in the platform system prompt
(``platform_prompt_service.PLATFORM_INSTRUCTIONS`` → "Operator Communication"
+ the ``_mode_guidance`` task-mode carve-out) with a synced reference copy in
``config/trinity-meta-prompt/prompt.md`` and an authoring section in
``docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md``.

These tests lock:
  * SENTINELS — stable short phrases that carry the contract. Copyedit freely
    around them, but keep the literal sentinel phrases (or update this file
    AND both doc copies together).
  * The OLD blocking-friendly wording is GONE (a prompt that says "check for
    responses by reading the file" with no turn framing invites in-turn
    polling — the exact anti-pattern #1402 exists to kill).
  * The Operator Communication section renders byte-identically across
    runtimes — it is deliberately MCP-tool-name-free, so the Codex
    ``mcp__trinity__``-stripping transform (#1187) cannot fork it.
"""

from __future__ import annotations

from pathlib import Path

import re

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import (
    PLATFORM_INSTRUCTIONS,
    ExecutionContext,
    _mode_guidance,
    compose_system_prompt,
    get_platform_system_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
META_PROMPT_PATH = REPO_ROOT / "config" / "trinity-meta-prompt" / "prompt.md"
AGENT_GUIDE_PATH = REPO_ROOT / "docs" / "TRINITY_COMPATIBLE_AGENT_GUIDE.md"

# Every runtime build must carry the contract (the section is runtime-agnostic).
RUNTIMES = ("claude-code", "codex", "gemini-cli", "")

# Test-locked sentinel phrases (see module docstring). Present in BOTH authored
# copies (PLATFORM_INSTRUCTIONS and config/trinity-meta-prompt/prompt.md).
SENTINELS = (
    "fire-and-park, never block-and-wait",
    "End your turn.",
    "Ask before irreversible actions",
    "Request IDs must be globally unique",
    "approval-{execution_id}",
    "expires_at",
)

# The pre-#1402 wording that invited in-turn polling. Must not reappear.
OLD_BLOCKING_PHRASE = "by reading the file and looking for items"

# Section boundary inside PLATFORM_INSTRUCTIONS. The START heading survives
# every runtime transform (the Codex adapter only rewrites mcp__trinity__
# tokens and prepends an orientation block). The END is DERIVED — the next
# ``### `` heading — rather than named: a hardcoded successor makes an
# unrelated section inserted after this one silently widen the slice, so the
# runtime-identity and MCP-name-free assertions below start policing text that
# is not the operator contract at all (#2454 tripped exactly that).
SECTION_START = "### Operator Communication"
_NEXT_HEADING = re.compile(r"^### ", re.MULTILINE)


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    """Pin the operator-configurable ``trinity_prompt`` to empty so these tests
    exercise only the authored constant, independent of DB state."""
    monkeypatch.setattr(
        platform_prompt_service.db, "get_setting_value", lambda *a, **k: None
    )


def _operator_section(prompt: str) -> str:
    start = prompt.index(SECTION_START)
    nxt = _NEXT_HEADING.search(prompt, start + len(SECTION_START))
    assert nxt is not None, "operator section must be followed by another ### section"
    return prompt[start:nxt.start()]


# ---------------------------------------------------------------------------
# Contract present, old wording gone — every runtime build
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", RUNTIMES)
def test_contract_sentinels_present(runtime):
    prompt = get_platform_system_prompt(runtime)
    for sentinel in SENTINELS:
        assert sentinel in prompt, f"missing sentinel {sentinel!r} for runtime={runtime!r}"


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_old_blocking_wording_gone(runtime):
    assert OLD_BLOCKING_PHRASE not in get_platform_system_prompt(runtime)


def test_source_constant_carries_contract():
    """The authored constant itself (not just a rendered build) carries the
    contract and has dropped the old wording."""
    for sentinel in SENTINELS:
        assert sentinel in PLATFORM_INSTRUCTIONS
    assert OLD_BLOCKING_PHRASE not in PLATFORM_INSTRUCTIONS


def test_stale_dateserial_id_only_as_negative_example():
    """The old collision-prone example id survives ONLY inside the 'never do
    this' sentence, not as the example request id."""
    section = _operator_section(PLATFORM_INSTRUCTIONS)
    assert '"id": "req-' not in section
    assert '"id": "approval-<execution_id>' in section


# ---------------------------------------------------------------------------
# Runtime invariance — the section must never fork per runtime
# ---------------------------------------------------------------------------

def test_operator_section_identical_across_runtimes():
    """The Operator Communication section is MCP-tool-name-free by design, so
    every runtime build must render it byte-identically. If this fails, someone
    added an ``mcp__trinity__`` reference to the section — keep it file-protocol
    only (#1402 / #1187)."""
    baseline = _operator_section(get_platform_system_prompt("claude-code"))
    for runtime in RUNTIMES:
        assert _operator_section(get_platform_system_prompt(runtime)) == baseline


def test_operator_section_is_mcp_name_free():
    assert "mcp__trinity__" not in _operator_section(PLATFORM_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Task-mode guidance carve-out (the contradiction fix)
# ---------------------------------------------------------------------------

def test_task_mode_guidance_carries_park_carveout():
    """Task mode says "execute to completion — do not ask clarifying questions";
    without the carve-out that directly contradicts parking an approval before
    an irreversible action (#1402 review finding A3)."""
    guidance = _mode_guidance("task")
    assert "fire-and-park" in guidance
    assert "operator queue" in guidance
    assert "never block" in guidance
    # The autonomous baseline is preserved, not replaced.
    assert "execute to completion" in guidance


def test_chat_mode_guidance_unchanged():
    guidance = _mode_guidance("chat")
    assert "clarifying questions" in guidance
    assert "fire-and-park" not in guidance


def test_composed_task_prompt_carries_carveout():
    """End-to-end: a composed task-mode prompt (the string a scheduled push
    dispatch actually sends) contains both the section contract and the
    mode-guidance carve-out. Explicit collaborators/platform_url skip the DB
    resolution path."""
    composed = compose_system_prompt(
        ExecutionContext(
            agent_name="a1",
            mode="task",
            triggered_by="schedule",
            collaborators=[],
            platform_url="",
        ),
        runtime="claude-code",
    )
    assert "fire-and-park, never block-and-wait" in composed
    assert "park an approval request in the operator queue" in composed


# ---------------------------------------------------------------------------
# Synced copies — meta-prompt reference file and the agent guide
# ---------------------------------------------------------------------------

def test_meta_prompt_reference_copy_in_sync():
    """config/trinity-meta-prompt/prompt.md is a dead-but-present reference copy
    (its injection path was removed in #136). Keep its Operator Communication
    text carrying the same contract so the next contributor can't resurrect the
    old blocking wording from it."""
    text = META_PROMPT_PATH.read_text(encoding="utf-8")
    for sentinel in SENTINELS:
        assert sentinel in text, f"meta-prompt copy missing sentinel {sentinel!r}"
    assert OLD_BLOCKING_PHRASE not in text


def test_agent_guide_documents_async_contract():
    text = AGENT_GUIDE_PATH.read_text(encoding="utf-8")
    assert "fire-and-park" in text
    assert "never block-and-wait" in text
    assert "approval-{execution_id}" in text
    # Honor-system framing must be stated, not implied (#1402 review S1).
    assert "not a security boundary" in text
