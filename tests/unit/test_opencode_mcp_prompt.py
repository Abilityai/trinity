"""``.mcp.json`` → OpenCode ``mcp:`` in an OpenCode turn — the AGENT half (#2981, checkpoint C).

What is pinned here:

  * the shape mapping (remote ``{url, headers}``, local ``{command, args, env}``,
    disabled, unmappable) against the published v1.18.32 config schema;
  * the secret rule (decision 6, AC bullet 3): the rendered config carries
    env-var NAMES. ``${VAR}``/``${VAR:-d}`` placeholders become ``{env:VAR}``
    (the default travels to the subprocess env, since the pinned OpenCode has
    no default form — ``packages/opencode/src/config/variable.ts``); a header
    or env value equal to any secret-named env var's CURRENT value is rewritten
    to reference that var by name (reverse lookup, not one hard-coded name); a
    secret-shaped literal that matches no env var is DROPPED with one warning;
  * operator precedence: an entry already in the shared config wins;
  * the seam: a ``.mcp.json`` fixture whose Trinity header carries a sentinel
    that is ALSO set in the environment renders into a serialized
    ``opencode.json`` with the sentinel nowhere and ``{env:TRINITY_MCP_API_KEY}``
    present.

NOT here, by design: the platform-prompt half — the backend's
``mcp__trinity__<tool>`` → ``trinity_<tool>`` rewrite for OpenCode's
``<server>_<tool>`` naming (the lab fork tested both halves in one file). That
is checkpoint G of #2981 and lands with the backend prompt adaptation.
"""

from __future__ import annotations

import json
import logging

import pytest

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_mcp import (  # noqa: E402
    ENV_MCP_CONFIG_PATH,
    apply_mcp_config,
    load_mcp_servers,
    map_mcp_servers,
    render_mcp_servers,
    render_secret_value,
)

TRINITY_KEY = "trinity_mcp_SENTINEL_0f1e2d3c4b5a69788796a5b4c3d2e1f0"
CONTEXT7_KEY = "ctx7-SENTINEL-abcdef0123456789abcdef"
LOCAL_SECRET = "local-SENTINEL-fedcba9876543210"

_LOGGER = "agent_server.services.opencode_mcp"


def _mcp_json(*, trinity_literal: str = TRINITY_KEY) -> dict:
    """The shape `trinity_mcp` + a template write today: the Trinity bearer
    LITERAL, the execution-id placeholder, a template remote server with its
    own literal key, and a stdio server with a literal secret in `env`."""
    return {
        "mcpServers": {
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {
                    "Authorization": f"Bearer {trinity_literal}",
                    "X-Trinity-Execution-Id": "${TRINITY_EXECUTION_ID:-manual}",
                },
            },
            "docs": {
                "url": "https://docs.example.test/mcp",
                "headers": {"X-Api-Key": CONTEXT7_KEY, "Accept": "application/json"},
            },
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp@latest"],
                "env": {"CONTEXT7_API_KEY": CONTEXT7_KEY, "NODE_ENV": "production",
                        "LOCAL_TOKEN": LOCAL_SECRET},
            },
        }
    }


@pytest.fixture
def secret_env(monkeypatch):
    """The environment the agent server runs with: the Trinity key injected,
    a template credential injected, and one secret NOT in the env."""
    monkeypatch.setenv("TRINITY_MCP_API_KEY", TRINITY_KEY)
    monkeypatch.setenv("CONTEXT7_API_KEY", CONTEXT7_KEY)
    monkeypatch.delenv("LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("TRINITY_EXECUTION_ID", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_PROVIDER", raising=False)


# ---------------------------------------------------------------------------
# shape mapping
# ---------------------------------------------------------------------------

def test_remote_entry_maps_to_type_remote():
    out = map_mcp_servers(
        {"t": {"type": "http", "url": "http://h/mcp", "headers": {"Accept": "text/event-stream"}}},
        environ={},
    )
    assert out == {"t": {"type": "remote", "url": "http://h/mcp", "enabled": True,
                         "headers": {"Accept": "text/event-stream"}}}


def test_url_without_type_is_remote():
    out = map_mcp_servers({"t": {"url": "http://h/mcp"}}, environ={})
    assert out["t"]["type"] == "remote" and "headers" not in out["t"]


def test_local_entry_maps_to_command_list_and_environment():
    out = map_mcp_servers(
        {"c": {"command": "npx", "args": ["-y", "pkg@1"], "env": {"NODE_ENV": "production"}}},
        environ={},
    )
    assert out == {"c": {"type": "local", "command": ["npx", "-y", "pkg@1"],
                         "environment": {"NODE_ENV": "production"}, "enabled": True}}


def test_disabled_entry_keeps_opencodes_disabled_shape():
    out = map_mcp_servers({"c": {"enabled": False, "command": "npx"}}, environ={})
    assert out == {"c": {"enabled": False}}


def test_unmappable_entry_is_skipped_with_a_named_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        out = map_mcp_servers({"broken": {"headers": {}}, "nope": "string"}, environ={})
    assert out == {}
    messages = [r.getMessage() for r in caplog.records]
    assert any("'broken'" in m for m in messages)
    assert any("'nope'" in m for m in messages)


def test_missing_or_malformed_file_degrades_to_no_servers(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(tmp_path / "absent.json"))
    assert load_mcp_servers() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_mcp_servers(str(bad)) == {}
    no_servers = tmp_path / "no.json"
    no_servers.write_text(json.dumps({"mcpServers": []}))
    assert load_mcp_servers(str(no_servers)) == {}


# ---------------------------------------------------------------------------
# the secret rule, one value at a time
# ---------------------------------------------------------------------------

def _render(value, *, field_name="Authorization", environ=None, server="s"):
    defaults: dict = {}
    out = render_secret_value(
        value, server=server, field_name=field_name,
        environ=environ or {}, env_defaults=defaults,
    )
    return out, defaults


def test_placeholder_becomes_env_reference():
    assert _render("${TRINITY_EXECUTION_ID}", field_name="X-Id")[0] == "{env:TRINITY_EXECUTION_ID}"


def test_placeholder_default_goes_to_the_subprocess_env_not_the_config():
    """variable.ts (v1.18.32) has NO default form: `{env:VAR}` of an unset
    var is "". So the default is returned for the runtime to export."""
    out, defaults = _render("${TRINITY_EXECUTION_ID:-manual}", field_name="X-Id")
    assert out == "{env:TRINITY_EXECUTION_ID}"
    assert "manual" not in out
    assert defaults == {"TRINITY_EXECUTION_ID": "manual"}


def test_literal_equal_to_a_secret_env_value_is_rewritten_by_name():
    out, _ = _render(f"Bearer {TRINITY_KEY}", environ={"TRINITY_MCP_API_KEY": TRINITY_KEY})
    assert out == "Bearer {env:TRINITY_MCP_API_KEY}"


def test_reverse_lookup_is_over_the_whole_environment_not_one_name():
    """A template server's own key, under a name nobody hard-coded."""
    out, _ = _render(CONTEXT7_KEY, field_name="X-Api-Key",
                     environ={"SOME_VENDOR_TOKEN": CONTEXT7_KEY, "PATH": "/usr/bin"})
    assert out == "{env:SOME_VENDOR_TOKEN}"


def test_only_secret_named_vars_take_part_in_the_reverse_lookup():
    """`NODE_ENV=production` is not a secret; a header saying "production"
    must stay literal rather than become `{env:NODE_ENV}`."""
    out, _ = _render("production", field_name="X-Mode", environ={"NODE_ENV": "production"})
    assert out == "production"


def test_short_env_values_never_rewrite():
    """`TOKEN_COUNT=1` is secret-NAMED but trivially short; without the
    length floor every "1" in every header would become `{env:TOKEN_COUNT}`."""
    out, _ = _render("version 1 of 3", field_name="X-Mode", environ={"TOKEN_COUNT": "1"})
    assert out == "version 1 of 3"


def test_unmatched_secret_in_a_credential_header_is_dropped_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        out, _ = _render(f"Bearer {LOCAL_SECRET}", server="trinity", environ={"OTHER_KEY": "x" * 40})
    assert out is None
    warned = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert len(warned) == 1
    assert "'trinity'" in warned[0] and "Authorization" in warned[0]
    assert LOCAL_SECRET not in warned[0]


def test_unmatched_opaque_literal_in_a_plain_header_is_dropped_too():
    out, _ = _render("ghp_" + "A" * 36, field_name="X-Custom", environ={})
    assert out is None


def test_plain_headers_pass_through_literally():
    for value, name in [("application/json", "Accept"), ("https://docs.example.test/v1/path", "X-Base-Url"),
                        ("en-US,en;q=0.9", "Accept-Language")]:
        out, _ = _render(value, field_name=name, environ={})
        assert out == value, name


def test_a_pure_reference_in_a_credential_header_is_not_a_literal():
    out, _ = _render("Bearer {env:MY_KEY}", environ={})
    assert out == "Bearer {env:MY_KEY}"


# ---------------------------------------------------------------------------
# the secret rule, over a local server's argv (review I2)
# ---------------------------------------------------------------------------

def test_argv_literal_equal_to_a_secret_env_value_is_rewritten_by_name():
    out = map_mcp_servers(
        {"v": {"command": "npx", "args": ["-y", "vendor-mcp", "--api-key", CONTEXT7_KEY]}},
        environ={"VENDOR_API_KEY": CONTEXT7_KEY},
    )
    assert out["v"]["command"] == ["npx", "-y", "vendor-mcp", "--api-key", "{env:VENDOR_API_KEY}"]


def test_argv_placeholder_becomes_env_reference():
    rendered = render_mcp_servers(
        {"v": {"command": "${VENDOR_BIN}", "args": ["--token", "${VENDOR_TOKEN:-none}"]}},
        environ={},
    )
    assert rendered.servers["v"]["command"] == ["{env:VENDOR_BIN}", "--token", "{env:VENDOR_TOKEN}"]
    assert rendered.env_defaults == {"VENDOR_TOKEN": "none"}


def test_argv_credential_residue_is_dropped_with_a_warning(caplog):
    literal = "sk-" + "a1B2c3D4" * 5
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        rendered = render_mcp_servers(
            {"v": {"command": "npx", "args": ["-y", "vendor-mcp", "--api-key", literal]}},
            environ={"OTHER_KEY": "x" * 40},
        )
    assert rendered.servers["v"]["command"] == ["npx", "-y", "vendor-mcp", "--api-key"]
    assert rendered.dropped == (("v", "args[3]"),)
    warned = [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()]
    assert len(warned) == 1 and "'v'" in warned[0] and "args[3]" in warned[0]
    assert literal not in warned[0]


def test_argv_dropped_command_skips_the_server():
    rendered = render_mcp_servers({"v": {"command": "sk-" + "a1B2c3D4" * 5}}, environ={})
    assert rendered.servers == {}
    assert rendered.dropped == (("v", "command"),)


@pytest.mark.parametrize("args", [
    ["-y", "@modelcontextprotocol/server-filesystem", "/home/developer"],
    ["-y", "@modelcontextprotocol/server-sequential-thinking"],
])
def test_plain_argv_is_unchanged(args):
    out = map_mcp_servers({"fs": {"command": "npx", "args": args}}, environ={"OTHER_KEY": "x" * 40})
    assert out["fs"]["command"] == ["npx", *args]


# ---------------------------------------------------------------------------
# the secret rule, over a whole `.mcp.json`
# ---------------------------------------------------------------------------

def test_rendered_servers_carry_names_never_values(secret_env, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        rendered = render_mcp_servers(_mcp_json()["mcpServers"])
    serialized = json.dumps(rendered.servers)
    for sentinel in (TRINITY_KEY, CONTEXT7_KEY, LOCAL_SECRET):
        assert sentinel not in serialized, sentinel
    trinity = rendered.servers["trinity"]["headers"]
    assert trinity["Authorization"] == "Bearer {env:TRINITY_MCP_API_KEY}"
    assert trinity["X-Trinity-Execution-Id"] == "{env:TRINITY_EXECUTION_ID}"
    assert rendered.servers["docs"]["headers"] == {
        "X-Api-Key": "{env:CONTEXT7_API_KEY}", "Accept": "application/json",
    }
    env = rendered.servers["context7"]["environment"]
    assert env == {"CONTEXT7_API_KEY": "{env:CONTEXT7_API_KEY}", "NODE_ENV": "production"}
    assert rendered.dropped == (("context7", "LOCAL_TOKEN"),)
    assert rendered.env_defaults == {"TRINITY_EXECUTION_ID": "manual"}
    dropped_warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert len(dropped_warnings) == 1
    assert "'context7'" in dropped_warnings[0].getMessage()
    assert "LOCAL_TOKEN" in dropped_warnings[0].getMessage()


def test_operator_declared_entry_wins(secret_env):
    config = {"mcp": {"trinity": {"type": "remote", "url": "http://operator/mcp", "enabled": True}}}
    defaults = apply_mcp_config(config, _mcp_json()["mcpServers"])
    assert config["mcp"]["trinity"] == {"type": "remote", "url": "http://operator/mcp", "enabled": True}
    assert set(config["mcp"]) == {"trinity", "docs", "context7"}
    assert defaults == {"TRINITY_EXECUTION_ID": "manual"}


def test_no_servers_leaves_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(tmp_path / "absent.json"))
    config: dict = {"share": "disabled"}
    assert apply_mcp_config(config) == {}
    assert config == {"share": "disabled"}


# ---------------------------------------------------------------------------
# the seam: a `.mcp.json` fixture → the serialized per-turn opencode.json
# ---------------------------------------------------------------------------

def test_serialized_turn_config_has_no_literal_secret(tmp_path, monkeypatch, secret_env):
    """Static over the file OpenCode will read: the Trinity sentinel is set in
    the environment AND written literally in `.mcp.json`; it must appear
    nowhere in the serialized turn config, which references the var by name."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(json.dumps(_mcp_json()))
    monkeypatch.setenv(ENV_MCP_CONFIG_PATH, str(mcp_file))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    shared = tmp_path / "config" / "opencode"
    shared.mkdir(parents=True)
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))

    env_out: dict = {}
    turn_dir = opencode_runtime._prepare_turn_config("exec-mcp", "PROMPT", None, env_out=env_out)
    try:
        raw = (tmp_path / "turns" / "exec-mcp" / "opencode" / "opencode.json").read_text()
    finally:
        opencode_runtime._cleanup_turn_dir(turn_dir)

    for sentinel in (TRINITY_KEY, CONTEXT7_KEY, LOCAL_SECRET):
        assert sentinel not in raw, sentinel
    assert "manual" not in raw
    config = json.loads(raw)
    assert config["mcp"]["trinity"]["headers"]["Authorization"] == "Bearer {env:TRINITY_MCP_API_KEY}"
    assert config["mcp"]["trinity"]["headers"]["X-Trinity-Execution-Id"] == "{env:TRINITY_EXECUTION_ID}"
    assert config["mcp"]["context7"]["environment"]["CONTEXT7_API_KEY"] == "{env:CONTEXT7_API_KEY}"
    assert "LOCAL_TOKEN" not in config["mcp"]["context7"]["environment"]
    assert env_out == {"TRINITY_EXECUTION_ID": "manual"}
