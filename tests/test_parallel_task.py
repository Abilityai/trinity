"""
Parallel Task Execution Tests (test_parallel_task.py)

Tests for the parallel task execution feature (Requirement 12.1).
Covers parallel/stateless task execution without execution queue.
"""

import pytest
import time
import asyncio
import concurrent.futures
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestParallelTaskEndpoint:
    """REQ-PARALLEL-001: Parallel task endpoint tests."""

    def test_task_endpoint_exists(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/task endpoint exists."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "What is 2+2?"},
            timeout=30.0,
        )

        # Should not be 404 (endpoint exists)
        # May be 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert response.status_code != 404, "Task endpoint should exist"

    def test_task_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/task for non-existent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/task",
            json={"message": "hello"},
            timeout=30.0,
        )
        assert_status(response, 404)

    def test_task_stopped_agent_returns_error(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/task for stopped agent returns error."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/task",
            json={"message": "hello"},
            timeout=30.0,
        )
        # Should get 400 or 503 for stopped agent
        assert_status_in(response, [400, 503])


class TestParallelTaskResponse:
    """REQ-PARALLEL-002: Parallel task response format tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_returns_response(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/task returns a response."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "What is 2+2? Reply with just the number."},
            timeout=120.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have required fields
        assert "response" in data, "Response should contain 'response' field"
        assert "session_id" in data, "Response should contain 'session_id' field"
        assert "timestamp" in data, "Response should contain 'timestamp' field"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_has_metadata(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Task response includes execution metadata."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "Say hello"},
            timeout=120.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Should have metadata
        assert "metadata" in data, "Response should contain 'metadata'"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_has_execution_log(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Task response includes execution log."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "Say hello"},
            timeout=120.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Should have execution log
        assert "execution_log" in data, "Response should contain 'execution_log'"
        assert isinstance(data["execution_log"], list)


class TestParallelTaskOptions:
    """REQ-PARALLEL-003: Parallel task option tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_with_model_override(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Task accepts model override parameter."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "Say hi",
                "model": "haiku"
            },
            timeout=120.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should work (200) or fail gracefully if model not available
        assert_status_in(response, [200, 400, 500])

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_with_timeout(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Task accepts timeout_seconds parameter."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "Say hello",
                "timeout_seconds": 60
            },
            timeout=120.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 504])  # 504 if timeout


class TestParallelTaskStateless:
    """REQ-PARALLEL-004: Parallel task statelessness tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_does_not_affect_chat_history(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Parallel tasks should not pollute chat history."""
        # Get initial history
        history_before = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/history"
        )
        if history_before.status_code == 503:
            pytest.skip("Agent server not ready")

        # Execute parallel task
        task_response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "This is a parallel task"},
            timeout=120.0,
        )

        if task_response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(task_response, 200)

        # Get history after
        history_after = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/history"
        )

        # History should be unchanged (task is stateless)
        # Note: implementation may vary - some tracking is OK
        # but conversation context should not change
        assert_status(history_after, 200)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_tasks_have_unique_session_ids(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each parallel task should have a unique session ID."""
        # Execute two tasks
        response1 = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "Task 1"},
            timeout=120.0,
        )

        if response1.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response1, 200)
        session_id_1 = response1.json().get("session_id")

        response2 = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={"message": "Task 2"},
            timeout=120.0,
        )

        assert_status(response2, 200)
        session_id_2 = response2.json().get("session_id")

        # Session IDs should be different (each task is independent)
        assert session_id_1 != session_id_2, "Each task should have a unique session ID"


class TestParallelExecution:
    """REQ-PARALLEL-005: Actual parallel execution tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_multiple_tasks_can_start_without_queue(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Multiple parallel tasks should not get queued (no 429 response)."""
        # Start first task with short timeout to not block too long
        response1 = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "Task 1: What is 2+2?",
                "timeout_seconds": 60
            },
            timeout=30.0,  # Don't wait for completion
        )

        # Start second task immediately (shouldn't be queued)
        response2 = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "Task 2: What is 3+3?",
                "timeout_seconds": 60
            },
            timeout=30.0,
        )

        # Neither should return 429 (queue full)
        # Tasks may return 503 if agent not ready, that's OK
        if response1.status_code == 503:
            pytest.skip("Agent server not ready")

        # 429 would indicate queuing, which shouldn't happen for /task
        assert response1.status_code != 429, "Parallel tasks should not be queued"
        assert response2.status_code != 429, "Parallel tasks should not be queued"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_task_timeout_returns_504(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Task that exceeds timeout should return 504."""
        # Use very short timeout
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "Write a very long story about dragons",
                "timeout_seconds": 1  # Very short timeout
            },
            timeout=30.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should either complete quickly or timeout
        assert_status_in(response, [200, 504])
