"""
Unit tests for agent-side credential sanitizer.

Tests the credential sanitization utility that prevents credential leakage
in agent execution logs, subprocess output, and Claude Code responses.

Module: docker/base-image/agent_server/utils/credential_sanitizer.py
"""

import pytest
import json
import os
import tempfile
from pathlib import Path

# Import the agent-side sanitizer directly from the module file
import sys
_project_root = Path(__file__).resolve().parents[2]
agent_utils_path = str(_project_root / 'docker' / 'base-image' / 'agent_server' / 'utils')
if agent_utils_path not in sys.path:
    sys.path.insert(0, agent_utils_path)

import credential_sanitizer as agent_sanitizer
from credential_sanitizer import (
    sanitize_text,
    sanitize_dict,
    sanitize_list,
    sanitize_json_string,
    sanitize_execution_log,
    sanitize_subprocess_line,
    refresh_credential_values,
    get_credential_values,
    REDACTION_PLACEHOLDER,
)


@pytest.mark.unit
class TestPatternDetection:
    """Test detection of secret patterns regardless of context."""

    def test_openai_api_key_pattern(self):
        """Detect and redact OpenAI API keys (sk-...)."""
        text = "My API key is sk-1234567890abcdefghij1234567890ghij1234567890"
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_openai_project_key_pattern(self):
        """Detect and redact OpenAI project keys (sk-proj-...)."""
        text = "Project key: sk-proj-abc123def456ghi789jkl012mno345pqr678"
        result = sanitize_text(text)
        assert "sk-proj-" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_anthropic_key_pattern(self):
        """Detect and redact Anthropic API keys (sk-ant-...)."""
        text = "Anthropic: sk-ant-api03-1234567890abcdefghij1234567890"
        result = sanitize_text(text)
        assert "sk-ant-" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_github_pat_fine_grained(self):
        """Detect and redact GitHub fine-grained PATs (ghp_...)."""
        text = "Token: ghp_1234567890abcdefghij1234567890abcdefabcdefghij1234567890abcdefghij1234567890abcdefghij1234567890abcdef"
        result = sanitize_text(text)
        assert "ghp_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_github_pat_classic(self):
        """Detect and redact GitHub classic PATs (github_pat_...)."""
        text = "Classic: github_pat_11ABCDEFG0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        result = sanitize_text(text)
        assert "github_pat_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_github_oauth_token(self):
        """Detect and redact GitHub OAuth tokens (gho_...)."""
        text = "OAuth: gho_1234567890abcdefghij1234567890abcdef"
        result = sanitize_text(text)
        assert "gho_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_github_app_token(self):
        """Detect and redact GitHub App tokens (ghs_...)."""
        text = "App: ghs_1234567890abcdefghij1234567890abcdef"
        result = sanitize_text(text)
        assert "ghs_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_github_refresh_token(self):
        """Detect and redact GitHub refresh tokens (ghr_...)."""
        text = "Refresh: ghr_1234567890abcdefghij1234567890abcdef"
        result = sanitize_text(text)
        assert "ghr_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_slack_bot_token(self):
        """Detect and redact Slack bot tokens (xoxb-...)."""
        text = "Bot: xoxb-FAKE-TEST-notreal"
        result = sanitize_text(text)
        assert "xoxb-" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_slack_user_token(self):
        """Detect and redact Slack user tokens (xoxp-...)."""
        text = "User: xoxp-FAKE-TEST-notreal"
        result = sanitize_text(text)
        assert "xoxp-" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_slack_app_token(self):
        """Detect and redact Slack app tokens (xoxa-...)."""
        text = "App: xoxa-FAKE-TEST-notreal"
        result = sanitize_text(text)
        assert "xoxa-" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_aws_access_key(self):
        """Detect and redact AWS access keys (AKIA...)."""
        text = "AWS: AKIAIOSFODNN7EXAMPLE"
        result = sanitize_text(text)
        assert "AKIA" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_trinity_mcp_key(self):
        """Detect and redact Trinity MCP keys (trinity_mcp_...)."""
        text = "MCP: trinity_mcp_1234567890abcdef"
        result = sanitize_text(text)
        assert "trinity_mcp_" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_bearer_token(self):
        """Detect and redact Bearer tokens."""
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = sanitize_text(text)
        assert "Bearer eyJ" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_basic_auth(self):
        """Detect and redact Basic auth credentials."""
        text = "Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQ="
        result = sanitize_text(text)
        assert "Basic dXNlcm5hbWU" not in result
        assert REDACTION_PLACEHOLDER in result


@pytest.mark.unit
class TestKeyValuePairs:
    """Test redaction of key=value pairs where key is sensitive."""

    def test_anthropic_api_key_unquoted(self):
        """Redact ANTHROPIC_API_KEY=value."""
        text = "ANTHROPIC_API_KEY=sk-ant-api03-1234567890abcdefghij12"
        result = sanitize_text(text)
        assert "sk-ant-api03-1234567890abcdefghij12" not in result
        assert f"ANTHROPIC_API_KEY={REDACTION_PLACEHOLDER}" in result

    def test_openai_api_key_quoted(self):
        """Redact OPENAI_API_KEY="value"."""
        text = 'OPENAI_API_KEY="sk-1234567890abcdefghij1234567890"'
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890" not in result
        assert f"OPENAI_API_KEY={REDACTION_PLACEHOLDER}" in result

    def test_github_token_single_quoted(self):
        """Redact GITHUB_TOKEN='value'."""
        text = "GITHUB_TOKEN='ghp_1234567890abcdefghij1234567890abcdefabcdefghij1234567890abcdefghij1234567890abcdef'"
        result = sanitize_text(text)
        assert "ghp_1234567890abcdefghij1234567890abcdefabcdefghij1234567890abcdef" not in result
        assert f"GITHUB_TOKEN={REDACTION_PLACEHOLDER}" in result

    def test_api_secret(self):
        """Redact API_SECRET=value."""
        text = "API_SECRET=super_secret_value_123"
        result = sanitize_text(text)
        assert "super_secret_value_123" not in result
        assert f"API_SECRET={REDACTION_PLACEHOLDER}" in result

    def test_password(self):
        """Redact PASSWORD=value."""
        text = "PASSWORD=my_secure_password"
        result = sanitize_text(text)
        assert "my_secure_password" not in result
        assert f"PASSWORD={REDACTION_PLACEHOLDER}" in result

    def test_bearer_auth(self):
        """Redact AUTH_BEARER=value."""
        text = "AUTH_BEARER=eyJhbGciOiJIUzI1NiJ9"
        result = sanitize_text(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert f"AUTH_BEARER={REDACTION_PLACEHOLDER}" in result

    def test_multiple_credentials_same_line(self):
        """Redact multiple credentials on same line."""
        text = "OPENAI_API_KEY=sk-1234567890abcdefghij1234567890ghij1234567890 ANTHROPIC_API_KEY=sk-ant-api03-456abcdefghij1234567890"
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert "sk-ant-api03-456abcdefghij1234567890" not in result
        assert result.count(REDACTION_PLACEHOLDER) >= 2

    def test_case_insensitive_key_matching(self):
        """Key patterns should match case-insensitively."""
        # Test each variant separately to avoid greedy regex matching
        text1 = "api_key=secret123value"
        result1 = sanitize_text(text1)
        assert "secret123value" not in result1

        text2 = "API_KEY=secret456value"
        result2 = sanitize_text(text2)
        assert "secret456value" not in result2

        text3 = "Api_Key=secret789value"
        result3 = sanitize_text(text3)
        assert "secret789value" not in result3


@pytest.mark.unit
class TestNestedStructures:
    """Test sanitization of nested dictionaries and lists."""

    def test_simple_dict(self):
        """Sanitize credentials in a simple dictionary."""
        data = {
            "api_key": "sk-1234567890abcdefghij1234567890",
            "name": "test-agent",
            "count": 42
        }
        result = sanitize_dict(data)
        assert result["api_key"] == REDACTION_PLACEHOLDER
        assert result["name"] == "test-agent"
        assert result["count"] == 42

    def test_nested_dict(self):
        """Sanitize credentials in nested dictionaries."""
        data = {
            "config": {
                "auth": {
                    "token": "ghp_1234567890abcdefghij1234567890abcdefabcdefghij1234567890abcdef",
                    "type": "github"
                },
                "name": "repo"
            }
        }
        result = sanitize_dict(data)
        assert result["config"]["auth"]["token"] == REDACTION_PLACEHOLDER
        assert result["config"]["auth"]["type"] == "github"

    def test_deeply_nested_dict(self):
        """Sanitize credentials in deeply nested structures."""
        data = {
            "level1": {
                "level2": {
                    "level3": {
                        "level4": {
                            "secret": "sk-ant-api03-1234567890abcdefghij12",
                            "public": "info"
                        }
                    }
                }
            }
        }
        result = sanitize_dict(data)
        assert result["level1"]["level2"]["level3"]["level4"]["secret"] == REDACTION_PLACEHOLDER
        assert result["level1"]["level2"]["level3"]["level4"]["public"] == "info"

    def test_simple_list(self):
        """Sanitize credentials in a simple list."""
        data = [
            "sk-1234567890abcdefghij1234567890",
            "normal string",
            "ghp_9876543210fedcbaabcdef1234567890abcd1234"
        ]
        result = sanitize_list(data)
        assert result[0] == REDACTION_PLACEHOLDER
        assert result[1] == "normal string"
        assert result[2] == REDACTION_PLACEHOLDER

    def test_list_of_dicts(self):
        """Sanitize credentials in list of dictionaries."""
        data = [
            {"name": "service1", "key": "sk-1111111111aaaaaaaaaabbbbbbbbbb"},
            {"name": "service2", "key": "sk-2222222222ccccccccccdddddddddd"}
        ]
        result = sanitize_list(data)
        assert result[0]["key"] == REDACTION_PLACEHOLDER
        assert result[1]["key"] == REDACTION_PLACEHOLDER
        assert result[0]["name"] == "service1"

    def test_dict_with_list_values(self):
        """Sanitize credentials in dict containing lists."""
        data = {
            "tokens": [
                "ghp_token1111111aaaaaaaaaabbbbbbbbbbcccc",
                "ghp_token2222222ccccccccccddddddddddeeee"
            ],
            "names": ["service1", "service2"]
        }
        result = sanitize_dict(data)
        assert result["tokens"][0] == REDACTION_PLACEHOLDER
        assert result["tokens"][1] == REDACTION_PLACEHOLDER
        assert result["names"][0] == "service1"

    def test_mixed_nested_structure(self):
        """Sanitize complex mixed nested structures."""
        data = {
            "agents": [
                {
                    "name": "agent1",
                    "credentials": {
                        "keys": ["sk-keyabcdefghij1234567890abcdefghij1111111aaaaaaaaabbbbbbbbbbcc", "sk-keyabcdefghij1234567890abcdefghij2222222ccccccccccddddddddddee"],
                        "tokens": {
                            "github": "ghp_tokenabcdef1234567890abcdef123456789"
                        }
                    }
                }
            ]
        }
        result = sanitize_dict(data)
        assert result["agents"][0]["credentials"]["keys"][0] == REDACTION_PLACEHOLDER
        assert result["agents"][0]["credentials"]["tokens"]["github"] == REDACTION_PLACEHOLDER
        assert result["agents"][0]["name"] == "agent1"

    def test_max_depth_protection(self):
        """Ensure max depth prevents infinite recursion."""
        # Create a 15-level deep structure (max_depth is 10)
        data = {"level": 1}
        current = data
        for i in range(2, 16):
            current["nested"] = {"level": i}
            current = current["nested"]
        current["secret"] = "sk-1234567890abcdefghij1234567890ghij1234567890"

        # Should not crash, and must not hand the uncut subtree back raw (#3312)
        result = sanitize_dict(data, max_depth=10)
        assert isinstance(result, dict)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in json.dumps(result)
        assert REDACTION_PLACEHOLDER in json.dumps(result)


@pytest.mark.unit
class TestJSONSanitization:
    """Test JSON string sanitization."""

    def test_valid_json_object(self):
        """Sanitize valid JSON object string."""
        json_str = '{"api_key": "sk-1234567890abcdefghij1234567890ghij1234567890", "name": "test"}'
        result = sanitize_json_string(json_str)
        parsed = json.loads(result)
        assert parsed["api_key"] == REDACTION_PLACEHOLDER
        assert parsed["name"] == "test"

    def test_valid_json_array(self):
        """Sanitize valid JSON array string."""
        json_str = '["sk-1234567890abcdefghij1234567890ghij1234567890", "normal", "ghp_token123abcdef1234567890abcdef123456"]'
        result = sanitize_json_string(json_str)
        parsed = json.loads(result)
        assert parsed[0] == REDACTION_PLACEHOLDER
        assert parsed[1] == "normal"
        assert parsed[2] == REDACTION_PLACEHOLDER

    def test_invalid_json_fallback_to_text(self):
        """Invalid JSON falls back to text sanitization."""
        text = "Not valid JSON but has sk-1234567890abcdefghij1234567890ghij1234567890 key"
        result = sanitize_json_string(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_empty_json_string(self):
        """Handle empty JSON string."""
        result = sanitize_json_string("")
        assert result == ""

    def test_none_json_string(self):
        """Handle None JSON string."""
        result = sanitize_json_string(None)
        assert result is None


@pytest.mark.unit
class TestExecutionLogSanitization:
    """Test Claude Code execution log sanitization."""

    def test_execution_log_list(self):
        """Sanitize list of execution log entries."""
        log_entries = [
            {
                "type": "tool_use",
                "output": "API_KEY=sk-1234567890abcdefghij1234567890ghij1234567890"
            },
            {
                "type": "text",
                "content": "Processing with token ghp_tokenabcdef1234567890abcdef123456789"
            }
        ]
        result = sanitize_execution_log(log_entries)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result[0]["output"]
        assert "ghp_tokenabcdef1234567890abcdef123456789" not in result[1]["content"]

    def test_empty_execution_log(self):
        """Handle empty execution log."""
        result = sanitize_execution_log([])
        assert result == []

    def test_none_execution_log(self):
        """Handle None execution log."""
        result = sanitize_execution_log(None)
        assert result is None


@pytest.mark.unit
class TestSubprocessLineSanitization:
    """Test line-by-line subprocess output sanitization."""

    def test_json_line(self):
        """Sanitize JSON line from subprocess."""
        line = '{"status": "success", "token": "ghp_1234567890abcdefghij1234567890abcdef"}\n'
        result = sanitize_subprocess_line(line)
        assert "ghp_1234567890abcdefghij1234567890abcdef" not in result
        parsed = json.loads(result)
        assert parsed["token"] == REDACTION_PLACEHOLDER

    def test_plain_text_line(self):
        """Sanitize plain text line from subprocess."""
        line = "Connecting with API key: sk-1234567890abcdefghij1234567890ghij1234567890\n"
        result = sanitize_subprocess_line(line)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_empty_line(self):
        """Handle empty line."""
        result = sanitize_subprocess_line("")
        assert result == ""

    def test_json_array_line(self):
        """Sanitize JSON array line."""
        line = '["sk-keyabcdefghij1234567890abcdefghij1111111aaaaaaaaabbbbbbbbbbcc", "normal", "sk-keyabcdefghij1234567890abcdefghij2222222ccccccccccddddddddddee"]'
        result = sanitize_subprocess_line(line)
        parsed = json.loads(result)
        assert parsed[0] == REDACTION_PLACEHOLDER
        assert parsed[1] == "normal"


@pytest.mark.unit
class TestDepthFailsClosed:
    """#3312 — the agent copy carries the same depth cap; past it the subtree
    is replaced, and a JSON string too deep for `json.loads` falls back to the
    linear text pass instead of raising RecursionError."""

    SECRET = "ghp_" + "a" * 36

    def test_dict_list_mix_past_max_depth_is_redacted(self):
        leaf = "key " + self.SECRET
        for i in range(14):
            leaf = {"n": leaf} if i % 2 else [leaf]
        result = sanitize_dict({"root": leaf})
        assert self.SECRET not in json.dumps(result)
        assert REDACTION_PLACEHOLDER in json.dumps(result)

    def test_very_deep_object_does_not_recurse(self):
        leaf = "key " + self.SECRET
        for _ in range(2000):
            leaf = [leaf]
        result = sanitize_list([leaf])
        assert self.SECRET not in json.dumps(result)

    def _deep_json(self) -> str:
        return "[" * 200_000 + json.dumps("key " + self.SECRET) + "]" * 200_000

    def test_deep_json_string_does_not_raise(self):
        out = sanitize_json_string(self._deep_json())
        assert isinstance(out, str)
        assert self.SECRET not in out

    def test_deep_subprocess_line_does_not_raise(self):
        out = sanitize_subprocess_line(self._deep_json())
        assert isinstance(out, str)
        assert self.SECRET not in out


@pytest.mark.unit
class TestCredentialValueCache:
    """Test credential value caching from environment."""

    def test_refresh_credential_values_from_env(self, monkeypatch):
        """Refresh should load credentials from environment variables."""
        # Set test environment variables
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-value-123")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_456abcdef1234567890abcdef12")
        monkeypatch.setenv("NORMAL_VAR", "not-a-secret")

        # Refresh cache
        refresh_credential_values()

        # Get cached values
        values = get_credential_values()

        # Should contain sensitive values
        assert "sk-ant-test-value-123" in values
        assert "ghp_test_token_456abcdef1234567890abcdef12" in values

    def test_credential_value_exact_match(self, monkeypatch):
        """Exact credential values should be redacted."""
        monkeypatch.setenv("OPENAI_API_KEY", "my-exact-secret-value")
        refresh_credential_values()

        text = "The key is my-exact-secret-value in this text"
        result = sanitize_text(text)
        assert "my-exact-secret-value" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_short_values_not_cached(self, monkeypatch):
        """Values shorter than 8 characters should not be cached."""
        monkeypatch.setenv("API_KEY", "short")
        refresh_credential_values()

        values = get_credential_values()
        assert "short" not in values

    def test_env_file_loading(self, tmp_path, monkeypatch):
        """Load credentials from .env file."""
        # Create temporary .env file
        env_file = tmp_path / ".env"
        env_file.write_text(
            "ANTHROPIC_API_KEY=sk-ant-from-file\n"
            "GITHUB_TOKEN='ghp_from_file1234567890abcdef1234567890ab'\n"
            '# Comment line\n'
            'OPENAI_API_KEY="sk-from-file"\n'
            'NORMAL_VAR=not-sensitive\n'
        )

        # Mock expanduser to return our test file
        def mock_expanduser(path):
            if path == "~/.env":
                return str(env_file)
            return path

        monkeypatch.setattr(os.path, "expanduser", mock_expanduser)

        refresh_credential_values()
        values = get_credential_values()

        # Should load values from file
        assert "sk-ant-from-file" in values
        assert "ghp_from_file1234567890abcdef1234567890ab" in values
        assert "sk-from-file" in values


@pytest.mark.unit
class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_string(self):
        """Handle empty string."""
        result = sanitize_text("")
        assert result == ""

    def test_none_value(self):
        """Handle None value."""
        result = sanitize_text(None)
        assert result is None

    def test_very_long_string(self):
        """Handle very long strings without performance issues."""
        # Create a 10KB string with a secret in the middle
        text = "x" * 5000 + "sk-1234567890abcdefghij1234567890" + "x" * 5000
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890" not in result
        assert len(result) <= len(text)  # Should not grow significantly

    def test_unicode_content(self):
        """Handle unicode content properly."""
        text = "API key: sk-1234567890abcdefghij1234567890ghij1234567890 用户名: admin 🔑"
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert "用户名: admin 🔑" in result

    def test_multiline_text(self):
        """Handle multiline text with credentials."""
        text = """
        Line 1: normal text
        Line 2: ANTHROPIC_API_KEY=sk-ant-api03-secretabcdef1234567890
        Line 3: more text
        Line 4: github token: ghp_token123abcdef1234567890abcdef123456
        """
        result = sanitize_text(text)
        assert "sk-ant-api03-secretabcdef1234567890" not in result
        assert "ghp_token123abcdef1234567890abcdef123456" not in result
        assert "Line 1: normal text" in result

    def test_mixed_types_in_dict(self):
        """Handle dictionaries with mixed value types."""
        data = {
            "string": "sk-1234567890abcdefghij1234567890ghij1234567890",
            "number": 42,
            "boolean": True,
            "null": None,
            "list": ["sk-keyabcdefghij1234567890abcdefghij", 123],
            "dict": {"nested": "ghp_tokenabcdef1234567890abcdef123456789"}
        }
        result = sanitize_dict(data)
        assert result["string"] == REDACTION_PLACEHOLDER
        assert result["number"] == 42
        assert result["boolean"] is True
        assert result["null"] is None
        assert result["list"][0] == REDACTION_PLACEHOLDER
        assert result["list"][1] == 123
        assert result["dict"]["nested"] == REDACTION_PLACEHOLDER

    def test_special_characters_in_credentials(self):
        """Handle credentials with special regex characters."""
        text = "Password: p@$$w0rd.with+special*chars"
        # This should be caught by PASSWORD= pattern if formatted that way
        text_formatted = "PASSWORD=p@$$w0rd.with+special*chars"
        result = sanitize_text(text_formatted)
        assert "p@$$w0rd.with+special*chars" not in result

    def test_url_encoded_credentials(self):
        """Handle URL-encoded credentials."""
        text = "URL: https://user:sk-1234567890abcdefghij1234567890ghij1234567890@api.example.com"
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result

    def test_base64_encoded_in_basic_auth(self):
        """Handle Base64-encoded credentials in Basic auth."""
        text = "Authorization: Basic c2stMTIzNDU2Nzg5MDphYmNkZWY="
        result = sanitize_text(text)
        assert "c2stMTIzNDU2Nzg5MDphYmNkZWY=" not in result
        assert REDACTION_PLACEHOLDER in result
