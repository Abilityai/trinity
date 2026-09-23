"""Trinity MCP + the platform prompt inside an OpenCode turn (Abilityai/lilu#257 step 2).

Two halves of "an unmodified Trinity agent behaves like a Trinity agent on
OpenCode":

  * **the platform prompt** — ``opencode run`` has no system-prompt flag, so the
    composed prompt (platform instructions + custom prompt + execution context +
    the MEM-001 per-user memory block, all folded into the one ``system_prompt``
    the backend already sends) travels through the ``instructions:`` config key.
    It must be FIRST and it must be ADDITIVE: a template author's own
    ``instructions`` entry survives, because dropping it would silently strip an
    agent's declared reading list on the runtime switch.
  * **Trinity MCP** — ``.mcp.json`` is the single store (Claude's, Trinity's
    injector's, the credential-injection path's). OpenCode cannot read it, so it
    is TRANSLATED into that turn's ``mcp:`` block. The shapes are pinned against
    the published v1.18.32 config schema (``McpLocalConfig``/``McpRemoteConfig``)
    and against a real turn: ``ollama/gemma4:12b`` called ``trinity_list_agents``
    and answered from its output, which is where the ``<server>_<tool>`` naming
    in the platform-prompt adaptation comes from.
"""

from __future__ import annotations

import json
import os

import pytest

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_mcp import (  # noqa: E402
    ENV_MCP_CONFIG_PATH,
    apply_mcp_config,
    load_mcp_servers,
    map_mcp_servers,
)


# ---------------------------------------------------------------------------
# PART A — the platform prompt via `instructions:`
# ---------------------------------------------------------------------------

def _turn_config(tmp_path, monkeypatch, *, shared: dict | None = None,
                 prompt: str | None = "PLATFORM PROMPT", model=None):
    """Build a turn config through the real seam and return (turn_dir, config)."""
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    shared_dir = tmp_path / "config" / "opencode"
    shared_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared_dir))
    if shared is not None:
        (shared_dir / "opencode.json").write_text(json.dumps(shared))
    # No `.mcp.json` unless a test asks for one.
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(tmp_path / "absent.mcp.json"))
    turn_dir = opencode_runtime._prepare_turn_config("exec-prompt", prompt, model)
    config = json.loads(
        (tmp_path / "turns" / "exec-prompt" / "opencode" / "opencode.json").read_text()
    )
    return turn_dir, config


def test_composed_prompt_is_written_and_listed_first(tmp_path, monkeypatch):
    """The composed prompt file EXISTS, holds the composed text verbatim, and is
    the first `instructions:` entry — OpenCode reads them in order, so an entry
    appended after a template author's would be outranked by it."""
    composed = (
        "# Trinity Platform Instructions\n\n## Custom Instructions\n\nBe terse.\n"
        "\n## What you know about this user\n\nName is Ada.\n"  # the MEM-001 block
    )
    turn_dir, config = _turn_config(tmp_path, monkeypatch, prompt=composed)
    try:
        assert config["instructions"], "instructions key missing"
        prompt_file = config["instructions"][0]
        assert os.path.exists(prompt_file), "composed prompt file was not written"
        assert open(prompt_file).read() == composed
    finally:
        opencode_runtime._cleanup_turn_dir(turn_dir)


def test_template_declared_instructions_survive_alongside(tmp_path, monkeypatch):
    """Additive, not replacing. A template author's `instructions:` entry is the
    agent's declared reading list; the runtime switch must not silently drop it."""
    turn_dir, config = _turn_config(
        tmp_path, monkeypatch,
        shared={"instructions": ["AGENTS.md", "docs/playbook.md"]},
    )
    try:
        entries = config["instructions"]
        assert entries[0].endswith("trinity-instructions.md"), entries
        assert entries[1:] == ["AGENTS.md", "docs/playbook.md"]
    finally:
        opencode_runtime._cleanup_turn_dir(turn_dir)


def test_no_system_prompt_leaves_template_instructions_untouched(tmp_path, monkeypatch):
    """A turn with no platform prompt must not fabricate an empty one — the
    template's own list is then the whole list."""
    turn_dir, config = _turn_config(
        tmp_path, monkeypatch, shared={"instructions": ["AGENTS.md"]}, prompt=None,
    )
    try:
        assert config["instructions"] == ["AGENTS.md"]
    finally:
        opencode_runtime._cleanup_turn_dir(turn_dir)


# ---------------------------------------------------------------------------
# PART A — runtime-aware MCP tool naming in the platform prompt (backend)
# ---------------------------------------------------------------------------

def test_platform_prompt_names_opencode_tools_server_scoped():
    """OpenCode namespaces MCP tools as `<server>_<tool>` — MEASURED on a real
    turn (`trinity_list_agents`). Both the Claude spelling and the Codex bare
    spelling are unknown tools to it, so this is a rewrite, not a strip."""
    from services.platform_prompt_service import _adapt_instructions_for_runtime

    src = "Call mcp__trinity__report(...) and mcp__trinity__chat_with_agent(...)."
    out = _adapt_instructions_for_runtime(src, "opencode")

    assert "mcp__trinity__" not in out
    assert "trinity_report(" in out
    assert "trinity_chat_with_agent(" in out
    # The orientation note is prepended, and it is OpenCode's, not Codex's.
    assert "OpenCode runtime" in out
    assert "Codex runtime" not in out


def test_other_runtimes_keep_their_own_naming():
    """Regression fence: adding OpenCode must not re-point Claude or Codex."""
    from services.platform_prompt_service import _adapt_instructions_for_runtime

    src = "Call mcp__trinity__report(...)."
    assert _adapt_instructions_for_runtime(src, "claude-code") == src
    codex = _adapt_instructions_for_runtime(src, "codex")
    assert "report(" in codex and "trinity_report(" not in codex


# ---------------------------------------------------------------------------
# PART B — `.mcp.json` → OpenCode `mcp:`
# ---------------------------------------------------------------------------

def test_remote_http_server_maps_to_opencode_remote_shape():
    """Trinity's own entry. `type: http` + headers is Claude's shape; OpenCode's
    is `type: remote` + headers, per the published McpRemoteConfig."""
    mapped = map_mcp_servers({
        "trinity": {
            "type": "http",
            "url": "http://mcp-server:8080/mcp",
            "headers": {"Authorization": "Bearer trinity_mcp_test"},
        }
    })
    assert mapped == {
        "trinity": {
            "type": "remote",
            "url": "http://mcp-server:8080/mcp",
            "enabled": True,
            "headers": {"Authorization": "Bearer trinity_mcp_test"},
        }
    }


def test_stdio_server_maps_to_opencode_local_shape():
    """Claude's `command` + `args` is OpenCode's single `command` ARRAY, and
    Claude's `env` is OpenCode's `environment`. Getting either wrong is a config
    parse error that takes the whole turn down, MCP or not."""
    mapped = map_mcp_servers({
        "context7": {
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp@latest"],
            "env": {"API_KEY": "k"},
        }
    })
    assert mapped["context7"] == {
        "type": "local",
        "command": ["npx", "-y", "@upstash/context7-mcp@latest"],
        "enabled": True,
        "environment": {"API_KEY": "k"},
    }


def test_url_wins_even_when_type_is_absent_or_unknown():
    """Claude makes `type` optional, and Trinity's own template omits it on
    stdio entries. `url` is the discriminator, not `type`."""
    mapped = map_mcp_servers({
        "a": {"url": "https://example.com/mcp"},
        "b": {"type": "sse", "url": "https://example.com/sse"},
        "c": {"type": "something-new", "url": "https://example.com/new"},
    })
    assert {k: v["type"] for k, v in mapped.items()} == {
        "a": "remote", "b": "remote", "c": "remote"
    }


def test_disabled_server_is_carried_across_not_dropped():
    mapped = map_mcp_servers({"x": {"enabled": False, "url": "https://e/mcp"}})
    assert mapped == {"x": {"enabled": False}}


def test_unmappable_entry_is_skipped_not_emitted_half_formed():
    """An entry with neither url nor command would fail OpenCode's config parse
    and take down a turn that had nothing to do with MCP."""
    mapped = map_mcp_servers({"broken": {"note": "nothing usable"}, "ok": {"url": "https://e/mcp"}})
    assert set(mapped) == {"ok"}


def test_missing_or_corrupt_mcp_json_degrades_to_no_servers(tmp_path, monkeypatch):
    """Graceful degradation: a broken MCP config means "no MCP tools this turn",
    never a failed turn — the agent can still answer."""
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(tmp_path / "nope.json"))
    assert load_mcp_servers() == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(corrupt))
    assert load_mcp_servers() == {}


def test_operator_declared_server_wins_over_mcp_json():
    """Same precedence rule the provider block uses: the file is a default, the
    operator's config is the override."""
    config = {"mcp": {"trinity": {"type": "remote", "url": "https://operator/mcp"}}}
    apply_mcp_config(config, {"trinity": {"url": "http://mcp-server:8080/mcp"},
                              "other": {"url": "https://other/mcp"}})
    assert config["mcp"]["trinity"]["url"] == "https://operator/mcp"
    assert config["mcp"]["other"]["url"] == "https://other/mcp"


def test_turn_config_carries_the_mcp_block_and_is_not_world_readable(tmp_path, monkeypatch):
    """End-to-end on disk through the real seam — and the file now holds the
    Trinity bearer token, so it gets `.mcp.json`'s 0600."""
    mcp_json = tmp_path / "agent.mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {
        "trinity": {"type": "http", "url": "http://mcp-server:8080/mcp",
                    "headers": {"Authorization": "Bearer trinity_mcp_test"}},
    }}))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        opencode_runtime, "_shared_config_dir", lambda: str(tmp_path / "shared")
    )
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(mcp_json))

    turn_dir = opencode_runtime._prepare_turn_config("exec-mcp", "PROMPT", None)
    try:
        path = tmp_path / "turns" / "exec-mcp" / "opencode" / "opencode.json"
        config = json.loads(path.read_text())
        assert config["mcp"]["trinity"] == {
            "type": "remote",
            "url": "http://mcp-server:8080/mcp",
            "enabled": True,
            "headers": {"Authorization": "Bearer trinity_mcp_test"},
        }
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    finally:
        opencode_runtime._cleanup_turn_dir(turn_dir)


def test_runtime_reports_mcp_configured_rather_than_stubbed(monkeypatch):
    """`configure_mcp` used to return False on purpose (a stub that claimed
    success would be indistinguishable from MCP silently doing nothing). Now it
    persists into the one store and says so."""
    seen = {}

    import agent_server.services.trinity_mcp as trinity_mcp

    monkeypatch.setattr(
        trinity_mcp, "_configure_claude_mcp_servers",
        lambda servers: seen.setdefault("servers", servers) is not None or True,
    )
    ok = opencode_runtime.OpenCodeRuntime().configure_mcp({"x": {"url": "https://e/mcp"}})
    assert ok is True
    assert seen["servers"] == {"x": {"url": "https://e/mcp"}}
