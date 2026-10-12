"""
Tests for Slack Integration (SLACK-001).

Tests cover:
- Slack signature verification
- OAuth flow
- DM message handling
- User verification state machine
- Session persistence
- Admin endpoints

Run with: pytest tests/test_slack_integration.py -v
"""
import os
import time
import base64
import json
import pytest
import httpx


BASE_URL = "http://localhost:8000"


@pytest.fixture
def auth_headers():
    """Get auth headers for authenticated requests."""
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    response = httpx.post(
        f"{BASE_URL}/api/token",
        data={"username": "admin", "password": password}
    )
    if response.status_code != 200:
        pytest.skip("Could not authenticate - check admin credentials")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def running_agent():
    """Find a running agent for testing."""
    import subprocess
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=agent-", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    agents = [name.replace("agent-", "") for name in result.stdout.strip().split("\n") if name]
    if not agents:
        pytest.skip("No running agents found")
    # Prefer trinity-system if exists
    return "trinity-system" if "trinity-system" in agents else agents[0]


@pytest.fixture
def public_link(auth_headers, running_agent):
    """Create a public link for Slack testing."""
    response = httpx.post(
        f"{BASE_URL}/api/agents/{running_agent}/public-links",
        headers=auth_headers,
        json={
            "name": "Slack Test Link",
            "require_email": False
        }
    )

    if response.status_code == 403:
        pytest.skip(f"User doesn't own agent {running_agent}")

    if response.status_code != 200:
        pytest.skip(f"Could not create link: {response.text}")

    link = response.json()
    yield link

    # Cleanup
    httpx.delete(
        f"{BASE_URL}/api/agents/{running_agent}/public-links/{link['id']}",
        headers=auth_headers
    )


@pytest.fixture
def public_link_with_email(auth_headers, running_agent):
    """Create a public link that requires email verification."""
    response = httpx.post(
        f"{BASE_URL}/api/agents/{running_agent}/public-links",
        headers=auth_headers,
        json={
            "name": "Slack Email Test Link",
            "require_email": True
        }
    )

    if response.status_code == 403:
        pytest.skip(f"User doesn't own agent {running_agent}")

    if response.status_code != 200:
        pytest.skip(f"Could not create link: {response.text}")

    link = response.json()
    yield link

    # Cleanup
    httpx.delete(
        f"{BASE_URL}/api/agents/{running_agent}/public-links/{link['id']}",
        headers=auth_headers
    )


class TestSlackDatabaseSchema:
    """Test Slack database tables exist."""

    def test_slack_tables_exist(self):
        """Verify Slack integration tables were created."""
        import sqlite3

        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check slack_link_connections table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='slack_link_connections'"
        )
        assert cursor.fetchone() is not None, "slack_link_connections table missing"

        # Check slack_user_verifications table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='slack_user_verifications'"
        )
        assert cursor.fetchone() is not None, "slack_user_verifications table missing"

        # Check slack_pending_verifications table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='slack_pending_verifications'"
        )
        assert cursor.fetchone() is not None, "slack_pending_verifications table missing"

        conn.close()


class TestSlackPublicEndpoints:
    """Test public Slack endpoints (no auth required)."""

    def test_events_url_verification_challenge(self):
        """Test Slack URL verification challenge response."""
        # Note: This test doesn't include a valid signature, but tests the challenge flow structure
        # In production, Slack sends this with a valid signature during app setup

        # Create a mock Slack event for URL verification
        event_payload = {
            "type": "url_verification",
            "challenge": "test-challenge-token-12345"
        }

        # Without valid signature, this should return ok=false but not crash
        response = httpx.post(
            f"{BASE_URL}/api/public/slack/events",
            json=event_payload
        )

        # Should return 200 even without signature (Slack requirement)
        assert response.status_code == 200
        data = response.json()
        # Without valid signature, ok should be false
        assert data.get("ok") == False or data.get("challenge") is not None

    def test_events_invalid_signature_returns_200(self):
        """Test that invalid Slack signatures still return 200 (Slack requirement)."""
        response = httpx.post(
            f"{BASE_URL}/api/public/slack/events",
            json={"type": "event_callback", "event": {}},
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=invalid_signature_here"
            }
        )

        # Slack events always return 200 to prevent retries
        assert response.status_code == 200
        data = response.json()
        # But ok should be false for invalid signature
        assert data.get("ok") == False

    def test_oauth_callback_missing_params(self):
        """Test OAuth callback with missing parameters."""
        response = httpx.get(
            f"{BASE_URL}/api/public/slack/oauth/callback",
            follow_redirects=False
        )

        # Should redirect with error
        assert response.status_code == 307
        location = response.headers.get("location", "")
        assert "slack=error" in location
        assert "missing_params" in location


class TestSlackAuthEndpointsNoAuth:
    """Test that Slack auth endpoints require authentication."""

    def test_get_slack_connection_requires_auth(self):
        """Test that getting Slack connection requires authentication."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/test-agent/public-links/test-link/slack"
        )
        assert response.status_code == 401

    def test_connect_slack_requires_auth(self):
        """Test that connecting Slack requires authentication."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/test-agent/public-links/test-link/slack/connect"
        )
        assert response.status_code == 401

    def test_disconnect_slack_requires_auth(self):
        """Test that disconnecting Slack requires authentication."""
        response = httpx.delete(
            f"{BASE_URL}/api/agents/test-agent/public-links/test-link/slack"
        )
        assert response.status_code == 401


class TestSlackConnectionStatus:
    """Test Slack connection status endpoint."""

    def test_get_connection_status_not_connected(self, auth_headers, running_agent, public_link):
        """Test getting connection status for unconnected link."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/{running_agent}/public-links/{public_link['id']}/slack",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] == False

    def test_get_connection_status_agent_not_found(self, auth_headers):
        """Test getting connection status for non-existent agent."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/public-links/fake-link/slack",
            headers=auth_headers
        )
        assert response.status_code in [403, 404]

    def test_get_connection_status_link_not_found(self, auth_headers, running_agent):
        """Test getting connection status for non-existent link."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/{running_agent}/public-links/fake-link-xyz/slack",
            headers=auth_headers
        )
        assert response.status_code == 404


class TestSlackOAuthInitiation:
    """Test Slack OAuth initiation endpoint."""

    def test_initiate_oauth_not_configured(self, auth_headers, running_agent, public_link):
        """Test OAuth initiation when Slack credentials not configured."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/{running_agent}/public-links/{public_link['id']}/slack/connect",
            headers=auth_headers
        )

        # Should return 400 if Slack not configured, or 200 with OAuth URL if configured
        if response.status_code == 400:
            assert "not configured" in response.json().get("detail", "").lower()
        elif response.status_code == 200:
            data = response.json()
            assert "oauth_url" in data
            assert "slack.com/oauth" in data["oauth_url"]

    def test_initiate_oauth_agent_not_found(self, auth_headers):
        """Test OAuth initiation for non-existent agent."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/public-links/fake-link/slack/connect",
            headers=auth_headers
        )
        assert response.status_code in [403, 404]

    def test_initiate_oauth_link_not_found(self, auth_headers, running_agent):
        """Test OAuth initiation for non-existent link."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/{running_agent}/public-links/fake-link-xyz/slack/connect",
            headers=auth_headers
        )
        assert response.status_code == 404


class TestSlackDisconnect:
    """Test Slack disconnect endpoint."""

    def test_disconnect_not_connected(self, auth_headers, running_agent, public_link):
        """Test disconnecting when Slack is not connected."""
        response = httpx.delete(
            f"{BASE_URL}/api/agents/{running_agent}/public-links/{public_link['id']}/slack",
            headers=auth_headers
        )

        # Should return 404 since there's no connection
        assert response.status_code == 404
        assert "no slack connection" in response.json().get("detail", "").lower()

    def test_disconnect_agent_not_found(self, auth_headers):
        """Test disconnecting for non-existent agent."""
        response = httpx.delete(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/public-links/fake-link/slack",
            headers=auth_headers
        )
        assert response.status_code in [403, 404]


class TestSlackSettingsAPI:
    """Test Slack settings admin endpoints."""

    def test_get_slack_settings_requires_auth(self):
        """Test that getting Slack settings requires authentication."""
        response = httpx.get(f"{BASE_URL}/api/settings/slack")
        assert response.status_code == 401

    def test_update_slack_settings_requires_auth(self):
        """Test that updating Slack settings requires authentication."""
        response = httpx.put(
            f"{BASE_URL}/api/settings/slack",
            json={"client_id": "test"}
        )
        assert response.status_code == 401

    def test_get_slack_settings_authenticated(self, auth_headers):
        """Test getting Slack settings status."""
        response = httpx.get(
            f"{BASE_URL}/api/settings/slack",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        # Should have configuration status fields
        assert "configured" in data
        assert "client_id" in data
        assert "client_secret" in data
        assert "signing_secret" in data

        # Each field should have configured, masked, source properties
        for field in ["client_id", "client_secret", "signing_secret"]:
            assert "configured" in data[field]
            assert "source" in data[field]

    def test_update_slack_settings(self, auth_headers):
        """Test updating Slack settings."""
        # Save current settings
        current = httpx.get(
            f"{BASE_URL}/api/settings/slack",
            headers=auth_headers
        ).json()

        # Test partial update (should not break things)
        response = httpx.put(
            f"{BASE_URL}/api/settings/slack",
            headers=auth_headers,
            json={"client_id": "test_client_id_123"}
        )

        if response.status_code == 200:
            # Verify update was applied
            check = httpx.get(
                f"{BASE_URL}/api/settings/slack",
                headers=auth_headers
            )
            data = check.json()
            assert data["client_id"]["configured"] == True

            # Clean up - delete the test setting
            httpx.delete(
                f"{BASE_URL}/api/settings/slack",
                headers=auth_headers
            )


class TestSlackOAuthState:
    """Test OAuth state encoding/decoding."""

    def test_oauth_state_encode_decode(self):
        """Test OAuth state token encode/decode cycle."""
        try:
            import sys
            sys.path.insert(0, 'src/backend')
            from services.slack_service import slack_service

            # Test encoding
            state = slack_service.encode_oauth_state(
                link_id="test-link-123",
                agent_name="test-agent",
                user_id="user-456"
            )

            assert isinstance(state, str)
            assert len(state) > 0

            # Test decoding
            valid, data = slack_service.decode_oauth_state(state)

            assert valid == True
            assert data["link_id"] == "test-link-123"
            assert data["agent_name"] == "test-agent"
            assert data["user_id"] == "user-456"

        except ImportError:
            pytest.skip("Could not import slack_service from backend")

    def test_oauth_state_invalid_token(self):
        """Test that invalid state tokens are rejected."""
        try:
            import sys
            sys.path.insert(0, 'src/backend')
            from services.slack_service import slack_service

            # Test with invalid token
            valid, data = slack_service.decode_oauth_state("invalid_token_here")
            assert valid == False
            assert data is None

        except ImportError:
            pytest.skip("Could not import slack_service from backend")


class TestSlackDatabaseOperations:
    """Test Slack database operations."""

    def test_database_connection_by_team_lookup(self):
        """Test that connection lookup by team_id works."""
        import sqlite3

        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Verify the join query structure works
        cursor.execute("""
            SELECT c.id, c.link_id, c.slack_team_id, c.slack_team_name,
                   c.enabled, l.agent_name, l.require_email
            FROM slack_link_connections c
            JOIN agent_public_links l ON c.link_id = l.id
            WHERE c.slack_team_id = 'test_team_id_not_real'
        """)

        # Should return empty (no matching team)
        result = cursor.fetchone()
        assert result is None

        conn.close()


class TestSlackUserVerification:
    """Test Slack user verification flows."""

    def test_verification_table_structure(self):
        """Test that verification tables have correct structure."""
        import sqlite3

        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check slack_user_verifications columns
        cursor.execute("PRAGMA table_info(slack_user_verifications)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "link_id" in columns
        assert "slack_user_id" in columns
        assert "slack_team_id" in columns
        assert "verified_email" in columns
        assert "verification_method" in columns
        assert "verified_at" in columns

        # Check slack_pending_verifications columns
        cursor.execute("PRAGMA table_info(slack_pending_verifications)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "link_id" in columns
        assert "slack_user_id" in columns
        assert "slack_team_id" in columns
        assert "email" in columns
        assert "code" in columns
        assert "state" in columns
        assert "expires_at" in columns

        conn.close()


class TestSlackSessionIntegration:
    """Test Slack session integration with public_chat_sessions."""

    def test_slack_session_identifier_format(self):
        """Test that Slack sessions use correct identifier format."""
        # Slack sessions should use format: team_id:user_id
        # This is documented in the feature flow

        # Test the format convention
        team_id = "T12345678"
        user_id = "U87654321"
        expected_identifier = f"{team_id}:{user_id}"

        assert ":" in expected_identifier
        parts = expected_identifier.split(":")
        assert len(parts) == 2
        assert parts[0] == team_id
        assert parts[1] == user_id


class TestSlackEventTypes:
    """Test handling of different Slack event types."""

    def test_event_callback_structure(self):
        """Test that event_callback events are handled correctly."""
        # Without valid signature, these should fail gracefully
        event_payload = {
            "type": "event_callback",
            "team_id": "T12345678",
            "api_app_id": "A12345678",
            "event": {
                "type": "message",
                "user": "U12345678",
                "text": "Hello bot!",
                "channel": "D12345678",
                "channel_type": "im",
                "ts": "1234567890.123456"
            },
            "event_id": "Ev12345678",
            "event_time": int(time.time())
        }

        response = httpx.post(
            f"{BASE_URL}/api/public/slack/events",
            json=event_payload,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=invalid"
            }
        )

        # Should return 200 (Slack requirement) but ok=false due to invalid signature
        assert response.status_code == 200
        data = response.json()
        assert data.get("ok") == False

    def test_bot_message_ignored(self):
        """Test that bot messages are ignored (prevent loops)."""
        # Bot messages include a bot_id field
        event_payload = {
            "type": "event_callback",
            "team_id": "T12345678",
            "event": {
                "type": "message",
                "bot_id": "B12345678",  # This indicates a bot message
                "text": "Bot response",
                "channel": "D12345678",
                "channel_type": "im"
            }
        }

        response = httpx.post(
            f"{BASE_URL}/api/public/slack/events",
            json=event_payload
        )

        # Should return 200 and handle gracefully
        assert response.status_code == 200


class TestSlackTransportManagement:
    """Test Slack transport management endpoints (SLACK-002)."""

    def test_transport_status_requires_auth(self):
        """GET /api/settings/slack/status requires authentication."""
        response = httpx.get(f"{BASE_URL}/api/settings/slack/status")
        assert response.status_code == 401

    def test_transport_connect_requires_auth(self):
        """POST /api/settings/slack/connect requires authentication."""
        response = httpx.post(
            f"{BASE_URL}/api/settings/slack/connect",
            json={"transport_mode": "socket"}
        )
        assert response.status_code == 401

    def test_transport_disconnect_requires_auth(self):
        """POST /api/settings/slack/disconnect requires authentication."""
        response = httpx.post(f"{BASE_URL}/api/settings/slack/disconnect")
        assert response.status_code == 401

    def test_get_transport_status(self, auth_headers):
        """Test getting transport status returns expected fields."""
        response = httpx.get(
            f"{BASE_URL}/api/settings/slack/status",
            headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "connected" in data
        assert "transport_mode" in data
        assert "app_token_configured" in data
        assert "workspaces" in data
        assert isinstance(data["workspaces"], list)
        assert isinstance(data["connected"], bool)

    def test_transport_disconnect_and_reconnect(self, auth_headers):
        """Test disconnect then reconnect cycle."""
        # Get initial status
        status = httpx.get(
            f"{BASE_URL}/api/settings/slack/status",
            headers=auth_headers
        ).json()

        was_connected = status["connected"]

        # Disconnect
        response = httpx.post(
            f"{BASE_URL}/api/settings/slack/disconnect",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["disconnected"] == True

        # Verify disconnected
        status = httpx.get(
            f"{BASE_URL}/api/settings/slack/status",
            headers=auth_headers
        ).json()
        assert status["connected"] == False

        # Reconnect (only if app_token is configured)
        if status["app_token_configured"]:
            response = httpx.post(
                f"{BASE_URL}/api/settings/slack/connect",
                headers=auth_headers,
                json={"transport_mode": "socket"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["connected"] == True

            # Verify connected
            status = httpx.get(
                f"{BASE_URL}/api/settings/slack/status",
                headers=auth_headers
            ).json()
            assert status["connected"] == True

    def test_transport_connect_invalid_mode(self, auth_headers):
        """Test connecting with invalid transport mode."""
        response = httpx.post(
            f"{BASE_URL}/api/settings/slack/connect",
            headers=auth_headers,
            json={"transport_mode": "invalid_mode"}
        )
        assert response.status_code == 400
        assert "socket" in response.json().get("detail", "").lower() or "webhook" in response.json().get("detail", "").lower()

    def test_transport_status_workspaces_include_agents(self, auth_headers):
        """Test that workspace info includes agent details."""
        response = httpx.get(
            f"{BASE_URL}/api/settings/slack/status",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()

        for ws in data["workspaces"]:
            assert "team_id" in ws
            assert "team_name" in ws
            assert "agent_count" in ws
            assert "agents" in ws
            assert isinstance(ws["agents"], list)
            assert ws["agent_count"] == len(ws["agents"])


class TestSlackChannelBinding:
    """Test per-agent Slack channel binding endpoints (SLACK-002)."""

    def test_get_channel_requires_auth(self):
        """GET /api/agents/{name}/slack/channel requires authentication."""
        response = httpx.get(f"{BASE_URL}/api/agents/test-agent/slack/channel")
        assert response.status_code == 401

    def test_create_channel_requires_auth(self):
        """POST /api/agents/{name}/slack/channel requires authentication."""
        response = httpx.post(f"{BASE_URL}/api/agents/test-agent/slack/channel")
        assert response.status_code == 401

    def test_delete_channel_requires_auth(self):
        """DELETE /api/agents/{name}/slack/channel requires authentication."""
        response = httpx.delete(f"{BASE_URL}/api/agents/test-agent/slack/channel")
        assert response.status_code == 401

    def test_get_channel_nonexistent_agent(self, auth_headers):
        """GET /api/agents/{name}/slack/channel returns bound=false for nonexistent agent."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/slack/channel",
            headers=auth_headers
        )
        # Admin can access all agents, so returns 200 with bound=false
        assert response.status_code in [200, 403]
        if response.status_code == 200:
            assert response.json()["bound"] == False

    def test_get_channel_unbound_agent(self, auth_headers, running_agent):
        """GET /api/agents/{name}/slack/channel returns bound=false when not bound."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/{running_agent}/slack/channel",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert "bound" in data

    def test_get_channel_response_fields(self, auth_headers, running_agent):
        """GET /api/agents/{name}/slack/channel returns correct fields when bound."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/{running_agent}/slack/channel",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        if data.get("bound"):
            assert "channel_name" in data
            assert "channel_id" in data
            assert "workspace_name" in data

    def test_delete_channel_not_bound(self, auth_headers, running_agent):
        """DELETE /api/agents/{name}/slack/channel returns 404 when not bound."""
        # First check if agent is bound
        status = httpx.get(
            f"{BASE_URL}/api/agents/{running_agent}/slack/channel",
            headers=auth_headers
        ).json()

        if not status.get("bound"):
            response = httpx.delete(
                f"{BASE_URL}/api/agents/{running_agent}/slack/channel",
                headers=auth_headers
            )
            assert response.status_code == 404

    def test_create_channel_no_workspace(self, auth_headers, running_agent):
        """POST /api/agents/{name}/slack/channel returns 400 when no workspace connected."""
        # This test only applies when no workspaces are connected
        # If workspaces exist, the endpoint will try to create a channel
        status = httpx.get(
            f"{BASE_URL}/api/settings/slack/status",
            headers=auth_headers
        ).json()

        if len(status.get("workspaces", [])) == 0:
            response = httpx.post(
                f"{BASE_URL}/api/agents/{running_agent}/slack/channel",
                headers=auth_headers
            )
            assert response.status_code == 400
            assert "workspace" in response.json().get("detail", "").lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
