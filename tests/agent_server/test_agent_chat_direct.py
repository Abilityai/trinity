"""
Agent Server Chat Direct Tests (test_agent_chat_direct.py)

Tests for direct chat endpoints on agent server.
Covers REQ-AS-CHAT-001 through REQ-AS-CHAT-005.
"""

import pytest
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
)


class TestSendMessage:
    """REQ-AS-CHAT-001: Send message direct tests."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_send_message_executes(self, agent_proxy_client):
        """POST /api/chat executes message with Claude Code."""
        api_client, agent_name = agent_proxy_client

        response = api_client.post(
            f"/api/agents/{agent_name}/chat",
            json={"message": "What is 1+1? Just reply with the number."},
            timeout=120.0
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")
        if response.status_code == 429:
            pytest.skip("Agent queue full")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert "response" in data or "content" in data


class TestChatHistory:
    """REQ-AS-CHAT-002: Chat history direct tests."""

    def test_get_history(self, agent_proxy_client):
        """GET /api/chat/history returns conversation history."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/chat/history")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)


class TestSessionInfo:
    """REQ-AS-CHAT-003: Session info direct tests."""

    def test_get_session_stats(self, agent_proxy_client):
        """GET /api/chat/session returns token usage."""
        api_client, agent_name = agent_proxy_client

        # Try to get session via chat history endpoint
        response = api_client.get(f"/api/agents/{agent_name}/chat/sessions")

        assert_status(response, 200)


class TestClearHistory:
    """REQ-AS-CHAT-004: Clear history direct tests."""

    def test_clear_conversation(self, agent_proxy_client):
        """DELETE /api/chat/history clears conversation."""
        api_client, agent_name = agent_proxy_client

        response = api_client.delete(f"/api/agents/{agent_name}/chat/history")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 204])


class TestModelManagement:
    """REQ-AS-CHAT-005: Model management direct tests."""

    def test_get_model(self, agent_proxy_client):
        """GET /api/model returns current model."""
        api_client, agent_name = agent_proxy_client

        response = api_client.get(f"/api/agents/{agent_name}/model")

        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)

    def test_set_model(self, agent_proxy_client):
        """PUT /api/model sets model."""
        api_client, agent_name = agent_proxy_client

        response = api_client.put(
            f"/api/agents/{agent_name}/model",
            json={"model": "sonnet"}
        )

        if response.status_code == 404:
            pytest.skip("Model endpoint not implemented")
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 204])
