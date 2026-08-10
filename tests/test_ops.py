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

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from testkit.cleanup import cleanup_test_agent


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


class TestSchedulesList:
    """Tests for GET /api/ops/schedules endpoint."""

    pytestmark = pytest.mark.smoke

    def test_schedules_list_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/ops/schedules requires authentication."""
        response = unauthenticated_client.get("/api/ops/schedules", auth=False)
        assert_status(response, 401)

    def test_schedules_list_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules returns expected structure."""
        response = api_client.get("/api/ops/schedules")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have timestamp, summary, and schedules
        assert_has_fields(data, ["timestamp", "summary", "schedules"])

        # Summary should have counts
        summary = data["summary"]
        assert_has_fields(summary, ["total", "enabled", "disabled", "agents_with_schedules"])
        assert isinstance(summary["total"], int)
        assert isinstance(summary["enabled"], int)
        assert isinstance(summary["disabled"], int)
        assert isinstance(summary["agents_with_schedules"], int)

        # Schedules should be a list
        assert isinstance(data["schedules"], list)

    def test_schedules_list_schedule_fields(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules returns schedules with expected fields."""
        response = api_client.get("/api/ops/schedules")
        assert_status(response, 200)
        data = response.json()

        schedules = data.get("schedules", [])
        for schedule in schedules:
            # Each schedule should have these fields
            assert_has_fields(schedule, [
                "id", "agent_name", "name", "cron_expression",
                "enabled", "timezone"
            ])
            assert isinstance(schedule["enabled"], bool)

    def test_schedules_list_with_agent_filter(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules filters by agent_name."""
        # First get all schedules to find an agent with schedules
        response = api_client.get("/api/ops/schedules")
        assert_status(response, 200)
        data = response.json()

        schedules = data.get("schedules", [])
        if schedules:
            # Filter by first agent's name
            agent_name = schedules[0]["agent_name"]
            response = api_client.get(f"/api/ops/schedules?agent_name={agent_name}")
            assert_status(response, 200)
            filtered_data = response.json()

            # All returned schedules should be for this agent
            for schedule in filtered_data["schedules"]:
                assert schedule["agent_name"] == agent_name

            # Summary should only count this agent's schedules
            assert filtered_data["summary"]["agents_with_schedules"] <= 1

    def test_schedules_list_nonexistent_agent_returns_empty(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules with nonexistent agent returns empty list."""
        response = api_client.get("/api/ops/schedules?agent_name=nonexistent-agent-xyz")
        assert_status(response, 200)
        data = response.json()

        assert data["summary"]["total"] == 0
        assert data["schedules"] == []

    def test_schedules_list_enabled_only_filter(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules filters by enabled_only."""
        response = api_client.get("/api/ops/schedules?enabled_only=true")
        assert_status(response, 200)
        data = response.json()

        # All returned schedules should be enabled
        for schedule in data["schedules"]:
            assert schedule["enabled"] is True

        # Summary disabled count should be 0 (filtered out)
        assert data["summary"]["disabled"] == 0

    def test_schedules_list_summary_counts_match(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules summary counts match schedules list."""
        response = api_client.get("/api/ops/schedules")
        assert_status(response, 200)
        data = response.json()

        summary = data["summary"]
        schedules = data["schedules"]

        # Total should match schedules count
        assert summary["total"] == len(schedules)

        # Enabled + disabled should equal total
        assert summary["enabled"] + summary["disabled"] == summary["total"]

        # Count enabled/disabled manually
        enabled_count = sum(1 for s in schedules if s["enabled"])
        disabled_count = sum(1 for s in schedules if not s["enabled"])
        assert summary["enabled"] == enabled_count
        assert summary["disabled"] == disabled_count

    def test_schedules_list_includes_execution_info(self, api_client: TrinityApiClient):
        """GET /api/ops/schedules includes last execution info when available."""
        response = api_client.get("/api/ops/schedules")
        assert_status(response, 200)
        data = response.json()

        schedules = data.get("schedules", [])
        for schedule in schedules:
            # These fields should always be present (can be null)
            assert "last_run_at" in schedule
            assert "next_run_at" in schedule
            assert "last_execution" in schedule

            # If last_execution exists, check its structure
            if schedule["last_execution"]:
                assert_has_fields(schedule["last_execution"], [
                    "id", "status", "started_at"
                ])


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
    These tests use a nonexistent system_prefix to verify endpoint structure
    without actually stopping any agents.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(60)
    def test_emergency_stop_returns_structure(self, api_client: TrinityApiClient):
        """POST /api/ops/emergency-stop returns expected structure."""
        # Use a nonexistent prefix so no agents are actually stopped
        # This tests the endpoint structure without side effects
        response = api_client.post("/api/ops/emergency-stop?system_prefix=nonexistent-test-prefix-xyz")
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert_has_fields(data, ["success", "message"])
            # Should have counts (will be 0 due to nonexistent prefix)
            assert "schedules_paused" in data
            assert "agents_stopped" in data
            # With nonexistent prefix, no agents should be stopped
            assert data["agents_stopped"] == 0


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


class TestContextStats:
    """Tests for GET /api/agents/context-stats endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_context_stats(self, api_client: TrinityApiClient):
        """GET /api/agents/context-stats returns context statistics."""
        response = api_client.get("/api/agents/context-stats")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should return dict with "agents" key
        assert isinstance(data, dict)
        assert "agents" in data
        assert isinstance(data["agents"], list)

    def test_context_stats_structure(self, api_client: TrinityApiClient, created_agent):
        """Context stats for an agent has expected structure."""
        response = api_client.get("/api/agents/context-stats")

        assert_status(response, 200)
        data = response.json()

        # Get agents list
        agents = data.get("agents", [])
        agent_name = created_agent["name"]

        # Find our agent in the list
        agent_stats = next((a for a in agents if a.get("name") == agent_name), None)
        if agent_stats:
            assert isinstance(agent_stats, dict)
            # Should have context-related fields
            assert "status" in agent_stats
            assert "activityState" in agent_stats

    def test_context_stats_entries_have_valid_structure(self, api_client: TrinityApiClient):
        """Context stats entries have expected fields."""
        response = api_client.get("/api/agents/context-stats")

        assert_status(response, 200)
        data = response.json()

        # Get agents list from response
        agents = data.get("agents", [])

        # All entries should be dicts with agent info
        for agent in agents:
            assert isinstance(agent, dict), "Each agent entry should be a dict"
            assert "name" in agent
            assert "status" in agent
            assert "activityState" in agent
            assert "contextPercent" in agent

    def test_context_stats_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/context-stats requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/context-stats",
            auth=False
        )
        assert_status(response, 401)

    def test_context_stats_returns_valid_response(self, api_client: TrinityApiClient):
        """Context stats returns valid response structure."""
        response = api_client.get("/api/agents/context-stats")

        assert_status(response, 200)
        data = response.json()

        # Should be dict with agents key
        assert isinstance(data, dict)
        assert "agents" in data
