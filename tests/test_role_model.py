"""
Role Model Tests (test_role_model.py)

Tests for the 4-tier role model: admin / creator / operator / user.
Covers ROLE-001 acceptance criteria from GitHub Issue #143.

Related flow: docs/memory/feature-flows/role-model.md
"""

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_json_response


class TestListUsers:
    """GET /api/users — admin-only user listing."""

    @pytest.mark.smoke
    def test_list_users_returns_array(self, api_client: TrinityApiClient):
        """Admin can list all users."""
        response = api_client.get("/api/users")
        assert_status(response, 200)
        data = response.json()
        assert isinstance(data, list), "Expected list of users"

    @pytest.mark.smoke
    def test_list_users_contains_admin(self, api_client: TrinityApiClient):
        """Admin user appears in the user list."""
        response = api_client.get("/api/users")
        assert_status(response, 200)
        users = response.json()
        usernames = [u["username"] for u in users]
        assert "admin" in usernames, "Admin user should be in the list"

    def test_list_users_has_required_fields(self, api_client: TrinityApiClient):
        """Each user has expected fields."""
        response = api_client.get("/api/users")
        assert_status(response, 200)
        users = response.json()
        assert len(users) > 0, "Expected at least one user"
        for user in users:
            assert "username" in user
            assert "role" in user
            assert user["role"] in {"admin", "creator", "operator", "user"}, \
                f"Unknown role: {user['role']}"

    def test_list_users_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/users requires authentication."""
        response = unauthenticated_client.get("/api/users", auth=False)
        assert_status(response, 401)


class TestUpdateUserRole:
    """PUT /api/users/{username}/role — admin-only role management."""

    def test_update_user_role_success(self, api_client: TrinityApiClient):
        """Admin can change another user's role."""
        # First list users to find a non-admin user (if any)
        users_resp = api_client.get("/api/users")
        assert_status(users_resp, 200)
        users = users_resp.json()
        non_admin = next((u for u in users if u["username"] != "admin"), None)

        if non_admin is None:
            pytest.skip("No non-admin users to test role update")

        target = non_admin["username"]
        original_role = non_admin["role"]
        new_role = "operator" if original_role != "operator" else "creator"

        try:
            response = api_client.put(
                f"/api/users/{target}/role",
                json={"role": new_role}
            )
            assert_status(response, 200)
            data = response.json()
            assert data["role"] == new_role
        finally:
            # Restore original role
            api_client.put(f"/api/users/{target}/role", json={"role": original_role})

    def test_update_own_role_rejected(self, api_client: TrinityApiClient):
        """Admin cannot change their own role."""
        response = api_client.put("/api/users/admin/role", json={"role": "creator"})
        assert_status(response, 400)
        data = response.json()
        assert "own role" in data.get("detail", "").lower()

    def test_update_role_invalid_value(self, api_client: TrinityApiClient):
        """Invalid role value is rejected with 400."""
        response = api_client.put("/api/users/admin/role", json={"role": "superuser"})
        assert_status(response, 400)

    def test_update_role_nonexistent_user(self, api_client: TrinityApiClient):
        """Updating role for non-existent user returns 404."""
        response = api_client.put(
            "/api/users/nonexistent-user-xyz/role",
            json={"role": "operator"}
        )
        assert_status(response, 404)

    def test_update_role_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/users/{username}/role requires authentication."""
        response = unauthenticated_client.put(
            "/api/users/admin/role",
            json={"role": "creator"},
            auth=False
        )
        assert_status(response, 401)


class TestAdminUserHasAdminRole:
    """Admin user created on startup has 'admin' role."""

    @pytest.mark.smoke
    def test_admin_user_has_admin_role(self, api_client: TrinityApiClient):
        """Admin user should have 'admin' role."""
        response = api_client.get("/api/users/me")
        assert_status(response, 200)
        data = response.json()
        assert data.get("role") == "admin", f"Expected admin role, got: {data.get('role')}"
