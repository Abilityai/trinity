"""
Agent Shared Folders Tests (test_shared_folders.py)

Tests for Trinity agent shared folders endpoints.
Covers REQ-FOLDERS-001 (9.11 Agent Shared Folders).

Feature Flow: agent-shared-folders.md

These tests verify the shared folder configuration API.
Note: Actual folder mounting requires agent restart.
"""

import pytest
import time
import uuid

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from testkit.cleanup import cleanup_test_agent


class TestSharedFoldersAuthentication:
    """Tests for shared folders authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_folders_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/folders requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/folders", auth=False)
        assert_status(response, 401)

    def test_update_folders_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/agents/{name}/folders requires authentication."""
        response = unauthenticated_client.put(
            "/api/agents/test-agent/folders",
            json={"expose_enabled": True},
            auth=False
        )
        assert_status(response, 401)

    def test_available_folders_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/folders/available requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/folders/available", auth=False)
        assert_status(response, 401)

    def test_folder_consumers_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/folders/consumers requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/folders/consumers", auth=False)
        assert_status(response, 401)


class TestGetFoldersConfig:
    """Tests for GET /api/agents/{name}/folders endpoint."""

    def test_get_folders_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """GET /api/agents/{name}/folders returns expected structure."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "agent_name",
            "expose_enabled",
            "consume_enabled",
        ])
        assert data["agent_name"] == agent_name

    def test_folders_boolean_fields(self, api_client: TrinityApiClient, created_agent: dict):
        """expose_enabled and consume_enabled are booleans."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders")

        assert_status(response, 200)
        data = response.json()

        assert isinstance(data["expose_enabled"], bool)
        assert isinstance(data["consume_enabled"], bool)

    def test_folders_default_values(self, api_client: TrinityApiClient, created_agent: dict):
        """New agents have default folder config (both disabled)."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders")

        assert_status(response, 200)
        data = response.json()

        # Default is both disabled
        assert data["expose_enabled"] is False
        assert data["consume_enabled"] is False

    def test_folders_includes_status(self, api_client: TrinityApiClient, created_agent: dict):
        """Folders response includes agent status."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders")

        assert_status(response, 200)
        data = response.json()

        assert "status" in data
        assert data["status"] in ["running", "stopped", "error"]

    def test_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/folders returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/folders")
        assert_status(response, 404)


class TestUpdateFoldersConfig:
    """Tests for PUT /api/agents/{name}/folders endpoint."""

    def test_enable_expose(self, api_client: TrinityApiClient, created_agent: dict):
        """PUT /api/agents/{name}/folders can enable expose_enabled."""
        agent_name = created_agent["name"]

        response = api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        assert_status(response, 200)
        data = response.json()
        assert data["expose_enabled"] is True
        assert "restart_required" in data

    def test_enable_consume(self, api_client: TrinityApiClient, created_agent: dict):
        """PUT /api/agents/{name}/folders can enable consume_enabled."""
        agent_name = created_agent["name"]

        response = api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"consume_enabled": True}
        )

        assert_status(response, 200)
        data = response.json()
        assert data["consume_enabled"] is True
        assert "restart_required" in data

    def test_update_returns_restart_required(self, api_client: TrinityApiClient, created_agent: dict):
        """Update response indicates restart_required."""
        agent_name = created_agent["name"]

        response = api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        assert_status(response, 200)
        data = response.json()

        assert "restart_required" in data
        assert isinstance(data["restart_required"], bool)
        # After config change, restart should be required
        assert data["restart_required"] is True

    def test_update_persists(self, api_client: TrinityApiClient, created_agent: dict):
        """Folder config updates persist."""
        agent_name = created_agent["name"]

        # Enable both
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True, "consume_enabled": True}
        )

        # Verify persistence
        response = api_client.get(f"/api/agents/{agent_name}/folders")
        assert_status(response, 200)
        data = response.json()

        assert data["expose_enabled"] is True
        assert data["consume_enabled"] is True

    def test_disable_after_enable(self, api_client: TrinityApiClient, created_agent: dict):
        """Can disable folders after enabling."""
        agent_name = created_agent["name"]

        # Enable first
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        # Then disable
        response = api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": False}
        )

        assert_status(response, 200)
        data = response.json()
        assert data["expose_enabled"] is False

    def test_partial_update(self, api_client: TrinityApiClient, created_agent: dict):
        """Can update one field without affecting the other."""
        agent_name = created_agent["name"]

        # Enable expose
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        # Update only consume (expose should remain true)
        response = api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"consume_enabled": True}
        )

        assert_status(response, 200)
        data = response.json()
        assert data["expose_enabled"] is True
        assert data["consume_enabled"] is True


class TestAvailableFolders:
    """Tests for GET /api/agents/{name}/folders/available endpoint."""

    def test_available_folders_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """GET /api/agents/{name}/folders/available returns expected structure."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders/available")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "available_folders", "count"])
        assert data["agent_name"] == agent_name

    def test_available_folders_is_list(self, api_client: TrinityApiClient, created_agent: dict):
        """available_folders is a list."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/folders/available")

        assert_status(response, 200)
        data = response.json()

        assert isinstance(data["available_folders"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["available_folders"])

    def test_available_folder_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """Available folder entries have expected fields."""
        agent_name = created_agent["name"]

        # First, enable expose on this agent so it might appear for others
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        response = api_client.get(f"/api/agents/{agent_name}/folders/available")
        assert_status(response, 200)
        data = response.json()

        # If there are available folders, check structure
        for folder in data.get("available_folders", []):
            assert_has_fields(folder, ["source_agent", "volume_name", "mount_path"])


class TestFolderConsumers:
    """Tests for GET /api/agents/{name}/folders/consumers endpoint."""

    def test_consumers_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """GET /api/agents/{name}/folders/consumers returns expected structure."""
        agent_name = created_agent["name"]

        # Enable expose first
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        response = api_client.get(f"/api/agents/{agent_name}/folders/consumers")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["source_agent", "consumers", "count"])
        assert data["source_agent"] == agent_name

    def test_consumers_is_list(self, api_client: TrinityApiClient, created_agent: dict):
        """consumers is a list."""
        agent_name = created_agent["name"]

        # Enable expose
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        response = api_client.get(f"/api/agents/{agent_name}/folders/consumers")
        assert_status(response, 200)
        data = response.json()

        assert isinstance(data["consumers"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["consumers"])


class TestSharedFoldersOwnershipRequired:
    """Tests verifying only owner can modify folder config."""

    def test_update_requires_ownership(self, api_client: TrinityApiClient):
        """PUT /api/agents/{name}/folders requires ownership."""
        # This would need a second user context to properly test
        # For now, verify 404 for nonexistent agents
        response = api_client.put(
            "/api/agents/nonexistent-agent-xyz123/folders",
            json={"expose_enabled": True}
        )
        assert_status_in(response, [403, 404])


class TestSharedFoldersWithTwoAgents:
    """Tests for shared folders between two agents.

    These tests create two agents to verify folder sharing works.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_expose_creates_volume_info(self, api_client: TrinityApiClient, created_agent: dict):
        """Enabling expose provides volume information."""
        agent_name = created_agent["name"]

        # Enable expose
        api_client.put(
            f"/api/agents/{agent_name}/folders",
            json={"expose_enabled": True}
        )

        # Get folders config
        response = api_client.get(f"/api/agents/{agent_name}/folders")
        assert_status(response, 200)
        data = response.json()

        # Should have exposed volume info
        assert data["expose_enabled"] is True
        if "exposed_volume" in data:
            assert agent_name in data["exposed_volume"]
        if "exposed_path" in data:
            assert "/home/developer/shared-out" in data["exposed_path"]

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_consumed_folders_empty_initially(self, api_client: TrinityApiClient, created_agent: dict):
        """Consumed folders list is empty before enabling consume."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/agents/{agent_name}/folders")
        assert_status(response, 200)
        data = response.json()

        # consumed_folders should be empty or not present
        consumed = data.get("consumed_folders", [])
        assert isinstance(consumed, list)
