"""Canvas guidance in the platform system prompt (ent#536) — the sibling of
`test_1535_report_prompt_guidance.py`.

The canvas (ent#438) was fully functional and invisible to the text agent: the
only guidance anywhere was the voice model's panel instructions, so a fresh
agent asked for "a bar chart on your canvas" had nothing to go on. This suite
pins the `### Your Canvas` block and — the part that matters — pins it to the
OTHER two surfaces that define the contract, so the prompt cannot drift into
teaching a kind the tool rejects or a payload key the renderer does not read:

  * the MCP tool contract (`src/mcp-server/src/tools/canvas.ts`) — `BLOCK_KINDS`
  * the frontend rules (`components/canvas/canvasUtils.js`) — the payload keys
    `chartModel`, `imageSource` and the diagram leaf dispatch on

A drift there is silent: the agent writes, the write succeeds, and the block
renders as raw JSON (Invariant #13, three surfaces in sync).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from models import (
    CANVAS_BLOCKS_MAX_BYTES,
    CANVAS_DIAGRAM_MAX_CHARS,
    CANVAS_IMAGE_INLINE_MAX_BYTES,
    CANVAS_MAX_BLOCKS,
    DEFAULT_CANVAS_ID,
)
from services import platform_prompt_service
from services.platform_prompt_service import (
    _KNOWN_SECTION_HEADINGS,
    _MINIMAL_DROP_SECTIONS,
    get_platform_system_prompt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_TOOL = REPO_ROOT / "src/mcp-server/src/tools/canvas.ts"
UTILS = REPO_ROOT / "src/frontend/src/components/canvas/canvasUtils.js"

# Context-budget guard, the #1535 discipline: this ships on every turn of every
# agent. Nine kinds with a payload example each, the fences, and patch-by-id
# land at ~2.4 KB; raising this ceiling should be a decision, not an accident.
MAX_BLOCK_CHARS = 2700

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


def _canvas_block(runtime: str = "claude-code") -> str:
    prompt = get_platform_system_prompt(runtime)
    m = re.search(r"### Your Canvas(.*?)\n### ", prompt, re.S)
    assert m, "the canvas guidance block is missing from the platform prompt"
    return m.group(1)


def _mcp_kinds() -> list[str]:
    source = MCP_TOOL.read_text()
    body = source[source.index("const BLOCK_KINDS"):source.index("] as const")]
    kinds = re.findall(r'"(\w+)"', body)
    assert kinds, "BLOCK_KINDS parsed empty"
    return kinds


def test_claude_prompt_documents_the_canvas_tools():
    block = _canvas_block()
    for tool in ("mcp__trinity__set_canvas(", "mcp__trinity__patch_canvas(", "mcp__trinity__get_canvas("):
        assert tool in block, f"{tool} is undocumented"
    assert f'"{DEFAULT_CANVAS_ID}"' in block, "the default canvas must be named"


def test_codex_prompt_uses_the_bare_tool_names():
    prompt = get_platform_system_prompt("codex")
    assert "set_canvas(" in prompt and "mcp__trinity__set_canvas" not in prompt
    orientation = prompt.split("---", 1)[0]
    for tool in ("`set_canvas`", "`patch_canvas`", "`get_canvas`"):
        assert tool in orientation, f"Codex orientation enumerates tools; {tool} must be listed"


def test_every_kind_from_the_mcp_contract_is_documented_with_a_payload():
    block = _canvas_block()
    for kind in _mcp_kinds():
        assert f"`{kind}`" in block, f"kind '{kind}' is undocumented in the platform prompt"


def test_documented_payload_keys_match_the_renderer_rules():
    """The keys the frontend rules dispatch on, and nothing else, are what the
    prompt teaches."""
    utils = UTILS.read_text()
    chart = utils[utils.index("export function chartModel"):utils.index("export function tsLabelFormatter")]
    for key in ("series", "points", "type"):
        assert f"payload.{key}" in chart or f".{key}" in chart
    block = _canvas_block()
    for key in ("series", "points", "ts", "value", "type", "label", "unit"):
        assert f'"{key}"' in block, f"chart key '{key}' is read by the renderer but not documented"
    assert '"mermaid"' in block, "the diagram payload key is `mermaid`"
    assert '"src"' in block and '"caption"' in block, "the image payload keys"
    assert '"tiles"' in block and '"columns"' in block and '"rows"' in block and '"events"' in block
    assert '"markdown"' in block and '"html"' in block


def test_every_chart_type_the_renderer_knows_is_documented():
    utils = UTILS.read_text()
    m = re.search(r"export const CHART_TYPES = \[(.*?)\]", utils)
    types_ = re.findall(r"'(\w+)'", m.group(1))
    block = _canvas_block()
    for t in types_:
        assert t in block, f"chart type '{t}' is undocumented"


def test_the_rich_fences_are_taught():
    block = _canvas_block()
    for fence in ("```chart", "```kpi", "```table", "```mermaid"):
        assert fence in block, f"{fence} fence is undocumented"


def test_documented_ceilings_match_the_enforced_ones():
    block = _canvas_block()
    assert f"{CANVAS_MAX_BLOCKS} blocks" in block
    assert f"{CANVAS_BLOCKS_MAX_BYTES // 1024} KB" in block
    assert f"{CANVAS_DIAGRAM_MAX_CHARS:,}" in block
    assert f"{CANVAS_IMAGE_INLINE_MAX_BYTES // 1024} KB" in block
    assert "__CANVAS" not in block, "an interpolation marker leaked into the prompt"


def test_block_stays_within_the_context_budget():
    block = _canvas_block().strip()
    assert len(block) <= MAX_BLOCK_CHARS, (
        f"canvas guidance grew to {len(block)} chars (cap {MAX_BLOCK_CHARS}); it ships on every turn"
    )


def test_the_block_routes_the_right_requests_and_forbids_scripts():
    block = _canvas_block()
    assert "on your canvas" in block, "the trigger phrase people actually say"
    assert "report" in block.lower(), "must say when a report is the better tool"
    assert "JavaScript" in block, "the no-scripts rule must be stated"


def test_the_section_is_registered_as_tool_guidance():
    """Same tier class as Publishing Reports: rendered at VERBOSE, dropped at
    MINIMAL. A renamed heading would orphan the drop entry — ent243's pin
    catches that; this one says which set the section belongs to."""
    assert "Your Canvas" in _KNOWN_SECTION_HEADINGS
    assert "Your Canvas" in _MINIMAL_DROP_SECTIONS


def test_report_block_is_not_swallowed_by_the_new_neighbour():
    """The report guard slices `### Publishing Reports … \\n### `; the canvas
    section must terminate it exactly there, not merge into it."""
    prompt = get_platform_system_prompt("claude-code")
    report = re.search(r"### Publishing Reports(.*?)\n### ", prompt, re.S).group(1)
    assert "set_canvas" not in report
