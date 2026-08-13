"""
Per-Subscription Usage Tracking Tests (SUB-004)

Tests for GET /api/subscriptions/{id}/usage endpoint.
Covers:
- Correct response structure
- Returns zeros when no usage data
- Rolling window data is returned
"""

import pytest
import uuid

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


VALID_TOKEN = "sk-ant-oat01-test-sub-usage-token-12345"

USAGE_WINDOW_FIELDS = ["input_tokens", "output_tokens", "message_count"]
USAGE_FIELDS = [
    "billing_mode", "provider", "subscription_id", "subscription_name",
    "subscription_type", "utilization", "window_5h", "window_7d", "agents",
]


# =============================================================================
# SUB-004: Subscription Usage Endpoint Tests (SMOKE)
# =============================================================================

class TestSubscriptionUsage:
    """SUB-004: GET /api/subscriptions/{id}/usage tests."""

    @pytest.fixture(autouse=True)
    def _setup_subscription(self, api_client: TrinityApiClient):
        """Create a fresh subscription for each test and clean up after."""
        name = f"test-usage-sub-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/subscriptions",
            json={
                "name": name,
                "token": VALID_TOKEN,
                "subscription_type": "max",
            }
        )
        assert_status(response, 200)
        self.subscription = response.json()
        self.subscription_id = self.subscription["id"]

        yield

        # Cleanup
        api_client.delete(f"/api/subscriptions/{self.subscription_id}")

    @pytest.mark.smoke
    def test_usage_returns_correct_structure(self, api_client: TrinityApiClient):
        """GET /api/subscriptions/{id}/usage returns expected top-level fields."""
        response = api_client.get(f"/api/subscriptions/{self.subscription_id}/usage")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, USAGE_FIELDS)
        assert data["billing_mode"] == "subscription"
        assert data["provider"] == "anthropic"
        assert data["subscription_name"] == self.subscription["name"]
        assert data["utilization"]["status"] == "unavailable"
        assert data["utilization"]["percent"] is None

    @pytest.mark.smoke
    def test_usage_window_fields(self, api_client: TrinityApiClient):
        """Each usage window contains correct sub-fields."""
        response = api_client.get(f"/api/subscriptions/{self.subscription_id}/usage")
        assert_status(response, 200)
        data = response.json()

        assert_has_fields(data["window_5h"], USAGE_WINDOW_FIELDS)
        assert_has_fields(data["window_7d"], USAGE_WINDOW_FIELDS)

    @pytest.mark.smoke
    def test_usage_returns_zeros_when_no_data(self, api_client: TrinityApiClient):
        """Fresh subscription returns zero usage in both windows."""
        response = api_client.get(f"/api/subscriptions/{self.subscription_id}/usage")
        assert_status(response, 200)
        data = response.json()

        for window_key in ("window_5h", "window_7d"):
            window = data[window_key]
            assert window["input_tokens"] == 0, f"{window_key}.input_tokens should be 0"
            assert window["output_tokens"] == 0, f"{window_key}.output_tokens should be 0"
            assert "cost_usd" not in window, f"{window_key} must not expose subscription dollars"
            assert window["message_count"] == 0, f"{window_key}.message_count should be 0"

    @pytest.mark.smoke
    def test_usage_subscription_id_matches(self, api_client: TrinityApiClient):
        """Response subscription_id matches the queried subscription."""
        response = api_client.get(f"/api/subscriptions/{self.subscription_id}/usage")
        assert_status(response, 200)
        data = response.json()
        assert data["subscription_id"] == self.subscription_id

    @pytest.mark.smoke
    def test_usage_agents_is_list(self, api_client: TrinityApiClient):
        """agents field is a list (empty for fresh subscription)."""
        response = api_client.get(f"/api/subscriptions/{self.subscription_id}/usage")
        assert_status(response, 200)
        data = response.json()
        assert isinstance(data["agents"], list)

    @pytest.mark.smoke
    def test_usage_not_found_returns_404(self, api_client: TrinityApiClient):
        """GET /api/subscriptions/{unknown}/usage returns 404."""
        fake_id = f"nonexistent-{uuid.uuid4().hex}"
        response = api_client.get(f"/api/subscriptions/{fake_id}/usage")
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_usage_requires_auth(self, api_client: TrinityApiClient):
        """Usage endpoint requires authentication."""
        import requests
        url = api_client.config.base_url + f"/api/subscriptions/{self.subscription_id}/usage"
        response = requests.get(url)
        # Unauthenticated access should be rejected
        assert response.status_code in (401, 403)

    @pytest.mark.smoke
    def test_usage_lookupable_by_name(self, api_client: TrinityApiClient):
        """Usage endpoint resolves subscription by name as well as ID."""
        name = self.subscription["name"]
        response = api_client.get(f"/api/subscriptions/{name}/usage")
        assert_status(response, 200)
        data = response.json()
        # subscription_id in response should be the actual UUID, not the name
        assert data["subscription_id"] == self.subscription_id
