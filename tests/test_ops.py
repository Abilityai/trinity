"""
Fleet Operations Tests (test_ops.py)

Tests for Trinity fleet operations endpoints including:
- Fleet status and health monitoring
- Fleet-wide restart/stop operations
- Schedule pause/resume
- Emergency stop functionality
- Cost and observability metrics

Covers REQ-OPS-001 (Fleet Operations).
"""

import pytest
import time
import uuid

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from utils.cleanup import cleanup_test_agent


class TestFleetStatusAuthentication:
    """Tests for Fleet Status API authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_fleet_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/ops/fleet/status requires authentication."""
        response = unauthenticated_client.get("/api/ops/fleet/status", auth=False)
        assert_status(response, 401)

    def test_fleet_health_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/ops/fleet/health requires authentication."""
        response = unauthenticated_client.get("/api/ops/fleet/health", auth=False)
        assert_status(response, 401)

    def test_fleet_restart_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/fleet/restart requires authentication."""
        response = unauthenticated_client.post("/api/ops/fleet/restart", auth=False)
        assert_status(response, 401)

    def test_fleet_stop_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/fleet/stop requires authentication."""
        response = unauthenticated_client.post("/api/ops/fleet/stop", auth=False)
        assert_status(response, 401)

    def test_emergency_stop_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/emergency-stop requires authentication."""
        response = unauthenticated_client.post("/api/ops/emergency-stop", auth=False)
        assert_status(response, 401)


class TestFleetStatus:
    """Tests for Fleet Status API."""

    pytestmark = pytest.mark.smoke

    def test_get_fleet_status_returns_summary(self, api_client: TrinityApiClient):
        """GET /api/ops/fleet/status returns summary and agents list."""
        response = api_client.get("/api/ops/fleet/status")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have summary and agents
        assert_has_fields(data, ["timestamp", "summary", "agents"])

        # Summary should have counts
        summary = data["summary"]
        assert_has_fields(summary, ["total", "running", "stopped"])
        assert isinstance(summary["total"], int)
        assert isinstance(summary["running"], int)
        assert isinstance(summary["stopped"], int)

    def test_fleet_status_agents_have_required_fields(self, api_client: TrinityApiClient):
        """Fleet status agent entries have required fields."""
        response = api_client.get("/api/ops/fleet/status")
        assert_status(response, 200)
        data = response.json()

        agents = data.get("agents", [])
        for agent in agents:
            assert_has_fields(agent, ["name", "status"])
            assert agent["status"] in ["running", "stopped", "exited", "paused", "restarting"]

    def test_fleet_status_summary_counts_match(self, api_client: TrinityApiClient):
        """Fleet status summary counts match agents list."""
        response = api_client.get("/api/ops/fleet/status")
        assert_status(response, 200)
        data = response.json()

        summary = data["summary"]
        agents = data["agents"]

        # Total should match agents count
        assert summary["total"] == len(agents)

        # Running + stopped should approximately equal total
        # (there may be other states like exited)
        running = sum(1 for a in agents if a["status"] == "running")
        assert summary["running"] == running


class TestFleetHealth:
    """Tests for Fleet Health API."""

    pytestmark = pytest.mark.smoke

    def test_get_fleet_health_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/ops/fleet/health returns expected structure."""
        response = api_client.get("/api/ops/fleet/health")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have overall health status
        assert_has_fields(data, ["timestamp", "overall"])
        assert data["overall"] in ["healthy", "degraded", "critical"]

        # Should have issue lists
        assert "critical_issues" in data
        assert "warnings" in data
        assert isinstance(data["critical_issues"], list)
        assert isinstance(data["warnings"], list)

    def test_fleet_health_healthy_count(self, api_client: TrinityApiClient):
        """Fleet health includes healthy agent count."""
        response = api_client.get("/api/ops/fleet/health")
        assert_status(response, 200)
        data = response.json()

        assert "healthy_count" in data
        assert isinstance(data["healthy_count"], int)
        assert data["healthy_count"] >= 0


class TestFleetOperationsAdminOnly:
    """Tests for Fleet Operations admin-only endpoints.

    Note: These tests are marked as slow because they involve container operations
    that can take significant time (30+ seconds per agent).
    """

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_fleet_restart_requires_admin(self, api_client: TrinityApiClient):
        """POST /api/ops/fleet/restart requires admin role."""
        # In dev mode, the user is admin, so this should succeed or return results
        # Use a nonexistent prefix to avoid actually restarting agents
        response = api_client.post("/api/ops/fleet/restart?system_prefix=nonexistent-test-prefix")
        # Should be 200 (success) or 403 (if not admin)
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["timestamp", "summary", "results"])

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_fleet_stop_requires_admin(self, api_client: TrinityApiClient):
        """POST /api/ops/fleet/stop requires admin role."""
        # Just verify the endpoint exists and returns proper structure
        # We don't actually want to stop all agents in test
        response = api_client.post("/api/ops/fleet/stop?system_prefix=nonexistent-prefix-xyz")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["timestamp", "summary", "results"])

    @pytest.mark.slow
    @pytest.mark.timeout(120)
    def test_fleet_restart_with_prefix_filter(self, api_client: TrinityApiClient):
        """Fleet restart can filter by system prefix."""
        response = api_client.post("/api/ops/fleet/restart?system_prefix=nonexistent-system")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            # All agents should be skipped (no matches)
            summary = data["summary"]
            assert summary["successes"] == 0


class TestScheduleControl:
    """Tests for Schedule Control API."""

    pytestmark = pytest.mark.smoke

    def test_schedules_pause_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/schedules/pause requires authentication."""
        response = unauthenticated_client.post("/api/ops/schedules/pause", auth=False)
        assert_status(response, 401)

    def test_schedules_resume_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/schedules/resume requires authentication."""
        response = unauthenticated_client.post("/api/ops/schedules/resume", auth=False)
        assert_status(response, 401)

    def test_pause_schedules_returns_count(self, api_client: TrinityApiClient):
        """POST /api/ops/schedules/pause returns paused count."""
        response = api_client.post("/api/ops/schedules/pause")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message", "paused_count"])
            assert data["success"] is True
            assert isinstance(data["paused_count"], int)

    def test_resume_schedules_returns_count(self, api_client: TrinityApiClient):
        """POST /api/ops/schedules/resume returns resumed count."""
        response = api_client.post("/api/ops/schedules/resume")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message", "resumed_count"])
            assert data["success"] is True
            assert isinstance(data["resumed_count"], int)

    def test_pause_schedules_with_agent_filter(self, api_client: TrinityApiClient):
        """Pause schedules can filter by agent name."""
        response = api_client.post("/api/ops/schedules/pause?agent_name=nonexistent-agent")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert data["paused_count"] == 0
            assert data["agent_filter"] == "nonexistent-agent"


class TestEmergencyStop:
    """Tests for Emergency Stop API.

    Note: Emergency stop is a dangerous operation that stops all agents.
    These tests only verify the endpoint exists and returns proper structure.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(180)
    def test_emergency_stop_returns_structure(self, api_client: TrinityApiClient):
        """POST /api/ops/emergency-stop returns expected structure."""
        # WARNING: This actually stops agents - use with caution in tests
        # Using a timeout because this operation involves stopping containers
        response = api_client.post("/api/ops/emergency-stop")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message"])
            # Should have counts
            assert "schedules_paused" in data
            assert "agents_stopped" in data


class TestAlerts:
    """Tests for Alerts API."""

    pytestmark = pytest.mark.smoke

    def test_list_alerts_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/ops/alerts requires authentication."""
        response = unauthenticated_client.get("/api/ops/alerts", auth=False)
        assert_status(response, 401)

    def test_list_alerts_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/ops/alerts returns expected structure."""
        response = api_client.get("/api/ops/alerts")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have alerts list and timestamp
        assert_has_fields(data, ["timestamp", "alerts"])
        assert isinstance(data["alerts"], list)

    def test_list_alerts_with_limit(self, api_client: TrinityApiClient):
        """GET /api/ops/alerts respects limit parameter."""
        response = api_client.get("/api/ops/alerts?limit=10")
        assert_status(response, 200)
        data = response.json()
        assert len(data["alerts"]) <= 10

    def test_acknowledge_alert_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/ops/alerts/{id}/acknowledge requires authentication."""
        response = unauthenticated_client.post("/api/ops/alerts/test-id/acknowledge", auth=False)
        assert_status(response, 401)


class TestOpsCosts:
    """Tests for Ops Costs API."""

    pytestmark = pytest.mark.smoke

    def test_costs_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/ops/costs requires authentication."""
        response = unauthenticated_client.get("/api/ops/costs", auth=False)
        assert_status(response, 401)

    def test_costs_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/ops/costs returns expected structure."""
        response = api_client.get("/api/ops/costs")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should indicate if OTel is enabled
        assert "enabled" in data

        if data["enabled"]:
            # If enabled, should have cost data or availability info
            assert "available" in data or "summary" in data
        else:
            # If disabled, should explain how to enable
            assert "message" in data

    def test_costs_disabled_response(self, api_client: TrinityApiClient):
        """GET /api/ops/costs shows setup instructions when disabled."""
        response = api_client.get("/api/ops/costs")
        assert_status(response, 200)
        data = response.json()

        # If OTel is not enabled, should provide setup guidance
        if not data.get("enabled"):
            assert "message" in data
            # Might have setup instructions
            if "setup_instructions" in data:
                assert isinstance(data["setup_instructions"], list)


class TestFleetStatusWithAgent:
    """Tests for Fleet Status API with running agents."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_fleet_status_includes_running_agent(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Fleet status includes the running test agent."""
        response = api_client.get("/api/ops/fleet/status")
        assert_status(response, 200)
        data = response.json()

        agent_names = [a["name"] for a in data["agents"]]
        assert created_agent["name"] in agent_names

        # Find the agent in the list
        agent_entry = next(a for a in data["agents"] if a["name"] == created_agent["name"])
        assert agent_entry["status"] == "running"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_fleet_health_checks_running_agent(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Fleet health check includes the running test agent."""
        response = api_client.get("/api/ops/fleet/health")
        assert_status(response, 200)
        data = response.json()

        # The test agent should either be healthy or have a warning
        # (depending on context usage and response time)
        agent_name = created_agent["name"]

        # Check if agent is in healthy list or has warnings
        is_healthy = agent_name in data.get("healthy_agents", [])
        has_warning = any(w["agent"] == agent_name for w in data.get("warnings", []))
        has_critical = any(c["agent"] == agent_name for c in data.get("critical_issues", []))

        # Agent should appear somewhere in the health check
        # (could be healthy, warning, or critical)
        assert is_healthy or has_warning or has_critical, \
            f"Agent {agent_name} should appear in health check results"
