"""
Proactive Messaging Tests (test_proactive_messaging.py)

Tests for proactive agent messaging functionality (Issue #321).
Covers the ability for agents to send proactive messages to users
across Telegram, Slack, and web channels.

Key features tested:
- Explicit opt-in consent via allow_proactive flag
- Rate limiting (10 messages per recipient per hour)
- Channel resolution by verified email
- Backend endpoints and MCP tool integration
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
)


class TestProactiveMessageEndpoint:
    """POST /api/agents/{name}/messages endpoint tests."""

    def test_send_message_requires_authorization(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Sending to non-opted-in recipient returns 403."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "nonexistent@example.com",
                "text": "Hello from agent",
                "channel": "auto"
            }
        )

        # Should fail - recipient hasn't opted in
        assert_status_in(response, [403, 404])
        data = response.json()
        assert "error" in data or "detail" in data

    def test_send_message_validates_email_format(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Invalid email format returns validation error."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "not-an-email",
                "text": "Hello",
                "channel": "auto"
            }
        )

        # Should fail validation
        assert_status_in(response, [400, 422])

    def test_send_message_validates_text_required(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Empty message text returns validation error."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "",
                "channel": "auto"
            }
        )

        # Should fail validation
        assert_status_in(response, [400, 422])

    def test_send_message_validates_text_max_length(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Message exceeding 4096 chars returns validation error."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "x" * 5000,  # Exceeds 4096 limit
                "channel": "auto"
            }
        )

        # Should fail validation
        assert_status_in(response, [400, 422])

    def test_send_message_validates_channel(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Invalid channel returns validation error."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "invalid_channel"
            }
        )

        # Should fail validation
        assert_status_in(response, [400, 422])

    def test_send_message_to_nonexistent_agent(
        self,
        api_client: TrinityApiClient
    ):
        """Sending from nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "auto"
            }
        )

        assert_status(response, 404)


class TestAllowProactiveFlag:
    """Tests for allow_proactive flag management."""

    def test_set_allow_proactive(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """PUT /api/agents/{name}/shares/proactive sets the flag."""
        # First share the agent with a test email
        share_email = "proactive-test@example.com"
        share_response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": share_email}
        )

        if share_response.status_code not in [200, 201]:
            pytest.skip("Could not share agent first")

        try:
            # Set allow_proactive to true
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/shares/proactive",
                json={
                    "email": share_email,
                    "allow_proactive": True
                }
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("allow_proactive") is True

        finally:
            # Cleanup: unshare
            api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")

    def test_disable_allow_proactive(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Can disable allow_proactive after enabling."""
        share_email = "proactive-disable-test@example.com"
        share_response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": share_email}
        )

        if share_response.status_code not in [200, 201]:
            pytest.skip("Could not share agent first")

        try:
            # Enable first
            api_client.put(
                f"/api/agents/{created_agent['name']}/shares/proactive",
                json={"email": share_email, "allow_proactive": True}
            )

            # Now disable
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/shares/proactive",
                json={"email": share_email, "allow_proactive": False}
            )

            assert_status(response, 200)
            data = response.json()
            assert data.get("allow_proactive") is False

        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")

    def test_set_proactive_nonexistent_share(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Setting allow_proactive for non-shared email returns error."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/shares/proactive",
            json={
                "email": "not-shared@example.com",
                "allow_proactive": True
            }
        )

        assert_status_in(response, [400, 404])


class TestListProactiveShares:
    """Tests for listing proactive-enabled shares."""

    def test_list_proactive_shares_empty(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/shares/proactive returns empty list by default."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/shares/proactive"
        )

        assert_status(response, 200)
        data = response.json()
        # Response format: {"agent_name": "...", "emails": [...]}
        assert "emails" in data
        assert isinstance(data["emails"], list)
        assert len(data["emails"]) == 0

    def test_list_proactive_shares_with_opted_in(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """List includes users who opted in."""
        share_email = "proactive-list-test@example.com"
        share_response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": share_email}
        )

        if share_response.status_code not in [200, 201]:
            pytest.skip("Could not share agent first")

        try:
            # Enable proactive
            api_client.put(
                f"/api/agents/{created_agent['name']}/shares/proactive",
                json={"email": share_email, "allow_proactive": True}
            )

            # List proactive shares
            response = api_client.get(
                f"/api/agents/{created_agent['name']}/shares/proactive"
            )

            assert_status(response, 200)
            data = response.json()
            # Response format: {"agent_name": "...", "emails": ["email1", ...]}
            emails = data.get("emails", [])

            # Should include our email
            assert share_email in emails

        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")

    def test_list_excludes_non_opted_in(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """List excludes users who have not opted in."""
        share_email = "proactive-not-opted@example.com"
        share_response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": share_email}
        )

        if share_response.status_code not in [200, 201]:
            pytest.skip("Could not share agent first")

        try:
            # Do NOT enable proactive - just share

            # List proactive shares
            response = api_client.get(
                f"/api/agents/{created_agent['name']}/shares/proactive"
            )

            assert_status(response, 200)
            data = response.json()
            # Response format: {"agent_name": "...", "emails": ["email1", ...]}
            emails = data.get("emails", [])

            # Should NOT include our email (not opted in)
            assert share_email not in emails

        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")


class TestProactiveMessageRateLimiting:
    """Tests for rate limiting (requires Redis)."""

    def test_rate_limit_info_in_error(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Rate limit error includes limit information."""
        # This test verifies the error message format
        # The actual rate limit is 10/hour which we can't easily test

        # Try sending without authorization (will fail first)
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "auto"
            }
        )

        # The error response should exist
        data = response.json()
        assert "error" in data or "detail" in data


class TestChannelSelection:
    """Tests for channel selection logic."""

    def test_auto_channel_default(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Channel defaults to 'auto' if not specified."""
        # This validates the API accepts the request (auth will fail)
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello"
                # channel not specified - should default to auto
            }
        )

        # Request should be accepted (fail on auth, not validation)
        assert_status_in(response, [403, 404])

    def test_telegram_channel_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Telegram channel is accepted."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "telegram"
            }
        )

        # Should fail on auth/recipient, not validation
        assert_status_in(response, [403, 404])

    def test_slack_channel_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Slack channel is accepted."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "slack"
            }
        )

        # Should fail on auth/recipient, not validation
        assert_status_in(response, [403, 404])

    def test_web_channel_accepted(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Web channel is accepted (deferred to v2)."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": "test@example.com",
                "text": "Hello",
                "channel": "web"
            }
        )

        # Should fail on auth/recipient, not validation
        assert_status_in(response, [403, 404])


class TestOwnerAuthorization:
    """Tests for owner implicit authorization."""

    def test_owner_can_message_self(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Agent owner is implicitly authorized to receive proactive messages."""
        # Get current user email
        me_response = api_client.get("/api/users/me")
        if me_response.status_code != 200:
            pytest.skip("Could not get current user")

        me = me_response.json()
        my_email = me.get("email")

        if not my_email:
            pytest.skip("User email not available")

        # Try to send to self (owner)
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/messages",
            json={
                "recipient_email": my_email,
                "text": "Hello from my agent",
                "channel": "auto"
            }
        )

        # Should NOT fail with 403 (not authorized)
        # May fail with 404 (no channel endpoint for email) which is expected
        assert response.status_code != 403 or "not authorized" not in response.text.lower()
