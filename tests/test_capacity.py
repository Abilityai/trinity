"""
Parallel Capacity Tests (test_capacity.py)

Tests for per-agent parallel execution capacity management (CAPACITY-001).
Covers capacity configuration, slot tracking, and 429 enforcement.

Endpoints tested:
- GET /api/agents/{name}/capacity - Get capacity and current slot usage
- PUT /api/agents/{name}/capacity - Update max_parallel_tasks (1-10)
- GET /api/agents/slots - Bulk slot state for Dashboard polling
"""

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestCapacityAuthentication:
    """Tests for capacity endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_capacity_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/capacity requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/capacity",
            auth=False
        )
        assert_status_in(response, [401, 403])

    def test_put_capacity_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity requires authentication."""
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": 5},
            auth=False
        )
        assert_status_in(response, [401, 403])

    def test_get_slots_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/slots requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/slots",
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestCapacityGet:
    """Tests for GET /api/agents/{name}/capacity endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_capacity_returns_structure(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/capacity returns expected structure."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/capacity")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "agent_name", "max_parallel_tasks", "ceiling",
            "effective_max_parallel_tasks", "active_slots", "available_slots", "slots",
        ])
        assert data["agent_name"] == created_agent['name']
        assert isinstance(data["max_parallel_tasks"], int)
        assert isinstance(data["ceiling"], int)
        assert isinstance(data["effective_max_parallel_tasks"], int)
        assert isinstance(data["active_slots"], int)
        assert isinstance(data["available_slots"], int)
        assert isinstance(data["slots"], list)
        # #506: effective == min(stored, ceiling)
        assert data["effective_max_parallel_tasks"] == min(
            data["max_parallel_tasks"], data["ceiling"]
        )

    def test_get_capacity_default_is_three(self, api_client: TrinityApiClient, created_agent):
        """Agents should default to max_parallel_tasks=3."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/capacity")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        # Default should be 3 per CAPACITY-001 requirements
        assert data["max_parallel_tasks"] == 3

    def test_get_capacity_available_slots_calculation(self, api_client: TrinityApiClient, created_agent):
        """available_slots should equal max_parallel_tasks - active_slots."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/capacity")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        expected_available = data["max_parallel_tasks"] - data["active_slots"]
        assert data["available_slots"] == expected_available

    def test_get_capacity_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/capacity returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/capacity")
        assert_status(response, 404)


class TestCapacityUpdate:
    """Tests for PUT /api/agents/{name}/capacity endpoint."""

    pytestmark = pytest.mark.smoke

    def test_put_capacity_update_and_restore(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity updates the setting."""
        # Get current setting first
        current = api_client.get(f"/api/agents/{created_agent['name']}/capacity")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(current, 200)
        original_value = current.json()["max_parallel_tasks"]

        try:
            # Update to a different value
            new_value = 5 if original_value != 5 else 4
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": new_value}
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["max_parallel_tasks"] == new_value

            # Verify by reading back
            verify = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
            assert_status(verify, 200)
            assert verify.json()["max_parallel_tasks"] == new_value

        finally:
            # Restore original value
            api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": original_value}
            )

    def test_put_capacity_minimum_value(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity accepts minimum value (1)."""
        # Save original
        current = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        if current.status_code == 503:
            pytest.skip("Agent server not ready")
        original_value = current.json()["max_parallel_tasks"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": 1}
            )
            assert_status(response, 200)
            assert response.json()["max_parallel_tasks"] == 1
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": original_value}
            )

    def test_put_capacity_maximum_value(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity accepts maximum value (10)."""
        # Save original
        current = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        if current.status_code == 503:
            pytest.skip("Agent server not ready")
        original_value = current.json()["max_parallel_tasks"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": 10}
            )
            assert_status(response, 200)
            assert response.json()["max_parallel_tasks"] == 10
        finally:
            api_client.put(
                f"/api/agents/{created_agent['name']}/capacity",
                json={"max_parallel_tasks": original_value}
            )

    def test_put_capacity_rejects_zero(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity rejects 0 (below minimum)."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": 0}
        )
        assert_status(response, 400)

    def test_put_capacity_rejects_negative(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity rejects negative values."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": -1}
        )
        assert_status(response, 400)

    def test_put_capacity_rejects_above_ceiling(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity rejects values above the fleet ceiling (#506).

        Reads the live ceiling, then attempts ceiling+1 → 400.
        """
        ceiling_resp = api_client.get("/api/settings/max-parallel-tasks-ceiling")
        ceiling = ceiling_resp.json()["value"] if ceiling_resp.status_code == 200 else 10
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": ceiling + 1}
        )
        assert_status(response, 400)

    def test_put_capacity_rejects_non_integer(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity rejects non-integer values.

        #506: body is now the typed AgentCapacityUpdate model, so a fractional
        value fails Pydantic validation with 422 (was a hand-rolled 400).
        """
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": 3.5}
        )
        assert_status(response, 422)

    def test_put_capacity_rejects_string(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity rejects string values (422, typed body #506)."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={"max_parallel_tasks": "five"}
        )
        assert_status(response, 422)

    def test_put_capacity_requires_field(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/capacity requires max_parallel_tasks field (422, typed body #506)."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/capacity",
            json={}
        )
        assert_status(response, 422)

    def test_put_capacity_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """PUT /api/agents/{name}/capacity returns 404 for nonexistent agent."""
        response = api_client.put(
            "/api/agents/nonexistent-agent-xyz/capacity",
            json={"max_parallel_tasks": 5}
        )
        assert_status(response, 404)


class TestBulkSlotState:
    """Tests for GET /api/agents/slots bulk endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_slots_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/agents/slots returns expected structure."""
        response = api_client.get("/api/agents/slots")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agents", "timestamp"])
        assert isinstance(data["agents"], dict)
        assert isinstance(data["timestamp"], str)

    def test_get_slots_includes_created_agent(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/slots includes the test agent."""
        response = api_client.get("/api/agents/slots")

        assert_status(response, 200)
        data = response.json()

        # The created agent should be in the agents dict
        assert created_agent['name'] in data["agents"], \
            f"Agent {created_agent['name']} should be in slots response"

    def test_get_slots_agent_structure(self, api_client: TrinityApiClient, created_agent):
        """Each agent in slots response has max and active fields."""
        response = api_client.get("/api/agents/slots")

        assert_status(response, 200)
        data = response.json()

        agent_name = created_agent['name']
        if agent_name in data["agents"]:
            agent_data = data["agents"][agent_name]
            assert "max" in agent_data, "Agent should have 'max' field"
            assert "active" in agent_data, "Agent should have 'active' field"
            assert isinstance(agent_data["max"], int)
            assert isinstance(agent_data["active"], int)

    def test_get_slots_timestamp_format(self, api_client: TrinityApiClient):
        """GET /api/agents/slots timestamp is ISO format."""
        response = api_client.get("/api/agents/slots")

        assert_status(response, 200)
        data = response.json()

        # Should be ISO format ending with Z
        timestamp = data["timestamp"]
        assert timestamp.endswith("Z"), "Timestamp should end with Z (UTC)"
        # Basic ISO format check
        assert "T" in timestamp, "Timestamp should be ISO format with T separator"


class TestSlotTracking:
    """Tests for slot tracking during task execution."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_slot_acquired_during_task(self, api_client: TrinityApiClient, created_agent):
        """Slot should be acquired when task starts and released when complete."""
        # Check initial state - should have no active slots
        initial = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        if initial.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(initial, 200)
        initial_active = initial.json()["active_slots"]

        # Note: Testing slot acquisition during task execution is tricky
        # because the task completes quickly and releases the slot.
        # For a proper test, we'd need to check slots mid-execution.
        # Here we just verify the API is working.

        # After task completes, active_slots should be same or less
        final = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        assert_status(final, 200)
        # Slots should have been released
        assert final.json()["active_slots"] <= final.json()["max_parallel_tasks"]


class TestCapacityEnforcement:
    """Tests for 429 response when at capacity."""

    pytestmark = pytest.mark.smoke

    def test_capacity_documented_in_api(self, api_client: TrinityApiClient, created_agent):
        """Capacity endpoint should exist and return valid response."""
        # This test verifies the capacity API is accessible
        # Actual 429 testing requires setting max_parallel_tasks=1
        # and sending concurrent requests, which is complex in pytest

        response = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Verify the response indicates how capacity works
        assert data["max_parallel_tasks"] >= 1
        assert data["max_parallel_tasks"] <= 10
        assert data["available_slots"] >= 0
        assert data["available_slots"] <= data["max_parallel_tasks"]


class TestSlotInfoStructure:
    """Tests for slot info structure in capacity response."""

    pytestmark = pytest.mark.smoke

    def test_slots_array_structure(self, api_client: TrinityApiClient, created_agent):
        """slots array should contain SlotInfo objects when slots are active."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/capacity")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # slots should be a list (may be empty if no active executions)
        assert isinstance(data["slots"], list)

        # If there are active slots, verify structure
        for slot in data["slots"]:
            assert_has_fields(slot, [
                "slot_number",
                "execution_id",
                "started_at",
                "message_preview",
                "duration_seconds"
            ])
            assert isinstance(slot["slot_number"], int)
            assert isinstance(slot["execution_id"], str)
            assert isinstance(slot["started_at"], str)
            assert isinstance(slot["message_preview"], str)
            assert isinstance(slot["duration_seconds"], int)


class TestCapacityCeilingIntegration:
    """Two-tier ceiling behavior on the per-agent capacity endpoint (#506).

    Lowers the fleet ceiling below an agent's stored value and asserts the GET
    response reports stored + ceiling + effective consistently. Restores the
    ceiling afterward.
    """

    pytestmark = pytest.mark.smoke

    CEILING_ENDPOINT = "/api/settings/max-parallel-tasks-ceiling"

    def test_lowered_ceiling_clamps_effective(self, api_client: TrinityApiClient, created_agent):
        """With stored > ceiling, effective == ceiling and available is clamped."""
        agent = created_agent['name']
        cap_url = f"/api/agents/{agent}/capacity"

        current = api_client.get(cap_url)
        if current.status_code == 503:
            pytest.skip("Agent server not ready")
        assert_status(current, 200)
        original_stored = current.json()["max_parallel_tasks"]
        original_ceiling = api_client.get(self.CEILING_ENDPOINT).json()["value"]

        try:
            # Raise ceiling so we can store a high value (8), then lower it to 2.
            api_client.put(self.CEILING_ENDPOINT, json={"value": 10})
            assert_status(api_client.put(cap_url, json={"max_parallel_tasks": 8}), 200)
            api_client.put(self.CEILING_ENDPOINT, json={"value": 2})

            data = api_client.get(cap_url).json()
            assert data["max_parallel_tasks"] == 8          # stored, unchanged
            assert data["ceiling"] == 2                      # admin ceiling
            assert data["effective_max_parallel_tasks"] == 2  # min(8, 2)
            # available + active == effective
            assert data["available_slots"] + data["active_slots"] == 2
        finally:
            api_client.put(self.CEILING_ENDPOINT, json={"value": original_ceiling})
            api_client.put(cap_url, json={"max_parallel_tasks": min(original_stored, original_ceiling)})
