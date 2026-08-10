"""
Subscription Auto-Switch Tests (test_subscription_auto_switch.py)

Tests for SUB-003: Automatic subscription switching on rate-limit errors.

Test tiers:
- SMOKE: Setting CRUD, rate-limit tracking endpoints (no agent required)
"""

import pytest
import uuid

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# =============================================================================
# Test Data
# =============================================================================

VALID_TOKEN = "sk-ant-oat01-test-access-token-12345"
VALID_TOKEN_2 = "sk-ant-oat01-test-access-token-67890"


# =============================================================================
# Auto-Switch Setting Tests (SMOKE)
# =============================================================================

class TestAutoSwitchSetting:
    """SUB-003: Auto-switch setting endpoint tests."""

    @pytest.mark.smoke
    def test_get_auto_switch_default_on(self, api_client: TrinityApiClient):
        """GET /api/subscriptions/settings/auto-switch defaults to enabled (#441 — flipped to opt-out).

        Note: this asserts the runtime default. The endpoint applies
        `default="true"` only when no value is stored in `system_settings`. If
        a prior test or the dev DB explicitly set it to "false", this test
        will fail — clear the stored value first or run against a fresh DB.
        """
        response = api_client.get("/api/subscriptions/settings/auto-switch")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert "enabled" in data
        assert data["enabled"] is True

    @pytest.mark.smoke
    def test_enable_auto_switch(self, api_client: TrinityApiClient):
        """PUT /api/subscriptions/settings/auto-switch enables the setting."""
        # Enable
        response = api_client.put("/api/subscriptions/settings/auto-switch?enabled=true")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["enabled"] is True

        # Verify persisted
        response = api_client.get("/api/subscriptions/settings/auto-switch")
        assert_status(response, 200)
        assert response.json()["enabled"] is True

        # Cleanup: disable
        api_client.put("/api/subscriptions/settings/auto-switch?enabled=false")

    @pytest.mark.smoke
    def test_disable_auto_switch(self, api_client: TrinityApiClient):
        """PUT /api/subscriptions/settings/auto-switch can disable."""
        # Enable first
        api_client.put("/api/subscriptions/settings/auto-switch?enabled=true")

        # Disable
        response = api_client.put("/api/subscriptions/settings/auto-switch?enabled=false")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["enabled"] is False

        # Verify persisted
        response = api_client.get("/api/subscriptions/settings/auto-switch")
        assert_status(response, 200)
        assert response.json()["enabled"] is False


# =============================================================================
# Auto-Switch Setting requires admin
# =============================================================================

class TestAutoSwitchRequiresAdmin:
    """SUB-003: Auto-switch setting requires admin access."""

    @pytest.mark.smoke
    def test_get_auto_switch_unauthenticated(self, api_client: TrinityApiClient):
        """GET /api/subscriptions/settings/auto-switch without auth returns 401."""
        import requests
        response = requests.get(f"{api_client.config.base_url}/api/subscriptions/settings/auto-switch")
        assert response.status_code == 401


# =============================================================================
# Multiple Subscription Setup for Auto-Switch
# =============================================================================

class TestAutoSwitchPrerequisites:
    """SUB-003: Verify auto-switch requires multiple subscriptions."""

    @pytest.mark.smoke
    def test_register_two_subscriptions(self, api_client: TrinityApiClient):
        """Can register two subscriptions (required for auto-switch)."""
        name1 = f"test-sub-as1-{uuid.uuid4().hex[:8]}"
        name2 = f"test-sub-as2-{uuid.uuid4().hex[:8]}"

        # Create first subscription
        r1 = api_client.post(
            "/api/subscriptions",
            json={"name": name1, "token": VALID_TOKEN, "subscription_type": "max"}
        )
        assert_status(r1, 200)
        id1 = r1.json()["id"]

        # Create second subscription
        r2 = api_client.post(
            "/api/subscriptions",
            json={"name": name2, "token": VALID_TOKEN_2, "subscription_type": "pro"}
        )
        assert_status(r2, 200)
        id2 = r2.json()["id"]

        # Verify both exist
        response = api_client.get("/api/subscriptions")
        assert_status(response, 200)
        subs = response.json()
        sub_ids = [s["id"] for s in subs]
        assert id1 in sub_ids
        assert id2 in sub_ids

        # Cleanup
        api_client.delete(f"/api/subscriptions/{id1}")
        api_client.delete(f"/api/subscriptions/{id2}")

    @pytest.mark.smoke
    def test_auto_switch_setting_toggle_cycle(self, api_client: TrinityApiClient):
        """Setting can be toggled on and off repeatedly."""
        for expected in [True, False, True, False]:
            response = api_client.put(
                f"/api/subscriptions/settings/auto-switch?enabled={'true' if expected else 'false'}"
            )
            assert_status(response, 200)
            assert response.json()["enabled"] is expected
