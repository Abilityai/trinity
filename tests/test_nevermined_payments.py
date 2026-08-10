"""
Nevermined Payment Flow Tests (test_nevermined_payments.py)

Tests for NVM-001: Nevermined x402 payment integration — paid chat flow.
Extends test_nevermined_permissions.py (which covers admin config CRUD).

Covers:
- GET /api/paid/{agent_name}/info — public payment info
- POST /api/paid/{agent_name}/chat — 402 response (no payment header)
- POST /api/paid/{agent_name}/chat — 403 response (invalid payment token)
- GET /api/nevermined/settlement-failures — admin endpoint
- POST /api/nevermined/retry-settlement/{log_id} — admin endpoint

Test tiers:
- SMOKE: Endpoint structure and error responses (no real payments)
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
    "nvm_api_key": "sandbox:test-key-for-payment-flow-tests",
    "nvm_environment": "sandbox",
    "nvm_agent_id": "12345678901234567890",
    "nvm_plan_id": "98765432109876543210",
    "credits_per_request": 1,
}


# =============================================================================
# Helper to set up Nevermined config on an agent
# =============================================================================

def _ensure_nvm_config(api_client, agent_name):
    """Configure Nevermined on an agent, skip if SDK not installed."""
    save_resp = api_client.post(
        f"/api/nevermined/agents/{agent_name}/config",
        json=VALID_NVM_CONFIG,
    )
    if save_resp.status_code == 501:
        pytest.skip("Nevermined SDK not installed")
    assert_status_in(save_resp, [200, 201])

    # Enable payments
    toggle_resp = api_client.put(
        f"/api/nevermined/agents/{agent_name}/config/toggle?enabled=true"
    )
    assert_status_in(toggle_resp, [200, 501])

    return save_resp.json()


# =============================================================================
# GET /api/paid/{agent_name}/info Tests
# =============================================================================

class TestPaidAgentInfo:
    """Test the public payment info endpoint."""

    @pytest.mark.smoke
    def test_info_no_config(self, api_client: TrinityApiClient, created_agent):
        """GET /api/paid/{name}/info returns 404/501 when no Nevermined config."""
        response = api_client.get(
            f"/api/paid/{created_agent['name']}/info",
            auth=False,
        )
        # 404 (no config) or 501 (SDK not installed)
        assert_status_in(response, [404, 501])

    @pytest.mark.smoke
    def test_info_nonexistent_agent(self, api_client: TrinityApiClient):
        """GET /api/paid/{name}/info returns 404/501 for nonexistent agent."""
        response = api_client.get(
            "/api/paid/nonexistent-paid-agent/info",
            auth=False,
        )
        assert_status_in(response, [404, 501])

    @pytest.mark.smoke
    def test_info_with_config(self, api_client: TrinityApiClient, created_agent):
        """GET /api/paid/{name}/info returns payment info when configured."""
        _ensure_nvm_config(api_client, created_agent["name"])

        response = api_client.get(
            f"/api/paid/{created_agent['name']}/info",
            auth=False,
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "credits_per_request", "nvm_plan_id", "payment_required"])
        assert data["agent_name"] == created_agent["name"]
        assert data["credits_per_request"] == 1

    @pytest.mark.smoke
    def test_info_disabled_returns_404(self, api_client: TrinityApiClient, created_agent):
        """GET /api/paid/{name}/info returns 404 when payments are disabled."""
        _ensure_nvm_config(api_client, created_agent["name"])

        # Disable payments
        api_client.put(
            f"/api/nevermined/agents/{created_agent['name']}/config/toggle?enabled=false"
        )

        response = api_client.get(
            f"/api/paid/{created_agent['name']}/info",
            auth=False,
        )
        assert_status(response, 404)

        # Re-enable for other tests
        api_client.put(
            f"/api/nevermined/agents/{created_agent['name']}/config/toggle?enabled=true"
        )


# =============================================================================
# POST /api/paid/{agent_name}/chat — 402 Payment Required Tests
# =============================================================================

class TestPaidChat402:
    """Test that chat without payment header returns 402."""

    @pytest.mark.smoke
    def test_chat_no_payment_header_returns_402(
        self, api_client: TrinityApiClient, created_agent
    ):
        """POST /api/paid/{name}/chat without payment-signature returns 402."""
        _ensure_nvm_config(api_client, created_agent["name"])

        response = api_client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={"message": "Hello"},
            auth=False,
        )
        assert_status(response, 402)
        data = assert_json_response(response)
        assert_has_fields(data, ["detail", "payment_required", "credits_per_request"])
        assert data["detail"] == "Payment required"
        assert data["credits_per_request"] == 1

    @pytest.mark.smoke
    def test_chat_402_has_payment_required_header(
        self, api_client: TrinityApiClient, created_agent
    ):
        """402 response includes payment-required header (base64-encoded JSON)."""
        _ensure_nvm_config(api_client, created_agent["name"])

        response = api_client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={"message": "Hello"},
            auth=False,
        )
        assert_status(response, 402)
        assert "payment-required" in response.headers
        # Verify it's valid base64
        import base64
        import json
        header_value = response.headers["payment-required"]
        decoded = base64.b64decode(header_value)
        payment_required = json.loads(decoded)
        assert isinstance(payment_required, dict)

    @pytest.mark.smoke
    def test_chat_no_config_returns_404(self, api_client: TrinityApiClient):
        """POST /api/paid/{name}/chat without config returns 404/501."""
        response = api_client.post(
            "/api/paid/nonexistent-paid-agent/chat",
            json={"message": "Hello"},
            auth=False,
        )
        assert_status_in(response, [404, 501])

    @pytest.mark.smoke
    def test_chat_disabled_returns_404(
        self, api_client: TrinityApiClient, created_agent
    ):
        """POST /api/paid/{name}/chat returns 404 when payments disabled."""
        _ensure_nvm_config(api_client, created_agent["name"])

        # Disable
        api_client.put(
            f"/api/nevermined/agents/{created_agent['name']}/config/toggle?enabled=false"
        )

        response = api_client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={"message": "Hello"},
            auth=False,
        )
        assert_status(response, 404)

        # Re-enable
        api_client.put(
            f"/api/nevermined/agents/{created_agent['name']}/config/toggle?enabled=true"
        )


# =============================================================================
# POST /api/paid/{agent_name}/chat — 403 Invalid Token Tests
# =============================================================================

class TestPaidChat403:
    """Test that chat with invalid payment token returns 403."""

    @pytest.mark.smoke
    def test_chat_invalid_token_returns_403(
        self, api_client: TrinityApiClient, created_agent
    ):
        """POST /api/paid/{name}/chat with invalid payment-signature returns 403."""
        _ensure_nvm_config(api_client, created_agent["name"])

        # Use httpx directly to set custom header
        response = api_client._client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={"message": "Hello"},
            headers={"payment-signature": "invalid-token-abc123"},
        )
        # 403 (verification failed) or 500 (SDK error during verification)
        assert_status_in(response, [403, 500])

        if response.status_code == 403:
            data = assert_json_response(response)
            assert_has_fields(data, ["detail", "error"])
            assert data["detail"] == "Payment verification failed"


# =============================================================================
# Admin Settlement Endpoints
# =============================================================================

class TestSettlementAdmin:
    """Test admin-only settlement failure and retry endpoints."""

    @pytest.mark.smoke
    def test_settlement_failures_list(self, api_client: TrinityApiClient):
        """GET /api/nevermined/settlement-failures returns list (admin)."""
        response = api_client.get(
            "/api/nevermined/settlement-failures"
        )
        assert_status(response, 200)
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.smoke
    def test_settlement_failures_unauthenticated(
        self, unauthenticated_client: TrinityApiClient
    ):
        """GET /api/nevermined/settlement-failures without auth returns 401."""
        response = unauthenticated_client.get(
            "/api/nevermined/settlement-failures",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_retry_settlement_nonexistent(self, api_client: TrinityApiClient):
        """POST /api/nevermined/retry-settlement/{log_id} returns 404 for bad ID."""
        response = api_client.post(
            "/api/nevermined/retry-settlement/nonexistent-log-id-123",
        )
        # 404 (not found) or 501 (SDK not installed)
        assert_status_in(response, [404, 501])

    @pytest.mark.smoke
    def test_retry_settlement_unauthenticated(
        self, unauthenticated_client: TrinityApiClient
    ):
        """POST /api/nevermined/retry-settlement/{id} without auth returns 401."""
        response = unauthenticated_client.post(
            "/api/nevermined/retry-settlement/any-id",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_settlement_failures_with_limit(self, api_client: TrinityApiClient):
        """GET /api/nevermined/settlement-failures respects limit param."""
        response = api_client.get(
            "/api/nevermined/settlement-failures?limit=5"
        )
        assert_status(response, 200)
        data = response.json()
        assert isinstance(data, list)
        assert len(data) <= 5


# =============================================================================
# Paid Chat Request Validation
# =============================================================================

class TestPaidChatValidation:
    """Test request validation for paid chat endpoint."""

    @pytest.mark.smoke
    def test_chat_missing_message(self, api_client: TrinityApiClient, created_agent):
        """POST /api/paid/{name}/chat without message field returns 422."""
        _ensure_nvm_config(api_client, created_agent["name"])

        response = api_client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={},
            auth=False,
        )
        assert_status(response, 422)

    @pytest.mark.smoke
    def test_chat_with_session_id(self, api_client: TrinityApiClient, created_agent):
        """POST /api/paid/{name}/chat accepts optional session_id."""
        _ensure_nvm_config(api_client, created_agent["name"])

        response = api_client.post(
            f"/api/paid/{created_agent['name']}/chat",
            json={"message": "Hello", "session_id": "test-session-123"},
            auth=False,
        )
        # 402 (no payment header) — but validates that session_id is accepted
        assert_status(response, 402)
