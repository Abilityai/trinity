"""
Agent Execution Timeout Tests (test_agent_timeout.py)

Tests for per-agent execution timeout configuration (TIMEOUT-001).
Covers timeout configuration endpoints and validation.

Endpoints tested:
- GET /api/agents/{name}/timeout - Get execution timeout setting
- PUT /api/agents/{name}/timeout - Update execution_timeout_seconds (60-7200)

NOTE: These tests require a running agent container. Tests will be skipped
if the agent is not found (404) or not ready (503).
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


def skip_if_agent_not_ready(response):
    """Skip test if agent container is not running or not ready."""
    if response.status_code == 404:
        pytest.skip("Agent container not running")
    if response.status_code == 503:
        pytest.skip("Agent server not ready")


class TestTimeoutAuthentication:
    """Tests for timeout endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_timeout_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/timeout requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/timeout",
            auth=False
        )
        assert_status_in(response, [401, 403])

    def test_put_timeout_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout requires authentication."""
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": 600},
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestTimeoutGet:
    """Tests for GET /api/agents/{name}/timeout endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_timeout_returns_structure(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/timeout returns expected structure."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(response)

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "execution_timeout_seconds", "execution_timeout_minutes"])
        assert data["agent_name"] == created_agent['name']
        assert isinstance(data["execution_timeout_seconds"], int)
        assert isinstance(data["execution_timeout_minutes"], int)

    def test_get_timeout_default_is_3600(self, api_client: TrinityApiClient, created_agent):
        """Agents should default to execution_timeout_seconds=3600 (60 minutes)."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(response)

        assert_status(response, 200)
        data = response.json()
        # Default raised from 900s to 3600s in #665 (TIMEOUT-001 update).
        assert data["execution_timeout_seconds"] == 3600

    def test_get_timeout_minutes_calculation(self, api_client: TrinityApiClient, created_agent):
        """execution_timeout_minutes should equal execution_timeout_seconds // 60."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(response)

        assert_status(response, 200)
        data = response.json()
        expected_minutes = data["execution_timeout_seconds"] // 60
        assert data["execution_timeout_minutes"] == expected_minutes

    def test_get_timeout_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/timeout returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/timeout")
        assert_status(response, 404)


class TestTimeoutUpdate:
    """Tests for PUT /api/agents/{name}/timeout endpoint."""

    pytestmark = pytest.mark.smoke

    def test_put_timeout_update_and_restore(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout updates the setting."""
        # Get current setting first
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)

        assert_status(current, 200)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            # Update to a different value
            new_value = 600 if original_value != 600 else 900
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": new_value}
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["execution_timeout_seconds"] == new_value
            assert data["execution_timeout_minutes"] == new_value // 60

            # Verify by reading back
            verify = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
            assert_status(verify, 200)
            assert verify.json()["execution_timeout_seconds"] == new_value

        finally:
            # Restore original value
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )

    def test_put_timeout_minimum_value(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout accepts minimum value (60 seconds = 1 min)."""
        # Save original
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": 60}
            )
            assert_status(response, 200)
            assert response.json()["execution_timeout_seconds"] == 60
            assert response.json()["execution_timeout_minutes"] == 1
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )

    def test_put_timeout_maximum_value(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout accepts maximum value (7200 seconds = 2 hours)."""
        # Save original
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": 7200}
            )
            assert_status(response, 200)
            assert response.json()["execution_timeout_seconds"] == 7200
            assert response.json()["execution_timeout_minutes"] == 120
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )

    def test_put_timeout_rejects_below_minimum(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects values below 60 seconds."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": 59}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_rejects_zero(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects 0."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": 0}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_rejects_negative(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects negative values."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": -1}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_rejects_above_maximum(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects values above 7200."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": 7201}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_rejects_non_integer(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects non-integer values."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": 300.5}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_rejects_string(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout rejects string values."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={"execution_timeout_seconds": "five minutes"}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_requires_field(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout requires execution_timeout_seconds field."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/timeout",
            json={}
        )
        skip_if_agent_not_ready(response)
        assert_status(response, 400)

    def test_put_timeout_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """PUT /api/agents/{name}/timeout returns 404 for nonexistent agent."""
        response = api_client.put(
            "/api/agents/nonexistent-agent-xyz/timeout",
            json={"execution_timeout_seconds": 600}
        )
        assert_status(response, 404)


class TestTimeoutCommonValues:
    """Tests for common timeout values used in practice."""

    pytestmark = pytest.mark.smoke

    def test_put_timeout_five_minutes(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout accepts 5 minutes (300s)."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": 300}
            )
            assert_status(response, 200)
            assert response.json()["execution_timeout_seconds"] == 300
            assert response.json()["execution_timeout_minutes"] == 5
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )

    def test_put_timeout_thirty_minutes(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout accepts 30 minutes (1800s)."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": 1800}
            )
            assert_status(response, 200)
            assert response.json()["execution_timeout_seconds"] == 1800
            assert response.json()["execution_timeout_minutes"] == 30
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )

    def test_put_timeout_one_hour(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/timeout accepts 1 hour (3600s)."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)
        original_value = current.json()["execution_timeout_seconds"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": 3600}
            )
            assert_status(response, 200)
            assert response.json()["execution_timeout_seconds"] == 3600
            assert response.json()["execution_timeout_minutes"] == 60
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )


class TestTimeoutResponseFormat:
    """Tests for timeout response format consistency."""

    pytestmark = pytest.mark.smoke

    def test_get_and_put_response_format_match(self, api_client: TrinityApiClient, created_agent):
        """GET and PUT responses should have consistent format."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/timeout")
        skip_if_agent_not_ready(current)

        assert_status(current, 200)
        get_data = current.json()
        original_value = get_data["execution_timeout_seconds"]

        try:
            # PUT and compare response format
            new_value = 600 if original_value != 600 else 900
            put_response = api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": new_value}
            )
            assert_status(put_response, 200)
            put_data = put_response.json()

            # Both should have these fields
            assert "agent_name" in put_data
            assert "execution_timeout_seconds" in put_data
            assert "execution_timeout_minutes" in put_data

            # PUT has additional "message" field
            assert "message" in put_data
            assert put_data["message"] == "Timeout updated"

        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/timeout",
                json={"execution_timeout_seconds": original_value}
            )
