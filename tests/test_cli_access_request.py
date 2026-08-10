"""
CLI Access Request Tests (test_cli_access_request.py)

Tests for the POST /api/access/request endpoint (CLI-002).

trinity-enterprise#10: public self-signup is now DISABLED by default (secure
default). The endpoint returns 403 unless an operator explicitly enables it via
the `public_access_requests_enabled` system setting / env. These tests cover
both states: the default-closed gate, and the operator-enabled auto-whitelist
behaviour (toggled per-test via the admin settings endpoint and restored).

FAST TESTS - No agent creation required.
"""

import uuid
import pytest

pytestmark = pytest.mark.smoke

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)

_SETTING_KEY = "public_access_requests_enabled"


def _unique_email():
    """Generate a unique test email to avoid whitelist collisions."""
    return f"cli-test-{uuid.uuid4().hex[:8]}@example.com"


def _set_self_signup(api_client: TrinityApiClient, enabled: bool):
    """Toggle the public-self-signup setting via the admin endpoint."""
    return api_client.put(
        f"/api/settings/{_SETTING_KEY}",
        json={"value": "true" if enabled else "false"},
    )


class TestAccessRequestDisabledByDefault:
    """trinity-enterprise#10: default-closed gate must reject self-signup."""

    def test_disabled_returns_403(self, api_client: TrinityApiClient,
                                  unauthenticated_client: TrinityApiClient):
        """With self-signup disabled, the endpoint returns 403 and does not whitelist."""
        # Ensure the secure default is in effect for this instance.
        if _set_self_signup(api_client, False).status_code != 200:
            pytest.skip("Cannot toggle public_access_requests_enabled (admin endpoint unavailable)")

        email = _unique_email()
        response = unauthenticated_client.post(
            "/api/access/request",
            json={"email": email},
            auth=False,
        )
        assert_status(response, 403)

        # The email must NOT have been added to the whitelist (the actual gate;
        # /api/auth/email/request 200s even for unknown emails to prevent
        # enumeration, so we check the authoritative whitelist directly).
        wl = api_client.get("/api/settings/email-whitelist")
        if wl.status_code == 200:
            data = wl.json()
            entries = data if isinstance(data, list) else data.get("emails", [])
            present = {
                (e.get("email") if isinstance(e, dict) else e) for e in entries
            }
            assert email not in present, "Disabled self-signup must not whitelist the email"


@pytest.fixture
def self_signup_enabled(api_client: TrinityApiClient):
    """Enable public self-signup for the duration of a test, then restore off."""
    resp = _set_self_signup(api_client, True)
    if resp.status_code != 200:
        pytest.skip("Cannot toggle public_access_requests_enabled (admin endpoint unavailable)")
    try:
        yield
    finally:
        _set_self_signup(api_client, False)


class TestAccessRequestEnabled:
    """Operator-enabled frictionless self-signup (opt-in)."""

    def test_access_request_grants_new_email(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """New email is auto-whitelisted and returns already_registered=False."""
        email = _unique_email()
        response = unauthenticated_client.post(
            "/api/access/request", json={"email": email}, auth=False,
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["success", "message", "already_registered"])
        assert data["success"] is True
        assert data["already_registered"] is False

    def test_access_request_idempotent(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """Calling twice for same email returns already_registered=True."""
        email = _unique_email()
        unauthenticated_client.post("/api/access/request", json={"email": email}, auth=False)
        response = unauthenticated_client.post("/api/access/request", json={"email": email}, auth=False)
        assert_status(response, 200)
        assert response.json()["already_registered"] is True

    def test_access_request_missing_email(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """Missing email returns 400 (validation runs once the gate is open)."""
        response = unauthenticated_client.post("/api/access/request", json={}, auth=False)
        assert_status(response, 400)

    def test_access_request_invalid_email(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """Email without @ returns 400."""
        response = unauthenticated_client.post(
            "/api/access/request", json={"email": "not-an-email"}, auth=False,
        )
        assert_status(response, 400)

    def test_access_request_normalizes_email(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """Email is lowercased — uppercase variant returns already_registered."""
        base = uuid.uuid4().hex[:8]
        unauthenticated_client.post(
            "/api/access/request", json={"email": f"cli-test-{base}@example.com"}, auth=False,
        )
        response = unauthenticated_client.post(
            "/api/access/request", json={"email": f"CLI-TEST-{base}@EXAMPLE.COM"}, auth=False,
        )
        assert_status(response, 200)
        assert response.json()["already_registered"] is True

    def test_access_request_response_fields(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """Response has exactly the expected fields."""
        email = _unique_email()
        response = unauthenticated_client.post("/api/access/request", json={"email": email}, auth=False)
        assert_status(response, 200)
        assert set(response.json().keys()) == {"success", "message", "already_registered"}

    def test_access_request_whitelists_for_login(self, self_signup_enabled, unauthenticated_client: TrinityApiClient):
        """After access request, email can request a login code (proves whitelist works)."""
        email = _unique_email()
        reg = unauthenticated_client.post("/api/access/request", json={"email": email}, auth=False)
        assert_status(reg, 200)
        login = unauthenticated_client.post("/api/auth/email/request", json={"email": email}, auth=False)
        assert_status(login, 200)
        assert login.json()["success"] is True


class TestAccessRequestCleanup:
    """Cleanup test whitelist entries created during testing."""

    def test_cleanup_test_emails(self, api_client: TrinityApiClient):
        """Remove any cli-test-* emails from whitelist (best-effort cleanup)."""
        # Ensure self-signup is left disabled after the suite.
        _set_self_signup(api_client, False)

        response = api_client.get("/api/settings/email-whitelist")
        if response.status_code != 200:
            pytest.skip("Cannot access whitelist endpoint")

        data = response.json()
        emails = data if isinstance(data, list) else data.get("emails", [])

        for entry in emails:
            email = entry.get("email", entry) if isinstance(entry, dict) else entry
            if isinstance(email, str) and email.startswith("cli-test-"):
                api_client.delete(f"/api/settings/email-whitelist/{email}")
