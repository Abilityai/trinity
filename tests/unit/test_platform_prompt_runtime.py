"""Runtime-aware platform prompt tests (#1187 F-MCP).

``PLATFORM_INSTRUCTIONS`` documents the Trinity MCP tools with Claude Code's
``mcp__trinity__<tool>`` naming. Codex auto-discovers MCP tools from the
configured ``trinity`` server and calls them by their bare names — the
Claude-only prefix makes a Codex model emit ``mcp__trinity`` → "unknown MCP
server". So the platform prompt must be runtime-aware:

  * ``runtime="codex"`` → no ``mcp__trinity__`` prefix anywhere, plus a short
    Codex-specific orientation note.
  * ``runtime="claude-code"`` (and Gemini / unknown / default) → the canonical
    text, unchanged.
"""

from __future__ import annotations

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import (
    PLATFORM_INSTRUCTIONS,
    compose_system_prompt,
    get_platform_system_prompt,
)


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    """Pin the operator-configurable ``trinity_prompt`` to empty so these tests
    exercise only the runtime branching, independent of DB state."""
    monkeypatch.setattr(
        platform_prompt_service.db, "get_setting_value", lambda *a, **k: None
    )


# ---------------------------------------------------------------------------
# get_platform_system_prompt
# ---------------------------------------------------------------------------

def test_claude_prompt_keeps_mcp_trinity_prefix():
    """The default (Claude) prompt is unchanged — it still documents the tools
    with the canonical mcp__trinity__ prefix."""
    prompt = get_platform_system_prompt("claude-code")
    assert "mcp__trinity__list_agents" in prompt
    assert "mcp__trinity__chat_with_agent" in prompt
    assert "mcp__trinity__share_file" in prompt
    assert "mcp__trinity__write_user_memory" in prompt
    # No-arg call preserves the historical default behavior.
    assert get_platform_system_prompt() == prompt


def test_codex_prompt_omits_mcp_trinity_prefix():
    """The Codex prompt must contain NO mcp__trinity__ token anywhere — that
    prefix is what made Codex emit `unknown MCP server`."""
    prompt = get_platform_system_prompt("codex")
    assert "mcp__trinity__" not in prompt
    # The bare tool names survive so the model still knows what to call.
    assert "list_agents" in prompt
    assert "chat_with_agent" in prompt
    assert "share_file" in prompt
    assert "write_user_memory" in prompt


def test_codex_prompt_includes_codex_orientation():
    """A Codex-specific orientation note is present so the model knows the
    `trinity` MCP server is configured and tools are called by bare name."""
    prompt = get_platform_system_prompt("codex")
    assert "Codex" in prompt
    assert "trinity" in prompt  # references the configured MCP server name
    # The claude prompt must NOT carry the Codex-only orientation.
    assert "Codex" not in get_platform_system_prompt("claude-code")


def test_unknown_and_gemini_runtimes_keep_claude_naming():
    """Gemini and any unrecognized runtime fall back to the canonical Claude
    naming (the plan's `default claude-code` behavior)."""
    for runtime in ("gemini-cli", "gemini", "something-new", ""):
        prompt = get_platform_system_prompt(runtime)
        assert "mcp__trinity__list_agents" in prompt


# ---------------------------------------------------------------------------
# compose_system_prompt threads runtime through
# ---------------------------------------------------------------------------

def test_compose_threads_runtime_to_platform_prompt():
    composed = compose_system_prompt(runtime="codex")
    assert "mcp__trinity__" not in composed


def test_compose_default_runtime_is_claude():
    composed = compose_system_prompt()
    assert "mcp__trinity__list_agents" in composed


# ---------------------------------------------------------------------------
# The source constant itself is Claude-flavored (transformation is non-mutating)
# ---------------------------------------------------------------------------

def test_source_constant_unchanged_after_codex_render():
    """Rendering the Codex prompt must not mutate the shared module constant."""
    before = PLATFORM_INSTRUCTIONS
    get_platform_system_prompt("codex")
    assert PLATFORM_INSTRUCTIONS == before
    assert "mcp__trinity__" in PLATFORM_INSTRUCTIONS


# ---------------------------------------------------------------------------
# OpenCode (#2981): tools are `trinity_<tool>`, not bare and not Claude-prefixed
# ---------------------------------------------------------------------------

def test_opencode_prompt_rewrites_prefix_to_server_id():
    prompt = get_platform_system_prompt("opencode")
    assert "mcp__trinity__" not in prompt
    assert "trinity_list_agents" in prompt
    assert "trinity_chat_with_agent" in prompt


def test_opencode_orientation_present_only_for_opencode():
    heading = "## MCP Tools (OpenCode runtime)"
    assert get_platform_system_prompt("opencode").startswith(heading)
    assert get_platform_system_prompt("OpenCode").startswith(heading)
    for runtime in ("claude-code", "codex", "gemini-cli", ""):
        assert heading not in get_platform_system_prompt(runtime)


def test_opencode_orientation_lists_every_codex_tool_prefixed():
    """Derived from the Codex note: every tool it names, rendered trinity_<tool>."""
    import re

    codex_tools = re.findall(r"`([a-z_]+)`", platform_prompt_service._CODEX_MCP_ORIENTATION)
    codex_tools = [t for t in codex_tools if t != "trinity"]
    assert codex_tools, "Codex orientation lists no tools"
    orientation = platform_prompt_service._OPENCODE_MCP_ORIENTATION
    for tool in codex_tools:
        assert f"`trinity_{tool}`" in orientation, tool
    assert "mcp__trinity__" not in orientation
    assert "`/<skill> [args]`" in orientation


def test_opencode_gets_the_unhooked_path_sentence():
    assert "opencode" not in platform_prompt_service._GUARDRAIL_HOOK_RUNTIMES
    prompt = get_platform_system_prompt("opencode")
    assert platform_prompt_service._PATH_DENY_SENTENCE_HOOKED not in prompt


def test_source_constant_unchanged_after_opencode_render():
    before = PLATFORM_INSTRUCTIONS
    get_platform_system_prompt("opencode")
    assert PLATFORM_INSTRUCTIONS == before
    assert "mcp__trinity__" in PLATFORM_INSTRUCTIONS
