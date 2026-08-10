"""
Unified Channel Access Control Tests (test_channel_access_control.py)

Tests for the per-agent channel access policy + access-requests inbox
introduced by GitHub issue #311.

Covers the four new endpoints on routers/sharing.py:
- GET  /api/agents/{name}/access-policy
- PUT  /api/agents/{name}/access-policy
- GET  /api/agents/{name}/access-requests
- POST /api/agents/{name}/access-requests/{id}/decide

Feature flow: docs/memory/feature-flows/unified-channel-access-control.md
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_policy(api_client: TrinityApiClient, agent_name: str) -> None:
    """Restore default policy (all flags off) to avoid cross-test pollution."""
    api_client.put(
        f"/api/agents/{agent_name}/access-policy",
        json={"require_email": False, "open_access": False, "group_auth_mode": "none"},
    )


def _create_pending_request(agent_name: str, email: str) -> str | None:
    """Seed a pending access_request directly in the backend container.

    The public API doesn't expose a create endpoint for access_requests —
    they're upserted by the router gate when a verified but non-shared user
    tries to chat. Tests that need a pre-existing request seed one via a
    small docker exec roundtrip. Returns the new request id, or None when
    seeding isn't possible (pytest will skip dependent tests).
    """
    try:
        import subprocess

        cmd = [
            "docker",
            "exec",
            "trinity-backend",
            "python",
            "-c",
            (
                "from database import db; "
                f"r = db.upsert_access_request({agent_name!r}, {email!r}, 'telegram'); "
                "print('RID=' + r['id'])"
            ),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("RID="):
                return line[4:]
        return None
    except Exception:
        return None


def _cleanup_request(agent_name: str, email: str) -> None:
    """Best-effort cleanup: remove share + access_request row."""
    try:
        import subprocess

        subprocess.run(
            [
                "docker",
                "exec",
                "trinity-backend",
                "python",
                "-c",
                (
                    "from database import db; "
                    f"db.unshare_agent({agent_name!r}, 'admin', {email!r}); "
                    "from db.connection import get_db_connection\n"
                    "with get_db_connection() as c:\n"
                    "    cur = c.cursor()\n"
                    f"    cur.execute('DELETE FROM access_requests WHERE agent_name=? AND email=?', ({agent_name!r}, {email!r}))\n"
                    "    c.commit()"
                ),
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GET /access-policy
# ---------------------------------------------------------------------------


class TestGetAccessPolicy:
    """GET /api/agents/{name}/access-policy (#311)."""

    @pytest.mark.smoke
    def test_get_policy_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent,
    ):
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/access-policy"
        )
        assert_status_in(response, [401, 403])

    @pytest.mark.smoke
    def test_get_policy_returns_shape(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/access-policy"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["require_email", "open_access", "group_auth_mode"])
        assert isinstance(data["require_email"], bool)
        assert isinstance(data["open_access"], bool)
        assert isinstance(data["group_auth_mode"], str)

    @pytest.mark.smoke
    def test_get_policy_default_values(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """New agents default to all flags off (legacy permissive behavior)."""
        _reset_policy(api_client, created_agent["name"])
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/access-policy"
        )
        assert_status(response, 200)
        data = response.json()
        assert data["require_email"] is False
        assert data["open_access"] is False
        assert data["group_auth_mode"] == "none"

    @pytest.mark.smoke
    def test_get_policy_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient,
    ):
        response = api_client.get(
            "/api/agents/does-not-exist-xyz/access-policy"
        )
        assert_status_in(response, [403, 404])


# ---------------------------------------------------------------------------
# PUT /access-policy
# ---------------------------------------------------------------------------


class TestUpdateAccessPolicy:
    """PUT /api/agents/{name}/access-policy (#311)."""

    @pytest.mark.smoke
    def test_put_policy_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent,
    ):
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/access-policy",
            json={"require_email": True, "open_access": False},
        )
        assert_status_in(response, [401, 403])

    @pytest.mark.smoke
    def test_put_policy_enables_require_email(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        agent_name = created_agent["name"]
        try:
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": True, "open_access": False},
            )
            assert_status(response, 200)
            data = response.json()
            assert data["require_email"] is True
            assert data["open_access"] is False
            # group_auth_mode defaults to "none" when not provided
            assert data["group_auth_mode"] == "none"

            # Verify persisted via GET
            get_response = api_client.get(
                f"/api/agents/{agent_name}/access-policy"
            )
            assert get_response.json()["require_email"] is True
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_enables_open_access(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        agent_name = created_agent["name"]
        try:
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": False, "open_access": True},
            )
            assert_status(response, 200)
            data = response.json()
            assert data["open_access"] is True
            assert data["require_email"] is False
            # group_auth_mode defaults to "none" when not provided
            assert data["group_auth_mode"] == "none"
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_both_flags(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        agent_name = created_agent["name"]
        try:
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": True, "open_access": True},
            )
            assert_status(response, 200)
            data = response.json()
            assert data["require_email"] is True
            assert data["open_access"] is True
            # group_auth_mode defaults to "none" when not provided
            assert data["group_auth_mode"] == "none"
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_validates_required_fields(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Both fields are required by the Pydantic model."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/access-policy",
            json={"require_email": True},
        )
        assert_status_in(response, [400, 422])

    @pytest.mark.smoke
    def test_put_policy_rejects_non_boolean(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/access-policy",
            json={"require_email": "yes please", "open_access": False},
        )
        assert_status_in(response, [400, 422])

    @pytest.mark.smoke
    def test_put_policy_sets_group_auth_mode_any_verified(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Setting group_auth_mode to 'any_verified' requires at least one verified member."""
        agent_name = created_agent["name"]
        try:
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": True, "open_access": False, "group_auth_mode": "any_verified"},
            )
            assert_status(response, 200)
            data = response.json()
            assert data["group_auth_mode"] == "any_verified"
            assert data["require_email"] is True

            # Verify persisted via GET
            get_response = api_client.get(
                f"/api/agents/{agent_name}/access-policy"
            )
            assert get_response.json()["group_auth_mode"] == "any_verified"
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_group_auth_mode_none(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Setting group_auth_mode to 'none' (default) allows unrestricted group access."""
        agent_name = created_agent["name"]
        try:
            # First set to any_verified
            api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": True, "open_access": False, "group_auth_mode": "any_verified"},
            )
            # Then reset to none
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": True, "open_access": False, "group_auth_mode": "none"},
            )
            assert_status(response, 200)
            data = response.json()
            assert data["group_auth_mode"] == "none"
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_invalid_group_auth_mode_falls_back_to_none(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Invalid group_auth_mode values silently fall back to 'none' for backwards compatibility."""
        agent_name = created_agent["name"]
        try:
            response = api_client.put(
                f"/api/agents/{agent_name}/access-policy",
                json={"require_email": False, "open_access": False, "group_auth_mode": "invalid_mode"},
            )
            assert_status(response, 200)
            data = response.json()
            # Invalid values fall back to "none" rather than failing
            assert data["group_auth_mode"] == "none"
        finally:
            _reset_policy(api_client, agent_name)

    @pytest.mark.smoke
    def test_put_policy_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient,
    ):
        response = api_client.put(
            "/api/agents/does-not-exist-xyz/access-policy",
            json={"require_email": False, "open_access": False},
        )
        assert_status_in(response, [403, 404])


# ---------------------------------------------------------------------------
# GET /access-requests
# ---------------------------------------------------------------------------


class TestListAccessRequests:
    """GET /api/agents/{name}/access-requests (#311)."""

    @pytest.mark.smoke
    def test_list_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent,
    ):
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/access-requests"
        )
        assert_status_in(response, [401, 403])

    @pytest.mark.smoke
    def test_list_returns_array(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/access-requests"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    @pytest.mark.smoke
    def test_list_accepts_status_filter(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        for status in ("pending", "approved", "denied"):
            response = api_client.get(
                f"/api/agents/{created_agent['name']}/access-requests",
                params={"status": status},
            )
            assert_status(response, 200)
            assert isinstance(response.json(), list)

    @pytest.mark.smoke
    def test_list_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient,
    ):
        response = api_client.get(
            "/api/agents/does-not-exist-xyz/access-requests"
        )
        assert_status_in(response, [403, 404])

    @pytest.mark.smoke
    def test_list_contains_seeded_request(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """A seeded pending request shows up in the listing."""
        agent_name = created_agent["name"]
        email = f"test-seeded-{agent_name[:10]}@example.com"

        request_id = _create_pending_request(agent_name, email)
        if not request_id:
            pytest.skip("Cannot seed access_request (docker exec unavailable)")

        try:
            response = api_client.get(
                f"/api/agents/{agent_name}/access-requests",
                params={"status": "pending"},
            )
            assert_status(response, 200)
            ids = [r["id"] for r in response.json()]
            assert request_id in ids, f"Seeded id missing from list: {ids}"
        finally:
            _cleanup_request(agent_name, email)


# ---------------------------------------------------------------------------
# POST /access-requests/{id}/decide
# ---------------------------------------------------------------------------


class TestDecideAccessRequest:
    """POST /api/agents/{name}/access-requests/{id}/decide (#311)."""

    @pytest.mark.smoke
    def test_decide_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient,
        created_agent,
    ):
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/access-requests/nonexistent/decide",
            json={"approve": True},
        )
        assert_status_in(response, [401, 403])

    @pytest.mark.smoke
    def test_decide_nonexistent_request_returns_404(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/access-requests/does-not-exist/decide",
            json={"approve": True},
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_decide_validates_payload(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/access-requests/anything/decide",
            json={},
        )
        assert_status_in(response, [400, 422])

    @pytest.mark.smoke
    def test_approve_inserts_share_and_marks_approved(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Approve adds email to agent_sharing and sets status=approved."""
        agent_name = created_agent["name"]
        email = f"test-approve-{agent_name[:10]}@example.com"

        request_id = _create_pending_request(agent_name, email)
        if not request_id:
            pytest.skip("Cannot seed access_request (docker exec unavailable)")

        try:
            decide_resp = api_client.post(
                f"/api/agents/{agent_name}/access-requests/{request_id}/decide",
                json={"approve": True},
            )
            assert_status(decide_resp, 200)
            decided = decide_resp.json()
            assert decided["status"] == "approved"
            assert decided["email"] == email

            # Email should now be in the agent's shares
            shares_resp = api_client.get(f"/api/agents/{agent_name}/shares")
            assert_status(shares_resp, 200)
            shares = shares_resp.json()
            shares = shares if isinstance(shares, list) else shares.get("shares", [])
            emails = [
                s.get("shared_with_email") or s.get("email")
                for s in shares
            ]
            assert email in emails, f"Email missing from shares: {emails}"
        finally:
            api_client.delete(f"/api/agents/{agent_name}/share/{email}")
            _cleanup_request(agent_name, email)

    @pytest.mark.smoke
    def test_deny_marks_denied_without_sharing(
        self,
        api_client: TrinityApiClient,
        created_agent,
    ):
        """Deny sets status=denied and does NOT insert into agent_sharing."""
        agent_name = created_agent["name"]
        email = f"test-deny-{agent_name[:10]}@example.com"

        request_id = _create_pending_request(agent_name, email)
        if not request_id:
            pytest.skip("Cannot seed access_request (docker exec unavailable)")

        try:
            decide_resp = api_client.post(
                f"/api/agents/{agent_name}/access-requests/{request_id}/decide",
                json={"approve": False},
            )
            assert_status(decide_resp, 200)
            assert decide_resp.json()["status"] == "denied"

            shares_resp = api_client.get(f"/api/agents/{agent_name}/shares")
            shares = shares_resp.json()
            shares = shares if isinstance(shares, list) else shares.get("shares", [])
            emails = [
                s.get("shared_with_email") or s.get("email")
                for s in shares
            ]
            assert email not in emails, \
                f"Denied email unexpectedly in shares: {emails}"
        finally:
            _cleanup_request(agent_name, email)
