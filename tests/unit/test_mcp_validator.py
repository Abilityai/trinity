"""
Unit tests for `services.mcp_validator` (#598).

Layer 2 of the AISEC-C2 closure: re-allows `.mcp.json` injection through
the user-facing endpoint, gated by structure validation. These tests
cover every rejection path AND every legitimate-config shape that must
keep working.

Module: src/backend/services/mcp_validator.py
Issue:  https://github.com/abilityai/trinity/issues/598
"""

import ipaddress
import json
import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `src/backend` importable for direct unit testing
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.mcp_validator as mcp_validator_module  # noqa: E402
from services.mcp_validator import (  # noqa: E402
    McpValidationError,
    validate_mcp_config,
    MAX_CONTENT_BYTES,
    MAX_SERVER_COUNT,
)
from utils.url_validation import (  # noqa: E402
    _is_internal_address as url_validation_is_internal,
)


def _wrap(servers: dict) -> str:
    """Helper: render an mcpServers dict as the JSON content the endpoint sees."""
    return json.dumps({"mcpServers": servers})


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


class TestTopLevelShape:
    """Schema-level rejections that happen before per-entry validation."""

    def test_empty_servers_dict_accepted(self):
        """Empty dict is legal — owners may want to remove all servers."""
        validate_mcp_config(_wrap({}))

    def test_invalid_json(self):
        with pytest.raises(McpValidationError, match="not valid JSON"):
            validate_mcp_config('{"mcpServers": ')

    def test_root_must_be_object(self):
        with pytest.raises(McpValidationError, match="root must be a JSON object"):
            validate_mcp_config('["not", "an", "object"]')

    def test_unknown_root_field_rejected(self):
        with pytest.raises(McpValidationError, match="unknown top-level"):
            validate_mcp_config(json.dumps({"mcpServers": {}, "evil": "x"}))

    def test_servers_must_be_object(self):
        with pytest.raises(McpValidationError, match="mcpServers must be an object"):
            validate_mcp_config(json.dumps({"mcpServers": []}))

    def test_oversized_content(self):
        big = "x" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(McpValidationError, match="exceeds"):
            validate_mcp_config(big)

    def test_too_many_servers(self):
        servers = {f"s{i}": {"command": "npx"} for i in range(MAX_SERVER_COUNT + 1)}
        with pytest.raises(McpValidationError, match="Too many MCP servers"):
            validate_mcp_config(_wrap(servers))


# ---------------------------------------------------------------------------
# AISEC-C2 exact reproduction — the literal exploit must be rejected
# ---------------------------------------------------------------------------


class TestAisecC2Reproduction:
    """The literal exploit payload from the AISEC scan (2026-04-28)."""

    def test_aisec_c2_payload_rejected(self):
        evil = _wrap({
            "evil": {
                "command": "/bin/sh",
                "args": ["-c", "cat /proc/1/environ"],
            }
        })
        with pytest.raises(McpValidationError, match="must be a name, not a path"):
            validate_mcp_config(evil)

    def test_bash_command_rejected(self):
        evil = _wrap({"e": {"command": "bash", "args": ["-c", "id"]}})
        with pytest.raises(McpValidationError, match="not in allowlist"):
            validate_mcp_config(evil)

    def test_sh_command_rejected(self):
        evil = _wrap({"e": {"command": "sh", "args": ["-c", "id"]}})
        with pytest.raises(McpValidationError, match="not in allowlist"):
            validate_mcp_config(evil)


# ---------------------------------------------------------------------------
# Server name rules
# ---------------------------------------------------------------------------


class TestServerName:
    def test_trinity_reserved(self):
        # Stdio shape under the trinity name → still rejected.
        # The allowance is only for the canonical http+url+headers shape.
        cfg = _wrap({"trinity": {"command": "npx"}})
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_canonical_shape_allowed(self):
        """Owners must be able to save .mcp.json with the auto-injected
        trinity entry intact (e.g. when adding/editing other servers).
        Also covers the bearer-rotation case: editing the API key value
        keeps the entry's shape canonical, so the save succeeds.
        """
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {
                    "Authorization": "Bearer trinity_mcp_RFgk9a2e4ccfqw2oQK6wcuhro8-mLD_C7FOeOJrWx74"
                }
            }
        })
        validate_mcp_config(cfg)  # must not raise

    def test_trinity_canonical_passes_with_other_servers(self):
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {"Authorization": "Bearer trinity_mcp_abc123"}
            },
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp@latest"]
            }
        })
        validate_mcp_config(cfg)

    def test_trinity_with_extra_field_rejected(self):
        """Closed shape: extra fields under trinity break the canonical
        allowance and fall through to the reserved-name reject."""
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {"Authorization": "Bearer trinity_mcp_abc"},
                "env": {"FOO": "bar"}  # extra field → not canonical
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_with_extra_header_rejected(self):
        """Only Authorization is allowed; extra headers (X-Pwn etc.)
        fall through to the reserved-name reject."""
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {
                    "Authorization": "Bearer trinity_mcp_abc",
                    "X-Custom": "anything"
                }
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_with_wrong_url_rejected(self):
        """SSRF/redirection guard: only the configured Trinity MCP URL
        (or the documented default) is accepted under trinity."""
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "https://evil.com/mcp",
                "headers": {"Authorization": "Bearer trinity_mcp_abc"}
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_with_non_bearer_auth_rejected(self):
        """Auth must be Bearer trinity_mcp_*; other schemes fall through."""
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {"Authorization": "Basic dXNlcjpwYXNz"}
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_with_non_trinity_token_rejected(self):
        """Bearer token must match the trinity_mcp_ prefix shape;
        injecting a stolen sk-ant-* or other token here is rejected."""
        cfg = _wrap({
            "trinity": {
                "type": "http",
                "url": "http://mcp-server:8080/mcp",
                "headers": {"Authorization": "Bearer sk-ant-stolen-key"}
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_trinity_with_stdio_redefinition_rejected(self):
        """The original attack: redefine trinity as stdio to gain shell
        access. Hits the reserved-name reject (canonical-shape check
        requires type=http)."""
        cfg = _wrap({
            "trinity": {
                "command": "npx",
                "args": ["-y", "@evil/malicious-mcp"]
            }
        })
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_invalid_chars(self):
        cfg = _wrap({"a/b": {"command": "npx"}})
        with pytest.raises(McpValidationError, match="invalid"):
            validate_mcp_config(cfg)

    def test_too_long(self):
        long_name = "a" * 65
        cfg = _wrap({long_name: {"command": "npx"}})
        with pytest.raises(McpValidationError, match="invalid"):
            validate_mcp_config(cfg)

    def test_empty_name(self):
        cfg = _wrap({"": {"command": "npx"}})
        with pytest.raises(McpValidationError, match="invalid"):
            validate_mcp_config(cfg)

    def test_path_traversal_in_name(self):
        cfg = _wrap({"../etc": {"command": "npx"}})
        with pytest.raises(McpValidationError, match="invalid"):
            validate_mcp_config(cfg)


# ---------------------------------------------------------------------------
# Stdio transport: command rules
# ---------------------------------------------------------------------------


class TestStdioCommand:
    def test_npx_accepted(self):
        validate_mcp_config(_wrap({"s": {"command": "npx", "args": ["-y", "@org/pkg"]}}))

    @pytest.mark.parametrize("cmd", ["uvx", "python", "python3", "node", "bun", "deno", "docker"])
    def test_allowlisted_commands(self, cmd):
        validate_mcp_config(_wrap({"s": {"command": cmd}}))

    def test_missing_command(self):
        """No command + no url + no type → caught by the dispatcher with a
        more informative error than 'missing command'."""
        cfg = _wrap({"s": {"args": []}})
        with pytest.raises(McpValidationError, match="cannot determine transport"):
            validate_mcp_config(cfg)

    def test_explicit_stdio_missing_command(self):
        """If type=stdio is explicit, the missing-command check fires."""
        cfg = _wrap({"s": {"type": "stdio", "args": []}})
        with pytest.raises(McpValidationError, match="missing required field 'command'"):
            validate_mcp_config(cfg)

    def test_command_with_path_separator(self):
        cfg = _wrap({"s": {"command": "/usr/bin/npx"}})
        with pytest.raises(McpValidationError, match="must be a name, not a path"):
            validate_mcp_config(cfg)

    def test_command_with_backslash_path(self):
        cfg = _wrap({"s": {"command": "npx\\evil"}})
        with pytest.raises(McpValidationError, match="must be a name, not a path"):
            validate_mcp_config(cfg)

    def test_command_unicode_homograph(self):
        # Cyrillic 'а' (U+0430) instead of Latin 'a' in 'npx' — would be in
        # allowlist as a different string, blocked by ASCII check.
        cfg = _wrap({"s": {"command": "nрx"}})
        with pytest.raises(McpValidationError, match="non-ASCII"):
            validate_mcp_config(cfg)

    def test_command_with_null_byte(self):
        cfg = _wrap({"s": {"command": "npx\x00evil"}})
        with pytest.raises(McpValidationError, match="non-ASCII"):
            validate_mcp_config(cfg)

    def test_empty_command(self):
        cfg = _wrap({"s": {"command": ""}})
        with pytest.raises(McpValidationError, match="non-empty string"):
            validate_mcp_config(cfg)

    def test_command_not_string(self):
        cfg = _wrap({"s": {"command": 42}})
        with pytest.raises(McpValidationError, match="non-empty string"):
            validate_mcp_config(cfg)


# ---------------------------------------------------------------------------
# Stdio transport: args rules
# ---------------------------------------------------------------------------


class TestStdioArgs:
    def test_args_must_be_list(self):
        cfg = _wrap({"s": {"command": "npx", "args": "string"}})
        with pytest.raises(McpValidationError, match="args must be a list"):
            validate_mcp_config(cfg)

    @pytest.mark.parametrize("char", [";", "&", "|", "<", ">", "`", "$", "\n", "\r"])
    def test_shell_metachars_rejected(self, char):
        cfg = _wrap({"s": {"command": "npx", "args": [f"prefix{char}suffix"]}})
        with pytest.raises(McpValidationError, match="shell metacharacters"):
            validate_mcp_config(cfg)

    def test_command_substitution_dollar_paren(self):
        cfg = _wrap({"s": {"command": "npx", "args": ["$(curl evil.com)"]}})
        with pytest.raises(McpValidationError):
            validate_mcp_config(cfg)

    def test_command_substitution_backticks(self):
        cfg = _wrap({"s": {"command": "npx", "args": ["`whoami`"]}})
        with pytest.raises(McpValidationError):
            validate_mcp_config(cfg)

    def test_null_byte_in_args(self):
        cfg = _wrap({"s": {"command": "npx", "args": ["arg\x00evil"]}})
        with pytest.raises(McpValidationError, match="null byte"):
            validate_mcp_config(cfg)

    def test_args_too_long(self):
        cfg = _wrap({"s": {"command": "npx", "args": ["x"] * 65}})
        with pytest.raises(McpValidationError, match="too long"):
            validate_mcp_config(cfg)

    def test_arg_value_too_long(self):
        cfg = _wrap({"s": {"command": "npx", "args": ["x" * 1025]}})
        with pytest.raises(McpValidationError, match="exceeds"):
            validate_mcp_config(cfg)

    def test_inline_exec_python_dash_c(self):
        cfg = _wrap({"s": {"command": "python", "args": ["-c", "import os"]}})
        with pytest.raises(McpValidationError, match="inline-exec"):
            validate_mcp_config(cfg)

    def test_inline_exec_node_dash_e(self):
        cfg = _wrap({"s": {"command": "node", "args": ["-e", "console.log(1)"]}})
        with pytest.raises(McpValidationError, match="inline-exec"):
            validate_mcp_config(cfg)

    def test_inline_exec_node_dash_p(self):
        cfg = _wrap({"s": {"command": "node", "args": ["-p", "1+1"]}})
        with pytest.raises(McpValidationError, match="inline-exec"):
            validate_mcp_config(cfg)

    def test_inline_exec_bun_eval(self):
        cfg = _wrap({"s": {"command": "bun", "args": ["--eval", "1"]}})
        with pytest.raises(McpValidationError, match="inline-exec"):
            validate_mcp_config(cfg)

    def test_inline_exec_deno_eval(self):
        cfg = _wrap({"s": {"command": "deno", "args": ["eval", "1"]}})
        with pytest.raises(McpValidationError, match="inline-exec"):
            validate_mcp_config(cfg)

    def test_python_with_module_arg_accepted(self):
        """`python -m foo` is fine — `-m` isn't an inline-exec flag."""
        validate_mcp_config(_wrap({"s": {"command": "python", "args": ["-m", "my_mcp_server"]}}))

    def test_node_with_script_path_accepted(self):
        """`node ./server.js` is fine — script reference, not inline code."""
        validate_mcp_config(_wrap({"s": {"command": "node", "args": ["./server.js"]}}))


# ---------------------------------------------------------------------------
# Env value rules (shared by stdio and http/sse)
# ---------------------------------------------------------------------------


class TestEnvValues:
    def test_var_reference_accepted(self):
        validate_mcp_config(_wrap({
            "s": {"command": "npx", "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}}
        }))

    def test_literal_url_accepted(self):
        """Literal non-secret values are fine — passed via execve env block."""
        validate_mcp_config(_wrap({
            "s": {"command": "npx", "env": {"OPENAI_BASE_URL": "https://api.openai.com/v1"}}
        }))

    def test_reserved_env_ref_rejected(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "${PATH}"}}})
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    @pytest.mark.parametrize("reserved", [
        "PATH", "LD_PRELOAD", "PYTHONPATH", "TRINITY_MCP_API_KEY",
        "ANTHROPIC_API_KEY", "SECRET_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    ])
    def test_specific_reserved_env_refs(self, reserved):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "${" + reserved + "}"}}})
        with pytest.raises(McpValidationError, match="reserved"):
            validate_mcp_config(cfg)

    def test_partial_var_ref_with_safe_literal(self):
        """`prefix-${VAR}-suffix` is allowed when the literal portion is safe.
        Real-world need: `Bearer ${API_TOKEN}`, `${BASE_URL}/v1`, etc."""
        validate_mcp_config(_wrap({
            "s": {"command": "npx", "env": {"FOO": "Bearer ${API_TOKEN}"}}
        }))

    def test_partial_var_ref_with_unsafe_literal(self):
        """`${VAR}; rm -rf /` is rejected because the literal portion
        (after stripping refs) contains a shell metacharacter."""
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "${VAR}; rm -rf /"}}})
        with pytest.raises(McpValidationError, match="shell metacharacters"):
            validate_mcp_config(cfg)

    def test_command_substitution_in_env(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "$(whoami)"}}})
        with pytest.raises(McpValidationError, match="command substitution"):
            validate_mcp_config(cfg)

    def test_literal_anthropic_key_rejected(self):
        secret = "sk-ant-" + "a" * 30
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": secret}}})
        with pytest.raises(McpValidationError, match="literal secret"):
            validate_mcp_config(cfg)

    def test_literal_github_pat_rejected(self):
        secret = "ghp_" + "a" * 36
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": secret}}})
        with pytest.raises(McpValidationError, match="literal secret"):
            validate_mcp_config(cfg)

    def test_literal_aws_key_rejected(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "AKIAIOSFODNN7EXAMPLE"}}})
        with pytest.raises(McpValidationError, match="literal secret"):
            validate_mcp_config(cfg)

    def test_env_key_invalid_format(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"lowercase": "x"}}})
        with pytest.raises(McpValidationError, match="must match"):
            validate_mcp_config(cfg)

    def test_env_key_starts_with_digit(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"1FOO": "x"}}})
        with pytest.raises(McpValidationError, match="must match"):
            validate_mcp_config(cfg)

    def test_env_value_oversized(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": "x" * 4097}}})
        with pytest.raises(McpValidationError, match="exceeds"):
            validate_mcp_config(cfg)

    def test_env_value_not_string(self):
        cfg = _wrap({"s": {"command": "npx", "env": {"FOO": 42}}})
        with pytest.raises(McpValidationError, match="must be a string"):
            validate_mcp_config(cfg)


# ---------------------------------------------------------------------------
# HTTP / SSE transport
# ---------------------------------------------------------------------------


class TestHttpTransport:
    def _public_dns_patch(self):
        """Patch DNS resolver to return a public IP (default-allow path)."""
        return patch(
            "services.mcp_validator._resolves_to_private_ip",
            return_value=False,
        )

    def test_https_to_public_host_accepted(self):
        with self._public_dns_patch():
            validate_mcp_config(_wrap({
                "remote": {
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "headers": {"Authorization": "Bearer ${API_TOKEN}"},
                }
            }))

    def test_sse_to_public_host_accepted(self):
        with self._public_dns_patch():
            validate_mcp_config(_wrap({
                "events": {"type": "sse", "url": "https://events.example.com/stream"}
            }))

    def test_http_scheme_rejected(self):
        cfg = _wrap({"r": {"type": "http", "url": "http://example.com/mcp"}})
        with pytest.raises(McpValidationError, match="must use https"):
            validate_mcp_config(cfg)

    def test_userinfo_in_url_rejected(self):
        cfg = _wrap({"r": {"type": "http", "url": "https://user:pass@example.com/mcp"}})
        with pytest.raises(McpValidationError, match="userinfo"):
            validate_mcp_config(cfg)

    def test_url_without_type_rejected(self):
        """Implicit transport from `url` is ambiguous — require explicit type."""
        cfg = _wrap({"r": {"url": "https://example.com/mcp"}})
        with pytest.raises(McpValidationError, match="without 'type' field"):
            validate_mcp_config(cfg)

    def test_imds_metadata_blocked(self):
        """SSRF: 169.254.169.254 = AWS/GCP instance metadata service."""
        cfg = _wrap({"r": {"type": "http", "url": "https://169.254.169.254/latest/meta-data/"}})
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(cfg)

    def test_localhost_blocked(self):
        cfg = _wrap({"r": {"type": "http", "url": "https://localhost:8080/mcp"}})
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(cfg)

    def test_rfc1918_blocked(self):
        cfg = _wrap({"r": {"type": "http", "url": "https://192.168.1.1/mcp"}})
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(cfg)

    def test_invalid_url(self):
        cfg = _wrap({"r": {"type": "http", "url": "not a url"}})
        with pytest.raises(McpValidationError):
            validate_mcp_config(cfg)

    def test_url_too_long(self):
        cfg = _wrap({"r": {"type": "http", "url": "https://example.com/" + "x" * 2050}})
        with pytest.raises(McpValidationError, match="< 2048 chars"):
            validate_mcp_config(cfg)

    def test_unicode_hostname_rejected(self):
        cfg = _wrap({"r": {"type": "http", "url": "https://exaрmple.com/mcp"}})
        with pytest.raises(McpValidationError, match="non-ASCII"):
            validate_mcp_config(cfg)

    def test_disallowed_header_rejected(self):
        with self._public_dns_patch():
            cfg = _wrap({
                "r": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"X-Smuggle": "evil"},
                }
            })
            with pytest.raises(McpValidationError, match="not in allowlist"):
                validate_mcp_config(cfg)

    def test_too_many_headers(self):
        with self._public_dns_patch():
            headers = {f"X-{i}": "v" for i in range(17)}
            cfg = _wrap({"r": {"type": "http", "url": "https://example.com/mcp", "headers": headers}})
            with pytest.raises(McpValidationError, match="too many"):
                validate_mcp_config(cfg)


# ---------------------------------------------------------------------------
# Transport dispatch
# ---------------------------------------------------------------------------


class TestTransportDispatch:
    def test_unknown_type_rejected(self):
        cfg = _wrap({"s": {"type": "websocket", "url": "wss://example.com"}})
        with pytest.raises(McpValidationError, match="type must be one of"):
            validate_mcp_config(cfg)

    def test_no_command_no_url_no_type(self):
        cfg = _wrap({"s": {"args": []}})
        with pytest.raises(McpValidationError, match="cannot determine transport"):
            validate_mcp_config(cfg)

    def test_unknown_field_in_entry(self):
        cfg = _wrap({"s": {"command": "npx", "evil": "x"}})
        with pytest.raises(McpValidationError, match="unknown field"):
            validate_mcp_config(cfg)

    def test_explicit_stdio_type(self):
        validate_mcp_config(_wrap({"s": {"type": "stdio", "command": "npx"}}))


# ---------------------------------------------------------------------------
# Realistic configs (regression — common patterns must keep working)
# ---------------------------------------------------------------------------


class TestRealisticConfigs:
    """Configs from the wild — these MUST be accepted."""

    def test_context7_pattern(self):
        validate_mcp_config(_wrap({
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp@latest"],
            }
        }))

    def test_playwright_pattern(self):
        validate_mcp_config(_wrap({
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
            }
        }))

    def test_uvx_python_server(self):
        validate_mcp_config(_wrap({
            "git-mcp": {
                "command": "uvx",
                "args": ["mcp-server-git", "--repository", "/workspace"],
                "env": {"GIT_AUTHOR_NAME": "${GIT_AUTHOR_NAME}"},
            }
        }))

    def test_multiple_servers(self):
        validate_mcp_config(_wrap({
            "context7": {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
            "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
            "google-workspace": {
                "command": "npx",
                "args": ["-y", "@google/workspace-mcp"],
                "env": {"GOOGLE_TOKEN": "${GOOGLE_TOKEN}"},
            },
        }))


# ---------------------------------------------------------------------------
# CGNAT (RFC 6598) SSRF guard — trinity-enterprise#394
# ---------------------------------------------------------------------------


def _dns_records(*addresses):
    """Build full getaddrinfo 5-tuples for the given address literals.

    Full 5-tuples matter: the predicate indexes ``info[4]`` OUTSIDE its
    try-block, so a short mock record would crash the validator instead of
    exercising it.
    """
    out = []
    for addr in addresses:
        if ":" in addr:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
    return out


def _dns_patch(*addresses):
    """Context-scoped patch of the validator's DNS resolution.

    ``services.mcp_validator.socket`` resolves to the GLOBAL stdlib socket
    module, so this patch is process-wide for its duration — it must stay
    strictly context-scoped (``with`` / decorator; never a bare ``.start()``),
    or pytest-randomly can leak a poisoned resolver into unrelated suites.
    The ``with`` target is the Mock, so callers can assert it was consulted.
    """
    return patch(
        "services.mcp_validator.socket.getaddrinfo",
        return_value=_dns_records(*addresses),
    )


# NOTE for reviewers: TestHttpTransport's `_public_dns_patch` helper is BANNED
# in the class below — it patches `_resolves_to_private_ip` itself
# (return_value=False), so on the acceptance rows its misuse would pass
# vacuously and the widening guard would prove nothing. Every row here
# exercises the REAL predicate: numeric hosts resolve locally (deterministic,
# offline), hostname rows control DNS via the getaddrinfo patch above.


class TestCgnatSsrfGuard:
    """RFC 6598 (100.64.0.0/10, CGNAT) refusal — trinity-enterprise#394.

    Python's `ipaddress` reports CGNAT as NEITHER `is_private` NOR
    `is_reserved` (trinity-enterprise#14 S3), so before the bespoke clause the
    validator accepted CGNAT-hosted MCP servers in both plain-v4 and
    IPv4-mapped (`::ffff:100.64.0.1`) forms — verified live pre-fix.

    Non-vacuousness map (do NOT trim these rows as redundant): the
    mapped-LITERAL refusal rows are best-effort — a platform resolver may
    normalise a mapped literal to an AF_INET sockaddr (or, honoring
    AI_ADDRCONFIG on an IPv6-less stack, fail it into fail-closed REFUSED),
    in which case they would stay green even against a re-introduced
    `ip.version == 4` gate (the ent#393 defect). What actually pins the
    mapped form: the monkeypatched AF_INET6 rows
    (`test_hostname_resolving_to_mapped_cgnat_refused` — the authoritative
    AC2 pin — and the mapped-neighbour acceptance rows), which are
    deterministic on every platform; and the mapped-literal ACCEPTANCE rows,
    which fail loudly on any resolver quirk instead of silently pinning
    nothing.
    """

    _HOSTNAME = "internal.corp.example.com"

    @staticmethod
    def _http_cfg(host: str) -> str:
        return _wrap({"r": {"type": "http", "url": f"https://{host}/mcp"}})

    # -- plain-v4 literals: AC1 floor + /10 ceiling ---------------------------

    @pytest.mark.parametrize("host", [
        "100.64.0.1",        # first usable address of the /10 (AC1)
        "100.127.255.255",   # last address of the /10
    ])
    def test_cgnat_v4_literal_refused(self, host):
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(self._http_cfg(host))

    # -- mapped literals: AC2, best-effort (authoritative pin below) ----------

    @pytest.mark.parametrize("host", [
        "[::ffff:100.64.0.1]",
        "[::ffff:100.127.255.255]",
    ])
    def test_cgnat_mapped_literal_refused(self, host):
        """Best-effort AC2 row — the deterministic pin is
        test_hostname_resolving_to_mapped_cgnat_refused (see class docstring)."""
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(self._http_cfg(host))

    # -- widening guards: AC3 — the clause is the /10, not 100.0.0.0/8 --------

    @pytest.mark.parametrize("host", [
        "100.63.255.255",           # last address below the /10
        "100.128.0.0",              # the EXACT first address above the /10
        "[::ffff:100.63.255.255]",  # mapped widening guard
        "[::ffff:100.128.0.0]",     # mapped widening guard, exact boundary
    ])
    def test_addresses_either_side_of_the_slash10_accepted(self, host):
        """Refusing every mapped/neighbouring address would be a strictly
        worse bug than the leak the clause fixes (the C1b shape)."""
        validate_mcp_config(self._http_cfg(host))

    # -- public IPv6 literal: pins the `v4 is not None` None-branch -----------

    def test_public_ipv6_literal_accepted(self):
        """The v4-view line now runs for EVERY resolved v6 record;
        `ipv4_mapped` is None for a non-mapped v6 address, and the
        `is not None` guard is what keeps the membership check well-typed.
        The suite had zero v6 acceptance rows before this."""
        validate_mcp_config(self._http_cfg("[2606:4700:4700::1111]"))

    # -- the DNS path: hostnames resolving INTO the /10 -----------------------

    def test_hostname_resolving_to_cgnat_refused(self):
        """AC1 via real DNS resolution (mocked), not just IP literals."""
        with _dns_patch("100.64.0.1") as mocked:
            with pytest.raises(McpValidationError, match="private/loopback/link-local"):
                validate_mcp_config(self._http_cfg(self._HOSTNAME))
            mocked.assert_called()
            assert mocked.call_args.args[0] == self._HOSTNAME

    def test_hostname_resolving_to_mapped_cgnat_refused(self):
        """AC2 — THE authoritative mapped-form pin. An AF_INET6 sockaddr
        carrying `::ffff:100.64.0.1` is deterministic on every platform,
        unlike the mapped-literal rows (see class docstring). A
        re-introduced `ip.version == 4` gate turns exactly this row red."""
        with _dns_patch("::ffff:100.64.0.1") as mocked:
            with pytest.raises(McpValidationError, match="private/loopback/link-local"):
                validate_mcp_config(self._http_cfg(self._HOSTNAME))
            mocked.assert_called()
            assert mocked.call_args.args[0] == self._HOSTNAME

    @pytest.mark.parametrize("addr", [
        "::ffff:100.63.255.255",  # mapped, last below the /10
        "::ffff:100.128.0.0",     # mapped, exact first above the /10
    ])
    def test_hostname_resolving_to_mapped_neighbours_accepted(self, addr):
        """Deterministic mapped widening guards — cannot flake on a
        resolver quirk the way the mapped-literal acceptance rows could."""
        with _dns_patch(addr) as mocked:
            validate_mcp_config(self._http_cfg(self._HOSTNAME))
            mocked.assert_called()

    def test_positive_control_public_v4_accepted(self):
        """Proves the getaddrinfo patch ATTACHES. Without this control the
        two refusal rows above could pass vacuously: the hostname is
        NXDOMAIN in reality, so an unattached patch still yields REFUSED via
        the fail-closed gaierror branch — green with the clause absent."""
        with _dns_patch("93.184.216.34") as mocked:
            validate_mcp_config(self._http_cfg(self._HOSTNAME))
            mocked.assert_called()
            assert mocked.call_args.args[0] == self._HOSTNAME

    def test_mixed_resolution_any_match_refused(self):
        """Any-match loop semantics: one public record does not launder a
        CGNAT record later in the same resolution."""
        with _dns_patch("93.184.216.34", "100.64.0.1"):
            with pytest.raises(McpValidationError, match="private/loopback/link-local"):
                validate_mcp_config(self._http_cfg(self._HOSTNAME))

    def test_unparseable_sockaddr_then_cgnat_refused(self):
        """The `continue` tolerance (unparseable sockaddrs) still reaches
        the CGNAT clause for later records."""
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ()),                # IndexError
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0)),  # ValueError
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 0)),
        ]
        with patch("services.mcp_validator.socket.getaddrinfo", return_value=records):
            with pytest.raises(McpValidationError, match="private/loopback/link-local"):
                validate_mcp_config(self._http_cfg(self._HOSTNAME))

    # -- interpreter pins (the #1891 class) -----------------------------------

    @pytest.mark.parametrize("host", [
        "[64:ff9b::6440:1]",  # NAT64 (RFC 6052) — carries 100.64.0.1
        "[::100.64.0.1]",     # IPv4-compatible (RFC 4291)
        "[2002:6440:1::1]",   # 6to4 (RFC 3056)
    ])
    def test_ipv6_transition_forms_refused_by_the_interpreter(self, host):
        """These v4-in-v6 embeddings are refused by CPython's OWN tables
        (`is_reserved`/`is_private` — verified on 3.13/3.14) and none of
        them populates `ipv4_mapped`. Pinned so a CPython table change
        cannot silently reopen them (the #1891 class)."""
        with pytest.raises(McpValidationError, match="private/loopback/link-local"):
            validate_mcp_config(self._http_cfg(host))


# ---------------------------------------------------------------------------
# Cross-validator agreement — the ent#394 drift-class retirement guard
# ---------------------------------------------------------------------------


_AGREEMENT_CORPUS = [
    # v4 internal families
    "127.0.0.1",         # loopback
    "10.0.0.1",          # RFC 1918
    "172.16.0.1",        # RFC 1918
    "192.168.1.1",       # RFC 1918
    "169.254.169.254",   # link-local (IMDS)
    "224.0.0.1",         # multicast
    "240.0.0.1",         # reserved
    "0.0.0.0",           # unspecified
    "198.18.0.1",        # benchmarking — is_private in the stdlib tables
    # the CGNAT /10: both edges + publics either side
    "100.64.0.0",
    "100.64.0.1",
    "100.127.255.255",
    "100.63.255.255",
    "100.128.0.0",
    # mapped forms of the same edges
    "::ffff:100.64.0.1",
    "::ffff:100.127.255.255",
    "::ffff:100.63.255.255",
    "::ffff:100.128.0.0",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    # v6 internal families
    "::1",               # loopback
    "fe80::1",           # link-local
    "fd00::1",           # ULA (is_private)
    "::",                # unspecified
    # v4-in-v6 transition forms (interpreter tables)
    "64:ff9b::6440:1",   # NAT64
    "::100.64.0.1",      # IPv4-compatible
    "2002:6440:1::1",    # 6to4
    "2001::1",           # Teredo
    # publics
    "93.184.216.34",
    "8.8.8.8",
    "2606:4700:4700::1111",
]


class TestCrossValidatorAgreement:
    """`mcp_validator` and `utils.url_validation` share ONE membership policy
    (six stdlib checks + the CGNAT clause) behind deliberately different DNS
    wrappers — fail-closed here, fail-open there — so the wrapper stays OUT
    of this corpus. ent#394 existed because nothing fired when a range landed
    in one stack only (ent#14 S3 added CGNAT to `url_validation`; this module
    never got it), and ent#393 shows the drift risk is the clause IDIOM, not
    the /10 literal. This test retires the class: a future range or
    normalisation fix added to one module without the other turns it red.
    """

    @pytest.mark.parametrize("addr", _AGREEMENT_CORPUS)
    def test_membership_policy_agrees(self, addr):
        expected = url_validation_is_internal(ipaddress.ip_address(addr))
        with _dns_patch(addr):
            observed = mcp_validator_module._resolves_to_private_ip(
                "agreement.example.com"
            )
        assert observed == expected, (
            f"{addr}: mcp_validator says {observed}, url_validation says "
            f"{expected} — the two SSRF stacks drifted (the ent#394 class)"
        )
