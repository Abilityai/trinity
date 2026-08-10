"""
Internal API Tests (test_internal.py)

Tests for internal API endpoints used by scheduler and agent containers.
These endpoints require X-Internal-Secret header for authentication.

Feature Flow: scheduler-service.md
Added: 2026-02-11
"""

import os
import pytest
import uuid
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


def _internal_headers() -> dict:
    """Get headers required for internal API authentication."""
    secret = os.getenv("INTERNAL_API_SECRET", "")
    if not secret:
        # Fall back to SECRET_KEY which the backend also accepts
        secret = os.getenv("SECRET_KEY", "")
    return {"X-Internal-Secret": secret} if secret else {}


class TestInternalHealth:
    """Tests for internal health check endpoint."""

    pytestmark = pytest.mark.smoke

    def test_internal_health(self, api_client: TrinityApiClient):
        """GET /api/internal/health returns ok status."""
        headers = _internal_headers()
        if not headers:
            pytest.skip("INTERNAL_API_SECRET or SECRET_KEY not set in test env")

        response = api_client.get("/api/internal/health", headers=headers)

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "ok"


class TestActivityTracking:
    """Tests for activity tracking endpoints (added 2026-02-11).

    These endpoints are called by the dedicated scheduler service
    to create agent_activities records for Timeline visibility.
    """

    @pytest.fixture(autouse=True)
    def _skip_without_secret(self):
        """Skip all tests if internal secret is not configured."""
        if not _internal_headers():
            pytest.skip("INTERNAL_API_SECRET or SECRET_KEY not set in test env")

    def test_track_activity_creates_record(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/internal/activities/track creates activity."""
        execution_id = f"test-exec-{uuid.uuid4().hex[:8]}"

        response = api_client.post(
            "/api/internal/activities/track",
            json={
                "agent_name": created_agent["name"],
                "activity_type": "schedule_start",
                "triggered_by": "schedule",
                "related_execution_id": execution_id,
                "details": {
                    "schedule_id": "test-schedule-id",
                    "schedule_name": "Test Schedule"
                }
            },
            headers=_internal_headers()
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["activity_id", "agent_name", "activity_type"])
        assert data["agent_name"] == created_agent["name"]
        assert data["activity_type"] == "schedule_start"

    def test_track_activity_with_manual_trigger(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/internal/activities/track accepts manual trigger."""
        execution_id = f"test-exec-{uuid.uuid4().hex[:8]}"

        response = api_client.post(
            "/api/internal/activities/track",
            json={
                "agent_name": created_agent["name"],
                "activity_type": "schedule_start",
                "triggered_by": "manual",
                "related_execution_id": execution_id,
                "details": {"manual_trigger": True}
            },
            headers=_internal_headers()
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("activity_id") is not None

    def test_track_activity_invalid_type(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/internal/activities/track rejects invalid activity type."""
        response = api_client.post(
            "/api/internal/activities/track",
            json={
                "agent_name": created_agent["name"],
                "activity_type": "invalid_type",
                "triggered_by": "schedule"
            },
            headers=_internal_headers()
        )

        assert_status(response, 400)

    def test_complete_activity(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/internal/activities/{id}/complete updates activity."""
        # First create an activity
        execution_id = f"test-exec-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            "/api/internal/activities/track",
            json={
                "agent_name": created_agent["name"],
                "activity_type": "schedule_start",
                "triggered_by": "schedule",
                "related_execution_id": execution_id
            },
            headers=_internal_headers()
        )

        if create_response.status_code != 200:
            pytest.skip("Could not create activity")

        activity_id = create_response.json().get("activity_id")

        # Complete the activity
        response = api_client.post(
            f"/api/internal/activities/{activity_id}/complete",
            json={
                "status": "completed",
                "details": {"duration_ms": 1000}
            },
            headers=_internal_headers()
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("completed") is True
        assert data.get("status") == "completed"

    def test_complete_activity_with_failure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/internal/activities/{id}/complete handles failures."""
        # First create an activity
        execution_id = f"test-exec-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            "/api/internal/activities/track",
            json={
                "agent_name": created_agent["name"],
                "activity_type": "schedule_start",
                "triggered_by": "schedule",
                "related_execution_id": execution_id
            },
            headers=_internal_headers()
        )

        if create_response.status_code != 200:
            pytest.skip("Could not create activity")

        activity_id = create_response.json().get("activity_id")

        # Complete with failure
        response = api_client.post(
            f"/api/internal/activities/{activity_id}/complete",
            json={
                "status": "failed",
                "error": "Agent not reachable"
            },
            headers=_internal_headers()
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data.get("status") == "failed"

    def test_complete_nonexistent_activity(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/internal/activities/{id}/complete returns 404 for unknown ID."""
        response = api_client.post(
            "/api/internal/activities/nonexistent-activity-id/complete",
            json={"status": "completed"},
            headers=_internal_headers()
        )

        assert_status(response, 404)
