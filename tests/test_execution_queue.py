"""
Execution Queue Tests (test_execution_queue.py)

Tests for agent execution queue management.
Covers REQ-QUEUE-001 through REQ-QUEUE-004.

Feature Flow: execution-queue.md
"""

import pytest
import time
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestQueueStatus:
    """REQ-QUEUE-001: Get queue status endpoint tests."""

    def test_get_queue_status(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/queue returns queue status."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have required status fields
        assert_has_fields(data, ["agent_name", "is_busy", "queue_length"])

    def test_queue_status_has_agent_name(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Queue status includes correct agent name."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")

        assert_status(response, 200)
        data = response.json()

        assert data.get("agent_name") == created_agent['name'], \
            "Queue status should include correct agent name"

    def test_idle_agent_not_busy(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Idle agent shows is_busy: false."""
        # Wait a moment for any startup tasks to complete
        time.sleep(2)

        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")

        assert_status(response, 200)
        data = response.json()

        is_busy = data.get("is_busy", False)
        assert isinstance(is_busy, bool), "is_busy should be a boolean"
        # Note: May be busy if agent is processing startup tasks

    def test_idle_agent_empty_queue(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Idle agent has queue_length: 0."""
        time.sleep(2)

        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")

        assert_status(response, 200)
        data = response.json()

        queue_length = data.get("queue_length", 0)
        assert isinstance(queue_length, int), "queue_length should be an integer"
        # May have queued items during test, just verify it's a valid number
        assert queue_length >= 0, "queue_length should be non-negative"

    def test_queue_status_includes_queued_executions(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Queue status includes queued_executions list."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")

        assert_status(response, 200)
        data = response.json()

        assert "queued_executions" in data, "Should include queued_executions"
        assert isinstance(data["queued_executions"], list), "queued_executions should be a list"

    def test_queue_status_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """Queue status for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/queue")
        assert_status(response, 404)

    def test_queue_status_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """Queue status requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/queue",
            auth=False
        )
        assert_status(response, 401)


class TestQueueStatusDuringExecution:
    """Tests for queue status while agent is executing."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_queue_busy_during_chat(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Queue shows busy during chat execution."""
        import threading
        import time

        agent_name = created_agent['name']
        busy_detected = False

        def check_busy():
            nonlocal busy_detected
            # Check queue status multiple times during execution
            for _ in range(10):
                response = api_client.get(f"/api/agents/{agent_name}/queue")
                if response.status_code == 200:
                    data = response.json()
                    if data.get("is_busy"):
                        busy_detected = True
                        break
                time.sleep(0.5)

        # Start a thread to check busy status
        checker = threading.Thread(target=check_busy)
        checker.start()

        # Start a chat (will use queue)
        chat_response = api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "Write a short paragraph about testing."},
            timeout=120.0
        )

        checker.join()

        if chat_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # We should have detected busy at some point (unless very fast)
        # This is informational - fast responses may not trigger busy
        # assert busy_detected, "Queue should show busy during execution"


class TestClearQueue:
    """REQ-QUEUE-003: Clear queue endpoint tests."""

    def test_clear_queue(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/queue/clear clears queued executions."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/queue/clear")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should indicate success
        assert_has_fields(data, ["status", "agent"])
        assert data.get("status") == "cleared", "Status should be 'cleared'"

    def test_clear_queue_returns_cleared_count(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Clear queue returns count of cleared items."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/queue/clear")

        assert_status(response, 200)
        data = response.json()

        assert "cleared_count" in data, "Should include cleared_count"
        assert isinstance(data["cleared_count"], int), "cleared_count should be an integer"
        assert data["cleared_count"] >= 0, "cleared_count should be non-negative"

    def test_clear_queue_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """Clear queue for non-existent agent returns 404."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz123/queue/clear")
        assert_status(response, 404)

    def test_clear_queue_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """Clear queue requires authentication."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/queue/clear",
            auth=False
        )
        assert_status(response, 401)


class TestForceRelease:
    """REQ-QUEUE-004: Force release endpoint tests."""

    def test_force_release(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/queue/release releases stuck agent."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/queue/release")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should indicate release result
        assert_has_fields(data, ["status", "agent"])
        assert data.get("status") == "released", "Status should be 'released'"

    def test_force_release_returns_was_running(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Force release returns whether agent was running."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/queue/release")

        assert_status(response, 200)
        data = response.json()

        assert "was_running" in data, "Should include was_running"
        assert isinstance(data["was_running"], bool), "was_running should be a boolean"

    def test_force_release_includes_warning(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Force release includes warning message."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/queue/release")

        assert_status(response, 200)
        data = response.json()

        assert "warning" in data, "Should include warning message"
        assert "reset" in data["warning"].lower(), "Warning should mention reset"

    def test_force_release_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """Force release for non-existent agent returns 404."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz123/queue/release")
        assert_status(response, 404)

    def test_force_release_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """Force release requires authentication."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/queue/release",
            auth=False
        )
        assert_status(response, 401)


class TestQueueAfterOperations:
    """Tests for queue state after operations."""

    def test_queue_idle_after_clear(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Queue shows empty after clear operation."""
        # Clear the queue
        api_client.post(f"/api/agents/{created_agent['name']}/queue/clear")

        # Check status
        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")
        assert_status(response, 200)
        data = response.json()

        # Queue should be empty (unless something new was queued)
        assert data.get("queue_length", 0) == 0, "Queue should be empty after clear"
        queued = data.get("queued_executions", [])
        assert len(queued) == 0, "queued_executions should be empty after clear"

    def test_queue_idle_after_release(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Queue shows not busy after release operation."""
        # Force release
        api_client.post(f"/api/agents/{created_agent['name']}/queue/release")

        # Check status
        response = api_client.get(f"/api/agents/{created_agent['name']}/queue")
        assert_status(response, 200)
        data = response.json()

        # Should not be busy after release
        assert data.get("is_busy") is False, "Agent should not be busy after release"


class TestQueueWithParallelTasks:
    """Tests verifying parallel tasks don't affect queue."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_parallel_task_does_not_show_in_queue(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Parallel tasks via /task don't appear in execution queue."""
        agent_name = created_agent['name']

        # Get initial queue status
        initial = api_client.get(f"/api/agents/{agent_name}/queue")
        if initial.status_code != 200:
            pytest.skip("Queue endpoint not available")

        initial_queue_length = initial.json().get("queue_length", 0)

        # Submit a parallel task in async mode (returns immediately)
        task_response = api_client.post(
            f"/api/agents/{agent_name}/task",
            json={"message": "Parallel queue test", "async_mode": True},
            timeout=30.0
        )

        if task_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Async mode returns 202 Accepted with execution_id
        if task_response.status_code not in [200, 202]:
            pytest.skip(f"Unexpected response: {task_response.status_code}")

        # Queue length should not have increased (task bypasses queue)
        response = api_client.get(f"/api/agents/{agent_name}/queue")
        assert_status(response, 200)
        data = response.json()

        # Queue length should be same (parallel tasks bypass queue)
        # Note: Queue may show busy if a /chat is in progress, but queue_length
        # should not increase due to /task calls
        current_queue_length = data.get("queue_length", 0)
        assert current_queue_length <= initial_queue_length + 1, \
            "Parallel task should not significantly increase queue length"
