"""Every MCP tool the platform prompt names is a tool the MCP server publishes.

The platform prompt (``services/platform_prompt_service.py``) teaches agents
tool names on every turn. A tool that is renamed or removed while the prompt
still names it fails silently: the agent calls a name that does not exist, or
is told a capability is there when it is not. Only the delegation contract was
pinned against the real tool surface (``test_ent568_delegation_contract.py``);
this suite extends that to every prompt fragment in the module.

Two checks:

  * every ``mcp__trinity__<name>`` anywhere in the module source — the
    canonical spelling, covering the constant and every ``build_*`` prompt;
  * every backticked snake_case token in the rendered platform instructions is
    either a published tool or listed in ``NOT_TOOLS`` below. A bare tool name
    (``ask_operator``) is indistinguishable from a field name by shape, so the
    list is what makes a renamed bare tool fail: its old name is then neither a
    tool nor classified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import render_platform_instructions
from services.prompt_tier import PromptTier

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = REPO_ROOT / "src" / "mcp-server" / "src"
PROMPT_MODULE = REPO_ROOT / "src" / "backend" / "services" / "platform_prompt_service.py"

_TOOL_NAME = re.compile(r'(?<![\w.])name:\s*"([a-z][a-z0-9_]*)"')
_PREFIXED = re.compile(r"mcp__trinity__([a-z][a-z0-9_]*)")
# A backticked snake_case identifier, optionally written as a call.
_BACKTICKED = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?:\([^`]*\))?`")

# Backticked snake_case tokens in the platform instructions that are NOT tool
# names, grouped by what they are. Adding a token here is a statement that it
# is not a tool; a token that is a tool must not be listed.
NOT_TOOLS = frozenset({
    # tool arguments and payload / response fields
    "agent_name", "audience_email", "display_hint", "execution_id", "expires_at",
    "fan_out_id", "report_type", "request_id", "responded_at", "response_text",
    "retry_after_seconds", "supersedes_expired",
    "on_ending", "visibility_note", "wake_reason",
    # statuses, refusals and events a tool answers with
    "agent_busy", "fan_out_timeout", "pending_approval", "queued_timeout",
    "replaces_ended", "already_pending", "ask_ended", "invalid_id",
    "invalid_options", "invalid_title", "max_pending", "queue_full",
    "rate_limited",
    # harness and template names
    "data_paths",
})


def _published_tools() -> set[str]:
    names: set[str] = set()
    for path in sorted(MCP_SRC.rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        names |= set(_TOOL_NAME.findall(path.read_text(encoding="utf-8")))
    return names


@pytest.fixture(scope="module")
def tools() -> set[str]:
    found = _published_tools()
    assert len(found) >= 50, f"found only {len(found)} tool definitions — the scan is broken"
    return found


def test_every_prefixed_tool_name_in_the_prompt_module_is_published(tools):
    named = set(_PREFIXED.findall(PROMPT_MODULE.read_text(encoding="utf-8")))
    assert len(named) >= 10, f"found only {len(named)} prefixed names — the scan is broken"
    missing = sorted(named - tools)
    assert missing == [], (
        f"the platform prompt names {missing}, but no MCP tool has that name — "
        "update the prompt in the same change that renames or removes a tool"
    )


@pytest.mark.parametrize("tier", list(PromptTier))
def test_every_backticked_token_is_a_tool_or_classified(tools, tier):
    tokens = set(_BACKTICKED.findall(render_platform_instructions(tier)))
    unknown = sorted(tokens - tools - NOT_TOOLS)
    assert unknown == [], (
        f"the platform instructions name {unknown}: neither a published MCP tool "
        "nor listed in NOT_TOOLS. A renamed or removed tool lands here — fix the "
        "prompt; a new field or status name — add it to NOT_TOOLS"
    )


def test_not_tools_lists_no_published_tool(tools):
    assert sorted(NOT_TOOLS & tools) == [], "a published tool is listed as not-a-tool"


def test_not_tools_has_no_dead_entries():
    tokens: set[str] = set()
    for tier in PromptTier:
        tokens |= set(_BACKTICKED.findall(render_platform_instructions(tier)))
    assert sorted(NOT_TOOLS - tokens) == [], (
        "NOT_TOOLS entries the prompt no longer names — remove them so the list "
        "keeps describing the prompt"
    )
