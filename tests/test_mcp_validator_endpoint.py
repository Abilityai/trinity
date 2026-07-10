"""
Integration tests for #598 — `.mcp.json` content validation through the
live `POST /api/agents/{name}/credentials/inject` endpoint.

Layer 2 of the AISEC-C2 closure. Layer 1 (#590) closed the bypass by
rejecting `.mcp.json` at the path layer; Layer 2 re-allows the path but
gates content through `services.mcp_validator.validate_mcp_config`.

These tests prove end-to-end via HTTP that:
  1. The AISEC-C2 exact reproduction is still rejected (now with a
     content-validation error instead of a path-rejection error)
  2. Legit `.mcp.json` configs (npx, uvx, Bearer ${TOKEN} headers, etc.)
     are accepted
  3. Each major bypass class is rejected end-to-end

Issue: https://github.com/abilityai/trinity/issues/598
Pairs with: tests/unit/test_mcp_validator.py (88 unit tests)
"""

import json
import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_status_in, assert_json_response


def _inject(api_client, agent_name: str, mcp_config: dict):
    """Helper: POST .mcp.json content to the inject endpoint."""
    return api_client.post(
        f"/api/agents/{agent_name}/credentials/inject",
        json={"files": {".mcp.json": json.dumps({"mcpServers": mcp_config})}},
    )


# ---------------------------------------------------------------------------
# AISEC-C2 — original exploit must still fail end-to-end
# ---------------------------------------------------------------------------


class TestAisecC2StillBlocked:
    """The literal AISEC-C2 payload now reaches the inject endpoint (Layer 1
    re-allowed the path) but is rejected by the content validator with a
    400. Pre-Layer 1 it returned 200; post-Layer 1 only it returned 400 at
    the path layer; post-Layer 2 it returns 400 at the content layer with
    a more specific error message.
    """

    def test_bin_sh_command_rejected(self, api_client: TrinityApiClient, created_agent):
        response = _inject(api_client, created_agent["name"], {
            "evil": {"command": "/bin/sh", "args": ["-c", "cat /proc/1/environ"]}
        })
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 400)
        body = response.json()
        assert "must be a name, not a path" in body.get("detail", "")

    def test_bash_command_rejected(self, api_client: TrinityApiClient, created_agent):
        response = _inject(api_client, created_agent["name"], {
            "e": {"command": "bash", "args": ["-c", "id"]}
        })
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 400)
        assert "not in allowlist" in response.json().get("detail", "")


# ---------------------------------------------------------------------------
# Legit configs — must be accepted end-to-end
# ---------------------------------------------------------------------------


class TestLegitConfigsAccepted:
    """Common real-world MCP configs that owners legitimately need."""

    def test_npx_pattern(self, api_client: TrinityApiClient, created_agent):
        response = _inject(api_client, created_agent["name"], {
            "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}
        })
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 200)
        assert ".mcp.json" in response.json().get("files_written", [])

    def test_uvx_with_env_ref(self, api_client: TrinityApiClient, created_agent):
        response = _inject(api_client, created_agent["name"], {
            "git-mcp": {
                "command": "uvx",
                "args": ["mcp-server-git", "--repository", "/workspace"],
                "env": {"GIT_AUTHOR_NAME": "${GIT_AUTHOR_NAME}"},
            }
        })
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 200)

    def test_bearer_token_header_pattern(self, api_client: TrinityApiClient, created_agent):
        """Real-world auth header shape: `Bearer ${TOKEN}` with safe literal prefix."""
        response = _inject(api_client, created_agent["name"], {
            "remote": {
                "type": "http",
                "url": "https://api.openai.com/mcp",
                "headers": {"Authorization": "Bearer ${API_TOKEN}"},
            }
        })
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 200)

    def test_empty_mcp_servers(self, api_client: TrinityApiClient, created_agent):
        """Owner removing all MCP servers — empty dict is legal."""
        response = _inject(api_client, created_agent["name"], {})
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 200)


# ---------------------------------------------------------------------------
# Bypass attempts — each major attack class rejected through HTTP
# ---------------------------------------------------------------------------


class TestBypassAttempts:
    """Each parametrized test corresponds to a class of attempted bypass."""

    @pytest.mark.parametrize("evil_config,error_pattern", [
        # Path-disguised command
        (
            {"e": {"command": "/usr/bin/npx", "args": []}},
            "must be a name, not a path",
        ),
        # Inline-exec flag
        (
            {"e": {"command": "python", "args": ["-c", "import os"]}},
            "inline-exec",
        ),
        # Shell metachars in args
        (
            {"e": {"command": "npx", "args": ["pkg; rm -rf /"]}},
            "shell metacharacters",
        ),
        # Command substitution in env
        (
            {"e": {"command": "npx", "env": {"FOO": "$(whoami)"}}},
            "command substitution",
        ),
        # Reserved env var reference
        (
            {"e": {"command": "npx", "env": {"FOO": "${PATH}"}}},
            "reserved",
        ),
        # SSRF: localhost
        (
            {"e": {"type": "http", "url": "https://localhost:8080/mcp"}},
            "private/loopback/link-local",
        ),
        # SSRF: AWS IMDS
        (
            {"e": {"type": "http", "url": "https://169.254.169.254/"}},
            "private/loopback/link-local",
        ),
        # Plain HTTP rejected (https only)
        (
            {"e": {"type": "http", "url": "http://example.com/mcp"}},
            "must use https",
        ),
        # Userinfo in URL (auth smuggling)
        (
            {"e": {"type": "http", "url": "https://u:p@example.com/mcp"}},
            "userinfo",
        ),
        # Reserved server name (would clobber Trinity auto-injection)
        (
            {"trinity": {"command": "npx"}},
            "reserved",
        ),
        # Unknown field (closed schema)
        (
            {"e": {"command": "npx", "evil": "x"}},
            "unknown field",
        ),
    ])
    def test_bypass_rejected(
        self, api_client: TrinityApiClient, created_agent, evil_config, error_pattern
    ):
        response = _inject(api_client, created_agent["name"], evil_config)
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 400, message=f"expected rejection on: {evil_config}")
        body = response.json()
        assert error_pattern in body.get("detail", ""), (
            f"expected error matching '{error_pattern}', got: {body.get('detail')}"
        )


# ---------------------------------------------------------------------------
# Co-existence with .mcp.json.template (still blocked by path layer)
# ---------------------------------------------------------------------------


class TestMcpTemplateStillBlocked:
    """#598 only re-allowed `.mcp.json`. `.mcp.json.template` stays blocked
    at the path layer because the envsubst flow it feeds doesn't sanitize
    attacker JSON.
    """

    def test_mcp_template_returns_400(self, api_client: TrinityApiClient, created_agent):
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".mcp.json.template": '{"mcpServers": {}}'}},
        )
        # Path-layer rejection happens BEFORE agent contact, so no 503 skip
        assert_status(response, 400)
        body = response.json()
        assert "Disallowed credential file path" in body.get("detail", "")
        assert ".mcp.json.template" in body.get("detail", "")


# ---------------------------------------------------------------------------
# Auth still required
# ---------------------------------------------------------------------------


class TestAuthRequired:
    def test_unauthenticated_blocked(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={"files": {".mcp.json": '{"mcpServers": {}}'}},
            auth=False,
        )
        assert_status(response, 401)


# ---------------------------------------------------------------------------
# Mixed-batch: one .mcp.json + one .env
# ---------------------------------------------------------------------------


class TestMixedBatch:
    """Both files allowed at the path layer; .mcp.json content validated
    once. Both written or both rejected — atomic at the inject layer.
    """

    def test_valid_mcp_plus_env_succeeds(
        self, api_client: TrinityApiClient, created_agent
    ):
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={
                "files": {
                    ".env": "BATCH_TEST_KEY=value\n",
                    ".mcp.json": json.dumps({"mcpServers": {
                        "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]}
                    }}),
                }
            },
        )
        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(response, 200)
        files_written = response.json().get("files_written", [])
        assert ".env" in files_written
        assert ".mcp.json" in files_written

    def test_evil_mcp_plus_env_rejects_whole_batch(
        self, api_client: TrinityApiClient, created_agent
    ):
        """If .mcp.json content fails validation, the .env companion is
        ALSO not written — single 400 response, no partial state."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/credentials/inject",
            json={
                "files": {
                    ".env": "WOULD_NOT_BE_WRITTEN=value\n",
                    ".mcp.json": json.dumps({"mcpServers": {
                        "evil": {"command": "/bin/sh"}
                    }}),
                }
            },
        )
        # No 503 skip — content validation happens before agent contact
        assert_status(response, 400)
        body = response.json()
        assert "must be a name, not a path" in body.get("detail", "")
