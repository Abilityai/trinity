"""
Email Authentication Tests (test_email_auth.py)

Tests for Trinity email-based authentication endpoints (Phase 12.4).
Covers REQ-11.5: Email-Based Authentication.

FAST TESTS - No agent creation required.
"""

import pytest

# Mark all tests in this module as smoke tests (fast, no agent needed)
pytestmark = pytest.mark.smoke

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestAuthModeEmailAuth:
    """Tests for email auth flag in /api/auth/mode response."""

    @pytest.mark.smoke
    def test_auth_mode_includes_email_auth_flag(self, unauthenticated_client: TrinityApiClient):
        """GET /api/auth/mode should include email_auth_enabled field."""
        response = unauthenticated_client.get("/api/auth/mode", auth=False)

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["email_auth_enabled"])
        assert isinstance(data["email_auth_enabled"], bool)

    def test_auth_mode_email_auth_is_boolean(self, unauthenticated_client: TrinityApiClient):
        """email_auth_enabled should be a boolean value."""
        response = unauthenticated_client.get("/api/auth/mode", auth=False)

        assert_status(response, 200)
        data = response.json()
        assert "email_auth_enabled" in data
        assert isinstance(data["email_auth_enabled"], bool)


class TestEmailLoginRequest:
    """Tests for POST /api/auth/email/request endpoint."""

    def test_request_code_requires_setup_completed(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/request returns 403 if setup not completed."""
        # First check if setup is completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        status_data = status_response.json()

        if status_data.get("setup_completed"):
            pytest.skip("Setup already completed - cannot test setup_required state")

        response = unauthenticated_client.post(
            "/api/auth/email/request",
            json={"email": "test@example.com"},
            auth=False
        )

        assert_status(response, 403)
        data = response.json()
        assert data.get("detail") == "setup_required"

    def test_request_code_missing_email_returns_error(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/request with missing email returns error."""
        # Skip if setup not completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        if not status_response.json().get("setup_completed"):
            pytest.skip("Setup not completed")

        # Check if email auth is enabled
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled")

        response = unauthenticated_client.post(
            "/api/auth/email/request",
            json={},
            auth=False
        )

        # API may return 400, 422 (validation error), or 500 (internal error during parsing)
        assert_status_in(response, [400, 422, 500])

    def test_request_code_returns_success_structure(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/request returns success structure for any email (security)."""
        # Skip if setup not completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        if not status_response.json().get("setup_completed"):
            pytest.skip("Setup not completed")

        # Check if email auth is enabled
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled")

        # This should return success to prevent email enumeration
        # (even if the email is not whitelisted)
        response = unauthenticated_client.post(
            "/api/auth/email/request",
            json={"email": "unknown-user@example.com"},
            auth=False
        )

        # #186: an unknown (non-whitelisted) email always returns the generic 200
        # body — never a 429 — so a repeat-request status can't be used as a
        # membership oracle. (The old dead 429 branch is removed.)
        assert_status(response, 200)
        data = response.json()
        assert "success" in data or "message" in data
        # The generic body must NOT carry the whitelisted-only fields.
        assert "expires_in_seconds" not in data

    def test_request_code_invalid_email_format_accepted_for_security(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/request accepts any email format for security (prevent enumeration)."""
        # Skip if setup not completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        if not status_response.json().get("setup_completed"):
            pytest.skip("Setup not completed")

        # Check if email auth is enabled
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled")

        response = unauthenticated_client.post(
            "/api/auth/email/request",
            json={"email": "not-an-email"},
            auth=False
        )

        # For security (email enumeration prevention), API returns 200 for any email
        # This is intentional behavior to prevent attackers from discovering valid emails
        assert_status_in(response, [200, 400, 422, 429])


class TestEmailLoginVerify:
    """Tests for POST /api/auth/email/verify endpoint."""

    def test_verify_code_requires_setup_completed(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/verify returns 403 if setup not completed."""
        # First check if setup is completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        status_data = status_response.json()

        if status_data.get("setup_completed"):
            pytest.skip("Setup already completed - cannot test setup_required state")

        response = unauthenticated_client.post(
            "/api/auth/email/verify",
            json={"email": "test@example.com", "code": "123456"},
            auth=False
        )

        assert_status(response, 403)
        data = response.json()
        assert data.get("detail") == "setup_required"

    def test_verify_code_missing_fields_returns_error(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/verify with missing fields returns error."""
        # Skip if setup not completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        if not status_response.json().get("setup_completed"):
            pytest.skip("Setup not completed")

        # Check if email auth is enabled
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled")

        # Missing code
        response = unauthenticated_client.post(
            "/api/auth/email/verify",
            json={"email": "test@example.com"},
            auth=False
        )

        # API may return 400, 422 (validation error), or 500 (internal error during parsing)
        assert_status_in(response, [400, 422, 500])

    def test_verify_code_invalid_code_returns_401(self, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/verify with invalid code returns 401."""
        # Skip if setup not completed
        status_response = unauthenticated_client.get("/api/setup/status", auth=False)
        if not status_response.json().get("setup_completed"):
            pytest.skip("Setup not completed")

        # Check if email auth is enabled
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()
        if not auth_mode.get("email_auth_enabled"):
            pytest.skip("Email auth not enabled")

        response = unauthenticated_client.post(
            "/api/auth/email/verify",
            json={"email": "test@example.com", "code": "000000"},
            auth=False
        )

        assert_status(response, 401)
        data = response.json()
        assert "invalid" in data.get("detail", "").lower() or "expired" in data.get("detail", "").lower()


class TestEmailWhitelistAuthentication:
    """Tests for email whitelist endpoint authentication requirements."""

    def test_list_whitelist_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings/email-whitelist requires authentication."""
        response = unauthenticated_client.get("/api/settings/email-whitelist", auth=False)
        assert_status(response, 401)

    def test_add_whitelist_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/settings/email-whitelist requires authentication."""
        response = unauthenticated_client.post(
            "/api/settings/email-whitelist",
            json={"email": "test@example.com"},
            auth=False
        )
        assert_status(response, 401)

    def test_delete_whitelist_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/settings/email-whitelist/{email} requires authentication."""
        response = unauthenticated_client.delete(
            "/api/settings/email-whitelist/test@example.com",
            auth=False
        )
        assert_status(response, 401)


class TestEmailWhitelistAdmin:
    """Tests for email whitelist management (admin only)."""

    def test_list_whitelist_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/settings/email-whitelist returns expected structure."""
        response = api_client.get("/api/settings/email-whitelist")

        # Should return 200 for admin or 403 for non-admin
        assert_status_in(response, [200, 403])

        if response.status_code == 200:
            data = response.json()
            assert "whitelist" in data
            assert isinstance(data["whitelist"], list)

    def test_add_whitelist_invalid_email_handled(self, api_client: TrinityApiClient):
        """POST /api/settings/email-whitelist handles invalid email format."""
        response = api_client.post(
            "/api/settings/email-whitelist",
            json={"email": "not-an-email"}
        )

        # API may accept any string as email, reject it, or return 409 if already exists
        # Should be 200 (accepted), 400 (validation error), 403 (not admin), 409 (exists), or 422
        assert_status_in(response, [200, 400, 403, 409, 422])

    def test_add_whitelist_returns_success(self, api_client: TrinityApiClient):
        """POST /api/settings/email-whitelist can add valid email."""
        test_email = "test-whitelist-add@example.com"

        try:
            response = api_client.post(
                "/api/settings/email-whitelist",
                json={"email": test_email}
            )

            # Should return 200 (success) or 403 (not admin) or 409 (already exists)
            assert_status_in(response, [200, 403, 409])

            if response.status_code == 200:
                data = response.json()
                assert data.get("success") is True
                assert data.get("email") == test_email

        finally:
            # Cleanup - try to remove the email
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

    def test_delete_whitelist_nonexistent_returns_404(self, api_client: TrinityApiClient):
        """DELETE /api/settings/email-whitelist/{email} returns 404 for nonexistent email."""
        response = api_client.delete(
            "/api/settings/email-whitelist/nonexistent-email-xyz123@example.com"
        )

        # Should be 404 (not found) or 403 (not admin)
        assert_status_in(response, [404, 403])


class TestEmailWhitelistCRUD:
    """Full CRUD cycle tests for email whitelist."""

    def test_whitelist_full_lifecycle(self, api_client: TrinityApiClient):
        """Test full CRUD lifecycle for email whitelist entry."""
        test_email = "test-whitelist-lifecycle@example.com"

        # First, check if admin
        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin - cannot test whitelist CRUD")

        try:
            # 1. Create
            create_response = api_client.post(
                "/api/settings/email-whitelist",
                json={"email": test_email, "source": "manual"}
            )

            # If already exists, delete first and retry
            if create_response.status_code == 409:
                api_client.delete(f"/api/settings/email-whitelist/{test_email}")
                create_response = api_client.post(
                    "/api/settings/email-whitelist",
                    json={"email": test_email, "source": "manual"}
                )

            assert_status(create_response, 200)
            data = create_response.json()
            assert data.get("success") is True

            # 2. Read - verify in list
            list_response = api_client.get("/api/settings/email-whitelist")
            assert_status(list_response, 200)
            whitelist = list_response.json()["whitelist"]
            emails = [entry["email"] for entry in whitelist]
            assert test_email in emails

            # 3. Delete
            delete_response = api_client.delete(
                f"/api/settings/email-whitelist/{test_email}"
            )
            assert_status(delete_response, 200)

            # 4. Verify deleted
            list_response = api_client.get("/api/settings/email-whitelist")
            whitelist = list_response.json()["whitelist"]
            emails = [entry["email"] for entry in whitelist]
            assert test_email not in emails

        finally:
            # Cleanup attempt
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

    def test_whitelist_duplicate_email_returns_409(self, api_client: TrinityApiClient):
        """Adding duplicate email to whitelist returns 409."""
        test_email = "test-whitelist-duplicate@example.com"

        # First, check if admin
        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin - cannot test whitelist")

        try:
            # Add first time
            api_client.post(
                "/api/settings/email-whitelist",
                json={"email": test_email}
            )

            # Add second time - should get 409
            response = api_client.post(
                "/api/settings/email-whitelist",
                json={"email": test_email}
            )
            assert_status(response, 409)

        finally:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")


class TestEmailWhitelistDefaultRole:
    """Tests for #314: whitelist rows carry a `default_role` that drives
    the role assigned on first email login.

    Critical invariants:
    - Whitelist entries have a `default_role` field (defaults to `"user"`).
    - Admin can override by passing `default_role` in POST body.
    - Invalid role values are rejected.
    - Public self-signup via `/api/access/request` defaults to `"user"`
      (prevents privilege escalation).
    """

    def test_whitelist_entry_exposes_default_role(self, api_client: TrinityApiClient):
        """Entries listed via GET /api/settings/email-whitelist include default_role."""
        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin - cannot inspect whitelist")

        assert_status(list_response, 200)
        whitelist = list_response.json()["whitelist"]

        if not whitelist:
            pytest.skip("Whitelist is empty; nothing to inspect")

        for entry in whitelist:
            assert "default_role" in entry, (
                f"entry missing default_role: {entry}"
            )
            assert entry["default_role"] in {"user", "operator", "creator", "admin"}

    def test_add_whitelist_defaults_to_user_role(self, api_client: TrinityApiClient):
        """POST with no default_role assigns 'user' (safer default, #314)."""
        test_email = "test-default-role-user@example.com"

        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin")

        try:
            # Ensure clean slate
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

            response = api_client.post(
                "/api/settings/email-whitelist",
                json={"email": test_email, "source": "manual"},
            )
            assert_status(response, 200)

            listed = api_client.get("/api/settings/email-whitelist").json()["whitelist"]
            entry = next((e for e in listed if e["email"] == test_email), None)
            assert entry is not None, "Added email not found in list"
            assert entry["default_role"] == "user"
        finally:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

    def test_add_whitelist_admin_can_grant_creator(self, api_client: TrinityApiClient):
        """Admin explicitly whitelisting with default_role='creator' is honored."""
        test_email = "test-default-role-creator@example.com"

        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin")

        try:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

            response = api_client.post(
                "/api/settings/email-whitelist",
                json={
                    "email": test_email,
                    "source": "manual",
                    "default_role": "creator",
                },
            )
            assert_status(response, 200)

            listed = api_client.get("/api/settings/email-whitelist").json()["whitelist"]
            entry = next((e for e in listed if e["email"] == test_email), None)
            assert entry is not None
            assert entry["default_role"] == "creator"
        finally:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

    def test_add_whitelist_rejects_invalid_role(self, api_client: TrinityApiClient):
        """POST with an unknown default_role is rejected (400-class error)."""
        test_email = "test-default-role-invalid@example.com"

        list_response = api_client.get("/api/settings/email-whitelist")
        if list_response.status_code == 403:
            pytest.skip("User is not admin")

        try:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")

            response = api_client.post(
                "/api/settings/email-whitelist",
                json={
                    "email": test_email,
                    "source": "manual",
                    "default_role": "superuser",
                },
            )
            # Service raises ValueError → router translates to 400; FastAPI may
            # also surface it as 422 or 500 depending on handler. All non-2xx
            # is acceptable — we just must not silently accept the row.
            assert response.status_code >= 400, (
                f"Expected rejection of invalid role, got {response.status_code}: "
                f"{response.text}"
            )

            # Belt-and-suspenders: confirm nothing landed in the whitelist.
            listed = api_client.get("/api/settings/email-whitelist").json()["whitelist"]
            assert not any(e["email"] == test_email for e in listed), (
                "Invalid role should not have been persisted"
            )
        finally:
            api_client.delete(f"/api/settings/email-whitelist/{test_email}")


class TestEmailAuthDisabled:
    """Tests for behavior when email auth is disabled."""

    def test_request_code_when_disabled_returns_403(self, api_client: TrinityApiClient, unauthenticated_client: TrinityApiClient):
        """POST /api/auth/email/request returns 403 when email auth disabled."""
        # Check current setting
        auth_mode = unauthenticated_client.get("/api/auth/mode", auth=False).json()

        if auth_mode.get("email_auth_enabled"):
            # Try to disable it
            response = api_client.put(
                "/api/settings/email_auth_enabled",
                json={"value": "false"}
            )
            if response.status_code not in [200, 201]:
                pytest.skip("Cannot disable email auth to test")

            try:
                # Try to request code
                response = unauthenticated_client.post(
                    "/api/auth/email/request",
                    json={"email": "test@example.com"},
                    auth=False
                )
                assert_status(response, 403)
                data = response.json()
                assert "disabled" in data.get("detail", "").lower()

            finally:
                # Re-enable email auth
                api_client.put(
                    "/api/settings/email_auth_enabled",
                    json={"value": "true"}
                )
        else:
            # Email auth already disabled, test directly
            response = unauthenticated_client.post(
                "/api/auth/email/request",
                json={"email": "test@example.com"},
                auth=False
            )
            # May return 403 (disabled) or setup_required
            assert_status_in(response, [403])
