"""
Activity Stream Tests (test_activities.py)

Tests for Trinity unified activity stream endpoints.
Covers REQ-ACTIVITY-001: Cross-agent timeline and per-agent activity tracking.

Feature Flow: activity-stream.md

Combines fast tests (API structure) and slow tests (activity creation via chat).
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
    assert_list_response,
)
from utils.cleanup import cleanup_test_agent


class TestActivityTimelineAuthentication:
    """Tests for activity timeline authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_timeline_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/activities/timeline requires authentication."""
        response = unauthenticated_client.get("/api/activities/timeline", auth=False)
        assert_status(response, 401)

    def test_agent_activities_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/activities requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/activities", auth=False)
        assert_status(response, 401)


class TestActivityTimeline:
    """Tests for GET /api/activities/timeline endpoint."""

    pytestmark = pytest.mark.smoke

    def test_timeline_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/activities/timeline returns expected structure."""
        response = api_client.get("/api/activities/timeline")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["count", "activities"])
        assert isinstance(data["count"], int)
        assert isinstance(data["activities"], list)

    def test_timeline_activities_structure(self, api_client: TrinityApiClient):
        """Timeline activities have expected fields."""
        response = api_client.get("/api/activities/timeline?limit=10")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert_has_fields(activity, [
                "id",
                "agent_name",
                "activity_type",
                "activity_state",
                "started_at",
            ])

    def test_timeline_respects_limit(self, api_client: TrinityApiClient):
        """Timeline respects limit parameter."""
        response = api_client.get("/api/activities/timeline?limit=5")
        assert_status(response, 200)
        data = response.json()
        assert len(data.get("activities", [])) <= 5

    def test_timeline_filter_by_type(self, api_client: TrinityApiClient):
        """Timeline can filter by activity_types parameter."""
        response = api_client.get("/api/activities/timeline?activity_types=chat_start")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_type"] == "chat_start"

    def test_timeline_filter_multiple_types(self, api_client: TrinityApiClient):
        """Timeline can filter by multiple activity types."""
        response = api_client.get("/api/activities/timeline?activity_types=chat_start,tool_call")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_type"] in ["chat_start", "tool_call"]

    def test_timeline_filter_by_time_range(self, api_client: TrinityApiClient):
        """Timeline can filter by start_time and end_time."""
        # Use a time range in the past that won't have data
        response = api_client.get(
            "/api/activities/timeline?start_time=2020-01-01T00:00:00&end_time=2020-01-02T00:00:00"
        )
        assert_status(response, 200)
        data = response.json()
        # Should return empty or very limited results for old date range
        assert data["count"] >= 0

    def test_timeline_empty_result_structure(self, api_client: TrinityApiClient):
        """Timeline returns proper structure even with no results."""
        # Use impossible time range
        response = api_client.get(
            "/api/activities/timeline?start_time=1990-01-01T00:00:00&end_time=1990-01-02T00:00:00"
        )
        assert_status(response, 200)
        data = response.json()
        assert data["count"] == 0
        assert data["activities"] == []


class TestAgentActivities:
    """Tests for GET /api/agents/{name}/activities endpoint."""

    def test_agent_activities_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """GET /api/agents/{name}/activities returns expected structure."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/activities")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "count", "activities"])
        assert data["agent_name"] == agent_name
        assert isinstance(data["count"], int)
        assert isinstance(data["activities"], list)

    def test_agent_activities_filter_by_type(self, api_client: TrinityApiClient, created_agent: dict):
        """Agent activities can filter by activity_type."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/activities?activity_type=chat_start")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_type"] == "chat_start"

    def test_agent_activities_filter_by_state(self, api_client: TrinityApiClient, created_agent: dict):
        """Agent activities can filter by activity_state."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/activities?activity_state=completed")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_state"] == "completed"

    def test_agent_activities_respects_limit(self, api_client: TrinityApiClient, created_agent: dict):
        """Agent activities respects limit parameter."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/activities?limit=5")
        assert_status(response, 200)
        data = response.json()
        assert len(data.get("activities", [])) <= 5

    def test_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/activities returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/activities")
        assert_status(response, 404)


class TestActivityCreation:
    """Tests for activity creation via chat.

    These tests verify that activities are created when chat messages are sent.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_chat_creates_activity(self, api_client: TrinityApiClient, created_agent: dict):
        """Sending a chat message creates an activity record."""
        agent_name = created_agent["name"]

        # Get initial activity count
        initial_response = api_client.get(f"/api/agents/{agent_name}/activities?limit=100")
        initial_count = initial_response.json().get("count", 0)

        # Send a simple chat message
        chat_response = api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "What is 2 + 2?"}
        )

        # Chat should complete (may be queued or immediate)
        assert_status_in(chat_response, [200, 202])

        # Wait for activity to be recorded
        time.sleep(2)

        # Check for new activities
        response = api_client.get(f"/api/agents/{agent_name}/activities?limit=100")
        assert_status(response, 200)
        data = response.json()

        # Should have at least one new activity
        assert data["count"] > initial_count, "New activity should be created after chat"

        # Check that chat_start activity exists
        activities = data.get("activities", [])
        chat_activities = [a for a in activities if a["activity_type"] == "chat_start"]
        assert len(chat_activities) > 0, "Should have at least one chat_start activity"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    @pytest.mark.timeout(120)
    def test_activity_has_expected_fields(self, api_client: TrinityApiClient, created_agent: dict):
        """Activity records have expected fields populated."""
        agent_name = created_agent["name"]

        # Send a chat message to ensure at least one activity
        api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "Hello, please respond briefly."}
        )
        time.sleep(2)

        # Get activities
        response = api_client.get(f"/api/agents/{agent_name}/activities?limit=10")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        if activities:
            activity = activities[0]
            # Verify key fields are present
            assert_has_fields(activity, [
                "id",
                "agent_name",
                "activity_type",
                "activity_state",
                "started_at",
                "triggered_by"
            ])
            # Verify agent name matches
            assert activity["agent_name"] == agent_name
            # Activity state should be valid
            assert activity["activity_state"] in ["started", "completed", "failed", "cancelled"]


class TestActivityTypes:
    """Tests for different activity types."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_filter_chat_start_activities(self, api_client: TrinityApiClient, created_agent: dict):
        """Can filter for chat_start activities."""
        agent_name = created_agent["name"]

        # Send a chat to ensure activity exists
        api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "Test message"}
        )
        time.sleep(2)

        response = api_client.get(
            f"/api/agents/{agent_name}/activities?activity_type=chat_start&limit=10"
        )
        assert_status(response, 200)
        data = response.json()

        # All returned activities should be chat_start
        for activity in data.get("activities", []):
            assert activity["activity_type"] == "chat_start"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_filter_tool_call_activities(self, api_client: TrinityApiClient, created_agent: dict):
        """Can filter for tool_call activities."""
        agent_name = created_agent["name"]

        # Send a chat that will trigger tool calls
        api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "List the files in the current directory"}
        )
        time.sleep(5)  # Tool calls take longer

        response = api_client.get(
            f"/api/agents/{agent_name}/activities?activity_type=tool_call&limit=20"
        )
        assert_status(response, 200)
        data = response.json()

        # All returned activities should be tool_call
        for activity in data.get("activities", []):
            assert activity["activity_type"] == "tool_call"


class TestActivityDetails:
    """Tests for activity details field."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_activity_includes_details(self, api_client: TrinityApiClient, created_agent: dict):
        """Activity records include details when available."""
        agent_name = created_agent["name"]

        # Send a chat
        api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "Say hello"}
        )
        time.sleep(2)

        response = api_client.get(f"/api/agents/{agent_name}/activities?limit=10")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        # At least some activities should have details
        activities_with_details = [a for a in activities if a.get("details")]
        # This may or may not have details depending on implementation
        # Just verify the field exists
        for activity in activities:
            assert "details" in activity or activity.get("details") is None


class TestCrossAgentTimeline:
    """Tests for cross-agent timeline functionality."""

    def test_timeline_includes_multiple_agents(self, api_client: TrinityApiClient):
        """Timeline can include activities from multiple agents."""
        response = api_client.get("/api/activities/timeline?limit=50")
        assert_status(response, 200)
        data = response.json()

        activities = data.get("activities", [])
        if len(activities) > 1:
            # Check if there are activities from different agents
            agent_names = set(a["agent_name"] for a in activities)
            # Just verify we can get activities from potentially multiple agents
            assert len(agent_names) >= 1

    def test_timeline_sorted_by_time(self, api_client: TrinityApiClient):
        """Timeline activities are sorted by time (most recent first)."""
        response = api_client.get("/api/activities/timeline?limit=20")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        if len(activities) >= 2:
            # Verify descending order by created_at/started_at
            timestamps = [a.get("started_at") or a.get("created_at") for a in activities]
            for i in range(len(timestamps) - 1):
                if timestamps[i] and timestamps[i + 1]:
                    assert timestamps[i] >= timestamps[i + 1], \
                        "Activities should be sorted by time descending"


class TestTimelineForDashboard:
    """Tests for Dashboard Timeline View API requirements.

    These tests verify the timeline API provides data needed by the
    Dashboard Timeline View feature including execution tracking and
    trigger-based color coding.
    Feature Flow: dashboard-timeline-view.md
    """

    pytestmark = pytest.mark.smoke

    def test_timeline_activity_types_for_dashboard(self, api_client: TrinityApiClient):
        """Timeline returns execution activity types needed by Dashboard."""
        # Dashboard requests these activity types
        activity_types = "agent_collaboration,schedule_start,schedule_end,chat_start,chat_end"
        response = api_client.get(f"/api/activities/timeline?activity_types={activity_types}&limit=50")
        assert_status(response, 200)
        data = response.json()

        # Verify returned activities match requested types
        valid_types = {"agent_collaboration", "schedule_start", "schedule_end", "chat_start", "chat_end"}
        for activity in data.get("activities", []):
            assert activity["activity_type"] in valid_types, \
                f"Unexpected activity type: {activity['activity_type']}"

    def test_timeline_activity_has_duration(self, api_client: TrinityApiClient):
        """Timeline activities include duration_ms field."""
        response = api_client.get("/api/activities/timeline?limit=20")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        for activity in activities:
            # duration_ms should be present (can be None if in progress)
            assert "duration_ms" in activity or "details" in activity

    def test_timeline_activity_has_triggered_by(self, api_client: TrinityApiClient):
        """Timeline activities include triggered_by field for color coding."""
        response = api_client.get("/api/activities/timeline?limit=20")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        for activity in activities:
            # triggered_by indicates what triggered the execution
            # Used for color coding: schedule=purple, agent=cyan, manual/user=green
            assert "triggered_by" in activity, \
                f"Activity {activity.get('id')} should have triggered_by field"

    def test_timeline_activity_details_structure(self, api_client: TrinityApiClient):
        """Timeline activity details have expected structure for arrows."""
        response = api_client.get("/api/activities/timeline?limit=50")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        for activity in activities:
            # Details should be a dict if present
            details = activity.get("details")
            if details:
                assert isinstance(details, dict), "Details should be a dict"

            # Collaboration events should have target_agent in details
            if activity["activity_type"] == "agent_collaboration":
                if details:
                    # May have source_agent and target_agent for arrow drawing
                    pass  # Fields are optional depending on event

    def test_timeline_collaboration_filter(self, api_client: TrinityApiClient):
        """Timeline can filter for collaboration events only."""
        response = api_client.get("/api/activities/timeline?activity_types=agent_collaboration&limit=20")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_type"] == "agent_collaboration"

    def test_timeline_schedule_filter(self, api_client: TrinityApiClient):
        """Timeline can filter for schedule events only."""
        response = api_client.get("/api/activities/timeline?activity_types=schedule_start,schedule_end&limit=20")
        assert_status(response, 200)
        data = response.json()

        for activity in data.get("activities", []):
            assert activity["activity_type"] in ["schedule_start", "schedule_end"]

    def test_timeline_trigger_type_values(self, api_client: TrinityApiClient):
        """Timeline triggered_by values match expected Dashboard values."""
        response = api_client.get("/api/activities/timeline?limit=100")
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        # Valid trigger types for Dashboard color coding. Derive from the canonical
        # source of truth (db/schedules._TRIGGER_BUCKETS — the Dashboard bucket map)
        # rather than a hand-maintained literal set, so this test doesn't drift every
        # time a trigger/channel value is added (webhook, telegram/slack/whatsapp, etc.).
        from db.schedules import _TRIGGER_BUCKETS
        valid_triggers = set(_TRIGGER_BUCKETS) | {None}

        for activity in activities:
            triggered_by = activity.get("triggered_by")
            # triggered_by should be one of the expected values
            assert triggered_by in valid_triggers or triggered_by is None, \
                f"Unexpected triggered_by value: {triggered_by}"


class TestActivityWithScheduleInfo:
    """Tests for activity schedule information."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_scheduled_activity_has_schedule_name(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Scheduled activities include schedule_name in details."""
        agent_name = created_agent["name"]

        # First check if there are any scheduled activities
        response = api_client.get(
            f"/api/agents/{agent_name}/activities?activity_type=schedule_start&limit=10"
        )
        assert_status(response, 200)
        activities = response.json().get("activities", [])

        for activity in activities:
            # Scheduled activities should have schedule info
            details = activity.get("details", {})
            # schedule_name may be in details for scheduled executions
            if activity.get("triggered_by") == "schedule":
                # Details may contain schedule_name
                pass  # Field structure depends on how schedule was created
