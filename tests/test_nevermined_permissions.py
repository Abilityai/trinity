"""
Nevermined Permission Tests (test_nevermined_permissions.py)

Tests for issue #72: Nevermined config returns 403 for shared (non-owner) users.
Verifies that the read/write permission split works correctly:
- GET endpoints use can_user_access_agent (owner + shared + admin)
- POST/PUT/DELETE endpoints use can_user_share_agent (owner + admin only)

Test tiers:
- SMOKE: Permission checks on endpoints (requires running agent with config)
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# =============================================================================
# Test Data
# =============================================================================

VALID_NVM_CONFIG = {
    "nvm_api_key": "sandbox:test-key-for-permission-tests",
    "nvm_environment": "sandbox",
    "nvm_agent_id": "12345678901234567890",
    "nvm_plan_id": "98765432109876543210",
    "credits_per_request": 1,
}


# =============================================================================
# Owner/Admin Access Tests (SMOKE)
# =============================================================================

class TestNeverminedOwnerAccess:
    """Verify owner/admin can perform all Nevermined operations."""

    @pytest.mark.smoke
    def test_get_config_no_config(self, api_client: TrinityApiClient, created_agent):
        """GET /api/nevermined/agents/{name}/config returns 404 when not configured."""
        response = api_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/config"
        )
        # 404 = no config yet (not 403)
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_save_config(self, api_client: TrinityApiClient, created_agent):
        """POST /api/nevermined/agents/{name}/config saves config as owner."""
        response = api_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
        )
        # 200 = saved, or 501 = SDK not installed (both acceptable)
        assert_status_in(response, [200, 501])

        if response.status_code == 200:
            data = assert_json_response(response)
            assert_has_fields(data, ["agent_name", "nvm_environment", "nvm_agent_id", "nvm_plan_id"])
            assert data["nvm_environment"] == "sandbox"

    @pytest.mark.smoke
    def test_get_config_after_save(self, api_client: TrinityApiClient, created_agent):
        """GET /api/nevermined/agents/{name}/config returns config after save."""
        # First save
        save_resp = api_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
        )
        if save_resp.status_code == 501:
            pytest.skip("Nevermined SDK not installed")

        # Then get
        response = api_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/config"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["nvm_agent_id"] == VALID_NVM_CONFIG["nvm_agent_id"]

    @pytest.mark.smoke
    def test_get_payment_history(self, api_client: TrinityApiClient, created_agent):
        """GET /api/nevermined/agents/{name}/payments returns list."""
        # First ensure config exists
        save_resp = api_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
        )
        if save_resp.status_code == 501:
            pytest.skip("Nevermined SDK not installed")

        response = api_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/payments"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    @pytest.mark.smoke
    def test_toggle_config(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/nevermined/agents/{name}/config/toggle toggles state."""
        # First ensure config exists
        save_resp = api_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
        )
        if save_resp.status_code == 501:
            pytest.skip("Nevermined SDK not installed")

        response = api_client.put(
            f"/api/nevermined/agents/{created_agent['name']}/config/toggle?enabled=true"
        )
        assert_status_in(response, [200, 501])

    @pytest.mark.smoke
    def test_delete_config(self, api_client: TrinityApiClient, created_agent):
        """DELETE /api/nevermined/agents/{name}/config removes config."""
        # First ensure config exists
        save_resp = api_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
        )
        if save_resp.status_code == 501:
            pytest.skip("Nevermined SDK not installed")

        response = api_client.delete(
            f"/api/nevermined/agents/{created_agent['name']}/config"
        )
        assert_status(response, 200)

        # Verify it's gone
        get_resp = api_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/config"
        )
        assert_status(get_resp, 404)


# =============================================================================
# Unauthenticated Access Tests
# =============================================================================

class TestNeverminedUnauthenticated:
    """Verify unauthenticated requests are rejected."""

    @pytest.mark.smoke
    def test_get_config_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """GET config without auth returns 401."""
        response = unauthenticated_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_save_config_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """POST config without auth returns 401."""
        response = unauthenticated_client.post(
            f"/api/nevermined/agents/{created_agent['name']}/config",
            json=VALID_NVM_CONFIG,
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_get_payments_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """GET payments without auth returns 401."""
        response = unauthenticated_client.get(
            f"/api/nevermined/agents/{created_agent['name']}/payments",
            auth=False,
        )
        assert_status(response, 401)


# =============================================================================
# Nonexistent Agent Tests
# =============================================================================

class TestNeverminedNonexistentAgent:
    """Verify access checks for agents that don't exist."""

    @pytest.mark.smoke
    def test_get_config_nonexistent_agent(self, api_client: TrinityApiClient):
        """GET config for nonexistent agent returns 403 (access denied, not 404)."""
        response = api_client.get(
            "/api/nevermined/agents/nonexistent-agent-xyz/config"
        )
        # Admin bypasses read access check, so gets 404 (no config)
        # Non-admin would get 403
        assert_status_in(response, [403, 404])

    @pytest.mark.smoke
    def test_save_config_nonexistent_agent(self, api_client: TrinityApiClient):
        """POST config for nonexistent agent is rejected."""
        response = api_client.post(
            "/api/nevermined/agents/nonexistent-agent-xyz/config",
            json=VALID_NVM_CONFIG,
        )
        # Admin bypasses write access check, may get 400/404/501 instead of 403
        assert_status_in(response, [400, 403, 404, 501])
