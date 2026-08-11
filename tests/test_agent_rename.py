"""
Tests for Agent Rename Feature (RENAME-001)
Related flow: docs/memory/feature-flows/agent-lifecycle.md

Tests the ability to rename agents via API:
- PUT /api/agents/{name}/rename
"""
import pytest
from testkit.api_client import TrinityApiClient


class TestAgentRename:
    """Test suite for agent rename functionality."""

    @pytest.fixture
    def test_agent_name(self, api_client: TrinityApiClient):
        """Create a test agent for rename tests."""
        # Create a unique test agent
        import uuid
        agent_name = f"rename-test-{uuid.uuid4().hex[:8]}"

        response = api_client.post(
            f"/api/agents",
            json={
                "name": agent_name,
                "type": "business-assistant",
                "resources": {"cpu": "1", "memory": "1g"}
            },
            timeout=60
        )

        if response.status_code == 200:
            yield agent_name
            # Cleanup - try to delete common variants that tests might create
            cleanup_names = [
                agent_name,
                f"{agent_name}-renamed",
                "test-agent-with-spaces",  # From sanitization test
            ]
            for name in cleanup_names:
                try:
                    api_client.delete(f"/api/agents/{name}", timeout=30)
                except Exception:
                    pass
        else:
            pytest.skip(f"Failed to create test agent: {response.text}")

    def test_rename_agent_success(self, api_client: TrinityApiClient, test_agent_name):
        """Test successful agent rename."""
        new_name = f"{test_agent_name}-renamed"

        response = api_client.put(
            f"/api/agents/{test_agent_name}/rename",
            json={"new_name": new_name},
            timeout=30
        )

        assert response.status_code == 200, f"Rename failed: {response.text}"
        data = response.json()
        assert data["old_name"] == test_agent_name
        assert data["new_name"] == new_name
        assert "message" in data

        # Verify agent is accessible at new name
        verify_response = api_client.get(f"/api/agents/{new_name}", timeout=10)
        assert verify_response.status_code == 200

        # Verify agent is NOT accessible at old name
        old_response = api_client.get(f"/api/agents/{test_agent_name}", timeout=10)
        assert old_response.status_code == 404

    def test_rename_agent_empty_name(self, api_client: TrinityApiClient, test_agent_name):
        """Test rename with empty name fails."""
        response = api_client.put(
            f"/api/agents/{test_agent_name}/rename",
            json={"new_name": ""},
            timeout=10
        )

        assert response.status_code == 400
        assert "empty" in response.text.lower() or "invalid" in response.text.lower()

    def test_rename_agent_same_name(self, api_client: TrinityApiClient, test_agent_name):
        """Test rename to same name fails."""
        response = api_client.put(
            f"/api/agents/{test_agent_name}/rename",
            json={"new_name": test_agent_name},
            timeout=10
        )

        assert response.status_code == 400
        assert "same" in response.text.lower()

    def test_rename_nonexistent_agent(self, api_client: TrinityApiClient):
        """Test rename of non-existent agent fails."""
        response = api_client.put(
            f"/api/agents/nonexistent-agent-xyz123/rename",
            json={"new_name": "new-name"},
            timeout=10
        )

        assert response.status_code in [403, 404]

    def test_rename_agent_unauthorized(self, api_config):
        """Test rename without auth fails."""
        import requests
        response = requests.put(
            f"{api_config.base_url}/api/agents/any-agent/rename",
            json={"new_name": "new-name"},
            timeout=10
        )

        assert response.status_code == 401

    def test_rename_agent_name_sanitization(self, api_client: TrinityApiClient, test_agent_name):
        """Test that new name is sanitized for Docker compatibility."""
        # First, cleanup any leftover from previous failed test runs
        try:
            api_client.delete("/api/agents/test-agent-with-spaces", timeout=10)
        except Exception:
            pass

        # Name with spaces and special chars should be sanitized
        new_name = "Test Agent With Spaces!"

        response = api_client.put(
            f"/api/agents/{test_agent_name}/rename",
            json={"new_name": new_name},
            timeout=30
        )

        assert response.status_code == 200, f"Rename failed: {response.text}"
        data = response.json()
        # Should be sanitized to lowercase with hyphens
        assert data["new_name"] == "test-agent-with-spaces"

    def test_rename_to_existing_name_fails(self, api_client: TrinityApiClient, test_agent_name):
        """Test rename to an already existing agent name fails."""
        # Create another agent
        import uuid
        other_name = f"other-agent-{uuid.uuid4().hex[:8]}"

        create_response = api_client.post(
            f"/api/agents",
            json={
                "name": other_name,
                "type": "business-assistant",
                "resources": {"cpu": "1", "memory": "1g"}
            },
            timeout=60
        )

        if create_response.status_code != 200:
            pytest.skip("Could not create second test agent")

        try:
            # Try to rename test_agent to other_name
            response = api_client.put(
                f"/api/agents/{test_agent_name}/rename",
                json={"new_name": other_name},
                timeout=10
            )

            assert response.status_code == 409
            assert "exists" in response.text.lower()
        finally:
            # Cleanup
            api_client.delete(f"/api/agents/{other_name}", timeout=30)


class TestAgentRenameSystemAgent:
    """Test that system agents cannot be renamed."""

    def test_rename_system_agent_fails(self, api_client: TrinityApiClient):
        """Test that system agent cannot be renamed."""
        response = api_client.put(
            f"/api/agents/trinity-system/rename",
            json={"new_name": "new-system-name"},
            timeout=10
        )

        assert response.status_code == 403
        assert "system" in response.text.lower()
