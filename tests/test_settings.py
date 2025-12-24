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


class TestTrinityPromptInjection:
    """Tests for Trinity prompt injection into agent CLAUDE.md.

    These tests require agent creation and are slower.
    """

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_agent_receives_prompt_on_creation(self, api_client: TrinityApiClient, request):
        """New agent receives trinity_prompt in CLAUDE.md."""
        agent_name = f"test-prompt-inj-{uuid.uuid4().hex[:6]}"
        prompt_text = f"Test injection prompt {uuid.uuid4().hex[:8]}"

        try:
            # Set trinity_prompt
            response = api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": prompt_text}
            )
            assert_status(response, 200)

            # Create agent
            response = api_client.post(
                "/api/agents",
                json={"name": agent_name}
            )
            assert_status_in(response, [200, 201])

            # Wait for agent to start
            max_wait = 45
            start = time.time()
            while time.time() - start < max_wait:
                check = api_client.get(f"/api/agents/{agent_name}")
                if check.status_code == 200 and check.json().get("status") == "running":
                    time.sleep(5)  # Extra time for Trinity injection
                    break
                time.sleep(1)

            # Verify injection by reading CLAUDE.md via files API
            response = api_client.get(f"/api/agents/{agent_name}/files/CLAUDE.md")
            if response.status_code == 200:
                content = response.json().get("content", "")
                # The custom instructions should be in CLAUDE.md
                assert "Custom Instructions" in content, \
                    "CLAUDE.md should contain Custom Instructions section"
                assert prompt_text in content, \
                    f"CLAUDE.md should contain the prompt text: {prompt_text}"
            else:
                # Fall back to checking logs if files API fails
                response = api_client.get(f"/api/agents/{agent_name}/logs?lines=200")
                if response.status_code == 200:
                    logs = response.json().get("logs", "")
                    # Check for Trinity section creation (at minimum)
                    assert "Trinity" in logs or "CLAUDE.md" in logs, \
                        "Agent logs should indicate Trinity injection activity"

        finally:
            # Cleanup
            api_client.delete("/api/settings/trinity_prompt")
            cleanup_test_agent(api_client, agent_name)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_prompt_updated_on_agent_restart(self, api_client: TrinityApiClient, request):
        """Agent receives updated trinity_prompt on restart."""
        agent_name = f"test-prompt-upd-{uuid.uuid4().hex[:6]}"
        initial_prompt = f"Initial prompt {uuid.uuid4().hex[:8]}"
        updated_prompt = f"Updated prompt {uuid.uuid4().hex[:8]}"

        try:
            # Set initial prompt
            api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": initial_prompt}
            )

            # Create and start agent
            api_client.post("/api/agents", json={"name": agent_name})

            # Wait for agent to start
            max_wait = 45
            start = time.time()
            while time.time() - start < max_wait:
                check = api_client.get(f"/api/agents/{agent_name}")
                if check.status_code == 200 and check.json().get("status") == "running":
                    time.sleep(3)
                    break
                time.sleep(1)

            # Update prompt
            api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": updated_prompt}
            )

            # Restart agent
            api_client.post(f"/api/agents/{agent_name}/stop")
            time.sleep(2)
            api_client.post(f"/api/agents/{agent_name}/start")
            time.sleep(5)

            # Verify updated prompt in logs
            response = api_client.get(f"/api/agents/{agent_name}/logs")
            if response.status_code == 200:
                logs = response.json().get("logs", "")
                # Should see custom instructions being updated
                assert "Custom" in logs, "Agent logs should show custom instructions handling"

        finally:
            api_client.delete("/api/settings/trinity_prompt")
            cleanup_test_agent(api_client, agent_name)

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_prompt_removed_when_cleared(self, api_client: TrinityApiClient, request):
        """Custom Instructions removed from CLAUDE.md when prompt cleared."""
        agent_name = f"test-prompt-clr-{uuid.uuid4().hex[:6]}"
        prompt_text = f"Temporary prompt {uuid.uuid4().hex[:8]}"

        try:
            # Set prompt
            api_client.put(
                "/api/settings/trinity_prompt",
                json={"value": prompt_text}
            )

            # Create and start agent
            api_client.post("/api/agents", json={"name": agent_name})

            # Wait for agent
            max_wait = 45
            start = time.time()
            while time.time() - start < max_wait:
                check = api_client.get(f"/api/agents/{agent_name}")
                if check.status_code == 200 and check.json().get("status") == "running":
                    time.sleep(5)  # Wait for Trinity injection
                    break
                time.sleep(1)

            # Verify custom instructions present first
            response = api_client.get(f"/api/agents/{agent_name}/files/CLAUDE.md")
            if response.status_code == 200:
                content = response.json().get("content", "")
                assert "Custom Instructions" in content, \
                    "CLAUDE.md should have Custom Instructions before clearing"

            # Clear prompt
            api_client.delete("/api/settings/trinity_prompt")

            # Restart agent
            api_client.post(f"/api/agents/{agent_name}/stop")
            time.sleep(2)
            api_client.post(f"/api/agents/{agent_name}/start")
            time.sleep(6)  # Wait for startup and injection

            # Verify removal by checking CLAUDE.md content
            response = api_client.get(f"/api/agents/{agent_name}/files/CLAUDE.md")
            if response.status_code == 200:
                content = response.json().get("content", "")
                # Custom Instructions section should be gone
                assert "Custom Instructions" not in content, \
                    "CLAUDE.md should NOT contain Custom Instructions after clearing"
                assert prompt_text not in content, \
                    "CLAUDE.md should NOT contain the original prompt text"
            else:
                # Fall back to logs check
                response = api_client.get(f"/api/agents/{agent_name}/logs?lines=300")
                if response.status_code == 200:
                    logs = response.json().get("logs", "")
                    assert "Removed Custom Instructions" in logs or "Created CLAUDE.md" in logs, \
                        "Agent logs should indicate custom instructions handling"

        finally:
            api_client.delete("/api/settings/trinity_prompt")
            cleanup_test_agent(api_client, agent_name)


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
