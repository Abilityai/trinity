"""
Tests for Public Agent Links feature (Phase 12.2).

Run with: pytest tests/test_public_links.py -v
"""
import os
import pytest
import httpx
import asyncio
from datetime import datetime, timedelta

# Base URL for backend
BASE_URL = "http://localhost:8000"

# Test fixtures
@pytest.fixture
def auth_headers():
    """Get auth headers for authenticated requests."""
    # Try to login with dev credentials
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    response = httpx.post(
        f"{BASE_URL}/api/token",
        data={"username": "admin", "password": password}
    )
    if response.status_code != 200:
        pytest.skip("Could not authenticate - check admin credentials")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestPublicLinkDatabase:
    """Test database operations for public links."""

    def test_database_tables_exist(self):
        """Verify public links tables were created."""
        import sqlite3
        import os

        # Check if db path is in docker volume
        db_path = os.path.expanduser("~/trinity-data/trinity.db")
        if not os.path.exists(db_path):
            pytest.skip(f"Database not found at {db_path}")

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_public_links'")
        assert cursor.fetchone() is not None, "agent_public_links table missing"

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='public_link_verifications'")
        assert cursor.fetchone() is not None, "public_link_verifications table missing"

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='public_link_usage'")
        assert cursor.fetchone() is not None, "public_link_usage table missing"

        conn.close()


class TestPublicEndpoints:
    """Test public (unauthenticated) endpoints."""

    def test_get_invalid_link(self):
        """Test getting info for non-existent link returns normalized response (pentest 3.3.2)."""
        response = httpx.get(f"{BASE_URL}/api/public/link/invalid-token-12345")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] == False
        # Normalized reason — no oracle for valid/invalid/disabled/expired (pentest 3.3.2)
        assert data["reason"] == "invalid_or_expired"

    def test_verify_request_invalid_link(self):
        """Test verification request for invalid link."""
        response = httpx.post(
            f"{BASE_URL}/api/public/verify/request",
            json={"token": "invalid-token", "email": "test@example.com"}
        )
        assert response.status_code == 404

    def test_verify_confirm_invalid_link(self):
        """Test verification confirm for invalid link."""
        response = httpx.post(
            f"{BASE_URL}/api/public/verify/confirm",
            json={"token": "invalid-token", "email": "test@example.com", "code": "123456"}
        )
        assert response.status_code == 404

    def test_public_chat_invalid_link(self):
        """Test chat with invalid link."""
        response = httpx.post(
            f"{BASE_URL}/api/public/chat/invalid-token",
            json={"message": "Hello"}
        )
        assert response.status_code == 404


class TestPublicLinkBruteForceProtection:
    """Tests for pentest finding 3.3.2 — brute force protection on public links.

    Verifies:
    1. Error responses are normalized (no oracle for valid vs invalid tokens)
    2. Rate limiting is enforced on all public token endpoints
    """

    @pytest.fixture(autouse=True)
    def flush_rate_limits(self):
        """Flush public link rate limit counters before each test."""
        try:
            import redis as _redis
            r = _redis.from_url("redis://localhost:6379", decode_responses=True)
            for key in r.scan_iter("public_link_lookups:*"):
                r.delete(key)
        except Exception:
            pass
        yield

    def test_normalized_error_link_info(self):
        """All invalid tokens return the same reason string (no oracle)."""
        # Different fake tokens should all get the same response
        for token in ["nonexistent-aaa", "nonexistent-bbb", "nonexistent-ccc"]:
            response = httpx.get(f"{BASE_URL}/api/public/link/{token}")
            assert response.status_code == 200
            data = response.json()
            assert data["valid"] == False
            assert data["reason"] == "invalid_or_expired"
            # Should NOT contain "not_found", "disabled", or "expired"
            assert "not_found" not in str(data)

    def test_normalized_error_history(self):
        """History endpoint returns generic 404 for invalid tokens."""
        response = httpx.get(f"{BASE_URL}/api/public/history/nonexistent-token-xyz")
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_session(self):
        """Session endpoint returns generic 404 for invalid tokens."""
        response = httpx.delete(
            f"{BASE_URL}/api/public/session/nonexistent-token-xyz",
            params={"session_id": "test"}
        )
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_chat(self):
        """Chat endpoint returns generic 404 for invalid tokens."""
        response = httpx.post(
            f"{BASE_URL}/api/public/chat/nonexistent-token-xyz",
            json={"message": "test"}
        )
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_intro(self):
        """Intro endpoint returns generic 404 for invalid tokens."""
        response = httpx.get(f"{BASE_URL}/api/public/intro/nonexistent-token-xyz")
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_playbooks(self):
        """Playbooks endpoint returns generic 404 for invalid tokens."""
        response = httpx.get(f"{BASE_URL}/api/public/playbooks/nonexistent-token-xyz")
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_execution_stream(self):
        """Execution stream returns generic 404 for invalid tokens."""
        response = httpx.get(f"{BASE_URL}/api/public/executions/nonexistent-token-xyz/fake-exec-id/stream")
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_normalized_error_execution_status(self):
        """Execution status returns generic 404 for invalid tokens."""
        response = httpx.get(f"{BASE_URL}/api/public/executions/nonexistent-token-xyz/fake-exec-id/status")
        assert response.status_code == 404
        data = response.json()
        assert data["detail"] == "Invalid or expired link"

    def test_rate_limiting_enforced(self):
        """Rate limiting triggers after threshold (60 req/min per IP)."""
        # Flush rate limit counters first so test is self-contained
        try:
            import redis as _redis
            r = _redis.from_url("redis://localhost:6379", decode_responses=True)
            for key in r.scan_iter("public_link_lookups:*"):
                r.delete(key)
        except Exception:
            pytest.skip("Redis not reachable for rate limit test")

        # Send 61 rapid requests — the 61st should be rate-limited
        responses = []
        for i in range(62):
            resp = httpx.get(f"{BASE_URL}/api/public/link/ratelimit-test-token-{i}")
            responses.append(resp.status_code)
            if resp.status_code == 429:
                break

        assert 429 in responses, "Rate limiting was not enforced after 60+ requests"

    def test_consistent_error_shape_across_endpoints(self):
        """All endpoints return identical error shape for invalid tokens."""
        # Flush rate limit counter from previous tests
        try:
            import redis as _redis
            r = _redis.from_url("redis://localhost:6379", decode_responses=True)
            for key in r.scan_iter("public_link_lookups:*"):
                r.delete(key)
        except Exception:
            pass  # If Redis not reachable, test may still work if under limit

        token = "consistency-test-token"
        endpoints = [
            ("GET", f"/api/public/history/{token}"),
            ("GET", f"/api/public/intro/{token}"),
            ("GET", f"/api/public/playbooks/{token}"),
            ("GET", f"/api/public/executions/{token}/fake-id/stream"),
            ("GET", f"/api/public/executions/{token}/fake-id/status"),
        ]

        error_details = set()
        for method, path in endpoints:
            if method == "GET":
                resp = httpx.get(f"{BASE_URL}{path}")
            assert resp.status_code == 404, f"{path} returned {resp.status_code}: {resp.text}"
            error_details.add(resp.json()["detail"])

        # All endpoints should return the exact same error message
        assert len(error_details) == 1, f"Inconsistent error messages: {error_details}"
        assert error_details.pop() == "Invalid or expired link"


class TestOwnerEndpointsNoAuth:
    """Test that owner endpoints require authentication."""

    def test_list_links_requires_auth(self):
        """Test that listing links requires authentication."""
        response = httpx.get(f"{BASE_URL}/api/agents/test-agent/public-links")
        assert response.status_code == 401

    def test_create_link_requires_auth(self):
        """Test that creating links requires authentication."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/test-agent/public-links",
            json={"name": "Test Link"}
        )
        assert response.status_code == 401


class TestOwnerEndpointsWithAuth:
    """Test owner endpoints with authentication."""

    def test_list_links_agent_not_found(self, auth_headers):
        """Test listing links for non-existent agent."""
        response = httpx.get(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/public-links",
            headers=auth_headers
        )
        assert response.status_code == 404

    def test_create_link_agent_not_found(self, auth_headers):
        """Test creating link for non-existent agent."""
        response = httpx.post(
            f"{BASE_URL}/api/agents/nonexistent-agent-xyz/public-links",
            headers=auth_headers,
            json={"name": "Test Link"}
        )
        assert response.status_code == 404


class TestPublicLinkLifecycle:
    """Test full lifecycle of public links with a real agent."""

    @pytest.fixture
    def running_agent(self):
        """Find a running agent for testing."""
        # Get list of agents
        response = httpx.get(f"{BASE_URL}/api/agents")
        # This will fail without auth, but we need to find an agent name somehow
        # Let's use docker to find running agents
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        agents = [name.replace("agent-", "") for name in result.stdout.strip().split("\n") if name]
        if not agents:
            pytest.skip("No running agents found")
        # Use trinity-system as it should exist
        return "trinity-system" if "trinity-system" in agents else agents[0]

    def test_full_link_lifecycle(self, auth_headers, running_agent):
        """Test create, list, update, delete link lifecycle."""
        agent_name = running_agent

        # 1. Create a public link (require_email is now agent-level, #311 follow-up)
        create_response = httpx.post(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
            json={"name": "Test Link for Lifecycle"}
        )

        # #186: the owner dependency now denies with a uniform 404 (was 403).
        if create_response.status_code in (403, 404):
            pytest.skip(f"User doesn't own agent {agent_name}")

        assert create_response.status_code == 200, f"Failed to create link: {create_response.text}"
        created_link = create_response.json()

        assert "id" in created_link
        assert "token" in created_link
        assert "url" in created_link
        assert created_link["name"] == "Test Link for Lifecycle"
        assert created_link["enabled"] == True
        # require_email is no longer a per-link field — it's on the agent policy
        assert "require_email" not in created_link

        link_id = created_link["id"]
        token = created_link["token"]

        try:
            # 2. List links and verify our link is there
            list_response = httpx.get(
                f"{BASE_URL}/api/agents/{agent_name}/public-links",
                headers=auth_headers
            )
            assert list_response.status_code == 200
            links = list_response.json()
            assert any(l["id"] == link_id for l in links)

            # 3. Get the specific link
            get_response = httpx.get(
                f"{BASE_URL}/api/agents/{agent_name}/public-links/{link_id}",
                headers=auth_headers
            )
            assert get_response.status_code == 200
            retrieved_link = get_response.json()
            assert retrieved_link["id"] == link_id

            # 4. Update the link
            update_response = httpx.put(
                f"{BASE_URL}/api/agents/{agent_name}/public-links/{link_id}",
                headers=auth_headers,
                json={
                    "name": "Updated Link Name",
                    "enabled": False
                }
            )
            assert update_response.status_code == 200
            updated_link = update_response.json()
            assert updated_link["name"] == "Updated Link Name"
            assert updated_link["enabled"] == False

            # 5. Verify public endpoint shows link as invalid (normalized reason)
            public_response = httpx.get(f"{BASE_URL}/api/public/link/{token}")
            assert public_response.status_code == 200
            public_info = public_response.json()
            assert public_info["valid"] == False
            assert public_info["reason"] == "invalid_or_expired"

            # 6. Re-enable the link
            enable_response = httpx.put(
                f"{BASE_URL}/api/agents/{agent_name}/public-links/{link_id}",
                headers=auth_headers,
                json={"enabled": True}
            )
            assert enable_response.status_code == 200

            # 7. Verify public endpoint shows link as valid
            public_response2 = httpx.get(f"{BASE_URL}/api/public/link/{token}")
            assert public_response2.status_code == 200
            public_info2 = public_response2.json()
            assert public_info2["valid"] == True

        finally:
            # 8. Delete the link
            delete_response = httpx.delete(
                f"{BASE_URL}/api/agents/{agent_name}/public-links/{link_id}",
                headers=auth_headers
            )
            assert delete_response.status_code == 200

            # Verify link is gone (normalized reason — no oracle)
            verify_deleted = httpx.get(f"{BASE_URL}/api/public/link/{token}")
            assert verify_deleted.json()["valid"] == False
            assert verify_deleted.json()["reason"] == "invalid_or_expired"


class TestEmailVerification:
    """Test email verification flow."""

    @pytest.fixture
    def link_with_email_required(self, auth_headers):
        """Create a link whose agent requires email verification.

        Email verification is now agent-level (agent_ownership.require_email,
        unified with Slack/Telegram via #311 follow-up). We create a link
        then set the agent's access policy to require_email=True, and
        reset both on teardown.
        """
        # Find a running agent
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        agents = [name.replace("agent-", "") for name in result.stdout.strip().split("\n") if name]
        if not agents:
            pytest.skip("No running agents found")
        agent_name = agents[0]

        # Remember prior policy so we can restore it
        policy_before = httpx.get(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
        )
        if policy_before.status_code != 200:
            pytest.skip(f"Could not read access policy: {policy_before.text}")
        prior_policy = policy_before.json()

        # Create the link
        response = httpx.post(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
            json={"name": "Email Required Link"}
        )
        if response.status_code != 200:
            pytest.skip(f"Could not create link: {response.text}")
        link = response.json()
        link["agent_name"] = agent_name  # for teardown

        # Set agent policy to require email
        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={"require_email": True, "open_access": prior_policy.get("open_access", False)},
        )

        yield link

        # Restore policy and delete link
        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={
                "require_email": bool(prior_policy.get("require_email", False)),
                "open_access": bool(prior_policy.get("open_access", False)),
            },
        )
        httpx.delete(
            f"{BASE_URL}/api/agents/{agent_name}/public-links/{link['id']}",
            headers=auth_headers
        )

    def test_link_requires_email(self, link_with_email_required):
        """Test that link info shows email requirement."""
        token = link_with_email_required["token"]

        response = httpx.get(f"{BASE_URL}/api/public/link/{token}")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] == True
        assert data["require_email"] == True

    def test_verification_request_sent(self, link_with_email_required):
        """Test that verification code is sent (console mode)."""
        token = link_with_email_required["token"]

        response = httpx.post(
            f"{BASE_URL}/api/public/verify/request",
            json={"token": token, "email": "test@example.com"}
        )
        if response.status_code == 500:
            pytest.skip("Email service not configured for test recipients")
        assert response.status_code == 200
        data = response.json()
        assert "expires_in_seconds" in data
        assert data["expires_in_seconds"] == 600  # 10 minutes

    def test_verification_rate_limiting(self, link_with_email_required):
        """Test verification rate limiting."""
        token = link_with_email_required["token"]

        # Send 3 requests (should succeed)
        for i in range(3):
            response = httpx.post(
                f"{BASE_URL}/api/public/verify/request",
                json={"token": token, "email": f"rate-limit-test-{i}@example.com"}
            )
            if response.status_code == 500:
                pytest.skip("Email service not configured for test recipients")
            assert response.status_code == 200

        # 4th request for same email should be rate limited
        # Note: Each email has its own rate limit, so we need to reuse an email
        response = httpx.post(
            f"{BASE_URL}/api/public/verify/request",
            json={"token": token, "email": "rate-limit-test-0@example.com"}
        )
        # First 3 for this email, should still work
        # Actually the rate limit is per email, not per link
        # So we'd need to send 3+ for the SAME email

    def test_invalid_verification_code(self, link_with_email_required):
        """Test that invalid verification code is rejected."""
        token = link_with_email_required["token"]
        email = "invalid-code-test@example.com"

        # Request a code first
        httpx.post(
            f"{BASE_URL}/api/public/verify/request",
            json={"token": token, "email": email}
        )

        # Try to verify with wrong code
        response = httpx.post(
            f"{BASE_URL}/api/public/verify/confirm",
            json={"token": token, "email": email, "code": "000000"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["verified"] == False
        assert data["error"] == "invalid_code"


class TestPublicChat:
    """Test public chat functionality."""

    @pytest.fixture
    def public_link(self, auth_headers):
        """Create a public link for chat testing."""
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        agents = [name.replace("agent-", "") for name in result.stdout.strip().split("\n") if name]
        if not agents:
            pytest.skip("No running agents found")
        agent_name = agents[0]

        response = httpx.post(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
            json={"name": "Chat Test Link"}
        )

        if response.status_code != 200:
            pytest.skip(f"Could not create link: {response.text}")

        link = response.json()
        yield link

        # Cleanup
        httpx.delete(
            f"{BASE_URL}/api/agents/{agent_name}/public-links/{link['id']}",
            headers=auth_headers
        )

    def test_chat_without_email_requirement(self, public_link):
        """Test chat when email is not required."""
        token = public_link["token"]

        # Should be able to chat directly
        response = httpx.post(
            f"{BASE_URL}/api/public/chat/{token}",
            json={"message": "Hello, this is a test message"},
            timeout=120.0
        )

        # If agent is not responsive or doesn't have /api/task endpoint, skip
        # Note: Agents created before the Parallel Headless Execution feature (12.1)
        # need to be recreated with the updated base image
        if response.status_code == 502:
            data = response.json()
            if "Failed to process" in data.get("detail", ""):
                pytest.skip("Agent needs updated base image with /api/task endpoint")
            pytest.skip("Agent not responsive")

        if response.status_code == 504:
            pytest.skip("Agent request timed out")

        if response.status_code == 429:
            pytest.skip("Agent at capacity (slot unavailable)")

        assert response.status_code == 200, f"Chat failed: {response.text}"
        data = response.json()
        assert "response" in data


@pytest.mark.smoke
class TestUnifiedAccessPolicy:
    """Public web chat honors the agent-level access policy (#311 follow-up, 2026-04-13).

    The per-link `require_email` flag was retired in favor of
    `agent_ownership.require_email` — the same policy that drives
    Slack and Telegram via adapters.message_router. These tests cover
    the unified behavior from the public-link side.
    """

    @pytest.fixture
    def agent_name(self):
        """Pick a running agent to host the link."""
        import subprocess
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        agents = [n.replace("agent-", "") for n in result.stdout.strip().split("\n") if n]
        if not agents:
            pytest.skip("No running agents found")
        return agents[0]

    @pytest.fixture
    def scoped_link(self, auth_headers, agent_name):
        """Create a link and snapshot the agent's policy; restore + delete on teardown."""
        prior = httpx.get(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
        )
        if prior.status_code != 200:
            pytest.skip(f"Could not read access policy: {prior.text}")
        prior_policy = prior.json()

        resp = httpx.post(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
            json={"name": "Unified Policy Test"},
        )
        if resp.status_code != 200:
            pytest.skip(f"Could not create link: {resp.text}")
        link = resp.json()

        yield {"agent_name": agent_name, "link": link, "prior_policy": prior_policy}

        # Restore policy and delete link
        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={
                "require_email": bool(prior_policy.get("require_email", False)),
                "open_access": bool(prior_policy.get("open_access", False)),
            },
        )
        httpx.delete(
            f"{BASE_URL}/api/agents/{agent_name}/public-links/{link['id']}",
            headers=auth_headers,
        )

    def test_create_link_response_has_no_require_email(self, scoped_link):
        """Per-link require_email was dropped from the API surface."""
        link = scoped_link["link"]
        assert "require_email" not in link, (
            "PublicLinkWithUrl must not expose require_email anymore; "
            "the flag is on the agent's access policy"
        )

    def test_public_link_info_mirrors_agent_policy_false(
        self, auth_headers, scoped_link
    ):
        """PublicLinkInfo.require_email reflects agent policy when policy is off."""
        agent_name = scoped_link["agent_name"]
        token = scoped_link["link"]["token"]

        # Ensure policy is OFF
        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={"require_email": False, "open_access": False},
        )

        resp = httpx.get(f"{BASE_URL}/api/public/link/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["require_email"] is False

    def test_public_link_info_mirrors_agent_policy_true(
        self, auth_headers, scoped_link
    ):
        """Toggling agent policy flips PublicLinkInfo.require_email live."""
        agent_name = scoped_link["agent_name"]
        token = scoped_link["link"]["token"]

        # Flip policy ON
        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={"require_email": True, "open_access": False},
        )

        resp = httpx.get(f"{BASE_URL}/api/public/link/{token}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["require_email"] is True

    def test_chat_requires_session_token_when_agent_policy_on(
        self, auth_headers, scoped_link
    ):
        """With agent policy require_email=True, chat without session_token is 401."""
        agent_name = scoped_link["agent_name"]
        token = scoped_link["link"]["token"]

        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={"require_email": True, "open_access": False},
        )

        resp = httpx.post(
            f"{BASE_URL}/api/public/chat/{token}",
            json={"message": "hi"},
        )
        assert resp.status_code == 401
        assert "Session token required" in resp.json()["detail"]

    def test_verify_request_rejected_when_agent_policy_off(
        self, auth_headers, scoped_link
    ):
        """verify/request returns 400 if the agent doesn't require email."""
        agent_name = scoped_link["agent_name"]
        token = scoped_link["link"]["token"]

        httpx.put(
            f"{BASE_URL}/api/agents/{agent_name}/access-policy",
            headers=auth_headers,
            json={"require_email": False, "open_access": False},
        )

        resp = httpx.post(
            f"{BASE_URL}/api/public/verify/request",
            json={"token": token, "email": "unified-test@example.com"},
        )
        assert resp.status_code == 400
        assert "does not require email verification" in resp.json()["detail"]

    def test_ignores_legacy_require_email_payload(
        self, auth_headers, agent_name
    ):
        """Callers passing the removed require_email field are ignored gracefully.

        Pydantic v2 silently drops unknown fields by default, so old clients
        don't break. The created link MUST NOT expose require_email in the
        response (proves the field isn't being round-tripped via another path).
        """
        resp = httpx.post(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
            json={"name": "Legacy Payload", "require_email": True},
        )
        # #186: the owner dependency now denies with a uniform 404 (was 403).
        if resp.status_code in (403, 404):
            pytest.skip(f"User doesn't own agent {agent_name}")
        assert resp.status_code == 200, resp.text
        link = resp.json()
        try:
            assert "require_email" not in link
        finally:
            httpx.delete(
                f"{BASE_URL}/api/agents/{agent_name}/public-links/{link['id']}",
                headers=auth_headers,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
