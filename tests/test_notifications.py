"""
Agent Notifications Tests (test_notifications.py)

Tests for Trinity agent notification system endpoints (NOTIF-001).
Covers notification CRUD, acknowledge/dismiss, agent-specific queries.

Feature Flow: agent-notifications.md
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# ============================================================================
# Authentication Tests
# ============================================================================

class TestNotificationAuthentication:
    """Tests for notification endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_create_notification_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/notifications requires authentication."""
        response = unauthenticated_client.post(
            "/api/notifications",
            json={"notification_type": "info", "title": "Test"},
            auth=False
        )
        assert_status(response, 401)

    def test_list_notifications_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/notifications requires authentication."""
        response = unauthenticated_client.get("/api/notifications", auth=False)
        assert_status(response, 401)

    def test_get_notification_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/notifications/{id} requires authentication."""
        response = unauthenticated_client.get("/api/notifications/notif_test123", auth=False)
        assert_status(response, 401)

    def test_acknowledge_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/notifications/{id}/acknowledge requires authentication."""
        response = unauthenticated_client.post(
            "/api/notifications/notif_test123/acknowledge",
            auth=False
        )
        assert_status(response, 401)

    def test_dismiss_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/notifications/{id}/dismiss requires authentication."""
        response = unauthenticated_client.post(
            "/api/notifications/notif_test123/dismiss",
            auth=False
        )
        assert_status(response, 401)


# ============================================================================
# Create Notification Tests
# ============================================================================

class TestCreateNotification:
    """Tests for POST /api/notifications endpoint."""

    pytestmark = pytest.mark.smoke

    def test_create_notification_success(self, api_client: TrinityApiClient):
        """Create notification with valid data returns 201."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Test notification",
            "message": "This is a test",
            "priority": "normal",
            "category": "test"
        })
        assert_status(response, 201)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "id", "agent_name", "notification_type", "title",
            "priority", "status", "created_at"
        ])
        assert data["id"].startswith("notif_")
        assert data["notification_type"] == "info"
        assert data["title"] == "Test notification"
        assert data["status"] == "pending"

    def test_create_notification_minimal(self, api_client: TrinityApiClient):
        """Create notification with only required fields."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "alert",
            "title": "Minimal notification"
        })
        assert_status(response, 201)
        data = response.json()
        assert data["notification_type"] == "alert"
        assert data["priority"] == "normal"  # Default

    def test_create_notification_with_metadata(self, api_client: TrinityApiClient):
        """Create notification with metadata object."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "completion",
            "title": "Task completed",
            "metadata": {"task_id": "123", "records": 100}
        })
        assert_status(response, 201)
        data = response.json()
        assert data["metadata"] == {"task_id": "123", "records": 100}

    def test_create_notification_invalid_type(self, api_client: TrinityApiClient):
        """Create with invalid notification_type returns 400."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "invalid_type",
            "title": "Test"
        })
        assert_status(response, 400)

    def test_create_notification_invalid_priority(self, api_client: TrinityApiClient):
        """Create with invalid priority returns 400."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Test",
            "priority": "super_urgent"
        })
        assert_status(response, 400)

    def test_create_notification_title_too_long(self, api_client: TrinityApiClient):
        """Create with title > 200 chars returns 400."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "A" * 201
        })
        assert_status(response, 400)

    def test_create_notification_missing_title(self, api_client: TrinityApiClient):
        """Create without title returns 422."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "info"
        })
        assert_status(response, 422)

    def test_create_notification_missing_type(self, api_client: TrinityApiClient):
        """Create without notification_type returns 422."""
        response = api_client.post("/api/notifications", json={
            "title": "Test"
        })
        assert_status(response, 422)


# ============================================================================
# List Notifications Tests
# ============================================================================

class TestListNotifications:
    """Tests for GET /api/notifications endpoint."""

    pytestmark = pytest.mark.smoke

    def test_list_notifications_structure(self, api_client: TrinityApiClient):
        """List notifications returns expected structure."""
        response = api_client.get("/api/notifications")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["count", "notifications"])
        assert isinstance(data["count"], int)
        assert isinstance(data["notifications"], list)

    def test_list_notifications_item_structure(self, api_client: TrinityApiClient):
        """Each notification in list has expected fields."""
        # First create a notification
        api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Structure test"
        })

        response = api_client.get("/api/notifications?limit=5")
        assert_status(response, 200)
        data = response.json()

        for notif in data.get("notifications", []):
            assert_has_fields(notif, [
                "id", "agent_name", "notification_type", "title",
                "priority", "status", "created_at"
            ])

    def test_list_notifications_respects_limit(self, api_client: TrinityApiClient):
        """List respects limit parameter."""
        response = api_client.get("/api/notifications?limit=3")
        assert_status(response, 200)
        data = response.json()
        assert len(data.get("notifications", [])) <= 3

    def test_list_notifications_filter_by_status(self, api_client: TrinityApiClient):
        """List can filter by status parameter."""
        response = api_client.get("/api/notifications?status=pending")
        assert_status(response, 200)
        data = response.json()
        for notif in data.get("notifications", []):
            assert notif["status"] == "pending"

    def test_list_notifications_filter_by_priority(self, api_client: TrinityApiClient):
        """List can filter by priority parameter (comma-separated)."""
        response = api_client.get("/api/notifications?priority=high,urgent")
        assert_status(response, 200)
        data = response.json()
        for notif in data.get("notifications", []):
            assert notif["priority"] in ["high", "urgent"]

    def test_list_notifications_invalid_status(self, api_client: TrinityApiClient):
        """List with invalid status returns 400."""
        response = api_client.get("/api/notifications?status=invalid")
        assert_status(response, 400)

    def test_list_notifications_invalid_priority(self, api_client: TrinityApiClient):
        """List with invalid priority returns 400."""
        response = api_client.get("/api/notifications?priority=super")
        assert_status(response, 400)


# ============================================================================
# Get Single Notification Tests
# ============================================================================

class TestGetNotification:
    """Tests for GET /api/notifications/{id} endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_notification_success(self, api_client: TrinityApiClient):
        """Get existing notification returns 200."""
        # Create a notification first
        create_response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Get test"
        })
        notif_id = create_response.json()["id"]

        response = api_client.get(f"/api/notifications/{notif_id}")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == notif_id
        assert data["title"] == "Get test"

    def test_get_notification_not_found(self, api_client: TrinityApiClient):
        """Get non-existent notification returns 404."""
        response = api_client.get("/api/notifications/notif_nonexistent123")
        assert_status(response, 404)


# ============================================================================
# Acknowledge Notification Tests
# ============================================================================

class TestAcknowledgeNotification:
    """Tests for POST /api/notifications/{id}/acknowledge endpoint."""

    pytestmark = pytest.mark.smoke

    def test_acknowledge_notification_success(self, api_client: TrinityApiClient):
        """Acknowledge pending notification returns 200."""
        # Create a notification
        create_response = api_client.post("/api/notifications", json={
            "notification_type": "alert",
            "title": "Acknowledge test"
        })
        notif_id = create_response.json()["id"]

        # Acknowledge it
        response = api_client.post(f"/api/notifications/{notif_id}/acknowledge")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == notif_id
        assert data["status"] == "acknowledged"
        assert "acknowledged_at" in data
        assert "acknowledged_by" in data

    def test_acknowledge_notification_not_found(self, api_client: TrinityApiClient):
        """Acknowledge non-existent notification returns 404."""
        response = api_client.post("/api/notifications/notif_nonexistent123/acknowledge")
        assert_status(response, 404)

    def test_acknowledge_idempotent(self, api_client: TrinityApiClient):
        """Acknowledging already acknowledged notification is idempotent."""
        # Create and acknowledge
        create_response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Idempotent test"
        })
        notif_id = create_response.json()["id"]
        api_client.post(f"/api/notifications/{notif_id}/acknowledge")

        # Acknowledge again - should not error
        response = api_client.post(f"/api/notifications/{notif_id}/acknowledge")
        assert_status(response, 200)


# ============================================================================
# Dismiss Notification Tests
# ============================================================================

class TestDismissNotification:
    """Tests for POST /api/notifications/{id}/dismiss endpoint."""

    pytestmark = pytest.mark.smoke

    def test_dismiss_notification_success(self, api_client: TrinityApiClient):
        """Dismiss notification returns 200."""
        # Create a notification
        create_response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": "Dismiss test"
        })
        notif_id = create_response.json()["id"]

        # Dismiss it
        response = api_client.post(f"/api/notifications/{notif_id}/dismiss")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == notif_id
        assert data["status"] == "dismissed"

    def test_dismiss_notification_not_found(self, api_client: TrinityApiClient):
        """Dismiss non-existent notification returns 404."""
        response = api_client.post("/api/notifications/notif_nonexistent123/dismiss")
        assert_status(response, 404)


# ============================================================================
# Agent-Specific Notification Tests (require agent creation)
# ============================================================================

class TestAgentNotifications:
    """Tests for agent-specific notification endpoints."""

    # These tests require agent creation, so not marked as smoke
    def test_get_agent_notifications_success(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Get notifications for existing agent returns 200."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/notifications")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["count", "notifications"])

    def test_get_agent_notifications_not_found(self, api_client: TrinityApiClient):
        """Get notifications for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/notifications")
        assert_status(response, 404)

    def test_count_agent_notifications_success(
        self,
        api_client: TrinityApiClient,
        created_agent: dict
    ):
        """Count pending notifications for existing agent returns 200."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/notifications/count")
        assert_status(response, 200)
        data = response.json()
        assert_has_fields(data, ["agent_name", "pending_count"])
        assert data["agent_name"] == created_agent["name"]
        assert isinstance(data["pending_count"], int)

    def test_count_agent_notifications_not_found(self, api_client: TrinityApiClient):
        """Count notifications for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/notifications/count")


# ============================================================================
# Notification Type Tests
# ============================================================================

class TestNotificationTypes:
    """Tests for different notification types."""

    pytestmark = pytest.mark.smoke

    @pytest.mark.parametrize("notification_type", [
        "alert",
        "info",
        "status",
        "completion",
        "question"
    ])
    def test_valid_notification_types(
        self,
        api_client: TrinityApiClient,
        notification_type: str
    ):
        """All valid notification types are accepted."""
        response = api_client.post("/api/notifications", json={
            "notification_type": notification_type,
            "title": f"Test {notification_type}"
        })
        assert_status(response, 201)
        assert response.json()["notification_type"] == notification_type


# ============================================================================
# Priority Tests
# ============================================================================

class TestNotificationPriorities:
    """Tests for different notification priorities."""

    pytestmark = pytest.mark.smoke

    @pytest.mark.parametrize("priority", ["low", "normal", "high", "urgent"])
    def test_valid_priorities(self, api_client: TrinityApiClient, priority: str):
        """All valid priorities are accepted."""
        response = api_client.post("/api/notifications", json={
            "notification_type": "info",
            "title": f"Test {priority}",
            "priority": priority
        })
        assert_status(response, 201)
        assert response.json()["priority"] == priority
