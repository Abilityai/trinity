"""
Unit tests for backend-side credential sanitizer.

Tests the backend defense-in-depth credential sanitization layer that
catches any credentials that may have bypassed agent-side sanitization.

Module: src/backend/utils/credential_sanitizer.py
"""

import pytest
import json

# Import the backend sanitizer directly
import sys
import importlib
from pathlib import Path

_project_root = Path(__file__).resolve().parents[2]

# Remove agent path if it was added by other test
agent_path = str(_project_root / 'docker' / 'base-image' / 'agent_server' / 'utils')
if agent_path in sys.path:
    sys.path.remove(agent_path)

# Remove cached module if it's the agent version
if 'credential_sanitizer' in sys.modules:
    del sys.modules['credential_sanitizer']

backend_utils_path = str(_project_root / 'src' / 'backend' / 'utils')
if backend_utils_path not in sys.path:
    sys.path.insert(0, backend_utils_path)

import credential_sanitizer as backend_sanitizer
from credential_sanitizer import (
    sanitize_text,
    sanitize_dict,
    sanitize_list,
    sanitize_json_string,
    sanitize_execution_log,
    sanitize_response,
    REDACTION_PLACEHOLDER,
)


@pytest.mark.unit
class TestPatternDetection:
    """Test detection of secret patterns in backend sanitizer."""

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

    def test_aws_token(self):
        """Redact AWS_ACCESS_KEY_ID=value."""
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = sanitize_text(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert f"AWS_ACCESS_KEY_ID={REDACTION_PLACEHOLDER}" in result

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
    """Test execution log sanitization (backend entry point)."""

    def test_execution_log_json_string(self):
        """Sanitize execution log as JSON string."""
        log_json = json.dumps([
            {
                "type": "tool_use",
                "output": "API_KEY=sk-1234567890abcdefghij1234567890ghij1234567890"
            },
            {
                "type": "text",
                "content": "Processing with token ghp_tokenabcdef1234567890abcdef123456789"
            }
        ])
        result = sanitize_execution_log(log_json)
        assert result is not None
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert "ghp_tokenabcdef1234567890abcdef123456789" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_empty_execution_log(self):
        """Handle empty execution log."""
        result = sanitize_execution_log("")
        assert result == ""

    def test_none_execution_log(self):
        """Handle None execution log."""
        result = sanitize_execution_log(None)
        assert result is None

    def test_execution_log_with_nested_credentials(self):
        """Sanitize execution log with deeply nested credentials."""
        log_json = json.dumps([
            {
                "type": "tool_result",
                "result": {
                    "stdout": "Connecting...\nANTHROPIC_API_KEY=sk-ant-api03-secretabcdef1234567890\nConnected!",
                    "stderr": ""
                }
            }
        ])
        result = sanitize_execution_log(log_json)
        assert "sk-ant-api03-secretabcdef1234567890" not in result
        parsed = json.loads(result)
        assert REDACTION_PLACEHOLDER in parsed[0]["result"]["stdout"]


@pytest.mark.unit
class TestResponseSanitization:
    """Test agent response sanitization."""

    def test_response_with_credentials(self):
        """Sanitize agent response containing credentials."""
        response = "I found your API key: sk-1234567890abcdefghij1234567890"
        result = sanitize_response(response)
        assert "sk-1234567890abcdefghij1234567890" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_response_with_multiple_secrets(self):
        """Sanitize response with multiple types of secrets."""
        response = "GitHub: ghp_tokenabcdef1234567890abcdef123456789, Slack: xoxb-FAKE-TEST-notreal, AWS: AKIAIOSFODNN7EXAMPLE"
        result = sanitize_response(response)
        assert "ghp_tokenabcdef1234567890abcdef123456789" not in result
        assert "xoxb-FAKE-TEST-notreal" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_empty_response(self):
        """Handle empty response."""
        result = sanitize_response("")
        assert result == ""

    def test_none_response(self):
        """Handle None response."""
        result = sanitize_response(None)
        assert result is None

    def test_response_with_key_value_pairs(self):
        """Sanitize response containing key=value pairs."""
        response = "Configuration:\nOPENAI_API_KEY=sk-1234567890abcdefghij1234567890ghij1234567890\nGITHUB_TOKEN=ghp_456abcdefghij1234567890abcdef1234567"
        result = sanitize_response(response)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result
        assert "ghp_456abcdefghij1234567890abcdef1234567" not in result


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

    def test_credentials_in_json_nested_in_text(self):
        """Handle JSON containing credentials embedded in text."""
        text = 'The response was: {"key": "sk-1234567890abcdefghij1234567890ghij1234567890"} from the API'
        result = sanitize_text(text)
        assert "sk-1234567890abcdefghij1234567890ghij1234567890" not in result

    def test_multiple_same_credential_instances(self):
        """Redact all instances of the same credential."""
        text = "Key 1: sk-secretabcdef1234567890abcdef12 Key 2: sk-secretabcdef1234567890abcdef12 Key 3: sk-secretabcdef1234567890abcdef12"
        result = sanitize_text(text)
        # All three instances should be redacted
        assert text.count("sk-secretabcdef1234567890abcdef12") == 3
        assert result.count("sk-secretabcdef1234567890abcdef12") == 0
        assert result.count(REDACTION_PLACEHOLDER) == 3


@pytest.mark.unit
class TestBackendSpecificBehavior:
    """Test backend-specific sanitization behavior."""

    def test_no_environment_variable_lookup(self):
        """Backend sanitizer does NOT look up environment variables.

        Unlike agent-side, backend only uses pattern matching.
        This is intentional - backend doesn't have access to agent env vars.
        """
        # This should only be caught if it matches a pattern
        text = "MY_CUSTOM_SECRET=random_value_123"
        result = sanitize_text(text)

        # Will be caught by SECRET pattern
        assert "random_value_123" not in result
        assert REDACTION_PLACEHOLDER in result

    def test_pattern_based_only(self):
        """Backend relies purely on pattern matching, not value caching."""
        # Even if a value doesn't match patterns, it won't be caught
        # unless the key name is sensitive
        text = "Some value that is secret: arbitrary_string"
        result = sanitize_text(text)

        # Should remain because neither key nor value match patterns
        assert "arbitrary_string" in result

    def test_defense_in_depth_pattern_coverage(self):
        """Backend patterns should overlap with agent patterns for defense-in-depth."""
        # Test that backend can catch what agent should have caught
        credentials = [
            "sk-1234567890abcdefghij1234567890",
            "sk-proj-project_key_123abcdefghij1234567890",
            "sk-ant-api03-anthropic_keyabcdefghij1234567890",
            "ghp_1234567890abcdefghij1234567890abcdef",
            "xoxb-FAKE-TEST-notreal",
            "AKIAIOSFODNN7EXAMPLE",
            "trinity_mcp_abcdef1234567890abcdef1234"
        ]

        for cred in credentials:
            text = f"Leaked: {cred}"
            result = sanitize_text(text)
            assert cred not in result, f"Failed to redact {cred}"
            assert REDACTION_PLACEHOLDER in result
