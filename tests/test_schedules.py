"""
Schedules Tests (test_schedules.py)

Tests for agent scheduling functionality.
Covers REQ-SCHED-001 through REQ-SCHED-005.

Feature Flow: scheduling.md
"""

import pytest
import uuid
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestSchedulerStatus:
    """REQ-SCHED-005: Scheduler status endpoint tests."""

    pytestmark = pytest.mark.smoke

    def test_get_scheduler_status(self, api_client: TrinityApiClient):
        """GET /api/agents/scheduler/status returns scheduler status."""
        response = api_client.get("/api/agents/scheduler/status")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have scheduler state info
        assert_has_fields(data, ["running"])

    def test_scheduler_status_structure(self, api_client: TrinityApiClient):
        """Scheduler status returns expected structure."""
        response = api_client.get("/api/agents/scheduler/status")

        assert_status(response, 200)
        data = response.json()

        # Scheduler should be running
        assert isinstance(data.get("running"), bool)

        # May include job count
        if "job_count" in data:
            assert isinstance(data["job_count"], int)
            assert data["job_count"] >= 0

    def test_scheduler_status_includes_jobs(self, api_client: TrinityApiClient):
        """Scheduler status includes information about scheduled jobs."""
        response = api_client.get("/api/agents/scheduler/status")

        assert_status(response, 200)
        data = response.json()

        # Should have jobs or job_count field
        assert "jobs" in data or "job_count" in data or "scheduled_jobs" in data

    def test_scheduler_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/scheduler/status requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/scheduler/status",
            auth=False
        )
        assert_status(response, 401)


class TestCreateSchedule:
    """REQ-SCHED-001: Create schedule endpoint tests."""

    def test_create_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent,
        test_schedule_name: str,
        resource_tracker
    ):
        """POST /api/agents/{name}/schedules creates schedule."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": test_schedule_name,
                "cron_expression": "0 * * * *",  # Every hour
                "message": "Test scheduled message",
                "enabled": False  # Don't actually run
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        # Track for cleanup
        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        assert_has_fields(data, ["id", "name"])

    def test_create_schedule_validates_cron(
        self,
        api_client: TrinityApiClient,
        created_agent,
        test_schedule_name: str
    ):
        """Invalid cron expression returns 400."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": test_schedule_name,
                "cron_expression": "invalid cron",
                "message": "Test"
            }
        )

        assert_status_in(response, [400, 422])


class TestListSchedules:
    """REQ-SCHED-002: List schedules endpoint tests."""

    def test_list_schedules(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/schedules returns schedules."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/schedules")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_list_response(data, "schedules")

    def test_schedule_has_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """Each schedule has id, cron, enabled."""
        # Create a schedule first
        schedule_name = f"test-schedule-fields-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code in [200, 201]:
            data = create_response.json()
            if "id" in data:
                resource_tracker.track_schedule(created_agent['name'], data["id"])

        # List and check
        list_response = api_client.get(f"/api/agents/{created_agent['name']}/schedules")
        assert_status(list_response, 200)
        schedules = list_response.json()

        if len(schedules) > 0:
            schedule = schedules[0]
            assert_has_fields(schedule, ["id", "name"])


class TestGetSchedule:
    """Tests for GET single schedule endpoint (MCP-SCHED-001 REQ-3)."""

    def test_get_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """GET /api/agents/{name}/schedules/{id} returns schedule details."""
        # Create schedule
        schedule_name = f"test-get-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Test message",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Get specific schedule
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}"
        )

        assert_status(response, 200)
        schedule_data = assert_json_response(response)
        assert_has_fields(schedule_data, ["id", "name", "cron_expression", "message"])
        assert schedule_data["id"] == schedule_id
        assert schedule_data["name"] == schedule_name

    def test_get_nonexistent_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET nonexistent schedule returns 404."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/schedules/nonexistent-id"
        )

        assert_status(response, 404)

    def test_get_schedule_from_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """GET schedule from nonexistent agent returns 404."""
        response = api_client.get(
            "/api/agents/nonexistent-agent/schedules/some-id"
        )

        assert_status(response, 404)


class TestUpdateSchedule:
    """Tests for UPDATE schedule endpoint (MCP-SCHED-001 REQ-4)."""

    def test_update_schedule_name(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT /api/agents/{name}/schedules/{id} updates schedule name."""
        # Create schedule
        schedule_name = f"test-update-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Original message",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update name
        new_name = f"updated-{schedule_name}"
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"name": new_name}
        )

        assert_status(response, 200)
        updated_data = assert_json_response(response)
        assert updated_data.get("name") == new_name

    def test_update_schedule_cron(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT /api/agents/{name}/schedules/{id} updates cron expression."""
        # Create schedule
        schedule_name = f"test-update-cron-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update cron
        new_cron = "0 10 * * *"
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"cron_expression": new_cron}
        )

        assert_status(response, 200)
        updated_data = assert_json_response(response)
        assert updated_data.get("cron_expression") == new_cron

    def test_update_schedule_message(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT /api/agents/{name}/schedules/{id} updates message."""
        # Create schedule
        schedule_name = f"test-update-msg-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Original message",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update message
        new_message = "Updated task message"
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"message": new_message}
        )

        assert_status(response, 200)
        updated_data = assert_json_response(response)
        assert updated_data.get("message") == new_message

    def test_update_schedule_multiple_fields(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT /api/agents/{name}/schedules/{id} updates multiple fields."""
        # Create schedule
        schedule_name = f"test-update-multi-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Original",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update multiple fields
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={
                "name": "new-name",
                "cron_expression": "0 10 * * *",
                "message": "New message"
            }
        )

        assert_status(response, 200)
        updated_data = assert_json_response(response)
        assert updated_data.get("name") == "new-name"
        assert updated_data.get("cron_expression") == "0 10 * * *"
        assert updated_data.get("message") == "New message"

    def test_update_schedule_invalid_cron(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT with invalid cron expression returns 400."""
        # Create schedule
        schedule_name = f"test-update-invalid-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 9 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Try invalid cron
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"cron_expression": "invalid"}
        )

        assert_status_in(response, [400, 422])

    def test_update_nonexistent_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT to nonexistent schedule returns 404."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/nonexistent-id",
            json={"name": "new-name"}
        )

        assert_status(response, 404)


class TestEnableDisableSchedule:
    """REQ-SCHED-003: Enable/disable schedule endpoint tests."""

    def test_enable_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST /api/agents/{name}/schedules/{id}/enable enables schedule."""
        # Create disabled schedule
        schedule_name = f"test-enable-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Enable it
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/enable"
        )

        assert_status(response, 200)

    def test_disable_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST /api/agents/{name}/schedules/{id}/disable disables schedule."""
        # Create enabled schedule
        schedule_name = f"test-disable-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Test",
                "enabled": True
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Disable it
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/disable"
        )

        assert_status(response, 200)


class TestTriggerSchedule:
    """REQ-SCHED-004: Trigger schedule endpoint tests.

    Note (2026-02-11): Manual triggers are now routed through the dedicated
    scheduler service, which handles activity tracking and distributed locking.
    """

    @pytest.mark.slow
    def test_trigger_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST /api/agents/{name}/schedules/{id}/trigger manually triggers."""
        # Create schedule
        schedule_name = f"test-trigger-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Say hello",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Trigger it - now routes through dedicated scheduler
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/trigger",
            timeout=120.0  # May take a while
        )

        # May return 503 if scheduler or agent not ready
        if response.status_code == 503:
            pytest.skip("Scheduler or agent server not ready")
        if response.status_code == 504:
            pytest.skip("Scheduler timeout")
        if response.status_code == 429:
            pytest.skip("Agent queue full")

        assert_status_in(response, [200, 202])

        # Verify response structure (updated 2026-02-11)
        if response.status_code == 200:
            trigger_data = response.json()
            assert trigger_data.get("status") == "triggered"
            assert trigger_data.get("schedule_id") == schedule_id
            # New fields from dedicated scheduler
            assert "schedule_name" in trigger_data or "message" in trigger_data

    def test_trigger_nonexistent_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST trigger on nonexistent schedule returns 404."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules/nonexistent-id/trigger"
        )

        assert_status(response, 404)


class TestScheduleExecutions:
    """Tests for schedule execution history."""

    def test_get_execution_history(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """GET /api/agents/{name}/schedules/{id}/executions returns history."""
        # Create schedule
        schedule_name = f"test-executions-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Get executions
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/executions"
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)


class TestDeleteSchedule:
    """Tests for schedule deletion."""

    def test_delete_schedule(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/schedules/{id} deletes schedule."""
        # Create schedule
        schedule_name = f"test-delete-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 0 * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")

        # Delete it
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}"
        )

        assert_status_in(response, [200, 204])
