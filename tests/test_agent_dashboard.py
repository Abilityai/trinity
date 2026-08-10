"""
Agent Dashboard Tests (test_agent_dashboard.py)

Tests for agent-defined dashboard API.
Covers DASH-001 (View agent-defined dashboards) and DASH-002 (Define widgets in dashboard.yaml).

Updated 2026-02-23: Added tests for Dynamic Dashboards (DASH-001):
- Query parameters: include_history, history_hours, include_platform_metrics
- History enrichment response structure
- Platform metrics section injection
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestDashboardEndpointAuthentication:
    """Tests for Dashboard API authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_dashboard_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agent-dashboard/{name} requires authentication."""
        response = unauthenticated_client.get("/api/agent-dashboard/test-agent", auth=False)
        assert_status(response, 401)


class TestDashboardEndpoint:
    """DASH-001: View agent-defined dashboards."""

    def test_dashboard_returns_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name} returns expected structure."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have required fields
        assert_has_fields(data, ["agent_name", "has_dashboard", "status"])
        assert data["agent_name"] == created_agent["name"]

    def test_agent_without_dashboard_returns_has_dashboard_false(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Agents without dashboard.yaml return has_dashboard: false."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Most test agents don't have a dashboard.yaml
        # Either has_dashboard is false, or config is present
        assert "has_dashboard" in data
        if not data["has_dashboard"]:
            # Config should be None or empty when no dashboard
            assert data.get("config") is None or data.get("config") == {}

    def test_dashboard_config_structure_when_present(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Dashboard config has correct structure when present."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            config = data["config"]
            # Config should have title and sections
            if "title" in config:
                assert isinstance(config["title"], str)
            if "sections" in config:
                assert isinstance(config["sections"], list)
                for section in config["sections"]:
                    if "widgets" in section:
                        assert isinstance(section["widgets"], list)

    def test_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agent-dashboard/{name} for nonexistent agent returns 404."""
        response = api_client.get("/api/agent-dashboard/nonexistent-agent-xyz")
        assert_status(response, 404)

    def test_dashboard_status_reflects_agent_state(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Dashboard status field reflects agent running state."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Status should be one of the valid states
        assert "status" in data
        assert data["status"] in ["running", "stopped", "error", "starting", "unknown"]


class TestDashboardWidgetTypes:
    """DASH-002: Dashboard widget type validation."""

    VALID_WIDGET_TYPES = [
        "metric", "status", "progress", "text", "markdown",
        "table", "list", "link", "image", "divider", "spacer"
    ]

    def test_dashboard_supports_metric_widget(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Dashboard API accepts metric widget type."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        # This test validates the API works - actual widget validation
        # happens when dashboards are defined in agents

    def test_dashboard_last_modified_timestamp(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Dashboard response includes last_modified timestamp when available."""
        response = api_client.get(f"/api/agent-dashboard/{created_agent['name']}")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # last_modified may be present
        if "last_modified" in data and data["last_modified"]:
            # Should be an ISO timestamp string
            assert isinstance(data["last_modified"], str)


class TestDashboardQueryParameters:
    """DASH-001: Dynamic Dashboard query parameter tests."""

    pytestmark = pytest.mark.smoke

    def test_include_history_param_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name}?include_history=true is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_history": True}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data

    def test_include_history_false_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name}?include_history=false is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_history": False}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data

    def test_history_hours_param_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name}?history_hours=48 is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"history_hours": 48}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data

    def test_history_hours_min_value(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """history_hours=1 (minimum) is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"history_hours": 1}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)

    def test_history_hours_max_value(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """history_hours=168 (maximum, 1 week) is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"history_hours": 168}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)

    def test_history_hours_below_min_rejected(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """history_hours=0 (below minimum) is rejected."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"history_hours": 0}
        )

        # Should return 422 validation error
        assert_status(response, 422)

    def test_history_hours_above_max_rejected(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """history_hours=200 (above maximum 168) is rejected."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"history_hours": 200}
        )

        # Should return 422 validation error
        assert_status(response, 422)

    def test_include_platform_metrics_true(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name}?include_platform_metrics=true is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_platform_metrics": True}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data

    def test_include_platform_metrics_false(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agent-dashboard/{name}?include_platform_metrics=false is accepted."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_platform_metrics": False}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data

    def test_all_params_combined(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """All query parameters can be used together."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={
                "include_history": True,
                "history_hours": 24,
                "include_platform_metrics": True
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "agent_name" in data
        assert data["agent_name"] == created_agent["name"]


class TestDashboardPlatformMetrics:
    """DASH-001: Platform metrics section tests."""

    def test_platform_metrics_section_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Platform metrics section has correct structure when present."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_platform_metrics": True}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Check if dashboard has config with sections
        if data.get("has_dashboard") and data.get("config"):
            config = data["config"]
            sections = config.get("sections", [])

            # Look for platform_managed section
            platform_sections = [s for s in sections if s.get("platform_managed")]

            for section in platform_sections:
                # Platform section should have expected fields
                assert "title" in section
                assert "widgets" in section
                assert isinstance(section["widgets"], list)
                assert section.get("platform_managed") is True

    def test_platform_metrics_widgets_have_source(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Platform metrics widgets have platform_source field."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_platform_metrics": True}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            sections = data["config"].get("sections", [])
            platform_sections = [s for s in sections if s.get("platform_managed")]

            for section in platform_sections:
                for widget in section.get("widgets", []):
                    # Platform widgets should have platform_source
                    assert "platform_source" in widget
                    assert widget["platform_source"].startswith(("executions.", "health."))

    def test_no_platform_section_when_disabled(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """No platform section when include_platform_metrics=false."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_platform_metrics": False}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            sections = data["config"].get("sections", [])

            # No platform_managed sections should exist
            platform_sections = [s for s in sections if s.get("platform_managed")]
            assert len(platform_sections) == 0, "Platform section should not be present when disabled"


class TestDashboardHistoryEnrichment:
    """DASH-001: History enrichment response structure tests."""

    def test_widget_history_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Widget history field has correct structure when present."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_history": True, "history_hours": 24}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            sections = data["config"].get("sections", [])

            for section in sections:
                for widget in section.get("widgets", []):
                    # If history is present, validate structure
                    if "history" in widget:
                        history = widget["history"]
                        assert "values" in history
                        assert "trend" in history
                        assert isinstance(history["values"], list)
                        assert history["trend"] in ["up", "down", "stable"]

                        # Optional fields
                        if "trend_percent" in history:
                            assert isinstance(history["trend_percent"], (int, float))
                        if "min" in history:
                            assert isinstance(history["min"], (int, float, type(None)))
                        if "max" in history:
                            assert isinstance(history["max"], (int, float, type(None)))
                        if "avg" in history:
                            assert isinstance(history["avg"], (int, float, type(None)))

    def test_history_values_format(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """History values are in {t, v} format."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_history": True}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            sections = data["config"].get("sections", [])

            for section in sections:
                for widget in section.get("widgets", []):
                    if "history" in widget and widget["history"].get("values"):
                        for value in widget["history"]["values"]:
                            # Each value should have t (timestamp) and v (value)
                            assert "t" in value, "History value should have 't' field"
                            assert "v" in value, "History value should have 'v' field"

    def test_no_history_when_disabled(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """No history field when include_history=false."""
        response = api_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            params={"include_history": False}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        if data.get("has_dashboard") and data.get("config"):
            sections = data["config"].get("sections", [])

            for section in sections:
                # Skip platform sections which may have internal tracking
                if section.get("platform_managed"):
                    continue

                for widget in section.get("widgets", []):
                    # Agent-defined widgets should not have history when disabled
                    # (Platform widgets may still have platform_source but no history)
                    if not widget.get("platform_source"):
                        assert "history" not in widget or widget.get("history") is None


class TestDashboardAccessControl:
    """DASH-001: Access control tests for dashboard endpoint."""

    pytestmark = pytest.mark.smoke

    def test_unauthenticated_access_denied(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """Unauthenticated user cannot access dashboard."""
        response = unauthenticated_client.get(
            f"/api/agent-dashboard/{created_agent['name']}",
            auth=False
        )
        assert_status(response, 401)

    def test_nonexistent_agent_returns_404_not_403(
        self,
        api_client: TrinityApiClient
    ):
        """Nonexistent agent returns 404, not 403 (no information leak)."""
        response = api_client.get("/api/agent-dashboard/definitely-not-real-agent-xyz123")
        # Should be 404 (not found), not 403 (forbidden)
        assert_status(response, 404)
