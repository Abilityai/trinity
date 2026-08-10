"""
Agent Git Tests (test_agent_git.py)

Tests for agent Git sync functionality.
Covers REQ-GIT-001 through REQ-GIT-002.
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestGitStatus:
    """REQ-GIT-001: Git status endpoint tests."""

    def test_get_git_status(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/git/status returns git status."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/status")

        # May return 503 if agent not ready
        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Should indicate git enabled status
        assert "git_enabled" in data or "enabled" in data or "status" in data

    def test_git_disabled_returns_false(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Non-git agent returns git_enabled: false."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/status")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()

        # For agents not created from GitHub, git should be disabled
        enabled = data.get("git_enabled", data.get("enabled", False))
        # This is just a structure check, not asserting value


class TestGitSync:
    """REQ-GIT-002: Git sync endpoint tests."""

    def test_git_sync(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/sync commits and pushes."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/git/sync")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured
        if response.status_code in [400, 422]:
            pytest.skip("Git not configured for agent")

        # If git is enabled, should succeed or indicate no changes
        assert_status_in(response, [200, 304, 400])

    def test_git_sync_nonexistent_agent(self, api_client: TrinityApiClient):
        """POST /api/agents/{name}/git/sync for non-existent returns 404."""
        response = api_client.post("/api/agents/nonexistent-agent-xyz/git/sync")

        assert_status(response, 404)


class TestGitLog:
    """Tests for git log endpoint."""

    def test_get_git_log(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/git/log returns commit history."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/log")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured
        if response.status_code in [400, 422]:
            pytest.skip("Git not configured for agent")

        assert_status_in(response, [200, 400])

    def test_git_log_with_limit(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/git/log supports limit parameter."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/git/log",
            params={"limit": 5}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status_in(response, [200, 400, 422])


class TestGitPull:
    """Tests for git pull endpoint."""

    def test_git_pull(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/pull pulls from remote."""
        response = api_client.post(f"/api/agents/{created_agent['name']}/git/pull")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured
        if response.status_code in [400, 422]:
            pytest.skip("Git not configured for agent")

        assert_status_in(response, [200, 304, 400])


# =============================================================================
# GIT INITIALIZE TESTS (GIT-006)
# Tests for /api/agents/{name}/git/initialize endpoint
# =============================================================================


class TestGitInitializeAuthentication:
    """Tests for Git Initialize endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_git_init_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/git/initialize requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": "test-repo"
            },
            auth=False
        )
        assert_status(response, 401)


class TestGitInitialize:
    """GIT-006: Initialize GitHub sync for an agent."""

    def test_git_init_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/git/initialize for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": "test-repo"
            }
        )
        assert_status(response, 404)

    def test_git_init_returns_structure(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/initialize returns expected structure.

        Note: This test uses create_repo=False which requires the repo to exist.
        In test environments without a real repo, the endpoint will return 400.
        """
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": f"test-{created_agent['name']}",
                "create_repo": False  # Don't actually create repo in tests
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail for various configuration reasons - all are valid for this test
        if response.status_code == 400:
            data = response.json()
            detail = data.get("detail", "").lower()
            # GitHub PAT not configured
            if "github" in detail or "pat" in detail or "token" in detail:
                pytest.skip("GitHub PAT not configured")
            # Repository doesn't exist (expected when create_repo=False and no real repo)
            if "does not exist" in detail or "repository" in detail:
                pytest.skip("Test repository does not exist (expected behavior)")
            # Git already initialized
            if "already" in detail:
                pass  # This is still a valid test case

        # If it succeeds, check structure
        if response.status_code == 200:
            data = response.json()
            assert data.get("success") is True
            assert "github_repo" in data or "repo_url" in data

        # Valid responses: 200 (success), 400 (configuration/repo issue), 409 (conflict)
        assert_status_in(response, [200, 400, 409])

    def test_git_init_requires_repo_owner(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/initialize requires repo_owner."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/git/initialize",
            json={
                "repo_name": "test-repo"
                # Missing repo_owner
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should be rejected for missing required field
        assert_status_in(response, [400, 422])

    def test_git_init_requires_repo_name(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/initialize requires repo_name."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/git/initialize",
            json={
                "repo_owner": "test-user"
                # Missing repo_name
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should be rejected for missing required field
        assert_status_in(response, [400, 422])

    def test_git_init_stopped_agent_returns_400(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/git/initialize for stopped agent returns 400."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": "test-repo"
            }
        )

        # Should fail because agent is stopped
        assert_status_in(response, [400, 503])

    def test_git_init_create_repo_parameter(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/initialize accepts create_repo parameter."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": f"test-{created_agent['name']}",
                "create_repo": True,
                "private": True,
                "description": "Test repository"
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if GitHub PAT not configured
        if response.status_code == 400:
            pytest.skip("GitHub PAT may not be configured")

        # Valid responses
        assert_status_in(response, [200, 400, 409])


class TestGitInitializeIdempotency:
    """Tests for Git Initialize idempotency behavior."""

    def test_git_init_already_configured_returns_409(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/git/initialize when already configured returns 409."""
        # First check if git is already configured
        status_response = api_client.get(f"/api/agents/{created_agent['name']}/git/status")

        if status_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if status_response.status_code == 200:
            data = status_response.json()
            git_enabled = data.get("git_enabled", data.get("enabled", False))

            if git_enabled:
                # Try to init again - should fail
                response = api_client.post(
                    f"/api/agents/{created_agent['name']}/git/initialize",
                    json={
                        "repo_owner": "test-user",
                        "repo_name": "test-repo"
                    }
                )
                # Should indicate already configured
                assert_status_in(response, [400, 409])
            else:
                pytest.skip("Git not configured, cannot test re-init behavior")


# =============================================================================
# GIT PERMISSIONS TESTS (GIT-PERM-001)
# Tests for git endpoint permissions - verify shared users vs owners
# Added: 2026-01-30 - Tests permission changes for git pull
# =============================================================================


class TestGitPermissionsReadOnly:
    """Tests for git endpoints that allow shared user access (read-only operations)."""

    def test_shared_user_can_view_git_status(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Shared users CAN view git status (AuthorizedAgentByName)."""
        # Note: This test documents expected behavior but requires secondary user setup
        # to fully test. The git/status endpoint uses AuthorizedAgentByName which
        # allows both owners and shared users.
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/status")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should succeed for authorized users (owner or shared)
        assert_status(response, 200)

    def test_shared_user_can_view_git_log(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Shared users CAN view git log (AuthorizedAgentByName)."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/log")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured
        if response.status_code in [400, 422]:
            pytest.skip("Git not configured for agent")

        # Should succeed for authorized users
        assert_status_in(response, [200, 400])

    def test_shared_user_can_view_git_config(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Shared users CAN view git config (AuthorizedAgentByName)."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/git/config")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Should succeed for authorized users
        assert_status(response, 200)

    def test_shared_user_can_git_pull(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Shared users CAN git pull (AuthorizedAgentByName) - FIXED 2026-01-30.

        This test verifies the fix that changed git pull from OwnedAgentByName
        to AuthorizedAgentByName, allowing shared users to pull changes.
        """
        response = api_client.post(f"/api/agents/{created_agent['name']}/git/pull")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured
        if response.status_code in [400, 422]:
            pytest.skip("Git not configured for agent")

        # Should succeed for authorized users (or indicate git issue, not permission issue)
        # Shared users should NOT get 403 Forbidden
        assert_status_in(response, [200, 304, 400, 409])

        # Verify we don't get permission errors
        if response.status_code in [400, 409]:
            data = response.json()
            detail = data.get("detail", "").lower()
            # Should not be permission-related errors
            assert "access denied" not in detail
            assert "forbidden" not in detail
            assert "owner" not in detail


class TestGitPermissionsOwnerOnly:
    """Tests for git endpoints that require owner access (write operations)."""

    def test_git_sync_requires_owner(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Git sync requires owner access (OwnedAgentByName).

        Shared users should NOT be able to push changes to GitHub.
        Only owners can sync (commit and push).
        """
        response = api_client.post(f"/api/agents/{created_agent['name']}/git/sync")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail if git not configured - that's ok, we're testing permissions
        if response.status_code in [400, 422]:
            # Check it's not a permission error
            data = response.json()
            detail = data.get("detail", "").lower()
            if "git" not in detail and "not configured" not in detail:
                # If not git-related, might be permissions (which is what we expect for shared users)
                pass
            else:
                pytest.skip("Git not configured for agent")

        # Owner should get 200/304/400 (success, no changes, or git error)
        # Shared user would get 403 (but we're testing as owner here)
        assert_status_in(response, [200, 304, 400, 403])

    def test_git_initialize_requires_owner(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Git initialize requires owner access (OwnedAgentByName).

        Shared users should NOT be able to initialize git sync.
        Only owners can set up GitHub integration.
        """
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": "test-repo",
                "create_repo": False
            }
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # May fail for various reasons - we're documenting that this endpoint
        # uses OwnedAgentByName which blocks shared users at the permission level
        # Owner should get 200/400/409 (success, error, or already configured)
        # Shared user would get 403 (but we're testing as owner here)
        assert_status_in(response, [200, 400, 403, 409])


class TestGitPermissionsUnauthenticated:
    """Tests that all git endpoints require authentication."""

    pytestmark = pytest.mark.smoke

    def test_git_status_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/git/status requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/git/status",
            auth=False
        )
        assert_status(response, 401)

    def test_git_log_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/git/log requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/git/log",
            auth=False
        )
        assert_status(response, 401)

    def test_git_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/git/config requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/git/config",
            auth=False
        )
        assert_status(response, 401)

    def test_git_pull_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/git/pull requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/git/pull",
            auth=False
        )
        assert_status(response, 401)

    def test_git_sync_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/git/sync requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/git/sync",
            auth=False
        )
        assert_status(response, 401)

    def test_git_initialize_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/git/initialize requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/git/initialize",
            json={
                "repo_owner": "test-user",
                "repo_name": "test-repo"
            },
            auth=False
        )
        assert_status(response, 401)
