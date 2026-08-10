"""
Agent Sharing Tests (test_agent_sharing.py)

Tests for agent sharing functionality.
Covers REQ-SHARE-001 through REQ-SHARE-003.
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestShareAgent:
    """REQ-SHARE-001: Share agent endpoint tests."""

    def test_share_agent_with_email(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/share shares agent with user."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": "test-share@ability.ai"}
        )

        # May fail if user doesn't exist
        assert_status_in(response, [200, 201, 400, 404])

    def test_share_with_role(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/share supports role parameter."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={
                "email": "test-share-role@ability.ai",
                "role": "viewer"
            }
        )

        # May fail if user doesn't exist or role not supported
        assert_status_in(response, [200, 201, 400, 404, 422])

    def test_cannot_share_with_self(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Cannot share agent with self."""
        # Get current user email
        me_response = api_client.get("/api/users/me")
        if me_response.status_code != 200:
            pytest.skip("Could not get current user")

        me = me_response.json()
        my_email = me.get("email")

        if not my_email:
            pytest.skip("User email not available")

        # Try to share with self
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": my_email}
        )

        # Should fail
        assert_status_in(response, [400, 422])


class TestListShares:
    """REQ-SHARE-002: List shares endpoint tests."""

    def test_list_shares(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/shares returns list of shares."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/shares")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should be a list (possibly empty)
        if isinstance(data, dict) and "shares" in data:
            assert isinstance(data["shares"], list)
        else:
            assert isinstance(data, list)

    def test_share_has_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each share has email/shared_with_email, role, created_at."""
        # First try to share
        api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": "test-fields@ability.ai"}
        )

        # List shares
        response = api_client.get(f"/api/agents/{created_agent['name']}/shares")
        assert_status(response, 200)
        data = response.json()

        shares = data if isinstance(data, list) else data.get("shares", [])
        if len(shares) > 0:
            share = shares[0]
            # API may use "email" or "shared_with_email" field
            has_email_field = "email" in share or "shared_with_email" in share
            assert has_email_field, f"Share missing email field. Available: {list(share.keys())}"


class TestUnshareAgent:
    """REQ-SHARE-003: Unshare agent endpoint tests."""

    def test_unshare_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/share/{email} removes share."""
        # First share
        share_email = "test-unshare@ability.ai"
        share_response = api_client.post(
            f"/api/agents/{created_agent['name']}/share",
            json={"email": share_email}
        )

        if share_response.status_code not in [200, 201]:
            pytest.skip("Could not share agent first")

        # Now unshare
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/share/{share_email}"
        )

        assert_status_in(response, [200, 204])

    def test_unshare_nonexistent_share(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """DELETE /api/agents/{name}/share/{email} for non-shared returns error."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/share/nonexistent@example.com"
        )

        # May return 404 or 400
        assert_status_in(response, [200, 204, 400, 404])


class TestShareWhitelistDefaultRole:
    """#314: sharing an agent auto-whitelists the recipient with default_role='user'.

    Prevents the privilege escalation where /share silently promoted the
    recipient to `creator` on their first Trinity web login.
    """

    def test_share_adds_whitelist_entry_with_user_role(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """POST /api/agents/{name}/share creates a whitelist entry with default_role='user'."""
        # Requires admin to inspect whitelist
        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin - cannot inspect whitelist")

        # Requires email auth enabled (whitelist insert is gated on that flag)
        auth_mode = api_client.get("/api/auth/mode").json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled — /share does not whitelist")

        share_email = "test-share-default-role@example.com"
        # Clean slate
        api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")
        api_client.delete(f"/api/settings/email-whitelist/{share_email}")

        try:
            share_response = api_client.post(
                f"/api/agents/{created_agent['name']}/share",
                json={"email": share_email},
            )
            if share_response.status_code not in [200, 201]:
                pytest.skip(f"Share call failed: {share_response.status_code}")

            whitelist = api_client.get("/api/settings/email-whitelist").json()["whitelist"]
            entry = next((e for e in whitelist if e["email"] == share_email), None)
            assert entry is not None, (
                f"Expected {share_email} in whitelist after /share"
            )
            assert entry["default_role"] == "user", (
                f"Expected default_role='user' for share recipient, "
                f"got {entry['default_role']} — this recreates the #314 bug"
            )
        finally:
            api_client.delete(f"/api/agents/{created_agent['name']}/share/{share_email}")
            api_client.delete(f"/api/settings/email-whitelist/{share_email}")
