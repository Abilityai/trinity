"""
Unit tests for the canonical dedicated-MCP-tool slug helper (#846).

Pure functions (no DB/Docker) — the single source of truth for the
chat_with_<slug> tool name the MCP server consumes verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

mcp_tool_names = pytest.importorskip(
    "services.agent_service.mcp_tool_names",
    reason="backend on sys.path required",
)
compute_tool_names = mcp_tool_names.compute_tool_names
compute_tool_name = mcp_tool_names.compute_tool_name
resolve_tool_name = mcp_tool_names.resolve_tool_name
build_tool_description = mcp_tool_names.build_tool_description


def test_basic_slug():
    assert compute_tool_name("support-bot") == "chat_with_support_bot"


def test_uppercase_and_spaces_normalized():
    assert compute_tool_name("Sales Desk") == "chat_with_sales_desk"


def test_special_chars_collapse_and_trim():
    # leading/trailing/duplicate separators collapse
    assert compute_tool_name("--My__Agent!!") == "chat_with_my_agent"


def test_collision_gets_distinct_stable_suffixes():
    out = compute_tool_names(["my-agent", "my_agent"])
    assert out["my-agent"] != out["my_agent"]
    assert out["my-agent"].startswith("chat_with_my_agent_")
    assert out["my_agent"].startswith("chat_with_my_agent_")


def test_empty_slug_falls_back_to_hash_suffix():
    out = compute_tool_names(["!!!", "@@@"])
    for name, tool in out.items():
        assert tool.startswith("chat_with_")
        # no trailing/double underscore artifacts
        assert "chat_with__" not in tool


def test_determinism_over_full_set():
    assert compute_tool_names(["b", "a", "c"]) == compute_tool_names(["c", "a", "b"])


def test_no_collision_no_suffix():
    out = compute_tool_names(["alpha", "beta"])
    assert out == {"alpha": "chat_with_alpha", "beta": "chat_with_beta"}


def test_compute_tool_names_globally_unique():
    # M2/M3: every assigned tool name is unique across the full set, even with
    # base-slug collisions and empty slugs mixed in.
    names = ["my-agent", "my_agent", "My Agent", "!!!", "@@@", "alpha", "alpha "]
    out = compute_tool_names(names)
    assert len(set(out.values())) == len(out)  # no duplicate tool names
    assert all(v.startswith("chat_with_") for v in out.values())


def test_resolve_matches_full_set_on_collision():
    # I2: resolve_tool_name must agree with the authoritative full-set name the
    # MCP server registers — including the collision suffix — not the bare
    # single-agent name. "my-agent" and "my_agent" both slugify to "my_agent".
    exposed = ["my-agent", "my_agent"]
    full = compute_tool_names(exposed)
    assert resolve_tool_name("my-agent", exposed) == full["my-agent"]
    assert resolve_tool_name("my-agent", exposed) != "chat_with_my_agent"  # suffixed


def test_resolve_no_collision_is_bare_name():
    # No collision → resolve returns the same bare name the UI previewed.
    assert resolve_tool_name("support-bot", ["support-bot"]) == "chat_with_support_bot"
    # An agent not yet in the exposed set is folded in for a preview.
    assert resolve_tool_name("support-bot", []) == "chat_with_support_bot"


def test_collision_suffix_is_stable_across_runs():
    a = compute_tool_names(["my-agent", "my_agent"])
    b = compute_tool_names(["my_agent", "my-agent"])
    assert a == b


def test_description_is_name_only():
    # #846 security fix: the dedicated-tool description is advertised globally to
    # every non-connector MCP session, so it must NOT carry per-agent metadata.
    # Name-only — no template/repo label (cross-tenant leak + injection surface).
    assert build_tool_description("foo") == 'Chat directly with the "foo" agent.'


def test_description_rejects_metadata_arg():
    # The signature is name-only by design — there is no second positional
    # metadata argument that could smuggle a template label into the global
    # description.
    with pytest.raises(TypeError):
        build_tool_description("foo", "github:Org/repo")  # type: ignore[call-arg]
