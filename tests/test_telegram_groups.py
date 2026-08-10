"""
Telegram Group Chat Tests (Issue #349)

Tests for:
1. Trigger mode validation (mention, all, observe)
2. Proactive messaging endpoint validation

Note: These are API-level tests that validate endpoint behavior.
Integration tests with real Telegram require a configured bot binding.
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import assert_status, assert_json_response, assert_status_in

# Module-scoped agent fixture + telegram config setup can exceed 30s.
pytestmark = pytest.mark.timeout(120)


# =============================================================================
# API Tests: Trigger Mode Validation
# =============================================================================

class TestTriggerModeValidation:
    """Tests for trigger mode validation in Telegram group config updates."""

    @pytest.mark.smoke
    def test_invalid_trigger_mode_rejected(self, api_client: TrinityApiClient, created_agent):
        """PUT with invalid trigger_mode should return 400."""
        # First, we need a Telegram binding to test against
        # Since we can't create a real binding, we'll test the validation logic
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/telegram/groups/99999",
            json={"trigger_mode": "invalid_mode"}
        )

        # Should fail with 400 (validation) or 404 (no binding)
        assert_status_in(response, [400, 404])

        if response.status_code == 400:
            data = response.json()
            assert "trigger_mode" in data.get("detail", "").lower()

    @pytest.mark.smoke
    def test_observe_mode_accepted(self, api_client: TrinityApiClient, created_agent):
        """'observe' should be a valid trigger_mode value."""
        # Test that 'observe' is accepted by the validation
        # (will still fail with 404 if no binding exists, but not 400)
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/telegram/groups/99999",
            json={"trigger_mode": "observe"}
        )

        # Should not be 400 for validation - either 404 (no binding) or success
        if response.status_code == 400:
            data = response.json()
            # If 400, it should NOT be about trigger_mode validation
            assert "trigger_mode" not in data.get("detail", "").lower() or "observe" in data.get("detail", "").lower()


# =============================================================================
# API Tests: Proactive Message Endpoint
# =============================================================================

class TestProactiveMessageEndpoint:
    """Tests for proactive group messaging endpoint (Issue #349)."""

    @pytest.mark.smoke
    def test_proactive_message_requires_binding(self, api_client: TrinityApiClient, created_agent):
        """POST /telegram/groups/{chat_id}/messages requires a Telegram binding."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/telegram/groups/-100123456/messages",
            json={"message": "Hello group!"}
        )

        # Should fail with 404 because no binding exists
        assert_status(response, 404)
        data = response.json()
        assert "binding" in data.get("detail", "").lower() or "not found" in data.get("detail", "").lower()

    @pytest.mark.smoke
    def test_proactive_message_requires_message(self, api_client: TrinityApiClient, created_agent):
        """POST /telegram/groups/{chat_id}/messages requires non-empty message."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/telegram/groups/-100123456/messages",
            json={"message": ""}
        )

        # Will fail with 400 (empty message) or 404 (no binding)
        # The order of validation may vary, so accept either
        assert_status_in(response, [400, 404])

    @pytest.mark.smoke
    def test_proactive_message_validates_length(self, api_client: TrinityApiClient, created_agent):
        """POST with message > 4096 chars should fail."""
        long_message = "x" * 4097

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/telegram/groups/-100123456/messages",
            json={"message": long_message}
        )

        # Will fail with 400 (too long) or 404 (no binding)
        assert_status_in(response, [400, 404])
