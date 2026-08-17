"""
MCP API Keys Tests (test_mcp_keys.py)

Tests for MCP API key management.
Covers REQ-MCP-001 through REQ-MCP-004.

FAST TESTS - No agent creation required.
"""

import pytest

# Mark all tests in this module as smoke tests (fast, no agent needed)
pytestmark = pytest.mark.smoke
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestCreateApiKey:
    """REQ-MCP-001: Create API key endpoint tests."""

    def test_create_api_key(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
        resource_tracker
    ):
        """POST /api/mcp/keys creates API key."""
        response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        # Track for cleanup
        if "id" in data:
            resource_tracker.track_mcp_key(data["id"])

        # Should return full key (only time it's shown)
        assert_has_fields(data, ["id", "name"])
        assert "key" in data or "api_key" in data or "access_key" in data

    def test_created_key_has_prefix(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
        resource_tracker
    ):
        """Created API key has expected prefix."""
        response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        if response.status_code not in [200, 201]:
            pytest.skip("Key creation failed")

        data = response.json()
        if "id" in data:
            resource_tracker.track_mcp_key(data["id"])

        # Get the full key
        key = data.get("key", data.get("api_key", data.get("access_key", "")))
        assert key.startswith("trinity_mcp_"), f"Key should start with trinity_mcp_, got: {key[:20]}..."


class TestListApiKeys:
    """REQ-MCP-002: List API keys endpoint tests."""

    def test_list_api_keys(self, api_client: TrinityApiClient):
        """GET /api/mcp/keys returns user's keys."""
        response = api_client.get("/api/mcp/keys")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_list_response(data, "API keys")

    def test_listed_keys_dont_expose_full_key(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
        resource_tracker
    ):
        """Listed keys don't include full key value."""
        # Create a key first
        create_response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        if create_response.status_code in [200, 201]:
            data = create_response.json()
            if "id" in data:
                resource_tracker.track_mcp_key(data["id"])

        # List and check
        list_response = api_client.get("/api/mcp/keys")
        assert_status(list_response, 200)
        keys = list_response.json()

        for key in keys:
            # Should only show prefix, not full key
            if "key" in key:
                # Key should be masked or shortened
                assert len(key["key"]) < 50, "Listed key should be truncated/masked"


class TestValidateApiKey:
    """REQ-MCP-003: Validate API key endpoint tests."""

    def test_validate_valid_key(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
        resource_tracker
    ):
        """POST /api/mcp/validate with valid key returns user info."""
        # Create a key
        create_response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Key creation failed")

        data = create_response.json()
        if "id" in data:
            resource_tracker.track_mcp_key(data["id"])

        full_key = data.get("key", data.get("api_key", data.get("access_key")))

        # Validate the key - use Authorization header
        validate_response = api_client._client.post(
            "/api/mcp/validate",
            headers={"Authorization": f"Bearer {full_key}"}
        )

        assert_status(validate_response, 200)
        validate_data = validate_response.json()

        # Should return user info
        assert "user_id" in validate_data or "valid" in validate_data

    def test_validate_invalid_key(self, api_client: TrinityApiClient):
        """POST /api/mcp/validate with invalid key returns 401."""
        response = api_client._client.post(
            "/api/mcp/validate",
            headers={"Authorization": "Bearer invalid_key_here"}
        )

        assert_status_in(response, [401, 403])


class TestRevokeApiKey:
    """REQ-MCP-004: Revoke API key endpoint tests."""

    def test_revoke_api_key(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
    ):
        """POST /api/mcp/keys/{id}/revoke revokes key."""
        # Create a key
        create_response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Key creation failed")

        data = create_response.json()
        key_id = data.get("id")
        full_key = data.get("key", data.get("api_key", data.get("access_key")))

        # Revoke the key
        revoke_response = api_client.post(f"/api/mcp/keys/{key_id}/revoke")

        # May be implemented as DELETE
        if revoke_response.status_code == 404:
            # Try DELETE instead
            revoke_response = api_client.delete(f"/api/mcp/keys/{key_id}")

        assert_status_in(revoke_response, [200, 204])

        # Verify key no longer validates
        validate_response = api_client._client.post(
            "/api/mcp/validate",
            headers={"Authorization": f"Bearer {full_key}"}
        )

        assert_status_in(validate_response, [401, 403, 404])


class TestDeleteApiKey:
    """Tests for deleting API keys."""

    def test_delete_api_key(
        self,
        api_client: TrinityApiClient,
        test_mcp_key_name: str,
    ):
        """DELETE /api/mcp/keys/{id} deletes key."""
        # Create a key
        create_response = api_client.post(
            "/api/mcp/keys",
            json={"name": test_mcp_key_name}
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Key creation failed")

        data = create_response.json()
        key_id = data.get("id")

        # Delete the key
        delete_response = api_client.delete(f"/api/mcp/keys/{key_id}")

        assert_status_in(delete_response, [200, 204])

        # Verify it's gone from list
        list_response = api_client.get("/api/mcp/keys")
        keys = list_response.json()
        key_ids = [k.get("id") for k in keys]
        assert key_id not in key_ids
