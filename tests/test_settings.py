"""
Settings Tests (test_settings.py)

Tests for Trinity system settings endpoints, including the system-wide Trinity prompt.
Covers REQ-SETTINGS-001 (10.6 System-Wide Trinity Prompt).

Combines fast tests (Settings API) and slow tests (agent injection verification).
"""

import pytest
import time
import uuid

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)
from utils.cleanup import cleanup_test_agent


class TestSettingsEndpointsAuthentication:
    """Tests for Settings API authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_list_settings_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings requires authentication."""
        response = unauthenticated_client.get("/api/settings", auth=False)
        assert_status(response, 401)

    def test_get_setting_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings/{key} requires authentication."""
        response = unauthenticated_client.get("/api/settings/trinity_prompt", auth=False)
        assert_status(response, 401)

    def test_update_setting_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/settings/{key} requires authentication."""
        response = unauthenticated_client.put(
            "/api/settings/trinity_prompt",
            json={"value": "test"},
            auth=False
        )
        assert_status(response, 401)

    def test_delete_setting_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/settings/{key} requires authentication."""
        response = unauthenticated_client.delete("/api/settings/trinity_prompt", auth=False)
        assert_status(response, 401)


class TestSettingsEndpointsAdmin:
    """Tests for Settings API admin-only access.

    Note: These tests use admin credentials (default dev mode user).
    In a multi-user scenario with non-admin users, we'd need additional tests.
    """

    pytestmark = pytest.mark.smoke

    def test_list_settings_returns_array(self, api_client: TrinityApiClient):
        """GET /api/settings returns array of settings."""
        response = api_client.get("/api/settings")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert isinstance(data, list)

    def test_get_nonexistent_setting_returns_404(self, api_client: TrinityApiClient):
        """GET /api/settings/{key} returns 404 for nonexistent setting."""
        response = api_client.get("/api/settings/nonexistent_key_12345")
        assert_status(response, 404)

    def test_create_and_get_setting(self, api_client: TrinityApiClient):
        """PUT /api/settings/{key} creates setting, GET retrieves it."""
        test_key = f"test_setting_{uuid.uuid4().hex[:8]}"
        test_value = "Test value for integration test"

        try:
            # Create setting
            response = api_client.put(
                f"/api/settings/{test_key}",
                json={"value": test_value}
            )
            assert_status(response, 200)
            data = assert_json_response(response)
            assert_has_fields(data, ["key", "value", "updated_at"])
            assert data["key"] == test_key
            assert data["value"] == test_value

            # Get setting
            response = api_client.get(f"/api/settings/{test_key}")
            assert_status(response, 200)
            data = response.json()
            assert data["key"] == test_key
            assert data["value"] == test_value
        finally:
            # Cleanup
            api_client.delete(f"/api/settings/{test_key}")

    def test_update_existing_setting(self, api_client: TrinityApiClient):
        """PUT /api/settings/{key} updates existing setting."""
        test_key = f"test_update_{uuid.uuid4().hex[:8]}"

        try:
            # Create initial setting
            api_client.put(f"/api/settings/{test_key}", json={"value": "initial"})

            # Update setting
            response = api_client.put(
                f"/api/settings/{test_key}",
                json={"value": "updated"}
            )
            assert_status(response, 200)
            data = response.json()
            assert data["value"] == "updated"

            # Verify update persisted
            response = api_client.get(f"/api/settings/{test_key}")
            assert response.json()["value"] == "updated"
        finally:
            api_client.delete(f"/api/settings/{test_key}")

    def test_delete_setting(self, api_client: TrinityApiClient):
        """DELETE /api/settings/{key} removes setting."""
        test_key = f"test_delete_{uuid.uuid4().hex[:8]}"

        # Create setting
        api_client.put(f"/api/settings/{test_key}", json={"value": "to_delete"})

        # Delete setting
        response = api_client.delete(f"/api/settings/{test_key}")
        assert_status(response, 200)
        data = response.json()
        assert data.get("deleted") is True

        # Verify deletion
        response = api_client.get(f"/api/settings/{test_key}")
        assert_status(response, 404)

    def test_delete_nonexistent_setting_returns_success(self, api_client: TrinityApiClient):
        """DELETE /api/settings/{key} returns success even for nonexistent (idempotent)."""
        response = api_client.delete("/api/settings/nonexistent_key_99999")
        assert_status(response, 200)
        data = response.json()
        # API returns success but deleted=false for nonexistent
        assert data.get("deleted") is False


class TestTrinityPromptSetting:
    """Tests specifically for the trinity_prompt setting."""

    pytestmark = pytest.mark.smoke

    def test_trinity_prompt_crud(self, api_client: TrinityApiClient):
        """Full CRUD cycle for trinity_prompt setting."""
        test_value = "Test Trinity prompt for integration test"

        try:
            # Create
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": test_value}
            )
            assert_status(response, 200)
            assert response.json()["key"] == "trinity_prompt"

            # Read
            response = api_client.get("/api/settings/trinity_prompt")
            assert_status(response, 200)
            assert response.json()["value"] == test_value

            # Update
            updated_value = "Updated Trinity prompt"
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": updated_value}
            )
            assert_status(response, 200)

            response = api_client.get("/api/settings/trinity_prompt")
            assert response.json()["value"] == updated_value

            # Delete
            response = api_client.delete("/api/settings/trinity_prompt")
            assert_status(response, 200)

            response = api_client.get("/api/settings/trinity_prompt")
            assert_status(response, 404)
        finally:
            # Ensure cleanup
            api_client.delete("/api/settings/trinity_prompt")

    def test_trinity_prompt_supports_markdown(self, api_client: TrinityApiClient):
        """Trinity prompt can contain Markdown content."""
        markdown_content = """## Guidelines

1. **Be helpful** and professional
2. Always *explain* your reasoning
3. Use `code blocks` when appropriate

### Sub-section
- Item one
- Item two
"""
        try:
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": markdown_content}
            )
            assert_status(response, 200)

            response = api_client.get("/api/settings/trinity_prompt")
            assert response.json()["value"] == markdown_content
        finally:
            api_client.delete("/api/settings/trinity_prompt")

    def test_trinity_prompt_appears_in_list(self, api_client: TrinityApiClient):
        """trinity_prompt setting appears in GET /api/settings list."""
        try:
            # Create the setting
            api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": "test"}
            )

            # Get all settings
            response = api_client.get("/api/settings")
            assert_status(response, 200)
            settings = response.json()

            # Find trinity_prompt in list
            trinity_settings = [s for s in settings if s["key"] == "trinity_prompt"]
            assert len(trinity_settings) == 1
            assert trinity_settings[0]["value"] == "test"
        finally:
            api_client.delete("/api/settings/trinity_prompt")


class TestTrinityPromptRuntimeInjection:
    """Tests for Trinity prompt runtime injection via --append-system-prompt.

    Issue #136: Platform instructions are now injected at runtime on every
    Claude Code invocation via --append-system-prompt. No file-based injection.

    These tests verify:
    1. Agent start no longer returns trinity_injection field
    2. The trinity_prompt setting CRUD still works (read at runtime)
    3. No CLAUDE.local.md file is written by the platform
    """

    def test_start_agent_no_trinity_injection_field(self, api_client: TrinityApiClient, created_agent):
        """Agent start response no longer includes trinity_injection field."""
        agent_name = created_agent["name"]

        # Stop then start
        api_client.post(f"/api/agents/{agent_name}/stop")
        time.sleep(2)
        response = api_client.post(f"/api/agents/{agent_name}/start")

        if response.status_code == 200:
            data = response.json()
            # trinity_injection field should no longer be in the response
            assert "trinity_injection" not in data, \
                "Start response should not contain trinity_injection (removed in #136)"
            assert "trinity_result" not in data, \
                "Start response should not contain trinity_result (removed in #136)"

        # Wait for agent to be running again
        max_wait = 30
        start_time = time.time()
        while time.time() - start_time < max_wait:
            check = api_client.get(f"/api/agents/{agent_name}")
            if check.status_code == 200 and check.json().get("status") == "running":
                break
            time.sleep(1)

    def test_no_claude_local_md_written(self, api_client: TrinityApiClient, created_agent):
        """Platform no longer writes CLAUDE.local.md into agent containers."""
        agent_name = created_agent["name"]

        # Check that CLAUDE.local.md does NOT exist (platform no longer writes it)
        response = api_client.get(f"/api/agents/{agent_name}/files/CLAUDE.local.md")
        # 404 means the file doesn't exist — correct behavior
        # 200 means a CLAUDE.local.md exists but could be from the agent's own repo
        if response.status_code == 200:
            content = response.json().get("content", "")
            # If it exists, it should NOT contain platform-injected content
            assert "Trinity Platform Instructions" not in content, \
                "CLAUDE.local.md should not contain platform-injected content (now runtime)"

    pytestmark = pytest.mark.smoke

    def test_custom_prompt_crud_still_works(self, api_client: TrinityApiClient):
        """Trinity prompt CRUD unaffected — setting is read at runtime now."""
        prompt_text = f"Runtime test prompt {uuid.uuid4().hex[:8]}"

        try:
            # Set prompt
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": prompt_text}
            )
            assert_status(response, 200)

            # Read back
            response = api_client.get("/api/settings/trinity_prompt")
            assert_status(response, 200)
            data = response.json()
            assert data.get("value") == prompt_text

            # Delete
            response = api_client.delete("/api/settings/trinity_prompt")
            assert_status(response, 200)

            # Verify deleted
            response = api_client.get("/api/settings/trinity_prompt")
            assert_status(response, 404)
        finally:
            api_client.delete("/api/settings/trinity_prompt")

    def test_custom_prompt_change_no_restart_needed(self, api_client: TrinityApiClient):
        """Changing trinity_prompt takes effect immediately — no agent restart needed.

        This is a key behavioral change from Issue #136: the setting is read
        on every request, so changes are instant.
        """
        prompt_v1 = f"Version 1 prompt {uuid.uuid4().hex[:8]}"
        prompt_v2 = f"Version 2 prompt {uuid.uuid4().hex[:8]}"

        try:
            # Set v1
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": prompt_v1}
            )
            assert_status(response, 200)

            # Update to v2 without any agent restart
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": prompt_v2}
            )
            assert_status(response, 200)

            # Verify v2 is active
            response = api_client.get("/api/settings/trinity_prompt")
            assert_status(response, 200)
            assert response.json().get("value") == prompt_v2

        finally:
            api_client.delete("/api/settings/trinity_prompt")


class TestSettingsValidation:
    """Tests for Settings API input validation."""

    pytestmark = pytest.mark.smoke

    def test_empty_value_rejected(self, api_client: TrinityApiClient):
        """PUT with empty value should be rejected or allowed (implementation-specific)."""
        response = api_client.put(
            "/api/settings/test_empty",
            json={"value": ""}
        )
        # Empty string might be valid or rejected - document behavior
        assert_status_in(response, [200, 400, 422])
        if response.status_code == 200:
            # Cleanup
            api_client.delete("/api/settings/test_empty")

    def test_missing_value_field_rejected(self, api_client: TrinityApiClient):
        """PUT without value field returns 422."""
        response = api_client.put(
            "/api/settings/test_missing",
            json={}
        )
        assert_status(response, 422)

    def test_invalid_json_rejected(self, api_client: TrinityApiClient):
        """PUT with invalid JSON returns 422."""
        response = api_client._client.put(
            f"{api_client.config.base_url}/api/settings/test_invalid",
            headers={
                **api_client._get_headers(),
                "Content-Type": "application/json"
            },
            content="not valid json"
        )
        assert_status(response, 422)


# =============================================================================
# API KEYS SETTINGS TESTS (REQ-SETUP-002)
# Tests for /api/settings/api-keys/* endpoints
# =============================================================================


class TestApiKeysAuthentication:
    """Tests for API Keys endpoints authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_api_keys_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings/api-keys requires authentication."""
        response = unauthenticated_client.get("/api/settings/api-keys", auth=False)
        assert_status(response, 401)

    def test_update_api_key_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/settings/api-keys/anthropic requires authentication."""
        response = unauthenticated_client.put(
            "/api/settings/api-keys/anthropic",
            json={"api_key": "sk-ant-test123"},
            auth=False
        )
        assert_status(response, 401)

    def test_delete_api_key_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """DELETE /api/settings/api-keys/anthropic requires authentication."""
        response = unauthenticated_client.delete(
            "/api/settings/api-keys/anthropic",
            auth=False
        )
        assert_status(response, 401)

    def test_test_api_key_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/settings/api-keys/anthropic/test requires authentication."""
        response = unauthenticated_client.post(
            "/api/settings/api-keys/anthropic/test",
            json={"api_key": "sk-ant-test123"},
            auth=False
        )
        assert_status(response, 401)


class TestApiKeysStatus:
    """Tests for GET /api/settings/api-keys endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_api_keys_status_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/settings/api-keys returns expected structure."""
        response = api_client.get("/api/settings/api-keys")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["anthropic"])

    def test_anthropic_key_status_fields(self, api_client: TrinityApiClient):
        """Anthropic key status has expected fields."""
        response = api_client.get("/api/settings/api-keys")
        assert_status(response, 200)
        data = response.json()

        anthropic = data.get("anthropic", {})
        assert "configured" in anthropic
        assert isinstance(anthropic["configured"], bool)

        if anthropic["configured"]:
            # If configured, should have masked key and source
            assert "masked" in anthropic
            assert "source" in anthropic
            assert anthropic["source"] in ["settings", "env"]
            # Masked key should hide most characters
            if anthropic["masked"]:
                assert "..." in anthropic["masked"] or "x" in anthropic["masked"].lower()


class TestApiKeyValidation:
    """Tests for API key format validation."""

    pytestmark = pytest.mark.smoke

    def test_invalid_format_rejected(self, api_client: TrinityApiClient):
        """PUT /api/settings/api-keys/anthropic rejects invalid format."""
        response = api_client.put(
            "/api/settings/api-keys/anthropic",
            json={"api_key": "invalid-key-format"}
        )
        assert_status(response, 400)
        data = response.json()
        assert "format" in data.get("detail", "").lower() or "invalid" in data.get("detail", "").lower()

    def test_missing_api_key_field_rejected(self, api_client: TrinityApiClient):
        """PUT /api/settings/api-keys/anthropic rejects missing api_key field."""
        response = api_client.put(
            "/api/settings/api-keys/anthropic",
            json={}
        )
        assert_status(response, 422)

    def test_empty_api_key_rejected(self, api_client: TrinityApiClient):
        """PUT /api/settings/api-keys/anthropic rejects empty api_key."""
        response = api_client.put(
            "/api/settings/api-keys/anthropic",
            json={"api_key": ""}
        )
        assert_status_in(response, [400, 422])

    def test_whitespace_only_api_key_rejected(self, api_client: TrinityApiClient):
        """PUT /api/settings/api-keys/anthropic rejects whitespace-only api_key."""
        response = api_client.put(
            "/api/settings/api-keys/anthropic",
            json={"api_key": "   "}
        )
        assert_status_in(response, [400, 422])


class TestApiKeyTest:
    """Tests for POST /api/settings/api-keys/anthropic/test endpoint."""

    pytestmark = pytest.mark.smoke

    def test_invalid_format_returns_invalid(self, api_client: TrinityApiClient):
        """Test endpoint returns valid=false for invalid format."""
        response = api_client.post(
            "/api/settings/api-keys/anthropic/test",
            json={"api_key": "not-a-valid-key"}
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("valid") is False
        assert "error" in data

    def test_wrong_prefix_returns_invalid(self, api_client: TrinityApiClient):
        """Test endpoint returns valid=false for wrong prefix."""
        response = api_client.post(
            "/api/settings/api-keys/anthropic/test",
            json={"api_key": "sk-openai-fake12345"}
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("valid") is False

    def test_fake_valid_format_returns_response(self, api_client: TrinityApiClient):
        """Test endpoint returns response for valid format (may be valid or invalid)."""
        # This uses a correctly formatted but fake key
        response = api_client.post(
            "/api/settings/api-keys/anthropic/test",
            json={"api_key": "sk-ant-api03-fakekey12345abcdefghijklmnop"}
        )
        assert_status(response, 200)
        data = response.json()
        # Should have valid field (true or false depending on actual key)
        assert "valid" in data
        assert isinstance(data["valid"], bool)


class TestApiKeyDelete:
    """Tests for DELETE /api/settings/api-keys/anthropic endpoint."""

    pytestmark = pytest.mark.smoke

    def test_delete_returns_success_structure(self, api_client: TrinityApiClient):
        """DELETE /api/settings/api-keys/anthropic returns expected structure."""
        response = api_client.delete("/api/settings/api-keys/anthropic")
        assert_status(response, 200)
        data = response.json()
        assert_has_fields(data, ["success", "deleted"])

    def test_delete_idempotent(self, api_client: TrinityApiClient):
        """DELETE /api/settings/api-keys/anthropic is idempotent."""
        # Delete twice - both should succeed
        response1 = api_client.delete("/api/settings/api-keys/anthropic")
        response2 = api_client.delete("/api/settings/api-keys/anthropic")

        assert_status(response1, 200)
        assert_status(response2, 200)
        # Second delete should also succeed (idempotent)
        assert response2.json().get("success") is True

    def test_delete_shows_env_fallback(self, api_client: TrinityApiClient):
        """DELETE response indicates if env fallback is configured."""
        response = api_client.delete("/api/settings/api-keys/anthropic")
        assert_status(response, 200)
        data = response.json()
        # Should indicate whether fallback to env var is available
        assert "fallback_configured" in data
        assert isinstance(data["fallback_configured"], bool)


# =============================================================================
# SSH ACCESS TESTS (CFG-009)
# Tests for /api/agents/{name}/ssh-access endpoint
# =============================================================================


class TestSshAccessAuthentication:
    """Tests for SSH access endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_ssh_access_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/agents/{name}/ssh-access requires authentication."""
        response = unauthenticated_client.post(
            "/api/agents/test-agent/ssh-access",
            json={"auth_method": "key"},
            auth=False
        )
        assert_status(response, 401)


class TestSshAccessEndpoint:
    """CFG-009: Generate ephemeral SSH credentials."""

    def test_ssh_access_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """POST /api/agents/{name}/ssh-access for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/ssh-access",
            json={"auth_method": "key"}
        )
        # 403 if SSH access is disabled globally, 404 if agent not found
        # When SSH is disabled, the check happens before agent lookup
        if response.status_code == 403:
            pytest.skip("SSH access is disabled - cannot test 404 for nonexistent agent")
        assert_status(response, 404)

    def test_ssh_access_returns_key_credentials(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/ssh-access with key method returns SSH key."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # SSH access disabled returns 403
        if response.status_code == 403:
            pytest.skip("SSH access is disabled")

        # May also be 400 if SSH access is disabled (legacy check)
        if response.status_code == 400:
            data = response.json()
            if "disabled" in data.get("detail", "").lower():
                pytest.skip("SSH access is disabled")

        assert_status(response, 200)
        data = response.json()

        assert_has_fields(data, ["status", "agent", "auth_method", "connection"])
        assert data["status"] == "success"
        assert data["auth_method"] == "key"

        # Key method should return private key
        assert "private_key" in data
        assert "-----BEGIN" in data["private_key"]

        # Connection details
        connection = data["connection"]
        assert_has_fields(connection, ["command", "host", "port", "user"])

    def test_ssh_access_returns_password_credentials(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """POST /api/agents/{name}/ssh-access with password method returns password."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"auth_method": "password", "ttl_hours": 1}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # SSH access disabled returns 403
        if response.status_code == 403:
            pytest.skip("SSH access is disabled")

        if response.status_code == 400:
            data = response.json()
            if "disabled" in data.get("detail", "").lower():
                pytest.skip("SSH access is disabled")

        assert_status(response, 200)
        data = response.json()

        assert_has_fields(data, ["status", "agent", "auth_method", "connection"])
        assert data["status"] == "success"
        assert data["auth_method"] == "password"

        # Password method should return password in connection
        connection = data["connection"]
        assert_has_fields(connection, ["command", "host", "port", "user", "password"])
        assert len(connection["password"]) >= 16  # Should be secure password

    def test_ssh_access_includes_expiry_time(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSH access response includes expiry information."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 2}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # SSH access disabled returns 403
        if response.status_code == 403:
            pytest.skip("SSH access is disabled")

        if response.status_code == 400:
            pytest.skip("SSH access may be disabled")

        assert_status(response, 200)
        data = response.json()

        assert "expires_at" in data
        assert "expires_in_hours" in data
        # TTL should be approximately what we requested
        assert 1.5 <= data["expires_in_hours"] <= 2.5

    def test_ssh_access_respects_ttl_parameter(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSH access uses specified TTL."""
        # Request with short TTL
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 0.5}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # SSH access disabled returns 403
        if response.status_code == 403:
            pytest.skip("SSH access is disabled")

        if response.status_code == 400:
            pytest.skip("SSH access may be disabled")

        assert_status(response, 200)
        data = response.json()

        # TTL should be close to requested
        assert data["expires_in_hours"] <= 1.0

    def test_ssh_access_includes_instructions(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSH access response includes setup instructions."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1}
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # SSH access disabled returns 403
        if response.status_code == 403:
            pytest.skip("SSH access is disabled")

        if response.status_code == 400:
            pytest.skip("SSH access may be disabled")

        assert_status(response, 200)
        data = response.json()

        # Should include instructions
        if "instructions" in data:
            assert isinstance(data["instructions"], list)
            assert len(data["instructions"]) > 0


class TestSshAccessStoppedAgent:
    """Tests for SSH access on stopped agents."""

    def test_ssh_access_stopped_agent_returns_400(
        self,
        api_client: TrinityApiClient,
        stopped_agent
    ):
        """POST /api/agents/{name}/ssh-access for stopped agent returns 400."""
        response = api_client.post(
            f"/api/agents/{stopped_agent['name']}/ssh-access",
            json={"auth_method": "key"}
        )

        # 403 if SSH access is disabled globally (checked before agent state)
        if response.status_code == 403:
            pytest.skip("SSH access is disabled - cannot test stopped agent behavior")

        # Should fail because agent is stopped
        assert_status_in(response, [400, 503])


# =============================================================================
# GITHUB PAT TEST (SET-005)
# Tests for /api/settings/api-keys/github/test endpoint
# =============================================================================


class TestGitHubPATTest:
    """SET-005: Test GitHub PAT validity."""

    pytestmark = pytest.mark.smoke

    def test_github_pat_test_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/settings/api-keys/github/test requires authentication."""
        response = unauthenticated_client.post(
            "/api/settings/api-keys/github/test",
            json={"api_key": "ghp_fake12345"},
            auth=False
        )
        assert_status(response, 401)

    def test_github_pat_invalid_format_rejected(self, api_client: TrinityApiClient):
        """Invalid PAT format is rejected."""
        response = api_client.post(
            "/api/settings/api-keys/github/test",
            json={"api_key": "not-a-github-pat"}
        )
        assert_status(response, 200)
        data = response.json()
        assert data.get("valid") is False
        assert "error" in data

    def test_github_pat_valid_format_returns_response(self, api_client: TrinityApiClient):
        """Valid PAT format returns structured response."""
        # Classic PAT format
        response = api_client.post(
            "/api/settings/api-keys/github/test",
            json={"api_key": "ghp_faketoken123456789abcdefghijklmnop"}
        )
        assert_status(response, 200)
        data = response.json()

        # Should have valid field
        assert "valid" in data
        assert isinstance(data["valid"], bool)

        # If invalid (which this fake token is), should have error
        if not data["valid"]:
            assert "error" in data

    def test_github_pat_fine_grained_format_accepted(self, api_client: TrinityApiClient):
        """Fine-grained PAT format is accepted."""
        response = api_client.post(
            "/api/settings/api-keys/github/test",
            json={"api_key": "github_pat_fake12345abcdefghijklmnopqrstuvwxyz"}
        )
        assert_status(response, 200)
        data = response.json()
        assert "valid" in data


# =============================================================================
# OPS SETTINGS TESTS (SET-011, SET-012)
# Tests for /api/settings/ops/* endpoints
# =============================================================================


class TestOpsSettingsAuthentication:
    """Tests for Ops Settings endpoints authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_ops_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings/ops/config requires authentication."""
        response = unauthenticated_client.get("/api/settings/ops/config", auth=False)
        assert_status(response, 401)

    def test_update_ops_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/settings/ops/config requires authentication."""
        response = unauthenticated_client.put(
            "/api/settings/ops/config",
            json={"settings": {"ssh_access_enabled": "true"}},
            auth=False
        )
        assert_status(response, 401)

    def test_reset_ops_config_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/settings/ops/reset requires authentication."""
        response = unauthenticated_client.post("/api/settings/ops/reset", auth=False)
        assert_status(response, 401)


class TestOpsSettingsGet:
    """SET-010: Get ops configuration."""

    pytestmark = pytest.mark.smoke

    def test_get_ops_config_returns_structure(self, api_client: TrinityApiClient):
        """GET /api/settings/ops/config returns settings structure."""
        response = api_client.get("/api/settings/ops/config")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert "settings" in data
        assert isinstance(data["settings"], dict)

    def test_ops_config_has_ssh_setting(self, api_client: TrinityApiClient):
        """Ops config includes ssh_access_enabled setting."""
        response = api_client.get("/api/settings/ops/config")
        assert_status(response, 200)
        data = response.json()

        settings = data.get("settings", {})
        # SSH access enabled should be a known setting
        if "ssh_access_enabled" in settings:
            ssh_setting = settings["ssh_access_enabled"]
            assert "value" in ssh_setting
            assert "default" in ssh_setting

    def test_ops_config_shows_default_indicator(self, api_client: TrinityApiClient):
        """Ops config shows whether value is default."""
        response = api_client.get("/api/settings/ops/config")
        assert_status(response, 200)
        data = response.json()

        settings = data.get("settings", {})
        for key, setting in settings.items():
            if isinstance(setting, dict):
                # Each setting should indicate if it's using default
                assert "is_default" in setting or "default" in setting


class TestOpsSettingsUpdate:
    """SET-011: Update ops settings."""

    pytestmark = pytest.mark.smoke

    def test_update_ops_setting_persists(self, api_client: TrinityApiClient):
        """PUT /api/settings/ops/config updates setting."""
        # Get current value first
        get_response = api_client.get("/api/settings/ops/config")
        if get_response.status_code != 200:
            pytest.skip("Could not get ops config")

        original_settings = get_response.json().get("settings", {})

        try:
            # Update a setting (API expects string values)
            response = api_client.put(
                "/api/settings/ops/config",
                json={"settings": {"ssh_access_enabled": "true"}}
            )
            assert_status(response, 200)
            data = response.json()

            assert data.get("success") is True
            assert "updated" in data

            # Verify update persisted
            verify_response = api_client.get("/api/settings/ops/config")
            verify_data = verify_response.json()
            ssh_setting = verify_data.get("settings", {}).get("ssh_access_enabled", {})
            if isinstance(ssh_setting, dict):
                # Value may be string or bool depending on API
                assert ssh_setting.get("value") in [True, "true"]
        finally:
            # Restore original if possible
            if "ssh_access_enabled" in original_settings:
                original_value = original_settings["ssh_access_enabled"]
                if isinstance(original_value, dict):
                    val = original_value.get("value")
                    # Convert to string for API
                    str_val = str(val).lower() if isinstance(val, bool) else str(val)
                    api_client.put(
                        "/api/settings/ops/config",
                        json={"settings": {"ssh_access_enabled": str_val}}
                    )

    def test_update_ignores_invalid_keys(self, api_client: TrinityApiClient):
        """PUT /api/settings/ops/config ignores invalid keys."""
        response = api_client.put(
            "/api/settings/ops/config",
            json={"settings": {"invalid_setting_key_xyz": "value"}}
        )
        assert_status(response, 200)
        data = response.json()

        # Should succeed but report ignored keys
        assert data.get("success") is True
        if "ignored" in data:
            assert "invalid_setting_key_xyz" in data["ignored"]


class TestOpsSettingsReset:
    """SET-012: Reset ops settings to defaults."""

    pytestmark = pytest.mark.smoke

    def test_reset_ops_settings_succeeds(self, api_client: TrinityApiClient):
        """POST /api/settings/ops/reset resets to defaults."""
        response = api_client.post("/api/settings/ops/reset")
        assert_status(response, 200)
        data = response.json()

        assert data.get("success") is True
        assert "message" in data or "reset" in data

    def test_reset_restores_default_values(self, api_client: TrinityApiClient):
        """After reset, settings are at default values."""
        # First modify a setting (API expects string values)
        api_client.put(
            "/api/settings/ops/config",
            json={"settings": {"ssh_access_enabled": "true"}}
        )

        # Reset
        reset_response = api_client.post("/api/settings/ops/reset")
        assert_status(reset_response, 200)

        # Verify settings are at defaults
        get_response = api_client.get("/api/settings/ops/config")
        assert_status(get_response, 200)
        data = get_response.json()

        settings = data.get("settings", {})
        for key, setting in settings.items():
            if isinstance(setting, dict) and "is_default" in setting:
                assert setting["is_default"] is True, \
                    f"Setting {key} should be at default after reset"


class TestMcpUrlSettings:
    """Tests for MCP Server URL configuration endpoints (#76)."""

    pytestmark = pytest.mark.smoke

    MCP_URL_ENDPOINT = "/api/settings/mcp-url"

    def test_get_mcp_url_no_custom_setting(self, api_client: TrinityApiClient):
        """GET /api/settings/mcp-url returns null url when no custom URL is set."""
        # Ensure clean state
        api_client.delete(self.MCP_URL_ENDPOINT)

        response = api_client.get(self.MCP_URL_ENDPOINT)
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["url"] is None
        assert "default_url" in data
        assert data["default_url"].endswith("/mcp")

    def test_put_valid_https_url(self, api_client: TrinityApiClient):
        """PUT /api/settings/mcp-url with valid HTTPS URL succeeds."""
        try:
            response = api_client.put(
                self.MCP_URL_ENDPOINT,
                json={"url": "https://example.com/mcp"}
            )
            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["success"] is True
            assert data["url"] == "https://example.com/mcp"

            # Verify it's stored
            get_response = api_client.get(self.MCP_URL_ENDPOINT)
            assert get_response.json()["url"] == "https://example.com/mcp"
        finally:
            api_client.delete(self.MCP_URL_ENDPOINT)

    def test_put_valid_http_url(self, api_client: TrinityApiClient):
        """PUT /api/settings/mcp-url with valid HTTP URL succeeds."""
        try:
            response = api_client.put(
                self.MCP_URL_ENDPOINT,
                json={"url": "http://192.168.1.100:8080/mcp"}
            )
            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["url"] == "http://192.168.1.100:8080/mcp"
        finally:
            api_client.delete(self.MCP_URL_ENDPOINT)

    def test_put_invalid_url_no_protocol(self, api_client: TrinityApiClient):
        """PUT /api/settings/mcp-url rejects URL without http/https."""
        response = api_client.put(
            self.MCP_URL_ENDPOINT,
            json={"url": "example.com/mcp"}
        )
        assert_status(response, 422)

    def test_put_invalid_url_not_ending_mcp(self, api_client: TrinityApiClient):
        """PUT /api/settings/mcp-url rejects URL not ending in /mcp."""
        response = api_client.put(
            self.MCP_URL_ENDPOINT,
            json={"url": "https://example.com/api"}
        )
        assert_status(response, 422)

    def test_delete_resets_to_null(self, api_client: TrinityApiClient):
        """DELETE /api/settings/mcp-url resets URL to auto-detect."""
        # Set a custom URL first
        api_client.put(
            self.MCP_URL_ENDPOINT,
            json={"url": "https://example.com/mcp"}
        )

        # Delete it
        response = api_client.delete(self.MCP_URL_ENDPOINT)
        assert_status(response, 200)

        # Verify it's gone
        get_response = api_client.get(self.MCP_URL_ENDPOINT)
        assert get_response.json()["url"] is None

    def test_unauthenticated_get_returns_401(self, unauthenticated_client: TrinityApiClient):
        """GET /api/settings/mcp-url requires authentication."""
        response = unauthenticated_client.get(self.MCP_URL_ENDPOINT, auth=False)
        assert_status(response, 401)

    def test_unauthenticated_put_returns_401(self, unauthenticated_client: TrinityApiClient):
        """PUT /api/settings/mcp-url requires authentication."""
        response = unauthenticated_client.put(
            self.MCP_URL_ENDPOINT,
            json={"url": "https://example.com/mcp"},
            auth=False
        )
        assert_status(response, 401)

    def test_put_strips_trailing_slash(self, api_client: TrinityApiClient):
        """PUT /api/settings/mcp-url normalizes trailing slash before /mcp check."""
        try:
            response = api_client.put(
                self.MCP_URL_ENDPOINT,
                json={"url": "https://example.com/mcp/"}
            )
            # After stripping trailing slash, URL ends with /mcp — should succeed
            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["url"] == "https://example.com/mcp"
        finally:
            api_client.delete(self.MCP_URL_ENDPOINT)
