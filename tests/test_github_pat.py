"""
Per-Agent GitHub PAT Tests (test_github_pat.py)

Tests for per-agent GitHub Personal Access Token configuration (#347).
Covers PAT status, set, clear, and encryption roundtrip.

Endpoints tested:
- GET /api/agents/{name}/github-pat - Get PAT configuration status
- PUT /api/agents/{name}/github-pat - Set per-agent PAT
- DELETE /api/agents/{name}/github-pat - Clear per-agent PAT (revert to global)

NOTE: Encryption roundtrip tests require CREDENTIAL_ENCRYPTION_KEY env var.
"""

import os
import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestGitHubPATStatus:
    """Test GET /api/agents/{name}/github-pat endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_pat_status_no_git_config(self, api_client: TrinityApiClient, created_agent):
        """Agent without git config should show global source."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/github-pat")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "configured", "source", "has_global"])
        assert data["agent_name"] == created_agent['name']
        assert data["configured"] == False
        assert data["source"] == "global"

    def test_get_pat_status_unauthorized(self, unauthenticated_client: TrinityApiClient, created_agent):
        """Should require authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/github-pat",
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestSetGitHubPAT:
    """Test PUT /api/agents/{name}/github-pat endpoint."""

    pytestmark = pytest.mark.smoke

    def test_set_pat_invalid_token(self, api_client: TrinityApiClient, created_agent):
        """Invalid PAT should be rejected with 400."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/github-pat",
            json={"pat": "invalid-token-12345"}
        )

        # GitHub validation should fail
        assert_status(response, 400)
        data = response.json()
        detail = data.get("detail", "")
        assert "Invalid" in detail or "GitHub" in detail or "validate" in detail.lower()

    def test_set_pat_empty_token(self, api_client: TrinityApiClient, created_agent):
        """Empty PAT should be rejected."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/github-pat",
            json={"pat": ""}
        )

        assert_status(response, 400)
        data = response.json()
        assert "empty" in data.get("detail", "").lower()

    def test_set_pat_no_git_config(self, api_client: TrinityApiClient, created_agent):
        """Should fail if agent has no git config."""
        # Assuming test agent doesn't have git initialized
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/github-pat",
            json={"pat": "ghp_test123456789"}
        )

        # Either validation fails first (400) or no git config (400)
        assert_status(response, 400)


class TestClearGitHubPAT:
    """Test DELETE /api/agents/{name}/github-pat endpoint."""

    pytestmark = pytest.mark.smoke

    def test_clear_pat_success(self, api_client: TrinityApiClient, created_agent):
        """Clearing PAT should succeed even if not configured."""
        response = api_client.delete(f"/api/agents/{created_agent['name']}/github-pat")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["source"] == "global"

    def test_clear_pat_unauthorized(self, unauthenticated_client: TrinityApiClient, created_agent):
        """Should require authentication."""
        response = unauthenticated_client.delete(
            f"/api/agents/{created_agent['name']}/github-pat",
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestGitPATDBOperations:
    """Test database operations for GitHub PAT encryption."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encryption and decryption should preserve the PAT value."""
        # Skip if encryption key not configured
        if not os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
            pytest.skip("CREDENTIAL_ENCRYPTION_KEY not configured")

        from db.agent_settings.git_pat import GitPATMixin

        mixin = GitPATMixin()
        original_pat = "ghp_test123456789abcdef"

        encrypted = mixin._encrypt_github_pat(original_pat)
        decrypted = mixin._decrypt_github_pat(encrypted)

        assert decrypted == original_pat

    def test_decrypt_invalid_data(self):
        """Decrypting invalid data should return None."""
        if not os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
            pytest.skip("CREDENTIAL_ENCRYPTION_KEY not configured")

        from db.agent_settings.git_pat import GitPATMixin

        mixin = GitPATMixin()
        result = mixin._decrypt_github_pat("not-valid-encrypted-data")

        assert result is None
