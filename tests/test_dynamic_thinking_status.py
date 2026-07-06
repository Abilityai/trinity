"""
Dynamic Thinking Status Tests (test_dynamic_thinking_status.py)

Tests for THINK-001: Dynamic status labels in Chat tab.
Covers:
- Async mode task submission returning execution_id
- SSE stream endpoint availability during execution
- Execution polling until completion
- Chat session persistence in async mode
- Status label mapping logic (unit-level)

Related flow: docs/memory/feature-flows/authenticated-chat-tab.md
"""

import pytest
import time
import json
import httpx
from utils.api_client import TrinityApiClient, ApiConfig
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# =============================================================================
# Unit Tests: Status Label Mapping Logic
# =============================================================================

class TestStatusLabelMapping:
    """THINK-001: Validate stream event to status label mapping logic.

    These tests verify the mapping rules defined in execution-status.js
    by testing the same logic in Python.
    """

    pytestmark = pytest.mark.smoke

    TOOL_STATUS_MAP = {
        "Read": "Reading file...",
        "Grep": "Searching code...",
        "Glob": "Finding files...",
        "Bash": "Running command...",
        "Edit": "Editing code...",
        "Write": "Writing file...",
        "WebSearch": "Searching web...",
        "WebFetch": "Fetching page...",
        "Task": "Delegating to agent...",
        "NotebookEdit": "Editing notebook...",
    }

    @staticmethod
    def get_status_from_event(event):
        """Python implementation of getStatusFromStreamEvent for testing."""
        if not event:
            return None
        if event.get("type") == "init":
            return "Starting session..."
        if event.get("type") in ("result", "stream_end", "error"):
            return None

        content = event.get("message", {}).get("content") or event.get("content") or []
        if not isinstance(content, list):
            return None

        tool_map = TestStatusLabelMapping.TOOL_STATUS_MAP
        for block in content:
            if block.get("type") == "thinking":
                return "Thinking..."
            if block.get("type") == "text":
                return "Responding..."
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if name.startswith("mcp__"):
                    parts = name.split("__")
                    server = parts[1] if len(parts) > 1 else "tool"
                    return f"Using {server}..."
                return tool_map.get(name, "Working...")
            if block.get("type") == "tool_result":
                return "Processing results..."

        return None

    def test_init_event(self):
        """Init event maps to 'Starting session...'."""
        assert self.get_status_from_event({"type": "init"}) == "Starting session..."

    def test_result_event_returns_none(self):
        """Result event returns None (done signal)."""
        assert self.get_status_from_event({"type": "result"}) is None

    def test_stream_end_returns_none(self):
        """Stream end event returns None."""
        assert self.get_status_from_event({"type": "stream_end"}) is None

    def test_error_event_returns_none(self):
        """Error event returns None."""
        assert self.get_status_from_event({"type": "error"}) is None

    def test_thinking_block(self):
        """Thinking content block maps to 'Thinking...'."""
        event = {"message": {"content": [{"type": "thinking"}]}}
        assert self.get_status_from_event(event) == "Thinking..."

    def test_text_block(self):
        """Text content block maps to 'Responding...'."""
        event = {"message": {"content": [{"type": "text"}]}}
        assert self.get_status_from_event(event) == "Responding..."

    def test_tool_result_block(self):
        """Tool result block maps to 'Processing results...'."""
        event = {"message": {"content": [{"type": "tool_result"}]}}
        assert self.get_status_from_event(event) == "Processing results..."

    @pytest.mark.parametrize("tool_name,expected_label", [
        ("Read", "Reading file..."),
        ("Grep", "Searching code..."),
        ("Glob", "Finding files..."),
        ("Bash", "Running command..."),
        ("Edit", "Editing code..."),
        ("Write", "Writing file..."),
        ("WebSearch", "Searching web..."),
        ("WebFetch", "Fetching page..."),
        ("Task", "Delegating to agent..."),
        ("NotebookEdit", "Editing notebook..."),
    ])
    def test_tool_use_known_tools(self, tool_name, expected_label):
        """Known tool names map to specific status labels."""
        event = {"message": {"content": [{"type": "tool_use", "name": tool_name}]}}
        assert self.get_status_from_event(event) == expected_label

    def test_tool_use_unknown_tool(self):
        """Unknown tool name falls back to 'Working...'."""
        event = {"message": {"content": [{"type": "tool_use", "name": "UnknownTool"}]}}
        assert self.get_status_from_event(event) == "Working..."

    def test_mcp_tool_extracts_server_name(self):
        """MCP tool names extract server name: mcp__serverName__toolName -> 'Using serverName...'."""
        event = {"message": {"content": [{"type": "tool_use", "name": "mcp__playwright__browser_click"}]}}
        assert self.get_status_from_event(event) == "Using playwright..."

    def test_mcp_tool_single_segment(self):
        """MCP tool with single segment: mcp__tool -> 'Using tool...'."""
        event = {"message": {"content": [{"type": "tool_use", "name": "mcp__context7"}]}}
        assert self.get_status_from_event(event) == "Using context7..."

    def test_none_event(self):
        """None event returns None."""
        assert self.get_status_from_event(None) is None

    def test_empty_content(self):
        """Event with empty content list returns None."""
        event = {"message": {"content": []}}
        assert self.get_status_from_event(event) is None

    def test_no_content_key(self):
        """Event without content key returns None."""
        event = {"message": {}}
        assert self.get_status_from_event(event) is None

    def test_content_directly_on_event(self):
        """Content array directly on event (not nested in message)."""
        event = {"content": [{"type": "thinking"}]}
        assert self.get_status_from_event(event) == "Thinking..."

    def test_first_matching_block_wins(self):
        """When multiple content blocks exist, first match wins."""
        event = {"message": {"content": [
            {"type": "thinking"},
            {"type": "text"},
        ]}}
        assert self.get_status_from_event(event) == "Thinking..."


# =============================================================================
# API Tests: Async Task Submission
# =============================================================================

class TestAsyncModeSubmission:
    """THINK-001: Async mode task submission returns execution_id immediately."""

    def test_async_mode_returns_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/task with async_mode=true returns status 'accepted'."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "What is 2+2? Reply with just the number.",
                "async_mode": True,
            },
            timeout=30.0,
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")
        if response.status_code == 429:
            pytest.skip("Agent at capacity (429)")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["status", "execution_id"])
        assert data["status"] == "accepted", f"Expected 'accepted', got '{data['status']}'"
        assert data["execution_id"], "execution_id should not be empty"

    def test_async_mode_execution_id_is_pollable(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Execution ID from async submission can be polled for status."""
        # Submit async task
        submit_response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "What is 2+2? Reply with just the number.",
                "async_mode": True,
            },
            timeout=30.0,
        )

        if submit_response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({submit_response.status_code})")

        assert_status(submit_response, 200)
        execution_id = submit_response.json()["execution_id"]

        # Poll execution status
        poll_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/{execution_id}",
            timeout=10.0,
        )

        assert_status(poll_response, 200)
        data = assert_json_response(poll_response)
        assert_has_fields(data, ["id", "status"])
        assert data["status"] in ["pending", "running", "success", "failed"], \
            f"Unexpected execution status: {data['status']}"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_async_mode_completes_eventually(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Async task eventually reaches a terminal status (success/failed)."""
        # Submit async task
        submit_response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "What is 2+2? Reply with just the number.",
                "async_mode": True,
            },
            timeout=30.0,
        )

        if submit_response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({submit_response.status_code})")

        assert_status(submit_response, 200)
        execution_id = submit_response.json()["execution_id"]

        # Poll until completion (max 120 seconds)
        max_wait = 120
        start = time.time()
        final_status = None

        while time.time() - start < max_wait:
            poll_response = api_client.get(
                f"/api/agents/{created_agent['name']}/executions/{execution_id}",
                timeout=10.0,
            )

            if poll_response.status_code == 200:
                data = poll_response.json()
                status = data.get("status")
                if status in ["success", "failed", "cancelled"]:
                    final_status = status
                    break

            time.sleep(3)

        assert final_status is not None, \
            f"Execution {execution_id} did not complete within {max_wait}s"

        if final_status != "success":
            # Fetch error details for debugging
            detail = api_client.get(
                f"/api/agents/{created_agent['name']}/executions/{execution_id}",
                timeout=10.0,
            )
            error_msg = detail.json().get("error", "Unknown") if detail.status_code == 200 else "Could not fetch details"
            pytest.fail(f"Expected success, got {final_status}. Error: {error_msg}")


# =============================================================================
# API Tests: SSE Stream During Async Execution
# =============================================================================

class TestSSEStreamDuringExecution:
    """THINK-001: SSE stream available during async task execution."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_sse_stream_available_during_execution(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSE stream endpoint returns 200 with event-stream content type during execution."""
        # Submit async task (something that takes a few seconds)
        submit_response = api_client.post(
            f"/api/agents/{created_agent['name']}/task",
            json={
                "message": "List the files in the current directory using ls.",
                "async_mode": True,
            },
            timeout=30.0,
        )

        if submit_response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({submit_response.status_code})")

        assert_status(submit_response, 200)
        execution_id = submit_response.json()["execution_id"]

        # Give agent a moment to start
        time.sleep(1)

        # Try SSE stream
        config = ApiConfig.from_env()
        with httpx.Client(base_url=config.base_url, timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_client.token}"}
            try:
                with client.stream(
                    "GET",
                    f"/api/agents/{created_agent['name']}/executions/{execution_id}/stream",
                    headers=headers,
                    timeout=10.0,
                ) as response:
                    # Accept 200 (streaming) or 404 (already completed)
                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        assert "text/event-stream" in content_type, \
                            f"Expected SSE content type, got: {content_type}"

                        # Try to read a few events
                        events_received = 0
                        for line in response.iter_lines():
                            if line.startswith("data: "):
                                events_received += 1
                                data = json.loads(line[6:])
                                # Verify event has a type field
                                assert "type" in data or "message" in data, \
                                    f"SSE event should have type or message field: {data}"
                                if data.get("type") == "stream_end":
                                    break
                                if events_received >= 5:
                                    break

                    elif response.status_code == 404:
                        # Execution already completed - that's OK
                        pass
                    else:
                        assert False, f"Unexpected status {response.status_code}"
            except httpx.ReadTimeout:
                # Acceptable - stream may be slow
                pass


# =============================================================================
# API Tests: Async Mode with Session Persistence
# =============================================================================

class TestAsyncModeSessionPersistence:
    """THINK-001: Async mode properly persists to chat sessions."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_async_mode_with_save_to_session(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Async mode with save_to_session=true persists messages to chat session."""
        agent_name = created_agent["name"]

        # Get initial session count
        sessions_before = api_client.get(
            f"/api/agents/{agent_name}/chat/sessions",
            timeout=10.0,
        )
        initial_count = 0
        if sessions_before.status_code == 200:
            initial_count = sessions_before.json().get("session_count", 0)

        # Submit async task with session persistence
        submit_response = api_client.post(
            f"/api/agents/{agent_name}/task",
            json={
                "message": "What is 2+2? Reply with just the number.",
                "async_mode": True,
                "save_to_session": True,
                "user_message": "What is 2+2?",
                "create_new_session": True,
            },
            timeout=30.0,
        )

        if submit_response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({submit_response.status_code})")

        assert_status(submit_response, 200)
        execution_id = submit_response.json()["execution_id"]

        # Wait for execution to complete
        max_wait = 120
        start = time.time()
        final_status = None
        final_error = None

        while time.time() - start < max_wait:
            poll_response = api_client.get(
                f"/api/agents/{agent_name}/executions/{execution_id}",
                timeout=10.0,
            )
            if poll_response.status_code == 200:
                data = poll_response.json()
                if data.get("status") in ["success", "failed", "cancelled"]:
                    final_status = data.get("status")
                    final_error = data.get("error")
                    break
            time.sleep(3)

        if final_status is None:
            pytest.skip(f"Execution did not complete within {max_wait}s")

        # #1444: persistence is guarded on a SUCCESS terminal — a FAILED /
        # CANCELLED turn correctly writes no session. Assert success FIRST so a
        # failing execution is attributed to the execution, not miscounted as a
        # persistence bug (the original test accepted any terminal here and then
        # demanded a session, conflating the two failure modes).
        if final_status != "success":
            pytest.fail(
                f"Execution {execution_id} did not succeed (status={final_status}, "
                f"error={final_error}); cannot assert session persistence."
            )

        # Wait for background session persistence to complete
        # The background task saves to chat_sessions asynchronously after execution finishes
        max_persist_wait = 10
        after_count = initial_count
        for _ in range(max_persist_wait):
            time.sleep(1)
            sessions_after = api_client.get(
                f"/api/agents/{agent_name}/chat/sessions",
                timeout=10.0,
            )
            if sessions_after.status_code == 200:
                after_count = sessions_after.json().get("session_count", 0)
                if after_count > initial_count:
                    break

        assert after_count > initial_count, \
            f"Expected new session to be created. Before: {initial_count}, After: {after_count}"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_async_mode_session_contains_messages(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Session created by async mode contains user and assistant messages."""
        agent_name = created_agent["name"]

        # Submit async task with session persistence
        submit_response = api_client.post(
            f"/api/agents/{agent_name}/task",
            json={
                "message": "Say hello",
                "async_mode": True,
                "save_to_session": True,
                "user_message": "Say hello",
                "create_new_session": True,
            },
            timeout=30.0,
        )

        if submit_response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({submit_response.status_code})")

        assert_status(submit_response, 200)
        execution_id = submit_response.json()["execution_id"]

        # Wait for completion
        max_wait = 120
        start = time.time()
        final_status = None
        final_error = None
        while time.time() - start < max_wait:
            poll_response = api_client.get(
                f"/api/agents/{agent_name}/executions/{execution_id}",
                timeout=10.0,
            )
            if poll_response.status_code == 200:
                data = poll_response.json()
                if data.get("status") in ["success", "failed", "cancelled"]:
                    final_status = data.get("status")
                    final_error = data.get("error")
                    break
            time.sleep(3)

        # #1444: only a SUCCESS turn is expected to persist — assert success
        # first so a failing execution isn't miscounted as a persistence bug.
        if final_status is None:
            pytest.skip(f"Execution did not complete within {max_wait}s")
        if final_status != "success":
            pytest.fail(
                f"Execution {execution_id} did not succeed (status={final_status}, "
                f"error={final_error}); cannot assert session persistence."
            )

        # Wait for background session persistence to complete
        max_persist_wait = 10
        sessions = []
        for _ in range(max_persist_wait):
            time.sleep(1)
            sessions_response = api_client.get(
                f"/api/agents/{agent_name}/chat/sessions",
                timeout=10.0,
            )
            if sessions_response.status_code == 200:
                sessions = sessions_response.json().get("sessions", [])
                if len(sessions) > 0:
                    break

        assert len(sessions) > 0, "Should have at least one session"

        # Get the latest session's messages
        latest_session = sessions[0]
        detail_response = api_client.get(
            f"/api/agents/{agent_name}/chat/sessions/{latest_session['id']}",
            timeout=10.0,
        )
        assert_status(detail_response, 200)
        messages = detail_response.json().get("messages", [])

        # Should have at least user + assistant messages
        roles = [m["role"] for m in messages]
        assert "user" in roles, "Session should contain a user message"
        assert "assistant" in roles, "Session should contain an assistant response"


# =============================================================================
# API Tests: Async Mode Authentication
# =============================================================================

class TestAsyncModeAuth:
    """THINK-001: Authentication requirements for async mode endpoints."""

    pytestmark = pytest.mark.smoke

    def test_async_task_requires_authentication(
        self,
        unauthenticated_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/task with async_mode requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/task",
            json={
                "message": "test",
                "async_mode": True,
            },
            auth=False,
        )
        assert_status(response, 401)

    def test_execution_poll_requires_authentication(
        self,
        unauthenticated_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/executions/{id} requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/executions/test-exec-id",
            auth=False,
        )
        assert_status(response, 401)

    def test_sse_stream_requires_authentication(
        self,
        unauthenticated_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/executions/{id}/stream requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/executions/test-exec-id/stream",
            auth=False,
        )
        assert_status(response, 401)
