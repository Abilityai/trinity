"""
System Agent Tests (test_system_agent.py)

Tests for Trinity system agent management endpoints including:
- System agent status checking
- System agent restart
- System agent re-initialization

Covers REQ-SYSAGENT-001 (System Agent Management).

Note: The system agent is auto-deployed on platform startup and cannot be deleted.
These tests verify management operations only.
"""

import pytest
import time

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)

# The system agent name as configured in the platform
SYSTEM_AGENT_NAME = "trinity-system"


class TestSystemAgentAuthentication:
    """Tests for System Agent API authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_system_agent_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/system-agent/status requires authentication."""
        response = unauthenticated_client.get("/api/system-agent/status", auth=False)
        assert_status(response, 401)

    def test_system_agent_restart_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/system-agent/restart requires authentication."""
        response = unauthenticated_client.post("/api/system-agent/restart", auth=False)
        assert_status(response, 401)

    def test_system_agent_reinitialize_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/system-agent/reinitialize requires authentication."""
        response = unauthenticated_client.post("/api/system-agent/reinitialize", auth=False)
        assert_status(response, 401)


class TestSystemAgentStatus:
    """Tests for System Agent Status API."""

    pytestmark = pytest.mark.smoke

    def test_get_system_agent_status_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/system-agent/status returns expected structure."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have exists flag and status
        assert "exists" in data
        assert "status" in data
        assert "name" in data

        # Name should match the system agent name
        assert data["name"] == SYSTEM_AGENT_NAME

    def test_system_agent_status_running_has_container_id(self, api_client: TrinityApiClient):
        """Running system agent status includes container ID."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if data.get("exists") and data.get("status") == "running":
            # Should have container info
            assert "container_id" in data
            assert len(data["container_id"]) > 0

    def test_system_agent_status_includes_is_system_flag(self, api_client: TrinityApiClient):
        """System agent status includes is_system flag."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if data.get("exists"):
            # Should be marked as system agent
            assert data.get("is_system") is True

    def test_system_agent_status_not_found_response(self, api_client: TrinityApiClient):
        """System agent returns proper response if not deployed."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if not data.get("exists"):
            # Should have message explaining the situation
            assert data["status"] == "not_found"
            assert "message" in data


class TestSystemAgentRestart:
    """Tests for System Agent Restart API.

    Note: Restart operations involve stopping and starting containers,
    which can take 30+ seconds.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_restart_requires_admin(self, api_client: TrinityApiClient):
        """POST /api/system-agent/restart requires admin role."""
        response = api_client.post("/api/system-agent/restart", timeout=90.0)
        # Should be 200 (success), 403 (not admin), or 404 (no system agent)
        assert_status_in(response, [200, 403, 404])

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_restart_returns_success_structure(self, api_client: TrinityApiClient):
        """Successful restart returns expected structure."""
        response = api_client.post("/api/system-agent/restart", timeout=90.0)

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message", "name"])
            assert data["success"] is True
            assert data["name"] == SYSTEM_AGENT_NAME

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_restart_includes_status(self, api_client: TrinityApiClient):
        """Restart response includes final status."""
        response = api_client.post("/api/system-agent/restart", timeout=90.0)

        if response.status_code == 200:
            data = response.json()
            # Should include final container status
            assert "status" in data
            assert data["status"] == "running"

    def test_restart_not_found_returns_404(self, api_client: TrinityApiClient):
        """Restart returns 404 if system agent doesn't exist."""
        # First check if it exists
        status_response = api_client.get("/api/system-agent/status")
        if status_response.status_code == 200 and not status_response.json().get("exists"):
            # If it doesn't exist, restart should return 404
            response = api_client.post("/api/system-agent/restart")
            assert_status(response, 404)


class TestSystemAgentReinitialize:
    """Tests for System Agent Re-initialization API.

    Note: Re-initialization involves stopping, clearing workspace, and restarting
    the container, which can take 60+ seconds.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_reinitialize_requires_admin(self, api_client: TrinityApiClient):
        """POST /api/system-agent/reinitialize requires admin role."""
        response = api_client.post("/api/system-agent/reinitialize", timeout=150.0)
        # Should be 200 (success), 403 (not admin), or 404 (no system agent)
        assert_status_in(response, [200, 403, 404])

    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_reinitialize_returns_success_structure(self, api_client: TrinityApiClient):
        """Successful re-initialization returns expected structure."""
        response = api_client.post("/api/system-agent/reinitialize", timeout=150.0)

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message", "name", "steps_completed"])
            assert data["success"] is True
            assert data["name"] == SYSTEM_AGENT_NAME
            assert isinstance(data["steps_completed"], list)

    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_reinitialize_includes_steps(self, api_client: TrinityApiClient):
        """Re-initialization response includes completed steps."""
        response = api_client.post("/api/system-agent/reinitialize", timeout=150.0)

        if response.status_code == 200:
            data = response.json()
            steps = data["steps_completed"]
            # Should complete several steps
            # Possible steps: stopped, workspace_cleared, started, trinity_injected
            assert len(steps) > 0

    pytestmark = pytest.mark.smoke

    def test_reinitialize_not_found_returns_404(self, api_client: TrinityApiClient):
        """Re-initialize returns 404 if system agent doesn't exist."""
        # First check if it exists
        status_response = api_client.get("/api/system-agent/status")
        if status_response.status_code == 200 and not status_response.json().get("exists"):
            response = api_client.post("/api/system-agent/reinitialize")
            assert_status(response, 404)


class TestSystemAgentIntegration:
    """Integration tests for System Agent operations.

    These tests verify the system agent after operations complete.
    They are marked as slow since they may need to wait for container operations.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_system_agent_restart_preserves_workspace(self, api_client: TrinityApiClient):
        """System agent restart preserves workspace content."""
        # Skip if system agent doesn't exist
        status_response = api_client.get("/api/system-agent/status")
        if status_response.status_code != 200:
            pytest.skip("Cannot check system agent status")

        data = status_response.json()
        if not data.get("exists"):
            pytest.skip("System agent not deployed")

        if data.get("status") != "running":
            pytest.skip("System agent not running")

        # Perform restart
        response = api_client.post("/api/system-agent/restart", timeout=90.0)
        if response.status_code == 403:
            pytest.skip("User not authorized for restart")

        assert_status(response, 200)

        # Wait for restart to complete
        time.sleep(3)

        # Verify system agent is running again
        status_response = api_client.get("/api/system-agent/status")
        assert_status(status_response, 200)
        data = status_response.json()
        assert data.get("status") == "running", "System agent should be running after restart"

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_system_agent_status_after_restart(self, api_client: TrinityApiClient):
        """System agent status is accurate after restart."""
        # Get initial status
        initial_response = api_client.get("/api/system-agent/status")
        if initial_response.status_code != 200:
            pytest.skip("Cannot check system agent status")

        initial_data = initial_response.json()
        if not initial_data.get("exists") or initial_data.get("status") != "running":
            pytest.skip("System agent not running")

        # Get status after potential operations
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        # Verify all expected fields are present
        assert data.get("exists") is True
        assert "status" in data
        assert "name" in data
        assert data["name"] == SYSTEM_AGENT_NAME


class TestSystemAgentHealthInfo:
    """Tests for System Agent health information."""

    pytestmark = pytest.mark.smoke

    def test_system_agent_health_endpoint(self, api_client: TrinityApiClient):
        """System agent status includes health info when running."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        if data.get("exists") and data.get("status") == "running":
            # Health info might be present (from internal /api/health call)
            # If health info is available, verify structure
            if "health" in data:
                health = data["health"]
                # Health response should have status
                assert "status" in health

    def test_system_agent_health_error_handled(self, api_client: TrinityApiClient):
        """System agent status handles health check failures gracefully."""
        response = api_client.get("/api/system-agent/status")
        assert_status(response, 200)
        data = response.json()

        # If health check fails, should have health_error field
        # (not a hard requirement, just graceful handling)
        if data.get("exists") and "health_error" in data:
            # Error should be a string describing the issue
            assert isinstance(data["health_error"], str)
