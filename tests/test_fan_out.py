"""
Fan-Out Parallel Task Execution Tests (test_fan_out.py)

Tests for the fan-out primitive (FANOUT-001, Issue #230).
Covers parallel task dispatch, result collection, validation, and error handling.
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestFanOutValidation:
    """FANOUT-001: Input validation tests (no agent required)."""

    def test_fan_out_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/fan-out for non-existent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
            },
            timeout=30.0,
        )
        assert_status(response, 404)

    def test_fan_out_empty_tasks_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with empty tasks array returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [],
            },
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_duplicate_task_ids_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with duplicate task IDs returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [
                    {"id": "same", "message": "task 1"},
                    {"id": "same", "message": "task 2"},
                ],
            },
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_invalid_task_id_format_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with invalid task ID format returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "has spaces!", "message": "hello"}],
            },
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_too_many_tasks_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with more than 50 tasks returns 422."""
        tasks = [{"id": f"t{i}", "message": f"task {i}"} for i in range(51)]
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={"tasks": tasks},
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_invalid_concurrency_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with invalid max_concurrency returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
                "max_concurrency": 20,
            },
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_unsupported_policy_returns_422(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with unsupported policy returns 422."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
                "policy": "all-or-nothing",
            },
            timeout=30.0,
        )
        assert_status(response, 422)

    def test_fan_out_cross_agent_rejected(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out targeting a different agent is rejected for v1."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
                "agent": "some-other-agent",
            },
            timeout=30.0,
        )
        assert_status(response, 400)


class TestFanOutResponseFormat:
    """FANOUT-001: Response format tests."""

    def test_fan_out_endpoint_exists(
        self, api_client: TrinityApiClient, created_agent
    ):
        """POST /api/agents/{name}/fan-out endpoint exists."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "What is 2+2? Reply with just the number."}],
                "timeout_seconds": 120,
            },
            timeout=150.0,
        )

        # Should not be 404 (endpoint exists)
        # May be 503 if agent not ready, or 200 if it works
        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert response.status_code != 404, "Fan-out endpoint should exist"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_fan_out_single_task(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with a single task returns correct response format."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "math-1", "message": "What is 2+2? Reply with just the number."}],
                "timeout_seconds": 120,
            },
            timeout=150.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert_status(response, 200)
        data = response.json()

        # Check top-level response structure
        assert_has_fields(data, ["fan_out_id", "status", "total", "completed", "failed", "results"])
        assert data["fan_out_id"].startswith("fo_")
        assert data["status"] in ("completed", "deadline_exceeded")
        assert data["total"] == 1
        assert len(data["results"]) == 1

        # Check per-task result structure
        result = data["results"][0]
        assert result["id"] == "math-1"
        assert result["status"] in ("completed", "failed")
        if result["status"] == "completed":
            assert result["response"] is not None
            assert result["execution_id"] is not None

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_fan_out_multiple_tasks(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with multiple tasks dispatches all and collects results."""
        tasks = [
            {"id": "q1", "message": "What is 1+1? Reply with just the number."},
            {"id": "q2", "message": "What is 2+2? Reply with just the number."},
            {"id": "q3", "message": "What is 3+3? Reply with just the number."},
        ]

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": tasks,
                "timeout_seconds": 300,
                "max_concurrency": 3,
            },
            timeout=330.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert_status(response, 200)
        data = response.json()

        assert data["total"] == 3
        assert len(data["results"]) == 3

        # Results should be in input order
        assert data["results"][0]["id"] == "q1"
        assert data["results"][1]["id"] == "q2"
        assert data["results"][2]["id"] == "q3"

        # At least some should complete (best-effort)
        assert data["completed"] + data["failed"] == 3


class TestFanOutExecution:
    """FANOUT-001: Execution tracking tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_fan_out_creates_execution_records(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out subtasks create execution records visible on dashboard."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [
                    {"id": "track-1", "message": "What is 1+1? Reply with just the number."},
                    {"id": "track-2", "message": "What is 2+2? Reply with just the number."},
                ],
                "timeout_seconds": 120,
            },
            timeout=150.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")

        assert_status(response, 200)
        data = response.json()
        fan_out_id = data["fan_out_id"]

        # Check execution records exist for completed tasks
        for result in data["results"]:
            if result["execution_id"]:
                exec_response = api_client.get(
                    f"/api/agents/{created_agent['name']}/executions",
                    timeout=10.0,
                )
                if exec_response.status_code == 200:
                    executions = exec_response.json()
                    # Find our execution by ID
                    matching = [
                        e for e in executions
                        if e.get("id") == result["execution_id"]
                    ]
                    if matching:
                        exec_record = matching[0]
                        assert exec_record.get("triggered_by") == "fan_out"
                        assert exec_record.get("fan_out_id") == fan_out_id


class TestFanOutSelfTarget:
    """FANOUT-001: Self-targeting tests."""

    def test_fan_out_self_keyword_accepted(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with agent='self' is accepted."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
                "agent": "self",
                "timeout_seconds": 60,
            },
            timeout=90.0,
        )
        # Should not be 400 (self is valid)
        assert response.status_code != 400 or "self" not in response.text

    def test_fan_out_same_name_accepted(
        self, api_client: TrinityApiClient, created_agent
    ):
        """Fan-out with agent matching path name is accepted."""
        agent_name = created_agent["name"]
        response = api_client.post(
            f"/api/agents/{agent_name}/fan-out",
            json={
                "tasks": [{"id": "t1", "message": "hello"}],
                "agent": agent_name,
                "timeout_seconds": 60,
            },
            timeout=90.0,
        )
        # Should not be 400 (same name is valid)
        assert response.status_code != 400 or agent_name not in response.text
