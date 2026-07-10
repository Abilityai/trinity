"""
Agent Event Subscriptions Tests (test_event_subscriptions.py)

Tests for Trinity agent event pub/sub system (EVT-001).
Covers subscription CRUD, event emission, permission gating,
template interpolation, and event history.

Feature Flow: agent-event-subscriptions.md
GitHub Issue: #169
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# ============================================================================
# Authentication Tests
# ============================================================================

class TestEventSubscriptionAuthentication:
    """Tests for event subscription endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_create_subscription_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/event-subscriptions requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/some-agent/event-subscriptions",
            json={
                "source_agent": "other-agent",
                "event_type": "test.event",
                "target_message": "Handle {{payload.data}}",
            },
            auth=False,
        )
        assert_status(response, 401)

    def test_list_subscriptions_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/event-subscriptions requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/some-agent/event-subscriptions",
            auth=False,
        )
        assert_status(response, 401)

    def test_emit_event_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/events requires authentication."""
        response = unauthenticated_client.post(
            "/api/events",
            json={"event_type": "test.event"},
            auth=False,
        )
        assert_status(response, 401)

    def test_list_events_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/events requires authentication."""
        response = unauthenticated_client.get("/api/events", auth=False)
        assert_status(response, 401)

    def test_get_subscription_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/event-subscriptions/{id} requires authentication."""
        response = unauthenticated_client.get(
            "/api/event-subscriptions/esub_nonexistent",
            auth=False,
        )
        assert_status(response, 401)

    def test_delete_subscription_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/event-subscriptions/{id} requires authentication."""
        response = unauthenticated_client.delete(
            "/api/event-subscriptions/esub_nonexistent",
            auth=False,
        )
        assert_status(response, 401)


# ============================================================================
# Event Emission Tests
# ============================================================================

class TestEmitEvent:
    """Tests for POST /api/events endpoint."""

    pytestmark = pytest.mark.smoke

    def test_emit_event_success(self, api_client: TrinityApiClient):
        """Emit event with valid data returns 201."""
        response = api_client.post("/api/events", json={
            "event_type": "test.emitted",
            "payload": {"key": "value", "count": 42},
        })
        assert_status(response, 201)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "id", "source_agent", "event_type", "payload",
            "subscriptions_triggered", "created_at",
        ])
        assert data["id"].startswith("evt_")
        assert data["event_type"] == "test.emitted"
        assert data["payload"] == {"key": "value", "count": 42}
        assert data["subscriptions_triggered"] == 0  # No subscriptions yet

    def test_emit_event_minimal(self, api_client: TrinityApiClient):
        """Emit event with only event_type (no payload)."""
        response = api_client.post("/api/events", json={
            "event_type": "test.minimal",
        })
        assert_status(response, 201)
        data = response.json()
        assert data["event_type"] == "test.minimal"
        assert data["payload"] is None

    def test_emit_event_nested_payload(self, api_client: TrinityApiClient):
        """Emit event with nested payload."""
        response = api_client.post("/api/events", json={
            "event_type": "test.nested",
            "payload": {
                "prediction": {"id": "pred-123", "outcome": "win"},
                "metadata": {"source": "model-v3"},
            },
        })
        assert_status(response, 201)
        data = response.json()
        assert data["payload"]["prediction"]["id"] == "pred-123"

    def test_emit_event_invalid_type_format(self, api_client: TrinityApiClient):
        """Emit event with invalid event_type format returns 400."""
        response = api_client.post("/api/events", json={
            "event_type": "invalid type!",
        })
        assert_status(response, 400)

    def test_emit_event_empty_type(self, api_client: TrinityApiClient):
        """Emit event with empty event_type returns 400."""
        response = api_client.post("/api/events", json={
            "event_type": "",
        })
        assert_status(response, 400)

    def test_emit_event_missing_type(self, api_client: TrinityApiClient):
        """Emit event without event_type returns 422."""
        response = api_client.post("/api/events", json={})
        assert_status(response, 422)

    def test_emit_event_single_word_type(self, api_client: TrinityApiClient):
        """Emit event with single word event_type succeeds."""
        response = api_client.post("/api/events", json={
            "event_type": "heartbeat",
        })
        assert_status(response, 201)

    def test_emit_event_multi_dot_type(self, api_client: TrinityApiClient):
        """Emit event with deeply namespaced type succeeds."""
        response = api_client.post("/api/events", json={
            "event_type": "org.team.pipeline.stage.completed",
        })
        assert_status(response, 201)

    def test_emit_event_underscore_type(self, api_client: TrinityApiClient):
        """Emit event with underscores in type succeeds."""
        response = api_client.post("/api/events", json={
            "event_type": "data_pipeline.batch_completed",
        })
        assert_status(response, 201)


# ============================================================================
# Event Emission for Specific Agent
# ============================================================================

class TestEmitEventForAgent:
    """Tests for POST /api/agents/{name}/emit-event endpoint."""

    def test_emit_event_for_agent_success(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Emit event on behalf of a specific agent."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/emit-event",
            json={
                "event_type": "agent.task_completed",
                "payload": {"task": "analysis", "duration_ms": 5000},
            },
        )
        assert_status(response, 201)
        data = response.json()
        assert data["source_agent"] == created_agent["name"]
        assert data["event_type"] == "agent.task_completed"

    def test_emit_event_for_nonexistent_agent(self, api_client: TrinityApiClient):
        """Emit event for nonexistent agent returns 403/404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/emit-event",
            json={"event_type": "test.event"},
        )
        assert_status_in(response, [403, 404])


# ============================================================================
# Event History Tests
# ============================================================================

class TestEventHistory:
    """Tests for event listing endpoints."""

    pytestmark = pytest.mark.smoke

    def test_list_all_events_structure(self, api_client: TrinityApiClient):
        """GET /api/events returns expected structure."""
        response = api_client.get("/api/events")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["count", "events"])
        assert isinstance(data["count"], int)
        assert isinstance(data["events"], list)

    def test_list_events_item_structure(self, api_client: TrinityApiClient):
        """Each event in list has expected fields."""
        # Create an event first
        api_client.post("/api/events", json={
            "event_type": "test.structure_check",
        })

        response = api_client.get("/api/events?limit=5")
        assert_status(response, 200)
        data = response.json()

        if data["events"]:
            event = data["events"][0]
            assert_has_fields(event, [
                "id", "source_agent", "event_type",
                "subscriptions_triggered", "created_at",
            ])

    def test_list_events_respects_limit(self, api_client: TrinityApiClient):
        """List events respects limit parameter."""
        response = api_client.get("/api/events?limit=2")
        assert_status(response, 200)
        data = response.json()
        assert len(data["events"]) <= 2

    def test_list_events_filter_by_source(self, api_client: TrinityApiClient):
        """Filter events by source_agent."""
        response = api_client.get("/api/events?source_agent=admin")
        assert_status(response, 200)
        data = response.json()
        for event in data["events"]:
            assert event["source_agent"] == "admin"

    def test_list_events_filter_by_type(self, api_client: TrinityApiClient):
        """Filter events by event_type."""
        # Emit a specific event
        api_client.post("/api/events", json={"event_type": "test.filter_check"})

        response = api_client.get("/api/events?event_type=test.filter_check")
        assert_status(response, 200)
        data = response.json()
        for event in data["events"]:
            assert event["event_type"] == "test.filter_check"

    def test_list_agent_events(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/events returns events for that agent."""
        # Emit an event for the agent
        api_client.post(
            f"/api/agents/{created_agent['name']}/emit-event",
            json={"event_type": "test.agent_history"},
        )

        response = api_client.get(
            f"/api/agents/{created_agent['name']}/events"
        )
        assert_status(response, 200)
        data = response.json()
        assert_has_fields(data, ["count", "events"])
        for event in data["events"]:
            assert event["source_agent"] == created_agent["name"]


# ============================================================================
# Subscription CRUD Tests
# ============================================================================

class TestCreateSubscription:
    """Tests for POST /api/agents/{name}/event-subscriptions endpoint."""

    def test_create_subscription_success(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Create subscription with valid data returns 201."""
        agent_name = created_agent["name"]

        # Grant self-permission (self-subscription is always allowed)
        response = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,  # Subscribe to own events
                "event_type": "self.test_event",
                "target_message": "Handle event: {{payload.data}}",
            },
        )
        assert_status(response, 201)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "id", "subscriber_agent", "source_agent", "event_type",
            "target_message", "enabled", "created_at", "updated_at", "created_by",
        ])
        assert data["id"].startswith("esub_")
        assert data["subscriber_agent"] == agent_name
        assert data["source_agent"] == agent_name
        assert data["event_type"] == "self.test_event"
        assert data["enabled"] is True

    def test_create_subscription_invalid_event_type(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Create subscription with invalid event_type returns 400."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/event-subscriptions",
            json={
                "source_agent": created_agent["name"],
                "event_type": "invalid type!",
                "target_message": "Test",
            },
        )
        assert_status(response, 400)

    def test_create_subscription_nonexistent_source(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Create subscription with nonexistent source agent returns 403.

        #186 enumeration-safety: "source not found" (was 400) is collapsed into
        the "not permitted" 403 so the body's source_agent can't be used as an
        existence oracle (commit 34ddf71a). Mirrors the [403, 404] sibling tests.
        """
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/event-subscriptions",
            json={
                "source_agent": "nonexistent-agent-xyz",
                "event_type": "test.event",
                "target_message": "Test",
            },
        )
        assert_status(response, 403)

    def test_create_subscription_missing_fields(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Create subscription without required fields returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/event-subscriptions",
            json={"source_agent": created_agent["name"]},
        )
        assert_status(response, 422)

    def test_create_duplicate_subscription(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Creating duplicate subscription returns 409."""
        agent_name = created_agent["name"]
        sub_data = {
            "source_agent": agent_name,
            "event_type": "duplicate.test",
            "target_message": "First",
        }

        # Create first
        resp1 = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json=sub_data,
        )
        assert_status(resp1, 201)

        # Try duplicate
        resp2 = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json=sub_data,
        )
        assert_status(resp2, 409)

    def test_create_subscription_disabled(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Create subscription with enabled=false."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/event-subscriptions",
            json={
                "source_agent": created_agent["name"],
                "event_type": "disabled.test",
                "target_message": "This should not trigger",
                "enabled": False,
            },
        )
        assert_status(response, 201)
        data = response.json()
        assert data["enabled"] is False

    def test_create_subscription_for_nonexistent_subscriber(
        self, api_client: TrinityApiClient
    ):
        """Create subscription for nonexistent subscriber agent returns 403/404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/event-subscriptions",
            json={
                "source_agent": "some-agent",
                "event_type": "test.event",
                "target_message": "Test",
            },
        )
        assert_status_in(response, [403, 404])


# ============================================================================
# Subscription List Tests
# ============================================================================

class TestListSubscriptions:
    """Tests for GET /api/agents/{name}/event-subscriptions endpoint."""

    def test_list_subscriptions_structure(
        self, api_client: TrinityApiClient, created_agent
    ):
        """List subscriptions returns expected structure."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/event-subscriptions"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["count", "subscriptions"])
        assert isinstance(data["count"], int)
        assert isinstance(data["subscriptions"], list)

    def test_list_subscriptions_as_subscriber(
        self, api_client: TrinityApiClient, created_agent
    ):
        """List with direction=subscriber returns subscriptions this agent has."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/event-subscriptions?direction=subscriber"
        )
        assert_status(response, 200)
        data = response.json()
        for sub in data["subscriptions"]:
            assert sub["subscriber_agent"] == created_agent["name"]

    def test_list_subscriptions_as_source(
        self, api_client: TrinityApiClient, created_agent
    ):
        """List with direction=source returns subscriptions to this agent's events."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/event-subscriptions?direction=source"
        )
        assert_status(response, 200)
        data = response.json()
        for sub in data["subscriptions"]:
            assert sub["source_agent"] == created_agent["name"]

    def test_list_subscriptions_both(
        self, api_client: TrinityApiClient, created_agent
    ):
        """List with direction=both returns all related subscriptions."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/event-subscriptions?direction=both"
        )
        assert_status(response, 200)


# ============================================================================
# Subscription Get/Update/Delete Tests
# ============================================================================

class TestSubscriptionCRUD:
    """Tests for individual subscription CRUD operations."""

    def test_get_subscription_by_id(
        self, api_client: TrinityApiClient, created_agent
    ):
        """GET /api/event-subscriptions/{id} returns the subscription."""
        agent_name = created_agent["name"]

        # Create a subscription
        create_resp = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "crud.get_test",
                "target_message": "Test get",
            },
        )
        assert_status(create_resp, 201)
        sub_id = create_resp.json()["id"]

        # Get it
        response = api_client.get(f"/api/event-subscriptions/{sub_id}")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == sub_id
        assert data["event_type"] == "crud.get_test"

    def test_get_nonexistent_subscription(self, api_client: TrinityApiClient):
        """GET /api/event-subscriptions/{id} returns 404 for missing."""
        response = api_client.get("/api/event-subscriptions/esub_nonexistent123")
        assert_status(response, 404)

    def test_update_subscription(
        self, api_client: TrinityApiClient, created_agent
    ):
        """PUT /api/event-subscriptions/{id} updates fields."""
        agent_name = created_agent["name"]

        # Create
        create_resp = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "crud.update_test",
                "target_message": "Original message",
            },
        )
        assert_status(create_resp, 201)
        sub_id = create_resp.json()["id"]

        # Update
        response = api_client.put(
            f"/api/event-subscriptions/{sub_id}",
            json={
                "target_message": "Updated message",
                "enabled": False,
            },
        )
        assert_status(response, 200)
        data = response.json()
        assert data["target_message"] == "Updated message"
        assert data["enabled"] is False

    def test_update_nonexistent_subscription(self, api_client: TrinityApiClient):
        """PUT /api/event-subscriptions/{id} returns 404 for missing."""
        response = api_client.put(
            "/api/event-subscriptions/esub_nonexistent123",
            json={"enabled": False},
        )
        assert_status(response, 404)

    def test_delete_subscription(
        self, api_client: TrinityApiClient, created_agent
    ):
        """DELETE /api/event-subscriptions/{id} removes subscription."""
        agent_name = created_agent["name"]

        # Create
        create_resp = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "crud.delete_test",
                "target_message": "To be deleted",
            },
        )
        assert_status(create_resp, 201)
        sub_id = create_resp.json()["id"]

        # Delete
        response = api_client.delete(f"/api/event-subscriptions/{sub_id}")
        assert_status(response, 200)
        data = response.json()
        assert data["status"] == "deleted"

        # Verify gone
        get_resp = api_client.get(f"/api/event-subscriptions/{sub_id}")
        assert_status(get_resp, 404)

    def test_delete_nonexistent_subscription(self, api_client: TrinityApiClient):
        """DELETE /api/event-subscriptions/{id} returns 404 for missing."""
        response = api_client.delete("/api/event-subscriptions/esub_nonexistent123")
        assert_status(response, 404)


# ============================================================================
# Event Trigger Tests (emit + subscription matching)
# ============================================================================

class TestEventTrigger:
    """Tests for event emission triggering matching subscriptions."""

    def test_emit_event_counts_matching_subscriptions(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Emitting event reports correct subscriptions_triggered count."""
        agent_name = created_agent["name"]

        # Create a subscription (self-subscription)
        api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "trigger.count_test",
                "target_message": "Triggered by {{payload.id}}",
            },
        )

        # Emit matching event
        response = api_client.post(
            f"/api/agents/{agent_name}/emit-event",
            json={
                "event_type": "trigger.count_test",
                "payload": {"id": "test-123"},
            },
        )
        assert_status(response, 201)
        data = response.json()
        assert data["subscriptions_triggered"] >= 1

    def test_emit_event_no_match(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Emitting event with no matching subscriptions returns 0."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/emit-event",
            json={"event_type": "no_match.event_xyz"},
        )
        assert_status(response, 201)
        data = response.json()
        assert data["subscriptions_triggered"] == 0

    def test_disabled_subscription_not_triggered(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Disabled subscriptions are not triggered by events."""
        agent_name = created_agent["name"]

        # Create disabled subscription
        create_resp = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "trigger.disabled_test",
                "target_message": "Should not trigger",
                "enabled": False,
            },
        )
        assert_status(create_resp, 201)

        # Emit matching event
        response = api_client.post(
            f"/api/agents/{agent_name}/emit-event",
            json={"event_type": "trigger.disabled_test"},
        )
        assert_status(response, 201)
        data = response.json()
        assert data["subscriptions_triggered"] == 0


# ============================================================================
# Permission Gating Tests
# ============================================================================

class TestPermissionGating:
    """Tests for permission enforcement on event subscriptions."""

    pytestmark = pytest.mark.smoke

    def test_self_subscription_always_allowed(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Agent can always subscribe to its own events."""
        agent_name = created_agent["name"]
        response = api_client.post(
            f"/api/agents/{agent_name}/event-subscriptions",
            json={
                "source_agent": agent_name,
                "event_type": "perm.self_test",
                "target_message": "Self subscription",
            },
        )
        # 201 or 409 (if already exists from prior test) are both acceptable
        assert_status_in(response, [201, 409])
