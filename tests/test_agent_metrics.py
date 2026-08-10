"""
Agent Custom Metrics Tests (test_agent_metrics.py)

Tests for Trinity agent custom metrics endpoint.
Covers REQ-METRICS-001 (9.9 Agent Custom Metrics).

Feature Flow: agent-custom-metrics.md

These tests verify the metrics proxy endpoint functionality.
Note: Actual metrics availability depends on template configuration.
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestAgentMetricsAuthentication:
    """Tests for agent metrics authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_metrics_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/metrics requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/metrics", auth=False)
        assert_status(response, 401)


class TestAgentMetricsEndpoint:
    """Tests for GET /api/agents/{name}/metrics endpoint."""

    def test_metrics_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """GET /api/agents/{name}/metrics returns expected structure."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        # Endpoint should return 200 with structure (even if no metrics defined)
        assert_status(response, 200)
        data = assert_json_response(response)

        # Must have these fields
        assert_has_fields(data, ["agent_name", "status", "has_metrics"])
        assert data["agent_name"] == agent_name

    def test_metrics_status_field(self, api_client: TrinityApiClient, created_agent: dict):
        """Metrics response includes agent status."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        assert data["status"] in ["running", "stopped", "error"]

    def test_metrics_has_metrics_boolean(self, api_client: TrinityApiClient, created_agent: dict):
        """has_metrics field is a boolean."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        assert isinstance(data["has_metrics"], bool)

    def test_metrics_with_definitions(self, api_client: TrinityApiClient, created_agent: dict):
        """If has_metrics=true, definitions array is present."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_metrics"):
            assert "definitions" in data
            assert isinstance(data["definitions"], list)
            # Each definition should have name, type, label
            for defn in data["definitions"]:
                assert_has_fields(defn, ["name", "type", "label"])

    def test_metrics_with_values(self, api_client: TrinityApiClient, created_agent: dict):
        """If has_metrics=true, values dict is present."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_metrics"):
            assert "values" in data
            assert isinstance(data["values"], dict)

    def test_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/metrics returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/metrics")
        assert_status(response, 404)


class TestAgentMetricsStopped:
    """Tests for metrics endpoint on stopped agents."""

    def test_stopped_agent_metrics(self, api_client: TrinityApiClient, stopped_agent: dict):
        """Stopped agent returns appropriate metrics response."""
        agent_name = stopped_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        # Should return 200 with status indicating stopped
        assert_status(response, 200)
        data = response.json()

        assert data["agent_name"] == agent_name
        assert data["status"] == "stopped"
        # Stopped agents typically have has_metrics=false
        assert isinstance(data["has_metrics"], bool)


class TestMetricTypes:
    """Tests for different metric types (if metrics are available)."""

    def test_metric_type_validation(self, api_client: TrinityApiClient, created_agent: dict):
        """Metric definitions have valid types."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        valid_types = ["counter", "gauge", "percentage", "status", "duration", "bytes"]

        if data.get("has_metrics") and data.get("definitions"):
            for defn in data["definitions"]:
                assert defn["type"] in valid_types, \
                    f"Metric type '{defn['type']}' should be one of {valid_types}"

    def test_percentage_metric_value_range(self, api_client: TrinityApiClient, created_agent: dict):
        """Percentage metric values should be 0-100."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_metrics") and data.get("definitions") and data.get("values"):
            for defn in data["definitions"]:
                if defn["type"] == "percentage":
                    metric_name = defn["name"]
                    if metric_name in data["values"]:
                        value = data["values"][metric_name]
                        if isinstance(value, (int, float)):
                            assert 0 <= value <= 100, \
                                f"Percentage metric '{metric_name}' should be 0-100, got {value}"


class TestMetricsLastUpdated:
    """Tests for metrics last_updated field."""

    def test_has_last_updated_when_metrics_present(self, api_client: TrinityApiClient, created_agent: dict):
        """Metrics response includes last_updated when metrics exist."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_metrics") and data.get("values"):
            # last_updated may be at top level or in values
            has_timestamp = (
                "last_updated" in data or
                "last_updated" in data.get("values", {})
            )
            # This is optional but good to have
            # Just verify the field structure is valid if present
            if has_timestamp:
                timestamp = data.get("last_updated") or data.get("values", {}).get("last_updated")
                assert isinstance(timestamp, str) or timestamp is None


class TestMetricsAccessControl:
    """Tests for metrics endpoint access control."""

    def test_shared_agent_metrics_accessible(self, api_client: TrinityApiClient, created_agent: dict):
        """Shared agents should have accessible metrics."""
        agent_name = created_agent["name"]

        # First share the agent (if we have another user context, we'd test that)
        # For now, just verify owner can access
        response = api_client.get(f"/api/agents/{agent_name}/metrics")
        assert_status(response, 200)

    def test_unowned_unshared_agent_denied(self, api_client: TrinityApiClient):
        """Access to unowned, unshared agent metrics should be denied."""
        # This would require a second user context to properly test
        # For now, just verify 404 for nonexistent agents
        response = api_client.get("/api/agents/other-users-private-agent/metrics")
        assert_status_in(response, [403, 404])
