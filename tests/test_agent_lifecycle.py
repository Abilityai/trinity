"""
Agent Lifecycle Tests (test_agent_lifecycle.py)

Tests for agent CRUD operations and lifecycle management.
Covers REQ-AGENT-001 through REQ-AGENT-008.
"""

import pytest
import time
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_agent_fields,
    assert_list_response,
    assert_contains_agent,
    assert_not_contains_agent,
    assert_error_response,
)
from testkit.cleanup import cleanup_test_agent


class TestListAgents:
    """REQ-AGENT-001: List agents endpoint tests."""

    @pytest.mark.smoke
    def test_list_agents_returns_array(self, api_client: TrinityApiClient):
        """GET /api/agents returns array of agents."""
        response = api_client.get("/api/agents")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_list_response(data, "agents")

    def test_list_agents_has_required_fields(self, api_client: TrinityApiClient, created_agent):
        """Each agent in list has required fields."""
        response = api_client.get("/api/agents")

        assert_status(response, 200)
        agents = response.json()

        # Find our created agent
        agent = assert_contains_agent(agents, created_agent["name"])
        assert_agent_fields(agent)

    def test_list_agents_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents requires authentication."""
        response = unauthenticated_client.get("/api/agents", auth=False)
        assert_status_in(response, [401, 403])


class TestCreateAgent:
    """REQ-AGENT-002: Create agent endpoint tests."""

    @pytest.mark.smoke
    def test_create_minimal_agent(
        self,
        api_client: TrinityApiClient,
        test_agent_name: str,
        resource_tracker
    ):
        """POST /api/agents creates agent with name only."""
        response = api_client.post(
            "/api/agents",
            json={"name": test_agent_name},
        )

        assert_status_in(response, [200, 201])
        resource_tracker.track_agent(test_agent_name)

        data = assert_json_response(response)
        assert data.get("name") == test_agent_name

    def test_create_agent_with_template(
        self,
        api_client: TrinityApiClient,
        test_agent_name: str,
        resource_tracker
    ):
        """POST /api/agents creates agent with template."""
        response = api_client.post(
            "/api/agents",
            json={
                "name": test_agent_name,
                "template": "local:default",
            },
        )

        # #1759: `local:default` is a real shipped template and an absent one
        # is now a hard 400, so the old `if status in [200, 201]` tolerance had
        # become a way for this test to silently assert nothing at all.
        assert_status_in(response, [200, 201])
        resource_tracker.track_agent(test_agent_name)
        data = response.json()
        assert data.get("name") == test_agent_name

    def test_create_agent_with_resources(
        self,
        api_client: TrinityApiClient,
        test_agent_name: str,
        resource_tracker
    ):
        """POST /api/agents creates agent with custom resources."""
        response = api_client.post(
            "/api/agents",
            json={
                "name": test_agent_name,
                "resources": {"cpu": "1", "memory": "2g"},
            },
        )

        assert_status_in(response, [200, 201])
        resource_tracker.track_agent(test_agent_name)

    def test_duplicate_name_returns_409(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents with duplicate name returns 409."""
        response = api_client.post(
            "/api/agents",
            json={"name": created_agent["name"]},
        )

        assert_status_in(response, [400, 409])

    def test_invalid_name_is_sanitized(self, api_client: TrinityApiClient, resource_tracker):
        """POST /api/agents with invalid name sanitizes it for Docker compatibility.

        Note: The API sanitizes names rather than rejecting them to ensure Docker
        container name compatibility. Names are converted to lowercase, spaces/special
        chars become hyphens. This is by design per sanitize_agent_name() in helpers.py.
        """
        invalid_name = "invalid name with spaces!"
        expected_sanitized = "invalid-name-with-spaces"

        response = api_client.post(
            "/api/agents",
            json={"name": invalid_name},
        )

        # API should accept and sanitize the name (200/201) OR reject if agent exists (400/409)
        if response.status_code in [200, 201]:
            data = response.json()
            # Verify name was sanitized
            assert data.get("name") == expected_sanitized
            resource_tracker.track_agent(expected_sanitized)
        else:
            # Agent may already exist from previous test run
            assert_status_in(response, [400, 409])

    def test_agent_auto_started_after_creation(
        self,
        api_client: TrinityApiClient,
        test_agent_name: str,
        resource_tracker
    ):
        """Agent is automatically started after creation."""
        response = api_client.post(
            "/api/agents",
            json={"name": test_agent_name},
        )

        if response.status_code not in [200, 201]:
            pytest.skip(f"Agent creation failed: {response.text}")

        resource_tracker.track_agent(test_agent_name)

        # Wait for agent to start
        time.sleep(10)

        # Check status
        check = api_client.get(f"/api/agents/{test_agent_name}")
        assert_status(check, 200)
        agent = check.json()

        # Should be running or at least starting
        assert agent["status"] in ["running", "starting"], \
            f"Expected running/starting, got {agent['status']}"


class TestGetAgent:
    """REQ-AGENT-003: Get agent details endpoint tests."""

    @pytest.mark.smoke
    def test_get_agent_returns_details(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name} returns full agent details."""
        response = api_client.get(f"/api/agents/{created_agent['name']}")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["name", "status"])
        assert data["name"] == created_agent["name"]

    def test_get_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name} for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz")
        assert_status(response, 404)

    def test_get_agent_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name} requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}",
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestStartAgent:
    """REQ-AGENT-004: Start agent endpoint tests."""

    def test_start_stopped_agent(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/start starts a stopped agent."""
        response = api_client.post(f"/api/agents/{stopped_agent['name']}/start")

        assert_status_in(response, [200, 202])

        # Wait and verify
        time.sleep(5)
        check = api_client.get(f"/api/agents/{stopped_agent['name']}")
        agent = check.json()
        assert agent["status"] in ["running", "starting"]

    def test_start_running_agent_idempotent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/start on running agent is idempotent."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/start")

        # Should succeed or return appropriate status
        assert_status_in(response, [200, 202, 400])

    def test_start_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/start for non-existent agent returns 404."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz/start")
        assert_status(response, 404)


class TestStopAgent:
    """REQ-AGENT-005: Stop agent endpoint tests."""

    def test_stop_running_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/stop stops a running agent."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/stop")

        assert_status_in(response, [200, 202])

        # Wait and verify
        time.sleep(3)
        check = api_client.get(f"/api/agents/{created_agent['name']}")
        agent = check.json()
        assert agent["status"] in ["stopped", "exited"]

    def test_stop_stopped_agent_idempotent(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/stop on stopped agent is idempotent."""
        response = api_client.post(f"/api/agents/{stopped_agent['name']}/stop")

        # Should succeed or return appropriate status
        assert_status_in(response, [200, 202, 400])

    def test_stop_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/stop for non-existent agent returns 404."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz/stop")
        assert_status(response, 404)


class TestDeleteAgent:
    """REQ-AGENT-006: Delete agent endpoint tests."""

    def test_delete_agent(
        self,
        api_client: TrinityApiClient,
        test_agent_name: str,
    ):
        """DELETE /api/agents/{name} deletes agent and container."""
        # First create an agent
        create_response = api_client.post(
            "/api/agents",
            json={"name": test_agent_name},
        )
        if create_response.status_code not in [200, 201]:
            pytest.skip("Failed to create agent for deletion test")

        # Wait briefly
        time.sleep(3)

        # Delete it
        response = api_client.delete(f"/api/agents/{test_agent_name}")
        assert_status_in(response, [200, 204])

        # Verify it's gone
        check = api_client.get(f"/api/agents/{test_agent_name}")
        assert_status(check, 404)

    def test_delete_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """DELETE /api/agents/{name} for non-existent agent returns 404."""
        response = api_client.delete("/api/agents/nonexistent-agent-xyz")
        assert_status(response, 404)


class TestAgentLogs:
    """REQ-AGENT-007: Agent logs endpoint tests."""

    def test_get_agent_logs(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/logs returns container logs."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/logs")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "logs" in data or isinstance(data, str) or isinstance(data, list)

    def test_get_logs_with_lines_param(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/logs supports lines parameter."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/logs",
            params={"lines": 10}
        )

        assert_status(response, 200)


class TestAgentInfo:
    """REQ-AGENT-008: Agent info endpoint tests."""

    def test_get_agent_info(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/info returns agent info."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/info")

        # May return 404/503 if agent server not ready
        if response.status_code == 200:
            data = assert_json_response(response)
            # Should have some info fields
            assert isinstance(data, dict)

    def test_get_info_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/info for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/info")
        assert_status_in(response, [404, 503])
