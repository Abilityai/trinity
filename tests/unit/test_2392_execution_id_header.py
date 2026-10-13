"""#2392: the Trinity MCP entry carries X-Trinity-Execution-Id.

Writers (agent server) emit the runtime-native form; the validator's
canonical-trinity allowlist accepts the old and the new shape and nothing else.
Byte parity of the two validator copies is pinned by
test_2007_mcp_template_render.py.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from agent_server.services import trinity_mcp  # noqa: E402
from services.mcp_validator import McpValidationError, validate_mcp_config  # noqa: E402

pytestmark = pytest.mark.unit

URL = "http://mcp-server:8080/mcp"
KEY = "trinity_mcp_abc123"
LITERAL = "${TRINITY_EXECUTION_ID:-manual}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Both writers hard-code Path("/home/developer").
    monkeypatch.setattr(trinity_mcp, "Path", lambda _p: tmp_path)
    return tmp_path


def test_claude_writer_emits_both_headers(home):
    assert trinity_mcp._inject_claude_mcp(URL, KEY) is True
    entry = json.loads((home / ".mcp.json").read_text())["mcpServers"]["trinity"]
    assert entry["headers"] == {
        "Authorization": f"Bearer {KEY}",
        "X-Trinity-Execution-Id": LITERAL,
    }
    # The writer's own output must pass the reserved-name gate.
    validate_mcp_config(json.dumps({"mcpServers": {"trinity": entry}}))


def test_gemini_writer_emits_expansion_literal(home):
    assert trinity_mcp._inject_gemini_mcp(URL, KEY) is True
    entry = json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]["trinity"]
    assert entry["headers"]["X-Trinity-Execution-Id"] == LITERAL


def test_codex_writer_uses_env_http_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert trinity_mcp._inject_codex_mcp(URL, KEY) is True
    trinity = tomllib.loads((tmp_path / "config.toml").read_text())["mcp_servers"]["trinity"]
    assert trinity["env_http_headers"] == {"X-Trinity-Execution-Id": "TRINITY_EXECUTION_ID"}
    assert trinity["bearer_token_env_var"] == "TRINITY_MCP_API_KEY"


def _cfg(headers: dict) -> str:
    return json.dumps({"mcpServers": {"trinity": {"type": "http", "url": URL, "headers": headers}}})


AUTH = {"Authorization": f"Bearer {KEY}"}


def test_validator_accepts_old_shape():
    validate_mcp_config(_cfg(AUTH))


def test_validator_accepts_new_shape():
    validate_mcp_config(_cfg({**AUTH, "X-Trinity-Execution-Id": LITERAL}))


@pytest.mark.parametrize(
    "headers",
    [
        {**AUTH, "X-Trinity-Execution-Id": "${TRINITY_EXECUTION_ID}"},
        {**AUTH, "X-Trinity-Execution-Id": "manual"},
        {**AUTH, "X-Trinity-Execution-Id": "${HOME:-manual}"},
        {**AUTH, "x-trinity-execution-id": LITERAL},
        {**AUTH, "X-Trinity-Execution-Id": LITERAL, "X-Extra": "1"},
        {"X-Trinity-Execution-Id": LITERAL},
    ],
    ids=["no-default", "static", "other-var", "lowercase", "extra-header", "no-auth"],
)
def test_validator_rejects_variants(headers):
    with pytest.raises(McpValidationError, match="reserved"):
        validate_mcp_config(_cfg(headers))
