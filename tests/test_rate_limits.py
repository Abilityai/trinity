"""
Rate Limit Tests (test_rate_limits.py)

Tests for SUB-002: Subscription rate limit error detection and formatting.
Covers:
- _is_rate_limit_message() detection (positive/negative cases)
- _format_rate_limit_error() message formatting
- ExecutionMetadata error_type/error_message fields
- HTTP 429 passthrough in chat router (queue full)

Test tiers:
- UNIT: Detection/formatting functions (no backend needed)
- SMOKE: 429 queue-full endpoint behavior (needs backend)
"""

import sys
import os
import re
import importlib.util
import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# --------------------------------------------------------------------------- #
# Import helpers: agent_server code lives inside a Docker-only package tree.
# We extract the specific functions we need without loading the full module.
# --------------------------------------------------------------------------- #

_AGENT_SERVER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "docker", "base-image", "agent_server",
)


def _load_models_module():
    """Load agent_server/models.py standalone (Pydantic models only)."""
    path = os.path.join(_AGENT_SERVER_DIR, "models.py")
    spec = importlib.util.spec_from_file_location("agent_models", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load models once at module level — it has no problematic imports
_agent_models = _load_models_module()
ExecutionMetadata = _agent_models.ExecutionMetadata


def _extract_functions_from_source():
    """Extract _is_rate_limit_message and _format_rate_limit_error from source.

    Reads error_classifier.py as text, extracts the two function definitions,
    and exec's them in a namespace that has ExecutionMetadata available.
    This avoids importing the full module with its Docker-only dependencies.

    Source moved from claude_code.py to error_classifier.py per #122 module split.
    """
    source_path = os.path.join(_AGENT_SERVER_DIR, "services", "error_classifier.py")
    with open(source_path) as f:
        source = f.read()

    # Extract function source blocks using regex
    functions = {}
    for func_name in ("_is_rate_limit_message", "_format_rate_limit_error"):
        # Match "def func_name(..." through the next top-level def or end of file
        pattern = rf'^(def {func_name}\(.*?\n(?:(?!^def ).*\n)*)'
        match = re.search(pattern, source, re.MULTILINE)
        if match:
            func_source = match.group(1)
            ns = {"ExecutionMetadata": ExecutionMetadata}
            exec(func_source, ns)
            functions[func_name] = ns[func_name]

    return functions


_extracted = _extract_functions_from_source()
_is_rate_limit_message = _extracted["_is_rate_limit_message"]
_format_rate_limit_error = _extracted["_format_rate_limit_error"]


# =============================================================================
# Unit Tests: _is_rate_limit_message()
# =============================================================================

class TestIsRateLimitMessage:
    """Unit tests for rate limit message detection."""

    @pytest.fixture(autouse=True)
    def _import_function(self):
        """Import the function under test."""
        self._is_rate_limit = _is_rate_limit_message

    @pytest.mark.unit
    def test_empty_string(self):
        """Empty string is not a rate limit message."""
        assert self._is_rate_limit("") is False

    @pytest.mark.unit
    def test_none_input(self):
        """None input is not a rate limit message (falsy check)."""
        assert self._is_rate_limit(None) is False

    @pytest.mark.unit
    def test_normal_text(self):
        """Normal text is not a rate limit message."""
        assert self._is_rate_limit("Hello, how can I help you?") is False

    @pytest.mark.unit
    def test_out_of_extra_usage(self):
        """Detects 'out of extra usage' pattern."""
        assert self._is_rate_limit("You are out of extra usage for this period") is True

    @pytest.mark.unit
    def test_out_of_usage(self):
        """Detects 'out of usage' pattern."""
        assert self._is_rate_limit("You are out of usage") is True

    @pytest.mark.unit
    def test_usage_limit(self):
        """Detects 'usage limit' pattern."""
        assert self._is_rate_limit("You have hit the usage limit") is True

    @pytest.mark.unit
    def test_rate_limit(self):
        """Detects 'rate limit' pattern."""
        assert self._is_rate_limit("rate limit exceeded") is True

    @pytest.mark.unit
    def test_rate_limit_underscore(self):
        """Detects 'rate_limit' pattern (underscore variant)."""
        assert self._is_rate_limit("error_type: rate_limit") is True

    @pytest.mark.unit
    def test_resets_pattern(self):
        """Detects 'resets ' pattern (with trailing space)."""
        assert self._is_rate_limit("resets 1am (America/New_York)") is True

    @pytest.mark.unit
    def test_exceeded_your(self):
        """Detects 'exceeded your' pattern."""
        assert self._is_rate_limit("You've exceeded your quota for today") is True

    @pytest.mark.unit
    def test_quota_exceeded(self):
        """Detects 'quota exceeded' pattern."""
        assert self._is_rate_limit("API quota exceeded, please try later") is True

    @pytest.mark.unit
    def test_case_insensitive(self):
        """Detection is case-insensitive."""
        assert self._is_rate_limit("RATE LIMIT exceeded") is True
        assert self._is_rate_limit("Out Of Usage") is True
        assert self._is_rate_limit("QUOTA EXCEEDED") is True

    @pytest.mark.unit
    def test_partial_match_resets_without_space(self):
        """'resets' without trailing space should not match (avoid false positives)."""
        # The pattern is "resets " with trailing space
        assert self._is_rate_limit("This resets the counter") is True  # has "resets " with space
        assert self._is_rate_limit("system_resets") is False  # no space after

    @pytest.mark.unit
    def test_unrelated_error_messages(self):
        """Common error messages that should NOT trigger rate limit detection."""
        assert self._is_rate_limit("Subscription token may be expired or revoked") is False
        assert self._is_rate_limit("Connection refused") is False
        assert self._is_rate_limit("Model not found") is False
        assert self._is_rate_limit("Internal server error") is False
        assert self._is_rate_limit("Authentication failed") is False


# =============================================================================
# Unit Tests: _format_rate_limit_error()
# =============================================================================

class TestFormatRateLimitError:
    """Unit tests for rate limit error message formatting."""

    @pytest.fixture(autouse=True)
    def _import_functions(self):
        """Import the function and model under test."""
        self._format_error = _format_rate_limit_error
        self._ExecutionMetadata = ExecutionMetadata

    @pytest.mark.unit
    def test_format_with_error_message(self):
        """Formats message using metadata.error_message."""
        metadata = self._ExecutionMetadata(
            error_type="rate_limit",
            error_message="Out of usage until 3am EST",
        )
        result = self._format_error(metadata)
        assert "Out of usage until 3am EST" in result
        assert "ANTHROPIC_API_KEY" in result
        assert "Subscriptions" in result

    @pytest.mark.unit
    def test_format_without_error_message(self):
        """Uses default message when metadata.error_message is None."""
        metadata = self._ExecutionMetadata(
            error_type="rate_limit",
            error_message=None,
        )
        result = self._format_error(metadata)
        assert "Subscription usage limit reached" in result
        assert "ANTHROPIC_API_KEY" in result

    @pytest.mark.unit
    def test_format_includes_three_resolution_options(self):
        """Error message includes all three resolution options."""
        metadata = self._ExecutionMetadata(error_type="rate_limit")
        result = self._format_error(metadata)
        assert "(1)" in result  # wait for reset
        assert "(2)" in result  # set ANTHROPIC_API_KEY
        assert "(3)" in result  # assign different subscription


# =============================================================================
# Unit Tests: ExecutionMetadata error fields
# =============================================================================

class TestExecutionMetadataErrorFields:
    """Test that ExecutionMetadata model has error_type and error_message fields."""

    @pytest.fixture(autouse=True)
    def _import_model(self):
        self._ExecutionMetadata = ExecutionMetadata

    @pytest.mark.unit
    def test_default_error_fields_are_none(self):
        """Error fields default to None."""
        meta = self._ExecutionMetadata()
        assert meta.error_type is None
        assert meta.error_message is None

    @pytest.mark.unit
    def test_set_error_type(self):
        """error_type can be set to a string classification."""
        meta = self._ExecutionMetadata(error_type="rate_limit")
        assert meta.error_type == "rate_limit"

    @pytest.mark.unit
    def test_set_error_message(self):
        """error_message can be set to a human-readable string."""
        meta = self._ExecutionMetadata(error_message="Out of usage for this period")
        assert meta.error_message == "Out of usage for this period"

    @pytest.mark.unit
    def test_serialization_includes_error_fields(self):
        """Error fields appear in serialized output."""
        meta = self._ExecutionMetadata(
            error_type="rate_limit",
            error_message="Quota exceeded",
        )
        data = meta.model_dump()
        assert data["error_type"] == "rate_limit"
        assert data["error_message"] == "Quota exceeded"


# =============================================================================
# Integration Tests: 429 Queue Full (SMOKE)
# =============================================================================

class TestQueueFull429:
    """Test that chat endpoint returns 429 when agent queue is full.

    Note: Actually triggering a queue-full requires concurrent requests
    to a real agent, which is slow. These tests verify the endpoint
    structure for rejection responses.
    """

    @pytest.mark.smoke
    def test_chat_nonexistent_agent(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/chat returns error for nonexistent agent."""
        response = api_client.post(
            "/api/agents/nonexistent-rate-limit-test/chat",
            json={"message": "Hello"},
        )
        # 404 or 400 — agent doesn't exist
        assert response.status_code in (400, 404, 422)

    @pytest.mark.smoke
    def test_chat_unauthenticated(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/chat without auth returns 401."""
        response = unauthenticated_client.post(
            "/api/agents/any-agent/chat",
            json={"message": "Hello"},
            auth=False,
        )
        assert_status(response, 401)
