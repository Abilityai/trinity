"""
Agent Monitoring Tests (test_monitoring.py)

Tests for Trinity Agent Monitoring Service endpoints (MON-001).
Covers health status, monitoring configuration, alerts, and history.

Feature: Agent Health Monitoring
Router: /api/monitoring/*
"""

import pytest
import time

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# ============================================================================
# Authentication Tests
# ============================================================================

class TestMonitoringAuthentication:
    """Tests for monitoring endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_fleet_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/monitoring/status requires authentication."""
        response = unauthenticated_client.get("/api/monitoring/status", auth=False)
        assert_status(response, 401)

    def test_agent_health_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/monitoring/agents/{name} requires authentication."""
        response = unauthenticated_client.get("/api/monitoring/agents/test-agent", auth=False)
        assert_status(response, 401)

    def test_health_history_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/monitoring/agents/{name}/history requires authentication."""
        response = unauthenticated_client.get(
            "/api/monitoring/agents/test-agent/history",
            auth=False
        )
        assert_status(response, 401)

    def test_trigger_check_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/monitoring/agents/{name}/check requires authentication."""
        response = unauthenticated_client.post(
            "/api/monitoring/agents/test-agent/check",
            auth=False
        )
        assert_status(response, 401)

    def test_get_alerts_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/monitoring/alerts requires authentication."""
        response = unauthenticated_client.get("/api/monitoring/alerts", auth=False)
        assert_status(response, 401)

    def test_get_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/monitoring/config requires authentication."""
        response = unauthenticated_client.get("/api/monitoring/config", auth=False)
        assert_status(response, 401)

    def test_update_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/monitoring/config requires authentication."""
        response = unauthenticated_client.put(
            "/api/monitoring/config",
            json={"enabled": True},
            auth=False
        )
        assert_status(response, 401)

    def test_enable_monitoring_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/monitoring/enable requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/enable", auth=False)
        assert_status(response, 401)

    def test_disable_monitoring_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/monitoring/disable requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/disable", auth=False)
        assert_status(response, 401)

    def test_check_all_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/monitoring/check-all requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/check-all", auth=False)
        assert_status(response, 401)

    def test_cleanup_history_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/monitoring/history requires authentication."""
        response = unauthenticated_client.delete("/api/monitoring/history", auth=False)
        assert_status(response, 401)


# ============================================================================
# Fleet Status Tests
# ============================================================================

class TestFleetStatus:
    """Tests for GET /api/monitoring/status endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_fleet_status_success(self, api_client: TrinityApiClient):
        """Get fleet status returns proper structure."""
        response = api_client.get("/api/monitoring/status")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Check top-level fields
        assert_has_fields(data, ["enabled", "summary", "agents"])

        # Check summary structure
        summary = data["summary"]
        assert_has_fields(summary, [
            "total_agents", "healthy", "degraded",
            "unhealthy", "critical", "unknown"
        ])
        assert isinstance(summary["total_agents"], int)
        assert summary["total_agents"] >= 0

    def test_fleet_status_agents_structure(self, api_client: TrinityApiClient):
        """Fleet status agents array has proper structure."""
        response = api_client.get("/api/monitoring/status")
        assert_status(response, 200)
        data = response.json()

        agents = data["agents"]
        assert isinstance(agents, list)

        if len(agents) > 0:
            agent = agents[0]
            assert_has_fields(agent, ["name", "status", "issues"])
            assert agent["status"] in [
                "healthy", "degraded", "unhealthy", "critical", "unknown"
            ]
            assert isinstance(agent["issues"], list)

    def test_fleet_status_empty_when_no_agents(self, api_client: TrinityApiClient):
        """Fleet status handles case with no accessible agents."""
        # This test passes as long as it returns valid structure
        response = api_client.get("/api/monitoring/status")
        assert_status(response, 200)
        data = response.json()
        assert "summary" in data
        assert "agents" in data


# ============================================================================
# Agent Health Tests
# ============================================================================

class TestAgentHealth:
    """Tests for GET /api/monitoring/agents/{name} endpoint."""

    def test_get_agent_health_nonexistent_returns_404(self, api_client: TrinityApiClient):
        """Getting health for nonexistent agent returns 404."""
        response = api_client.get("/api/monitoring/agents/nonexistent-agent-xyz")
        assert_status(response, 404)

    def test_get_agent_health_success(self, api_client: TrinityApiClient, created_agent: dict):
        """Get agent health returns detailed health info."""
        agent_name = created_agent["name"]

        # Give the agent time to have health data
        time.sleep(2)

        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Check required fields
        assert_has_fields(data, [
            "agent_name", "aggregate_status", "issues"
        ])
        assert data["agent_name"] == agent_name
        assert data["aggregate_status"] in [
            "healthy", "degraded", "unhealthy", "critical", "unknown"
        ]
        assert isinstance(data["issues"], list)

    def test_agent_health_has_layer_checks(self, api_client: TrinityApiClient, created_agent: dict):
        """Agent health includes docker/network/business layer checks."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        data = response.json()

        # Check that at least some layer data exists
        # Note: Layers may be None if checks haven't run yet
        assert "docker" in data
        assert "network" in data
        assert "business" in data

    def test_agent_health_docker_layer_structure(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Docker health check has proper structure when present."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        data = response.json()

        if data.get("docker"):
            docker = data["docker"]
            assert_has_fields(docker, ["agent_name", "checked_at"])
            assert docker["agent_name"] == agent_name

    def test_agent_health_network_layer_structure(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Network health check has proper structure when present."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        data = response.json()

        if data.get("network"):
            network = data["network"]
            assert_has_fields(network, ["agent_name", "reachable", "checked_at"])
            assert network["agent_name"] == agent_name
            assert isinstance(network["reachable"], bool)

    def test_agent_health_business_layer_structure(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Business health check has proper structure when present."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        data = response.json()

        if data.get("business"):
            business = data["business"]
            assert_has_fields(business, ["agent_name", "status", "checked_at"])
            assert business["agent_name"] == agent_name


# ============================================================================
# Health History Tests
# ============================================================================

class TestHealthHistory:
    """Tests for GET /api/monitoring/agents/{name}/history endpoint."""

    def test_get_health_history_success(self, api_client: TrinityApiClient, created_agent: dict):
        """Get health history returns proper structure."""
        agent_name = created_agent["name"]

        response = api_client.get(f"/api/monitoring/agents/{agent_name}/history")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert_has_fields(data, ["agent_name", "check_type", "hours", "count", "checks"])
        assert data["agent_name"] == agent_name
        assert data["check_type"] == "aggregate"  # Default
        assert data["hours"] == 24  # Default
        assert isinstance(data["checks"], list)

    def test_health_history_with_check_type(self, api_client: TrinityApiClient, created_agent: dict):
        """Get health history with specific check type."""
        agent_name = created_agent["name"]

        response = api_client.get(
            f"/api/monitoring/agents/{agent_name}/history",
            params={"check_type": "docker"}
        )
        assert_status(response, 200)
        data = response.json()
        assert data["check_type"] == "docker"

    def test_health_history_with_hours_limit(self, api_client: TrinityApiClient, created_agent: dict):
        """Get health history with custom hours limit."""
        agent_name = created_agent["name"]

        response = api_client.get(
            f"/api/monitoring/agents/{agent_name}/history",
            params={"hours": 12}
        )
        assert_status(response, 200)
        data = response.json()
        assert data["hours"] == 12

    def test_health_history_with_limit(self, api_client: TrinityApiClient, created_agent: dict):
        """Get health history with result limit."""
        agent_name = created_agent["name"]

        response = api_client.get(
            f"/api/monitoring/agents/{agent_name}/history",
            params={"limit": 10}
        )
        assert_status(response, 200)
        data = response.json()
        assert len(data["checks"]) <= 10

    def test_health_history_invalid_check_type(self, api_client: TrinityApiClient, created_agent: dict):
        """Invalid check type returns 422."""
        agent_name = created_agent["name"]

        response = api_client.get(
            f"/api/monitoring/agents/{agent_name}/history",
            params={"check_type": "invalid"}
        )
        assert_status(response, 422)

    def test_health_history_nonexistent_agent(self, api_client: TrinityApiClient):
        """Get history for nonexistent agent returns 404."""
        response = api_client.get("/api/monitoring/agents/nonexistent-agent/history")
        assert_status(response, 404)


# ============================================================================
# Health Check Trigger Tests (Admin Only)
# ============================================================================

class TestTriggerHealthCheck:
    """Tests for POST /api/monitoring/agents/{name}/check endpoint."""

    pytestmark = pytest.mark.slow

    def test_trigger_health_check_success(self, api_client: TrinityApiClient, created_agent: dict):
        """Triggering health check returns updated health status."""
        agent_name = created_agent["name"]

        response = api_client.post(f"/api/monitoring/agents/{agent_name}/check")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should return same structure as get agent health
        assert_has_fields(data, [
            "agent_name", "aggregate_status", "issues"
        ])
        assert data["agent_name"] == agent_name

    def test_trigger_health_check_nonexistent_agent(self, api_client: TrinityApiClient):
        """Triggering check for nonexistent agent returns 404."""
        response = api_client.post("/api/monitoring/agents/nonexistent-agent/check")
        assert_status(response, 404)


# ============================================================================
# Alerts Tests (Admin Only)
# ============================================================================

class TestMonitoringAlerts:
    """Tests for GET /api/monitoring/alerts endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_alerts_success(self, api_client: TrinityApiClient):
        """Get active alerts returns proper structure."""
        response = api_client.get("/api/monitoring/alerts")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert_has_fields(data, ["count", "alerts"])
        assert isinstance(data["alerts"], list)
        assert data["count"] == len(data["alerts"])

    def test_get_alerts_with_status_filter(self, api_client: TrinityApiClient):
        """Get alerts with status filter."""
        response = api_client.get("/api/monitoring/alerts", params={"status": "pending"})
        assert_status(response, 200)

    def test_get_alerts_all_status(self, api_client: TrinityApiClient):
        """Get all alerts regardless of status."""
        response = api_client.get("/api/monitoring/alerts", params={"status": "all"})
        assert_status(response, 200)

    def test_get_alerts_with_limit(self, api_client: TrinityApiClient):
        """Get alerts with limit."""
        response = api_client.get("/api/monitoring/alerts", params={"limit": 10})
        assert_status(response, 200)
        data = response.json()
        assert len(data["alerts"]) <= 10


# ============================================================================
# Monitoring Configuration Tests (Admin Only)
# ============================================================================

class TestMonitoringConfig:
    """Tests for monitoring configuration endpoints."""

    pytestmark = pytest.mark.smoke

    def test_get_config_success(self, api_client: TrinityApiClient):
        """Get monitoring config returns proper structure."""
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Check required config fields
        assert_has_fields(data, [
            "enabled",
            "docker_check_interval",
            "network_check_interval",
            "business_check_interval",
            "http_timeout",
            "cpu_warning_percent",
            "memory_warning_percent"
        ])
        assert isinstance(data["enabled"], bool)
        assert isinstance(data["docker_check_interval"], int)

    def test_update_config_success(self, api_client: TrinityApiClient):
        """Update monitoring config persists changes."""
        # Get current config
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        original_config = response.json()

        # Update config
        updated_config = original_config.copy()
        updated_config["docker_check_interval"] = 45

        response = api_client.put("/api/monitoring/config", json=updated_config)
        assert_status(response, 200)
        data = response.json()
        assert data["docker_check_interval"] == 45

        # Verify persistence
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        data = response.json()
        assert data["docker_check_interval"] == 45

        # Restore original config
        api_client.put("/api/monitoring/config", json=original_config)

    def test_update_config_partial(self, api_client: TrinityApiClient):
        """Update config with partial data."""
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        original_config = response.json()

        # Update only specific fields
        updated_config = original_config.copy()
        updated_config["network_check_interval"] = 35

        response = api_client.put("/api/monitoring/config", json=updated_config)
        assert_status(response, 200)

        # Restore
        api_client.put("/api/monitoring/config", json=original_config)

    def test_update_config_validation(self, api_client: TrinityApiClient):
        """Non-positive check intervals are rejected (#1121)."""
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        config = response.json()

        # #1121: a non-positive interval makes asyncio.sleep return
        # immediately, turning the loop into a tight health-check flood.
        # The MonitoringConfig validator now rejects it → 422.
        for field in ("docker_check_interval", "network_check_interval", "business_check_interval"):
            for bad_value in (-10, 0):
                bad = config.copy()
                bad[field] = bad_value
                response = api_client.put("/api/monitoring/config", json=bad)
                assert response.status_code == 422, f"{field}={bad_value} should be rejected"


# ============================================================================
# Service Control Tests (Admin Only)
# ============================================================================

class TestMonitoringServiceControl:
    """Tests for monitoring service enable/disable endpoints."""

    pytestmark = pytest.mark.smoke

    def test_enable_monitoring_success(self, api_client: TrinityApiClient):
        """Enable monitoring service returns success."""
        response = api_client.post("/api/monitoring/enable")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert "status" in data
        assert data["status"] in ["starting", "already_running"]

    def test_enable_monitoring_idempotent(self, api_client: TrinityApiClient):
        """Enabling already-running service is idempotent."""
        # Enable twice
        api_client.post("/api/monitoring/enable")
        response = api_client.post("/api/monitoring/enable")

        assert_status(response, 200)
        data = response.json()
        # Should indicate already running
        assert data["status"] == "already_running"

    def test_disable_monitoring_success(self, api_client: TrinityApiClient):
        """Disable monitoring service returns success."""
        # Ensure it's enabled first
        api_client.post("/api/monitoring/enable")
        time.sleep(1)

        response = api_client.post("/api/monitoring/disable")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert "status" in data
        assert data["status"] in ["stopping", "already_stopped"]

        # Re-enable for other tests
        api_client.post("/api/monitoring/enable")

    def test_disable_monitoring_idempotent(self, api_client: TrinityApiClient):
        """Disabling already-stopped service is idempotent."""
        # Disable twice
        api_client.post("/api/monitoring/disable")
        time.sleep(1)
        response = api_client.post("/api/monitoring/disable")

        assert_status(response, 200)
        data = response.json()
        assert data["status"] == "already_stopped"

        # Re-enable for other tests
        api_client.post("/api/monitoring/enable")


# ============================================================================
# Batch Operations Tests (Admin Only)
# ============================================================================

class TestBatchOperations:
    """Tests for batch monitoring operations."""

    pytestmark = pytest.mark.slow

    def test_check_all_agents_success(self, api_client: TrinityApiClient):
        """Trigger fleet-wide health check."""
        response = api_client.post("/api/monitoring/check-all")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert "status" in data
        assert data["status"] in ["started", "no_agents"]

        if data["status"] == "started":
            assert "agents" in data
            assert isinstance(data["agents"], list)

    def test_cleanup_health_history_success(self, api_client: TrinityApiClient):
        """Cleanup old health records."""
        response = api_client.delete("/api/monitoring/history", params={"days": 7})
        assert_status(response, 200)
        data = assert_json_response(response)

        assert_has_fields(data, ["status", "deleted_records", "retention_days"])
        assert data["status"] == "success"
        assert isinstance(data["deleted_records"], int)
        assert data["retention_days"] == 7

    def test_cleanup_history_with_custom_days(self, api_client: TrinityApiClient):
        """Cleanup with custom retention period."""
        response = api_client.delete("/api/monitoring/history", params={"days": 30})
        assert_status(response, 200)
        data = response.json()
        assert data["retention_days"] == 30

    def test_cleanup_history_validation(self, api_client: TrinityApiClient):
        """Cleanup validates retention days range."""
        # Try with too many days
        response = api_client.delete("/api/monitoring/history", params={"days": 365})
        assert_status(response, 422)

        # Try with zero or negative days
        response = api_client.delete("/api/monitoring/history", params={"days": 0})
        assert_status(response, 422)


# ============================================================================
# Permission Tests
# ============================================================================

class TestMonitoringPermissions:
    """Tests for monitoring endpoint permissions."""

    # Note: These tests would need a non-admin user fixture to fully test.
    # For now, we verify admin access works correctly.

    pytestmark = pytest.mark.smoke

    def test_admin_can_access_fleet_status(self, api_client: TrinityApiClient):
        """Admin user can access fleet status."""
        response = api_client.get("/api/monitoring/status")
        assert_status(response, 200)

    def test_admin_can_update_config(self, api_client: TrinityApiClient):
        """Admin user can update monitoring config."""
        response = api_client.get("/api/monitoring/config")
        assert_status(response, 200)
        config = response.json()

        response = api_client.put("/api/monitoring/config", json=config)
        assert_status(response, 200)

    def test_admin_can_control_service(self, api_client: TrinityApiClient):
        """Admin user can enable/disable monitoring."""
        response = api_client.post("/api/monitoring/enable")
        assert_status(response, 200)


# ============================================================================
# Integration Tests
# ============================================================================

class TestMonitoringIntegration:
    """Integration tests for monitoring workflows."""

    pytestmark = pytest.mark.slow

    def test_full_monitoring_workflow(self, api_client: TrinityApiClient, created_agent: dict):
        """Complete monitoring workflow from check to history."""
        agent_name = created_agent["name"]

        # 1. Trigger health check
        response = api_client.post(f"/api/monitoring/agents/{agent_name}/check")
        assert_status(response, 200)
        check_data = response.json()
        initial_status = check_data["aggregate_status"]

        # 2. Get agent health detail
        response = api_client.get(f"/api/monitoring/agents/{agent_name}")
        assert_status(response, 200)
        detail_data = response.json()
        assert detail_data["aggregate_status"] == initial_status

        # 3. Check appears in history
        time.sleep(1)
        response = api_client.get(f"/api/monitoring/agents/{agent_name}/history")
        assert_status(response, 200)
        history_data = response.json()
        assert history_data["count"] > 0

        # 4. Agent appears in fleet status
        response = api_client.get("/api/monitoring/status")
        assert_status(response, 200)
        fleet_data = response.json()
        agent_names = [a["name"] for a in fleet_data["agents"]]
        assert agent_name in agent_names

    def test_config_persistence_across_requests(self, api_client: TrinityApiClient):
        """Configuration changes persist across requests."""
        # Get original config
        response = api_client.get("/api/monitoring/config")
        original_config = response.json()

        # Update config
        new_config = original_config.copy()
        new_config["docker_check_interval"] = 60

        response = api_client.put("/api/monitoring/config", json=new_config)
        assert_status(response, 200)

        # Verify persistence in multiple requests
        for _ in range(3):
            response = api_client.get("/api/monitoring/config")
            assert_status(response, 200)
            data = response.json()
            assert data["docker_check_interval"] == 60

        # Restore original
        api_client.put("/api/monitoring/config", json=original_config)
