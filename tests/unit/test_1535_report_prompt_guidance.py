"""Report-tool guidance in the platform system prompt (#1535, epic #1534).

The `report` MCP tool (#918) was fully functional but invisible: nothing in the
platform prompt told an agent it existed, so reports only got published when an
agent's own CLAUDE.md happened to mention them. This suite pins the guidance
block and — more importantly — pins it to the OTHER two surfaces that define the
contract, so the prompt can't quietly drift into teaching agents a payload shape
the dashboard doesn't render:

  * the MCP tool contract (`src/mcp-server/src/tools/reports.ts`) — the
    `display_hint` enum
  * the frontend renderers (`components/reports/ReportRenderer.vue`) — the
    payload keys each hint is dispatched on

A drift there is silent in production: the agent publishes, the write succeeds,
and the report renders as raw JSON. Only a cross-surface assertion catches it
(Invariant #13, three surfaces in sync).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import get_platform_system_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDERER = REPO_ROOT / "src/frontend/src/components/reports/ReportRenderer.vue"
MCP_TOOL = REPO_ROOT / "src/mcp-server/src/tools/reports.ts"

# Upper bound on the block, in characters. The prompt ships on EVERY turn of
# EVERY agent, so this is a context-budget guard, not style policing: the block
# landed at ~1.3 KB (~330 tokens, +18% on the platform prompt) after a
# deliberate trim from ~1.8 KB. Raising this ceiling should be a decision, not
# an accident.
MAX_BLOCK_CHARS = 2000


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    """Pin the operator-configurable `trinity_prompt` to empty so these tests
    read only the built-in instructions, independent of DB state."""
    monkeypatch.setattr(
        platform_prompt_service.db, "get_setting_value", lambda *a, **k: None
    )


def _report_block(runtime: str = "claude-code") -> str:
    prompt = get_platform_system_prompt(runtime)
    m = re.search(r"### Publishing Reports(.*?)\n### ", prompt, re.S)
    assert m, "the report guidance block is missing from the platform prompt"
    return m.group(1)


def test_claude_prompt_documents_the_report_tool():
    block = _report_block()
    assert "mcp__trinity__report(" in block
    # The two arguments an agent cannot guess.
    assert "report_type" in block
    assert "display_hint" in block


def test_codex_prompt_uses_the_bare_tool_name():
    """Codex calls MCP tools by bare name; the Claude-only prefix makes it emit
    `unknown MCP server` (#1187). The existing runtime suite asserts the prefix
    is absent prompt-wide — this pins that the report tool survives the strip
    with a usable name, and that the Codex orientation lists it alongside the
    others it enumerates."""
    prompt = get_platform_system_prompt("codex")
    assert "report(" in prompt
    assert "mcp__trinity__report" not in prompt
    orientation = prompt.split("---", 1)[0]
    assert "`report`" in orientation, "Codex orientation enumerates tools; report must be listed"


def test_every_display_hint_from_the_mcp_contract_is_documented():
    """The prompt must cover exactly the hints the tool accepts. A hint the tool
    accepts but the prompt omits is an undiscoverable feature; a hint the prompt
    teaches but the tool rejects is a guaranteed 422."""
    source = MCP_TOOL.read_text()
    m = re.search(r"display_hint:\s*z\.enum\(\[(.*?)\]\)", source, re.S)
    assert m, "could not read the display_hint enum from the MCP tool contract"
    hints = re.findall(r'"([a-z]+)"', m.group(1))
    assert hints, "display_hint enum parsed empty"

    block = _report_block()
    for hint in hints:
        assert f"`{hint}`" in block, f"display_hint '{hint}' is undocumented in the platform prompt"


def test_documented_payload_keys_match_the_renderers():
    """Each hint is dispatched by the renderer on a specific payload key
    (`columns`/`rows`, `tiles`, `events`, `markdown`). Teaching a different key
    fails silently — the write succeeds and the dashboard falls back to raw
    JSON — so the prompt is pinned to the keys the renderer actually reads."""
    renderer = RENDERER.read_text()
    keys = set(re.findall(r"payload\.([a-zA-Z_]+)", renderer))
    assert {"columns", "rows", "tiles", "events", "markdown"} <= keys, (
        f"renderer contract changed; keys found: {sorted(keys)}"
    )

    block = _report_block()
    for key in sorted(keys):
        assert f'"{key}"' in block, f"payload key '{key}' is read by the renderer but not documented"


def test_block_stays_within_the_context_budget():
    block = _report_block().strip()
    assert len(block) <= MAX_BLOCK_CHARS, (
        f"report guidance grew to {len(block)} chars (cap {MAX_BLOCK_CHARS}); "
        "it ships on every turn of every agent"
    )


def test_block_points_decisions_at_the_operator_queue():
    """Reports are one-way. Without this line agents reach for a report when they
    actually need an approval, and nothing ever answers them."""
    block = _report_block()
    assert "operator queue" in block.lower()
