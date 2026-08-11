"""
Cleanup Service Tests (test_cleanup_service.py)

Tests for cleanup service behavior including:
- Issue #106: No-session execution fast-fail
- Issue #106: Orphaned skipped execution finalization
- Issue #219: Slot-execution correlation (stale_slot_executions)
- Cleanup report structure validation

Feature Flow: docs/memory/feature-flows/cleanup-service.md
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


class TestCleanupStatus:
    """Tests for cleanup status endpoint."""

    pytestmark = pytest.mark.smoke

    def test_cleanup_status_returns_200(self, api_client: TrinityApiClient):
        """GET /api/monitoring/cleanup-status returns 200."""
        response = api_client.get("/api/monitoring/cleanup-status")
        assert_status(response, 200)

    def test_cleanup_status_structure(self, api_client: TrinityApiClient):
        """Cleanup status includes expected fields."""
        response = api_client.get("/api/monitoring/cleanup-status")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["running", "last_run_at"])

    def test_cleanup_status_report_fields(self, api_client: TrinityApiClient):
        """Cleanup report includes Issue #106 and #219 fields."""
        response = api_client.get("/api/monitoring/cleanup-status")
        assert_status(response, 200)
        data = response.json()

        # last_report may be None if cleanup hasn't run yet
        if data.get("last_report"):
            report = data["last_report"]
            # Verify Issue #106 fields exist
            assert "no_session_executions" in report, "Missing no_session_executions field"
            assert "orphaned_skipped" in report, "Missing orphaned_skipped field"
            # Verify Issue #219 field exists
            assert "stale_slot_executions" in report, "Missing stale_slot_executions field"
            # Issue #772: retention sweeps
            assert "execution_logs_pruned" in report, "Missing execution_logs_pruned field (#772)"
            assert "execution_rows_pruned" in report, "Missing execution_rows_pruned field (#772)"
            assert "health_checks_pruned" in report, "Missing health_checks_pruned field (#772)"
            # Verify existing fields still present
            assert "stale_executions" in report
            assert "stale_activities" in report
            assert "stale_slots" in report
            assert "total" in report
            # Total = sum of every int counter except `total` itself
            expected_total = sum(
                v for k, v in report.items()
                if k != "total" and isinstance(v, int)
            )
            assert report["total"] == expected_total


class TestCleanupTrigger:
    """Tests for manual cleanup trigger."""

    pytestmark = pytest.mark.smoke

    def test_trigger_cleanup_returns_200(self, api_client: TrinityApiClient):
        """POST /api/monitoring/cleanup-trigger runs cleanup and returns report."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["status"] == "completed"
        assert "report" in data

    def test_trigger_cleanup_report_has_issue_106_and_219_fields(self, api_client: TrinityApiClient):
        """Triggered cleanup report includes no_session_executions, orphaned_skipped, and stale_slot_executions."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        data = response.json()

        report = data["report"]
        assert "no_session_executions" in report, "Missing no_session_executions field"
        assert "orphaned_skipped" in report, "Missing orphaned_skipped field"
        assert "stale_slot_executions" in report, "Missing stale_slot_executions field (#219)"
        # Issue #772: retention sweeps
        assert "execution_logs_pruned" in report, "Missing execution_logs_pruned field (#772)"
        assert "execution_rows_pruned" in report, "Missing execution_rows_pruned field (#772)"
        assert "health_checks_pruned" in report, "Missing health_checks_pruned field (#772)"
        assert isinstance(report["no_session_executions"], int)
        assert isinstance(report["orphaned_skipped"], int)
        assert isinstance(report["stale_slot_executions"], int)
        assert isinstance(report["execution_logs_pruned"], int)
        assert isinstance(report["execution_rows_pruned"], int)
        assert isinstance(report["health_checks_pruned"], int)
        assert report["no_session_executions"] >= 0
        assert report["orphaned_skipped"] >= 0
        assert report["stale_slot_executions"] >= 0
        assert report["execution_logs_pruned"] >= 0
        assert report["execution_rows_pruned"] >= 0
        assert report["health_checks_pruned"] >= 0

    def test_trigger_cleanup_total_includes_all_fields(self, api_client: TrinityApiClient):
        """Cleanup total correctly sums all fields including Issue #106 and #219 additions."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        report = response.json()["report"]

        expected_total = sum(
            v for k, v in report.items()
            if k != "total" and isinstance(v, int)
        )
        assert report["total"] == expected_total

    def test_trigger_cleanup_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """Cleanup trigger requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/cleanup-trigger")
        assert response.status_code in [401, 403]
