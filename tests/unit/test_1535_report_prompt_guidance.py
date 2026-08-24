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
#
# ent#365 raised it 2000 -> 2400, deliberately. The audience half of reports
# (`audience_email`) is what makes a report reach the person it was produced
# for; without it in this block every agent keeps publishing operator-only and
# the Workspace deliverables surface is empty by construction — the lever
# exists and nothing pulls it (#1039/#1056 class). #1535's own argument applies
# verbatim: reporting belongs in the platform prompt so it is a fleet-wide
# default rather than a per-template opt-in, and the same is true of who a
# report is for. Cost is ~400 chars (~100 tokens) per turn; the alternative was
# trimming working guidance to fit, which trades one inert feature for another.
MAX_BLOCK_CHARS = 2400


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


def test_audience_is_documented_so_the_deliverables_surface_can_fill():
    """The audience arg must ship in the fleet-wide block (ent#365).

    `addressed_to_email` is nullable and every agent defaults to NULL, so if
    this block never mentions `audience_email` the Workspace deliverables list
    is empty on every install forever — not "until agents adopt it", but with
    no mechanism by which adoption happens. Caught in review on PR #2383.
    """
    block = _report_block()
    assert "audience_email" in block, (
        "the report block must document audience_email or no agent will ever "
        "address a report, leaving the Workspace deliverables surface inert"
    )
    # The arg name must match what the MCP tool actually accepts.
    assert "audience_email" in MCP_TOOL.read_text(), (
        "prompt documents an argument the report tool does not accept"
    )


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


def test_documented_payload_ceiling_matches_the_enforced_one():
    """The seventh drift guard, and the one that was missing when it mattered.

    The block shipped `max 256 KB` in the same PR that raised
    `REPORT_PAYLOAD_MAX_BYTES` to 5 MiB — a 20x understatement told to every
    agent on every turn. That doesn't just misinform: it partly cancels #1537,
    because an agent that believes the wall is at 256 KB pre-aggregates the very
    payloads the raise exists to accept, and the raise buys nothing.

    Six guards pinned the block against the MCP enum and the renderer keys;
    none pinned a number. The block now interpolates the constant, so this test
    is really asserting that nobody replaces the interpolation with a literal.
    """
    from models import REPORT_PAYLOAD_MAX_BYTES

    block = _report_block()
    expected = f"{REPORT_PAYLOAD_MAX_BYTES // (1024 * 1024)} MB"
    assert expected in block, (
        f"the prompt must state the enforced ceiling ({expected}); "
        "interpolate REPORT_PAYLOAD_MAX_BYTES rather than typing a literal"
    )
    # A stale hand-typed figure is the exact failure being guarded against.
    assert "256 KB" not in block


def _file_sharing_block(runtime: str = "claude-code") -> str:
    prompt = get_platform_system_prompt(runtime)
    return prompt[prompt.index("### Sharing Files with Users"):prompt.index("### Publishing Reports")]


def test_file_sharing_block_does_not_claim_structured_results():
    """The two blocks are adjacent and were competing for the same request.

    Found in live use: asked for "weather for 500 places", the agent wrote a CSV
    to /home/developer/public/ and called share_file — because the file-sharing
    block's trigger list literally read "(CSV, PDF, report, image, exported
    data, etc.)". It then hit FEATURE_DISABLED and delivered nothing, while the
    report block sat directly underneath unused.

    A prompt block cannot be evaluated alone; what the NEIGHBOURING block claims
    decides which one fires. Keep 'report'/'CSV'/'exported data' out of the
    file-sharing trigger so structured results route to the report tool.
    """
    block = _file_sharing_block()
    # Scope the assertion to the PARENTHESISED claim list, not the whole
    # sentence — the sentence legitimately names the report block when handing
    # structured results off to it.
    claim_list = re.search(r"When the user asks for a file \(([^)]*)\)", block)
    assert claim_list, "could not locate the file-sharing trigger list"
    listed = claim_list.group(1).lower()
    for claimed in ("report", "csv", "exported data"):
        assert claimed not in listed, (
            f"the file-sharing trigger list claims {claimed!r}, which steals "
            f"rows-and-columns requests from the Publishing Reports block: {listed!r}"
        )
    # …and it must actively hand those off rather than stay silent.
    assert "Publishing Reports" in block


def test_report_trigger_covers_a_one_off_table_not_only_scheduled_work():
    """The original trigger was framed entirely around recurring work ("a
    scheduled run", "compares against next period"), so a one-off interactive
    "give me 500 rows" matched nothing. Volume and shape are the trigger too."""
    block = _report_block().lower()
    assert "rows-and-columns" in block or "table" in block
    assert "csv" in block, (
        "the block must explicitly outrank hand-writing a CSV + share_file — "
        "that is the path agents actually took"
    )


def test_block_points_decisions_at_the_operator_queue():
    """Reports are one-way. Without this line agents reach for a report when they
    actually need an approval, and nothing ever answers them."""
    block = _report_block()
    assert "operator queue" in block.lower()
