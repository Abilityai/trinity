"""
Observability Tests (test_observability.py)

Tests for Trinity observability endpoints including:
- OpenTelemetry status checking
- Metrics retrieval and parsing
- Cost and usage data

Covers REQ-OTEL-001 (OpenTelemetry Integration).

Note: Full OTel functionality requires the OTel collector to be running.
These tests verify API contract and graceful handling when OTel is not available.
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestObservabilityAuthentication:
    """Tests for Observability API authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_observability_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/observability/status requires authentication."""
        response = unauthenticated_client.get("/api/observability/status", auth=False)
        assert_status(response, 401)

    def test_observability_metrics_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/observability/metrics requires authentication."""
        response = unauthenticated_client.get("/api/observability/metrics", auth=False)
        assert_status(response, 401)


class TestObservabilityStatus:
    """Tests for Observability Status API."""

    pytestmark = pytest.mark.smoke

    def test_get_observability_status_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/observability/status returns expected structure."""
        response = api_client.get("/api/observability/status")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should always have enabled flag
        assert "enabled" in data
        assert isinstance(data["enabled"], bool)

    def test_observability_status_when_disabled(self, api_client: TrinityApiClient):
        """Observability status shows collector info when disabled."""
        response = api_client.get("/api/observability/status")
        assert_status(response, 200)
        data = response.json()

        if not data.get("enabled"):
            # When disabled, should indicate configuration status
            assert "collector_configured" in data
            assert data["collector_configured"] is False

    def test_observability_status_when_enabled(self, api_client: TrinityApiClient):
        """Observability status shows reachability when enabled."""
        response = api_client.get("/api/observability/status")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled"):
            # When enabled, should show collector reachability
            assert "collector_configured" in data
            assert "collector_reachable" in data
            # Endpoint should be shown if configured
            if data.get("collector_configured"):
                assert "endpoint" in data


class TestObservabilityMetrics:
    """Tests for Observability Metrics API."""

    pytestmark = pytest.mark.smoke

    def test_get_observability_metrics_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/observability/metrics returns expected structure."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = assert_json_response(response)

        # Should always have enabled flag
        assert "enabled" in data
        assert isinstance(data["enabled"], bool)

    def test_observability_metrics_when_disabled(self, api_client: TrinityApiClient):
        """Observability metrics explains how to enable when disabled."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if not data.get("enabled"):
            # Should have a helpful message
            assert "message" in data
            assert "OTEL_ENABLED" in data["message"] or "OpenTelemetry" in data["message"]

    def test_observability_metrics_structure_when_available(self, api_client: TrinityApiClient):
        """Observability metrics has correct structure when available."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled") and data.get("available"):
            # Should have metrics and totals
            assert "metrics" in data
            assert "totals" in data

            # Check metrics structure
            metrics = data["metrics"]
            expected_metric_keys = [
                "cost_by_model",
                "tokens_by_model",
                "lines_of_code",
                "sessions",
                "commits",
                "pull_requests"
            ]
            for key in expected_metric_keys:
                assert key in metrics, f"Missing metric key: {key}"

            # Check totals structure
            totals = data["totals"]
            assert "total_cost" in totals
            assert "total_tokens" in totals

    def test_observability_metrics_graceful_failure(self, api_client: TrinityApiClient):
        """Observability metrics handles collector unavailability gracefully."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled") and not data.get("available"):
            # Should have error message
            assert "error" in data
            # Should still return metrics as None
            assert data.get("metrics") is None
            assert data.get("totals") is None


class TestObservabilityMetricsData:
    """Tests for Observability Metrics data types and values."""

    pytestmark = pytest.mark.smoke

    def test_cost_by_model_is_dict(self, api_client: TrinityApiClient):
        """Cost by model is a dictionary."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled") and data.get("available"):
            metrics = data["metrics"]
            assert isinstance(metrics["cost_by_model"], dict)
            # Each value should be a number
            for model, cost in metrics["cost_by_model"].items():
                assert isinstance(cost, (int, float)), f"Cost for {model} should be numeric"

    def test_tokens_by_model_structure(self, api_client: TrinityApiClient):
        """Tokens by model has correct nested structure."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled") and data.get("available"):
            metrics = data["metrics"]
            tokens_by_model = metrics["tokens_by_model"]
            assert isinstance(tokens_by_model, dict)

            # Each model should have token type breakdown
            for model, token_types in tokens_by_model.items():
                assert isinstance(token_types, dict), f"Token types for {model} should be dict"
                # Token type values should be numeric
                for token_type, count in token_types.items():
                    assert isinstance(count, (int, float)), \
                        f"Token count for {model}/{token_type} should be numeric"

    def test_totals_are_numeric(self, api_client: TrinityApiClient):
        """Total values are all numeric."""
        response = api_client.get("/api/observability/metrics")
        assert_status(response, 200)
        data = response.json()

        if data.get("enabled") and data.get("available"):
            totals = data["totals"]
            numeric_fields = ["total_cost", "total_tokens", "total_lines", "sessions", "commits", "pull_requests"]

            for field in numeric_fields:
                if field in totals:
                    assert isinstance(totals[field], (int, float)), f"{field} should be numeric"
                    assert totals[field] >= 0, f"{field} should be non-negative"


class TestObservabilityIntegration:
    """Integration tests for Observability with OTel collector."""

    @pytest.mark.slow
    def test_metrics_and_status_consistency(self, api_client: TrinityApiClient):
        """Metrics and status endpoints are consistent."""
        # Get status
        status_response = api_client.get("/api/observability/status")
        assert_status(status_response, 200)
        status_data = status_response.json()

        # Get metrics
        metrics_response = api_client.get("/api/observability/metrics")
        assert_status(metrics_response, 200)
        metrics_data = metrics_response.json()

        # Both should agree on whether OTel is enabled
        assert status_data["enabled"] == metrics_data["enabled"], \
            "Status and metrics should agree on OTel enabled state"

        # If enabled and reachable, metrics should be available
        if status_data.get("enabled") and status_data.get("collector_reachable"):
            assert metrics_data.get("available") is True, \
                "Metrics should be available when collector is reachable"


# Note: Unit tests for parse_prometheus_metrics and calculate_totals
# are not included here because they require importing from the backend
# module, which is not accessible from the test directory.
# These functions should be tested via integration tests or within
# the backend's own test suite.
