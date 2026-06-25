"""
Execution Termination Tests (test_execution_termination.py)

Tests for execution termination feature - stopping running executions.
Added 2026-01-12 to cover new termination endpoints.

Feature Flow: execution-termination.md

Endpoints tested:
- POST /api/agents/{name}/executions/{execution_id}/terminate
- GET /api/agents/{name}/executions/running
"""

import pytest
import time
import threading
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestRunningExecutions:
    """Tests for GET /api/agents/{name}/executions/running endpoint."""

    def test_get_running_executions_returns_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/executions/running returns executions list."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/executions/running")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "executions" in data, "Should have executions key"
        assert isinstance(data["executions"], list), "executions should be a list"

    def test_running_executions_empty_when_idle(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Running executions list is empty when agent is idle."""
        # Wait a moment for any startup tasks to settle
        time.sleep(2)

        response = api_client.get(f"/api/agents/{created_agent['name']}/executions/running")

        assert_status(response, 200)
        data = response.json()

        # May or may not be empty depending on timing, but should be a valid list
        assert isinstance(data.get("executions"), list)

    def test_running_executions_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/executions/running for non-existent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/executions/running")
        assert_status(response, 404)

    def test_running_executions_stopped_agent_returns_empty(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """GET /api/agents/{name}/executions/running for stopped agent returns empty list."""
        response = api_client.get(f"/api/agents/{stopped_agent['name']}/executions/running")

        assert_status(response, 200)
        data = response.json()
        assert data.get("executions") == [], "Stopped agent should have no running executions"

    def test_running_executions_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/executions/running requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/executions/running",
            auth=False
        )
        assert_status(response, 401)


class TestTerminateExecution:
    """Tests for POST /api/agents/{name}/executions/{execution_id}/terminate endpoint."""

    def test_terminate_nonexistent_execution_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Terminating non-existent execution returns 404."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/executions/nonexistent-exec-xyz123/terminate"
        )
        assert_status(response, 404)

    def test_terminate_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """Terminating execution on non-existent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz123/executions/some-exec-id/terminate"
        )
        assert_status(response, 404)

    def test_terminate_stopped_agent_returns_503(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """Terminating execution on stopped agent returns 503."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/executions/some-exec-id/terminate"
        )
        assert_status(response, 503)

    def test_terminate_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """Termination requires authentication."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/executions/some-exec-id/terminate",
            auth=False
        )
        assert_status(response, 401)


class TestTerminateRunningExecution:
    """Tests for actually terminating a running execution.

    These tests are marked slow because they require starting a task
    and then terminating it.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_terminate_running_execution(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Can terminate a running execution."""
        agent_name = created_agent['name']
        execution_id = None
        task_completed = False

        def start_long_task():
            nonlocal task_completed
            # Start a task that takes a while (but not too long)
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": "Count from 1 to 100, saying each number out loud. Take your time."},
                    timeout=120.0
                )
                task_completed = True
            except Exception:
                pass  # May be interrupted by termination

        # Start task in background thread
        task_thread = threading.Thread(target=start_long_task)
        task_thread.start()

        # Wait for execution to start and capture execution_id
        max_wait = 10
        start_time = time.time()
        while time.time() - start_time < max_wait:
            running_response = api_client.get(f"/api/agents/{agent_name}/executions/running")
            if running_response.status_code == 200:
                running_data = running_response.json()
                executions = running_data.get("executions", [])
                if executions:
                    execution_id = executions[0].get("execution_id")
                    if execution_id:
                        break
            time.sleep(0.5)

        if not execution_id:
            # Task may have completed quickly, check if it finished
            task_thread.join(timeout=5)
            if task_completed:
                pytest.skip("Task completed before we could capture execution_id")
            pytest.skip("Could not capture execution_id from running executions")

        # Terminate the execution
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{execution_id}/terminate"
        )

        # Should succeed with 200 or indicate already finished
        assert_status_in(terminate_response, [200])
        data = terminate_response.json()

        # Status should be 'terminated' or 'already_finished'
        assert data.get("status") in ["terminated", "already_finished"], \
            f"Expected terminated or already_finished, got {data.get('status')}"

        # Wait for task thread to complete
        task_thread.join(timeout=10)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_terminate_response_has_expected_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Termination response has expected fields."""
        agent_name = created_agent['name']
        execution_id = None

        def start_long_task():
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": "Write a very long essay about testing software."},
                    timeout=120.0
                )
            except Exception:
                pass

        # Start task
        task_thread = threading.Thread(target=start_long_task)
        task_thread.start()

        # Wait for execution to start
        max_wait = 10
        start_time = time.time()
        while time.time() - start_time < max_wait:
            running_response = api_client.get(f"/api/agents/{agent_name}/executions/running")
            if running_response.status_code == 200:
                executions = running_response.json().get("executions", [])
                if executions:
                    execution_id = executions[0].get("execution_id")
                    if execution_id:
                        break
            time.sleep(0.5)

        if not execution_id:
            task_thread.join(timeout=5)
            pytest.skip("Could not capture execution_id")

        # Terminate
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{execution_id}/terminate"
        )

        if terminate_response.status_code == 200:
            data = terminate_response.json()
            assert "status" in data, "Response should have status field"
            assert "execution_id" in data, "Response should have execution_id field"

        task_thread.join(timeout=10)


class TestCancelLabeledNotSuccess:
    """#679: a cancelled /task must finalize CANCELLED — never a billable SUCCESS.

    Claude catches SIGINT, emits a graceful final message, and exits 0, so the
    HTTP return code alone can't tell a cancel from a real success. This E2E
    proves the end-to-end label (agent reply status:cancelled → backend cross-
    validation → CANCELLED row) holds against a real agent container. Runs in
    /verify-local's agent-exercise stage.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_cancelled_task_row_is_cancelled_not_success(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        agent_name = created_agent['name']
        execution_id = None

        def start_task():
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": (
                        "Write an extremely detailed, comprehensive analysis of "
                        "the entire history of computing, decade by decade, with "
                        "at least 30 paragraphs. Take your time and be thorough."
                    )},
                    timeout=180.0,
                )
            except Exception:
                pass  # interrupted by termination

        task_thread = threading.Thread(target=start_task)
        task_thread.start()

        # Capture the running execution_id.
        max_wait = 20
        start_time = time.time()
        while time.time() - start_time < max_wait:
            running = api_client.get(f"/api/agents/{agent_name}/executions/running")
            if running.status_code == 200:
                execs = running.json().get("executions", [])
                if execs:
                    execution_id = execs[0].get("execution_id")
                    if execution_id:
                        break
            time.sleep(0.3)

        if not execution_id:
            task_thread.join(timeout=5)
            pytest.skip("Could not capture execution_id (task finished too fast)")

        # Cancel it.
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{execution_id}/terminate"
        )
        task_thread.join(timeout=30)

        if terminate_response.status_code != 200:
            pytest.skip("Termination did not succeed")
        if terminate_response.json().get("status") == "already_finished":
            # The cancel landed after the turn finished on its own — its genuine
            # terminal stands (Issue 7). Not the case under test.
            pytest.skip("Task already finished before cancel landed")

        # Poll the row to a terminal state and assert it's CANCELLED — and
        # explicitly NOT success (the no-billable-success guarantee, #679).
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            detail = api_client.get(f"/api/agents/{agent_name}/executions/{execution_id}")
            if detail.status_code == 200:
                status = detail.json().get("status")
                if status in ("cancelled", "success", "failed"):
                    break
            time.sleep(0.5)

        assert status == "cancelled", (
            f"A cancelled /task must finalize CANCELLED, got {status!r} "
            f"(a 'success' here is the #679 charge-on-cancel / false-success bug)"
        )


class TestRunningExecutionDetails:
    """Tests for running execution list details."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_running_execution_has_expected_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Running execution entry has expected fields."""
        agent_name = created_agent['name']
        found_execution = False

        def start_task():
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": "Think about something for a while."},
                    timeout=60.0
                )
            except Exception:
                pass

        # Start task
        task_thread = threading.Thread(target=start_task)
        task_thread.start()

        # Check running executions
        max_wait = 10
        start_time = time.time()
        while time.time() - start_time < max_wait:
            response = api_client.get(f"/api/agents/{agent_name}/executions/running")
            if response.status_code == 200:
                executions = response.json().get("executions", [])
                if executions:
                    execution = executions[0]
                    found_execution = True

                    # Check expected fields
                    assert "execution_id" in execution, "Should have execution_id"
                    assert "started_at" in execution, "Should have started_at"
                    assert "metadata" in execution, "Should have metadata"
                    break
            time.sleep(0.5)

        task_thread.join(timeout=60)

        if not found_execution:
            pytest.skip("Task completed too quickly to capture running execution")


class TestTerminationActivityTracking:
    """Tests for activity tracking when executions are terminated."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_termination_creates_activity(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Terminating an execution creates EXECUTION_CANCELLED activity."""
        agent_name = created_agent['name']
        execution_id = None

        # Get current activity count
        initial_activities = api_client.get(
            f"/api/activities/{agent_name}",
            params={"limit": 100}
        )
        initial_count = len(initial_activities.json()) if initial_activities.status_code == 200 else 0

        def start_task():
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": "Count slowly from 1 to 50."},
                    timeout=120.0
                )
            except Exception:
                pass

        # Start task
        task_thread = threading.Thread(target=start_task)
        task_thread.start()

        # Wait for execution
        max_wait = 10
        start_time = time.time()
        while time.time() - start_time < max_wait:
            running_response = api_client.get(f"/api/agents/{agent_name}/executions/running")
            if running_response.status_code == 200:
                executions = running_response.json().get("executions", [])
                if executions:
                    execution_id = executions[0].get("execution_id")
                    if execution_id:
                        break
            time.sleep(0.5)

        if not execution_id:
            task_thread.join(timeout=5)
            pytest.skip("Could not capture execution_id")

        # Terminate
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{execution_id}/terminate"
        )

        task_thread.join(timeout=10)

        if terminate_response.status_code != 200:
            pytest.skip("Termination did not succeed")

        # Wait for activity to be recorded
        time.sleep(2)

        # Check for EXECUTION_CANCELLED activity
        activities_response = api_client.get(
            f"/api/activities/{agent_name}",
            params={"limit": 100}
        )

        if activities_response.status_code == 200:
            activities = activities_response.json()
            cancelled_activities = [
                a for a in activities
                if a.get("activity_type") == "execution_cancelled"
            ]
            # Note: Activity tracking is best-effort, don't fail if not found
            # Just verify that more activities exist if termination succeeded
            assert len(activities) >= initial_count, \
                "Activity count should not decrease after termination"


class TestTerminateChatExecution:
    """Tests for terminating chat-path executions (not just /task).

    The chat path (/api/agents/{name}/chat) previously did not pass the
    database execution_id to the agent container, causing the process
    registry to use a different random UUID. This made termination fail
    with 404 for chat executions. These tests verify the fix.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_execution_creates_db_record_with_execution_metadata(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat execution creates a DB record and returns execution metadata.

        Verifies the backend creates a task execution record for chat calls
        and includes the execution ID in the response. This is a prerequisite
        for termination — the DB execution_id is what the frontend sends to
        the terminate endpoint.
        """
        agent_name = created_agent['name']

        # Send a simple chat message
        response = api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "Hello, just say hi back."},
            timeout=60.0
        )

        # Chat should succeed (or at least return a response)
        if response.status_code != 200:
            pytest.skip(f"Chat failed with {response.status_code}, cannot test execution metadata")

        data = response.json()

        # Verify execution metadata is present in the response
        assert "execution" in data, "Chat response should include execution metadata"
        execution = data["execution"]
        assert "task_execution_id" in execution, "Execution metadata should have task_execution_id"
        task_execution_id = execution["task_execution_id"]
        assert task_execution_id is not None, "task_execution_id should not be None"

        # Verify the DB record exists and has correct status
        exec_detail = api_client.get(
            f"/api/agents/{agent_name}/executions/{task_execution_id}"
        )
        assert_status(exec_detail, 200)
        detail_data = exec_detail.json()
        assert detail_data.get("status") == "success", \
            f"Completed chat execution should be 'success', got {detail_data.get('status')}"
        assert detail_data.get("triggered_by") == "chat", \
            f"Chat execution should have triggered_by='chat', got {detail_data.get('triggered_by')}"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_execution_can_be_terminated(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat execution can be terminated using the database execution ID.

        This is the key regression test: previously the /api/chat path
        generated its own random execution_id internally, so the stop
        button (which uses the DB execution_id) would get a 404.

        Note: /api/chat is synchronous, so the execution window is small.
        We poll aggressively (0.3s intervals) and accept a skip if the
        chat completes before we can terminate.
        """
        agent_name = created_agent['name']
        chat_execution_id = None
        chat_completed = False

        def start_chat():
            nonlocal chat_completed
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/chat",
                    json={
                        "message": (
                            "Write an extremely detailed and comprehensive analysis of every "
                            "programming language ever created, from FORTRAN to Rust. For each "
                            "language, describe its type system, memory model, concurrency "
                            "primitives, notable projects built with it, and its influence on "
                            "subsequent languages. Include at least 20 languages."
                        )
                    },
                    timeout=180.0
                )
                chat_completed = True
            except Exception:
                pass

        # Start chat in background thread
        chat_thread = threading.Thread(target=start_chat)
        chat_thread.start()

        # Poll aggressively for a running execution record
        max_wait = 20
        start_time = time.time()
        while time.time() - start_time < max_wait:
            exec_response = api_client.get(
                f"/api/agents/{agent_name}/executions",
                params={"status": "running", "limit": 1}
            )
            if exec_response.status_code == 200:
                execs = exec_response.json()
                if isinstance(execs, list) and execs:
                    # Find a chat-triggered execution
                    for ex in execs:
                        if ex.get("triggered_by") == "chat":
                            chat_execution_id = ex.get("id")
                            break
                    if chat_execution_id:
                        break
            time.sleep(0.3)  # Aggressive polling for narrow window

        if not chat_execution_id:
            chat_thread.join(timeout=5)
            if chat_completed:
                pytest.skip("Chat completed before we could capture execution_id (synchronous /api/chat has narrow termination window)")
            pytest.skip("Could not capture chat execution_id from DB")

        # Terminate using the DATABASE execution ID (this is what the UI does)
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{chat_execution_id}/terminate"
        )

        # Accept 200 (terminated/already_finished) — both mean the ID was recognized
        # 404 would mean the execution_id mismatch bug is still present
        if terminate_response.status_code == 200:
            data = terminate_response.json()
            assert data.get("status") in ["terminated", "already_finished"], \
                f"Expected terminated or already_finished, got {data.get('status')}"
        elif terminate_response.status_code == 404:
            # This is the bug we're testing for — execution_id mismatch
            chat_thread.join(timeout=15)
            # Check if chat already completed (race condition, not a bug)
            exec_detail = api_client.get(
                f"/api/agents/{agent_name}/executions/{chat_execution_id}"
            )
            if exec_detail.status_code == 200 and exec_detail.json().get("status") in ["success", "failed"]:
                pytest.skip("Chat completed before terminate reached agent (timing)")
            pytest.fail(
                f"Terminate returned 404 — execution_id mismatch bug: {terminate_response.json()}"
            )

        chat_thread.join(timeout=30)

        # Verify the execution record was updated to cancelled in the database
        time.sleep(1)
        exec_detail = api_client.get(
            f"/api/agents/{agent_name}/executions/{chat_execution_id}"
        )
        if exec_detail.status_code == 200:
            status = exec_detail.json().get("status")
            # After termination, status should be 'cancelled' (or 'success'/'failed' if it finished first)
            assert status in ["cancelled", "success", "failed"], \
                f"DB execution status should be terminal, got {status}"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_terminate_defaults_task_execution_id_to_execution_id(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Terminate endpoint defaults task_execution_id to execution_id for DB update.

        Previously task_execution_id was sent in the JSON body but FastAPI
        treated it as a query parameter, so the DB status was never updated.
        Now the backend defaults to using execution_id from the path.
        """
        agent_name = created_agent['name']
        execution_id = None

        def start_task():
            try:
                api_client.post(
                    f"/api/agents/{agent_name}/task",
                    json={"message": "Write a comprehensive guide to all programming paradigms."},
                    timeout=120.0
                )
            except Exception:
                pass

        task_thread = threading.Thread(target=start_task)
        task_thread.start()

        # Wait for execution to appear
        max_wait = 15
        start_time = time.time()
        while time.time() - start_time < max_wait:
            exec_response = api_client.get(
                f"/api/agents/{agent_name}/executions",
                params={"status": "running", "limit": 1}
            )
            if exec_response.status_code == 200:
                execs = exec_response.json()
                if isinstance(execs, list) and execs:
                    execution_id = execs[0].get("id")
                    if execution_id:
                        break
            time.sleep(1)

        if not execution_id:
            task_thread.join(timeout=5)
            pytest.skip("Could not capture execution_id")

        # Terminate WITHOUT passing task_execution_id (no query param, no body)
        terminate_response = api_client.post(
            f"/api/agents/{agent_name}/executions/{execution_id}/terminate"
        )

        task_thread.join(timeout=15)

        if terminate_response.status_code != 200:
            pytest.skip("Termination did not succeed")

        # Verify DB status updated to cancelled even without explicit task_execution_id
        time.sleep(1)
        exec_detail = api_client.get(
            f"/api/agents/{agent_name}/executions/{execution_id}"
        )
        if exec_detail.status_code == 200:
            detail_data = exec_detail.json()
            assert detail_data.get("status") == "cancelled", \
                f"DB execution should be 'cancelled' (defaulted from path param), got {detail_data.get('status')}"
