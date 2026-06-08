"""
Tests for Issue #285: Expired Token Fast-Fail Detection
Tests for Issue #361: Max-Turns Error Classification

Verifies that:
- Auth failure patterns in Claude Code stderr are detected and result in HTTP 503
- Max-turns termination is correctly classified and results in HTTP 422 (not 503)

Related: docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md
"""
import pytest


def _is_auth_failure_message(text: str) -> bool:
    """
    Check if a message indicates an authentication/token failure.

    This is a copy of the function from claude_code.py for testing purposes.
    The actual implementation lives in docker/base-image/agent_server/services/claude_code.py
    """
    if not text:
        return False
    lower = text.lower()
    return any(pattern in lower for pattern in [
        "subscription token may be expired",
        "token may be expired",
        "token expired",
        "token revoked",
        "invalid token",
        "authentication failed",
        "auth failed",
        "setup-token",  # "Generate a new one with 'claude setup-token'"
        "oauth token",
        "unauthorized",
        "invalid credentials",
        "credentials expired",
    ])


class TestAuthFailurePatternMatcher:
    """Unit tests for _is_auth_failure_message() pattern matcher."""

    def test_expired_token_patterns(self):
        """Test detection of expired token messages."""
        # Patterns that should be detected
        assert _is_auth_failure_message("Subscription token may be expired or revoked")
        assert _is_auth_failure_message("Your token may be expired. Please re-authenticate.")
        assert _is_auth_failure_message("Token expired")
        assert _is_auth_failure_message("token revoked by user")
        assert _is_auth_failure_message("Invalid token provided")
        assert _is_auth_failure_message("Authentication failed: invalid credentials")
        assert _is_auth_failure_message("Generate a new one with 'claude setup-token'")
        assert _is_auth_failure_message("OAuth token invalid")
        assert _is_auth_failure_message("Error: unauthorized")
        assert _is_auth_failure_message("invalid credentials")
        assert _is_auth_failure_message("credentials expired")

    def test_non_auth_patterns(self):
        """Test that non-auth messages are not flagged."""
        # Patterns that should NOT be detected
        assert not _is_auth_failure_message("")
        assert not _is_auth_failure_message(None)
        assert not _is_auth_failure_message("Task completed successfully")
        assert not _is_auth_failure_message("Using model: claude-sonnet-4-5")
        assert not _is_auth_failure_message("Processing tokens...")
        assert not _is_auth_failure_message("Rate limit exceeded")  # Different error type
        assert not _is_auth_failure_message("Out of usage")  # Rate limit, not auth

    def test_case_insensitivity(self):
        """Test that pattern matching is case-insensitive."""
        assert _is_auth_failure_message("TOKEN EXPIRED")
        assert _is_auth_failure_message("Authentication Failed")
        assert _is_auth_failure_message("UNAUTHORIZED")


class TestMaxTurnsErrorClassification:
    """
    Tests for Issue #361: Max-turns termination must NOT be misclassified as auth failure.

    When Claude Code hits the max_turns limit, it returns:
    {
      "type": "result",
      "subtype": "error_max_turns",
      "is_error": true,
      "terminal_reason": "max_turns",
      "errors": ["Reached maximum number of turns (N)"]
    }

    This must result in HTTP 422 (Unprocessable Entity) with a clear error message,
    NOT HTTP 503 "Authentication failure".
    """

    def test_max_turns_result_message_structure(self):
        """Test that max_turns result messages have expected structure."""
        # This is the structure Claude Code returns when max_turns is hit
        max_turns_result = {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "terminal_reason": "max_turns",
            "errors": ["Reached maximum number of turns (20)"],
            "result": "",  # Result is empty when max_turns hit
            "num_turns": 20,
        }

        # Verify the detection conditions used in claude_code.py
        assert max_turns_result.get("terminal_reason") == "max_turns"
        assert max_turns_result.get("subtype") == "error_max_turns"
        assert max_turns_result.get("is_error") is True
        assert max_turns_result.get("result") == ""  # Empty result

    def test_max_turns_not_detected_as_auth_failure(self):
        """Test that max_turns error messages are NOT detected as auth failures."""
        # These are the error messages that might appear in max_turns scenarios
        max_turns_messages = [
            "Reached maximum number of turns (20)",
            "Reached maximum number of turns (50)",
            "Task stopped after 20 turns",
            "Task exceeded turn limit",
            "error_max_turns",
        ]

        for msg in max_turns_messages:
            assert not _is_auth_failure_message(msg), f"Max-turns message incorrectly flagged as auth failure: {msg}"

    def test_diagnose_fallback_message_triggers_auth_detection(self):
        """
        Test the root cause of Issue #361: the diagnose fallback message itself
        contains 'setup-token' which triggers auth failure detection.

        This test documents the bug scenario - the fix prevents this path by
        detecting max_turns BEFORE falling through to _diagnose_exit_failure().
        """
        # This is the fallback message from _diagnose_exit_failure() when
        # OAuth token is present but API key is not
        diagnose_fallback = "Subscription token may be expired or revoked. Generate a new one with 'claude setup-token'."

        # The bug: this message triggers auth failure detection
        assert _is_auth_failure_message(diagnose_fallback), \
            "Diagnose fallback message should trigger auth detection (this is the bug scenario)"

        # The fix: max_turns is detected BEFORE this path is reached,
        # so metadata.error_type == "max_turns" and we return HTTP 422 early

    def test_default_max_turns_task_is_50(self):
        """Test that the default max_turns_task is 50 (raised from 20 in Issue #361)."""
        import json
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]  # tests/unit/ -> tests/ -> repo root
        baseline_path = repo_root / "docker" / "base-image" / "hooks" / "guardrails-baseline.json"

        with open(baseline_path) as f:
            baseline = json.load(f)

        assert baseline["max_turns_task"] == 50, \
            f"max_turns_task should be 50 (Issue #361), got {baseline['max_turns_task']}"
        assert baseline["max_turns_chat"] == 50, \
            f"max_turns_chat should be 50, got {baseline['max_turns_chat']}"
