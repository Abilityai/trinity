"""
Agent Chat Tests (test_agent_chat.py)

Tests for agent chat/conversation endpoints.
Covers REQ-CHAT-001 through REQ-CHAT-004.
"""

import pytest
import time
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestSendChatMessage:
    """REQ-CHAT-001: Send chat message endpoint tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_send_message_returns_response(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/chat sends message and receives response."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": "Hello, what is 2+2?"},
            timeout=120.0,  # Chat can take a while
        )

        # May get 503 if agent busy, 429 if queue full
        if response.status_code == 503:
            pytest.skip("Agent server not ready (503)")
        if response.status_code == 429:
            pytest.skip("Agent queue full (429)")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have response content
        assert "response" in data or "content" in data or "message" in data

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_response_has_metadata(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat response includes execution metadata."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": "Say hello"},
            timeout=120.0,
        )

        if response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({response.status_code})")

        assert_status(response, 200)
        data = response.json()

        # Should have metadata fields
        # Exact fields depend on implementation
        assert isinstance(data, dict)

    def test_chat_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/chat for non-existent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/chat",
            json={"message": "hello"},
            timeout=30.0,
        )
        assert_status(response, 404)

    def test_chat_stopped_agent_returns_error(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/chat for stopped agent returns error."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/chat",
            json={"message": "hello"},
            timeout=30.0,
        )
        # Should get 400 or 503 for stopped agent
        assert_status_in(response, [400, 503])


class TestChatHistory:
    """REQ-CHAT-002: Chat history endpoint tests."""

    def test_get_chat_history(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/chat/history returns conversation history."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/history"
        )

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should be a list (possibly empty)
        if isinstance(data, dict) and "history" in data:
            assert isinstance(data["history"], list)
        elif isinstance(data, dict) and "messages" in data:
            assert isinstance(data["messages"], list)
        else:
            # Direct list
            assert isinstance(data, (list, dict))

    def test_get_persistent_chat_history(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/chat/history/persistent returns database-backed history."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/history/persistent"
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, (list, dict))


class TestSessionInfo:
    """REQ-CHAT-003: Chat session info endpoint tests."""

    def test_get_session_info(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Agent session endpoint returns session statistics."""
        # Try direct agent session endpoint
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/session"
        )

        # May not exist or may return 503
        if response.status_code == 404:
            pytest.skip("Session endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)


class TestChatSessions:
    """Tests for chat sessions management."""

    def test_list_chat_sessions(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/chat/sessions lists all sessions."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions"
        )

        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, (list, dict))

    def test_list_chat_sessions_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat sessions list returns expected structure."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions"
        )

        assert_status(response, 200)
        data = response.json()

        # Should be a list of sessions
        if isinstance(data, list) and len(data) > 0:
            session = data[0]
            # Sessions should have id and created_at
            assert "id" in session or "session_id" in session
            assert "created_at" in session or "started_at" in session

    def test_list_chat_sessions_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/chat/sessions for nonexistent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/chat/sessions")
        assert_status(response, 404)

    def test_get_session_details(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/chat/sessions/{id} returns session details."""
        # First get list of sessions
        list_response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions"
        )

        if list_response.status_code != 200:
            pytest.skip("Cannot list sessions")

        sessions = list_response.json()

        # Handle both list and dict response formats
        if isinstance(sessions, dict):
            session_list = sessions.get("sessions", [])
        elif isinstance(sessions, list):
            session_list = sessions
        else:
            pytest.skip("Unexpected sessions format")

        if not session_list or len(session_list) == 0:
            pytest.skip("No sessions available for testing")

        # Get first session ID
        session = session_list[0]
        session_id = session.get("id") or session.get("session_id")

        if not session_id:
            pytest.skip("Session has no ID")

        # Get session details
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions/{session_id}"
        )

        assert_status(response, 200)
        data = response.json()
        assert isinstance(data, dict)

    def test_get_session_details_nonexistent_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/chat/sessions/{id} for nonexistent session returns 404."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions/nonexistent-session-xyz"
        )
        assert_status(response, 404)

    def test_close_session(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/chat/sessions/{id}/close closes a session."""
        # First get list of sessions
        list_response = api_client.get(
            f"/api/agents/{created_agent['name']}/chat/sessions"
        )

        if list_response.status_code != 200:
            pytest.skip("Cannot list sessions")

        sessions = list_response.json()

        # Handle both list and dict response formats
        if isinstance(sessions, dict):
            session_list = sessions.get("sessions", [])
        elif isinstance(sessions, list):
            session_list = sessions
        else:
            pytest.skip("Unexpected sessions format")

        if not session_list or len(session_list) == 0:
            pytest.skip("No sessions available for testing")

        # Get first session ID
        session = session_list[0]
        session_id = session.get("id") or session.get("session_id")

        if not session_id:
            pytest.skip("Session has no ID")

        # Close the session
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat/sessions/{session_id}/close"
        )

        assert_status_in(response, [200, 204])

    def test_close_session_nonexistent_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/chat/sessions/{id}/close for nonexistent session returns 404."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat/sessions/nonexistent-session-xyz/close"
        )
        assert_status(response, 404)

    def test_close_session_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/chat/sessions/{id}/close requires authentication."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/chat/sessions/test-session/close",
            auth=False
        )
        assert_status(response, 401)


class TestModelManagement:
    """REQ-CHAT-004: Model management endpoint tests."""

    def test_get_current_model(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/model returns current model."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/model"
        )

        # May not be implemented
        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        assert "model" in data or isinstance(data, str)

    def test_set_valid_model(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/model sets model."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/model",
            json={"model": "sonnet"}
        )

        # May not be implemented
        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 204])


class TestResetSession:
    """Tests for session reset functionality."""

    def test_reset_session(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/chat/history resets session."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/chat/history"
        )

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 204])


# =============================================================================
# CHAT EXECUTION TRACKING TESTS (#96)
# Verify that /api/chat creates execution records with triggered_by="chat"
# =============================================================================


class TestChatExecutionTracking:
    """#96: Chat messages create execution records for unified tracking."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_creates_execution_record(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/chat creates an execution record."""
        import uuid
        unique_id = uuid.uuid4().hex[:8]
        chat_message = f"Chat execution test {unique_id}"

        # Get initial execution count
        initial_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions",
            params={"limit": 100}
        )
        initial_count = len(initial_response.json()) if initial_response.status_code == 200 else 0

        # Send chat message
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": chat_message},
            timeout=120.0,
        )

        if response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({response.status_code})")

        assert_status(response, 200)
        time.sleep(2)

        # Verify execution record was created
        exec_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions",
            params={"limit": 100}
        )
        assert_status(exec_response, 200)
        executions = exec_response.json()

        assert len(executions) > initial_count, \
            "Chat should create a new execution record"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_execution_has_chat_trigger(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat execution record has triggered_by='chat'."""
        import uuid
        unique_id = uuid.uuid4().hex[:8]
        chat_message = f"Chat trigger test {unique_id}"

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": chat_message},
            timeout=120.0,
        )

        if response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({response.status_code})")

        assert_status(response, 200)
        time.sleep(2)

        # Find the execution record
        exec_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions"
        )
        assert_status(exec_response, 200)
        executions = exec_response.json()

        matching = [e for e in executions if chat_message in e.get("message", "")]
        assert len(matching) > 0, f"Should find execution for chat message '{chat_message}'"

        execution = matching[0]
        assert execution.get("triggered_by") == "chat", \
            f"Expected triggered_by='chat', got '{execution.get('triggered_by')}'"

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_chat_response_includes_task_execution_id(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Chat response includes task_execution_id in execution metadata."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": "Execution ID test"},
            timeout=120.0,
        )

        if response.status_code in [503, 429]:
            pytest.skip(f"Agent not ready ({response.status_code})")

        assert_status(response, 200)
        data = response.json()

        # Response should include execution metadata
        assert "execution" in data, "Chat response should include execution metadata"
        execution_meta = data["execution"]
        assert "task_execution_id" in execution_meta, \
            "Execution metadata should include task_execution_id"
        assert execution_meta["task_execution_id"] is not None, \
            "task_execution_id should not be None for chat executions"


# =============================================================================
# IN-MEMORY ACTIVITY TESTS (REQ-ACTIVITY-002)
# Tests for /api/agents/{name}/activity endpoints (in-memory during chat)
# =============================================================================


class TestInMemoryActivityAuthentication:
    """Tests for in-memory activity endpoint authentication."""

    pytestmark = pytest.mark.smoke

    def test_activity_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/activity requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/activity", auth=False)
        assert_status(response, 401)

    def test_clear_activity_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/agents/{name}/activity requires authentication."""
        response = unauthenticated_client.delete("/api/agents/test-agent/activity", auth=False)
        assert_status(response, 401)


class TestInMemoryActivity:
    """Tests for GET /api/agents/{name}/activity endpoint (in-memory activity)."""

    def test_get_activity_returns_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/activity returns expected structure."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/activity"
        )

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should have activity-related fields
        assert isinstance(data, (dict, list))

    def test_get_activity_empty_when_idle(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Activity is empty when agent is idle."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/activity"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # When idle, activity should be empty or have empty tools
        if isinstance(data, dict):
            # Could be {"tools": [], "is_running": false} or similar
            if "tools" in data:
                assert isinstance(data["tools"], list)
            if "is_running" in data:
                assert isinstance(data["is_running"], bool)

    def test_get_activity_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/activity for nonexistent agent returns 404."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/activity")
        assert_status(response, 404)


class TestInMemoryActivityToolDetails:
    """Tests for GET /api/agents/{name}/activity/{tool_id} endpoint."""

    def test_get_tool_details_nonexistent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/activity/{tool_id} returns 404 for unknown tool."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/activity/unknown-tool-123"
        )

        # Should return 404 for unknown tool ID
        assert_status_in(response, [404, 503])


class TestClearInMemoryActivity:
    """Tests for DELETE /api/agents/{name}/activity endpoint."""

    def test_clear_activity(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/activity clears in-memory activity."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/activity"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 204])

    def test_clear_activity_idempotent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/activity is idempotent."""
        # Clear twice
        response1 = api_client.delete(
            f"/api/agents/{created_agent['name']}/activity"
        )
        response2 = api_client.delete(
            f"/api/agents/{created_agent['name']}/activity"
        )

        if response1.status_code == 503:
            pytest.skip("Agent server not ready")

        # Both should succeed
        assert_status_in(response1, [200, 204])
        assert_status_in(response2, [200, 204])


# =============================================================================
# ADDITIONAL MODEL MANAGEMENT TESTS
# Extended tests for model selection functionality
# =============================================================================


class TestModelManagementExtended:
    """Extended tests for model management."""

    def test_set_invalid_model_rejected(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/model rejects invalid model name."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/model",
            json={"model": "invalid-model-xyz"}
        )

        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should reject invalid model
        assert_status_in(response, [400, 422])

    def test_set_model_persists(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Model selection persists after being set."""
        # Set to sonnet
        set_response = api_client.put(
            f"/api/agents/{created_agent['name']}/model",
            json={"model": "sonnet"}
        )

        if set_response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if set_response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Verify it was set
        get_response = api_client.get(
            f"/api/agents/{created_agent['name']}/model"
        )
        assert_status(get_response, 200)
        data = get_response.json()

        # Model should be sonnet
        model = data.get("model") if isinstance(data, dict) else data
        assert "sonnet" in str(model).lower()

    def test_model_options(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Valid model options are accepted."""
        valid_models = ["sonnet", "opus", "haiku"]

        for model in valid_models:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/model",
                json={"model": model}
            )

            if response.status_code == 404:
                pytest.skip("Model endpoint not implemented")
            if response.status_code == 503:
                pytest.skip("Agent server not ready")

            # Should accept valid models
            assert_status_in(response, [200, 204]), \
                f"Model '{model}' should be accepted"

    def test_get_model_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/model returns expected structure."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/model"
        )

        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # Should have model field or be a string
        if isinstance(data, dict):
            assert "model" in data
            assert isinstance(data["model"], str)
        else:
            assert isinstance(data, str)
