"""
Read-Only Mode Tests (test_read_only_mode.py)

Tests for Trinity Read-Only Mode endpoints (CFG-007).
Covers: Code protection for deployed agents using Claude Code PreToolUse hooks.

These tests verify the read-only mode CRUD operations, validation, and protection.
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# Default patterns as defined in the implementation
DEFAULT_BLOCKED_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.vue", "*.svelte",
    "*.go", "*.rs", "*.rb", "*.java", "*.c", "*.cpp", "*.h",
    "*.sh", "*.bash", "Makefile", "Dockerfile",
    "CLAUDE.md", "README.md", ".claude/*", ".env", ".env.*",
    "template.yaml", "*.yaml", "*.yml", "*.json", "*.toml"
]

DEFAULT_ALLOWED_PATTERNS = [
    "content/*", "output/*", "reports/*", "exports/*",
    "*.log", "*.txt"
]


class TestReadOnlyModeAuthentication:
    """Tests for read-only mode endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_get_read_only_status_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/read-only requires authentication."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/read-only",
            auth=False
        )
        assert_status_in(response, [401, 403])

    def test_put_read_only_status_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/read-only requires authentication."""
        response = unauthenticated_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": True},
            auth=False
        )
        assert_status_in(response, [401, 403])


class TestReadOnlyModeCRUD:
    """Tests for read-only mode CRUD operations using created_agent fixture."""

    pytestmark = pytest.mark.smoke

    def test_get_read_only_status_returns_structure(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/read-only returns expected structure."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "enabled", "config"])
        assert data["agent_name"] == created_agent['name']
        assert isinstance(data["enabled"], bool)
        assert isinstance(data["config"], dict)

    def test_get_read_only_status_default_is_disabled(self, api_client: TrinityApiClient, created_agent):
        """Agents should default to read-only mode disabled."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        # Default should be False (read-only disabled)
        assert data["enabled"] is False

    def test_get_read_only_status_has_default_config(self, api_client: TrinityApiClient, created_agent):
        """Even when disabled, config should have default patterns."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        config = data["config"]

        # Config should have blocked_patterns and allowed_patterns
        assert "blocked_patterns" in config
        assert "allowed_patterns" in config
        assert isinstance(config["blocked_patterns"], list)
        assert isinstance(config["allowed_patterns"], list)

    def test_get_read_only_status_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/read-only returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz/read-only")
        assert_status(response, 404)

    def test_put_read_only_enable_and_disable(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/read-only can enable and disable read-only mode."""
        # Get current status first
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(current, 200)
        original_enabled = current.json()["enabled"]

        try:
            # Enable read-only mode
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            assert_status(response, 200)
            data = assert_json_response(response)
            assert data["enabled"] is True
            assert "hooks_injected" in data
            assert "message" in data

            # Verify the change persisted
            get_response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")
            assert_status(get_response, 200)
            assert get_response.json()["enabled"] is True

            # Now disable it
            disable_response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )
            assert_status(disable_response, 200)
            assert disable_response.json()["enabled"] is False

        finally:
            # Restore original state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": original_enabled}
            )

    def test_put_read_only_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """PUT /api/agents/{name}/read-only returns 404 for nonexistent agent."""
        response = api_client.put(
            "/api/agents/nonexistent-agent-xyz/read-only",
            json={"enabled": True}
        )
        assert_status(response, 404)

    def test_put_read_only_missing_enabled_returns_error(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/read-only with missing enabled field returns error."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={}
        )

        # Should reject with 400 (missing required field)
        assert_status(response, 400)


class TestReadOnlyModeConfig:
    """Tests for read-only mode configuration patterns."""

    pytestmark = pytest.mark.smoke

    def test_enable_with_default_config(self, api_client: TrinityApiClient, created_agent):
        """Enabling without custom config uses default patterns."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            assert_status(response, 200)
            data = response.json()
            config = data["config"]

            # Should have default blocked patterns
            assert "*.py" in config["blocked_patterns"]
            assert "*.js" in config["blocked_patterns"]
            assert "CLAUDE.md" in config["blocked_patterns"]

            # Should have default allowed patterns
            assert "content/*" in config["allowed_patterns"]
            assert "output/*" in config["allowed_patterns"]

        finally:
            # Restore disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

    def test_enable_with_custom_config(self, api_client: TrinityApiClient, created_agent):
        """Enabling with custom config uses provided patterns."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        custom_config = {
            "blocked_patterns": ["*.py", "*.md"],
            "allowed_patterns": ["drafts/*", "temp/*"]
        }

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True, "config": custom_config}
            )

            assert_status(response, 200)
            data = response.json()
            config = data["config"]

            # Should have custom patterns
            assert config["blocked_patterns"] == ["*.py", "*.md"]
            assert config["allowed_patterns"] == ["drafts/*", "temp/*"]

        finally:
            # Restore disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

    def test_invalid_config_type_returns_error(self, api_client: TrinityApiClient, created_agent):
        """Invalid config type returns error."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": True, "config": "not-an-object"}
        )

        assert_status(response, 400)

    def test_invalid_blocked_patterns_type_returns_error(self, api_client: TrinityApiClient, created_agent):
        """Invalid blocked_patterns type returns error."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": True, "config": {"blocked_patterns": "not-a-list"}}
        )

        assert_status(response, 400)

    def test_invalid_allowed_patterns_type_returns_error(self, api_client: TrinityApiClient, created_agent):
        """Invalid allowed_patterns type returns error."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": True, "config": {"allowed_patterns": "not-a-list"}}
        )

        assert_status(response, 400)


class TestReadOnlyModeSystemAgent:
    """Tests for read-only mode system agent protection."""

    pytestmark = pytest.mark.smoke

    def test_cannot_enable_read_only_on_system_agent(self, api_client: TrinityApiClient):
        """Cannot enable read-only mode on system agent (trinity-system)."""
        response = api_client.put(
            "/api/agents/trinity-system/read-only",
            json={"enabled": True}
        )

        # Should return 403 (forbidden) for system agent
        # Or 404 if system agent doesn't exist in test environment
        assert_status_in(response, [403, 404])

    def test_can_read_system_agent_read_only_status(self, api_client: TrinityApiClient):
        """Can read system agent read-only status (should be disabled)."""
        response = api_client.get("/api/agents/trinity-system/read-only")

        # May be 404 if system agent doesn't exist in test environment
        if response.status_code == 404:
            pytest.skip("System agent not present in test environment")

        # If system agent exists, it should be readable
        if response.status_code == 200:
            data = response.json()
            assert data["enabled"] is False


class TestReadOnlyModePermissions:
    """Tests for read-only mode permission checks."""

    pytestmark = pytest.mark.smoke

    def test_admin_can_read_read_only_status(self, api_client: TrinityApiClient, created_agent):
        """Admin can read read-only status."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)

    def test_admin_can_update_read_only_status(self, api_client: TrinityApiClient, created_agent):
        """Admin can update read-only status."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(current, 200)
        original_enabled = current.json()["enabled"]

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": not original_enabled}
            )
            assert_status(response, 200)

        finally:
            # Restore original state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": original_enabled}
            )


class TestReadOnlyModeHookInjection:
    """Tests for read-only mode hook injection response."""

    pytestmark = pytest.mark.smoke

    def test_enable_returns_hooks_injected_status(self, api_client: TrinityApiClient, created_agent):
        """Enabling read-only mode returns hooks_injected status."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            assert_status(response, 200)
            data = response.json()

            # Should have hooks_injected field
            assert "hooks_injected" in data
            assert isinstance(data["hooks_injected"], bool)

        finally:
            # Restore disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

    def test_enable_returns_message(self, api_client: TrinityApiClient, created_agent):
        """Enabling read-only mode returns informative message."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            assert_status(response, 200)
            data = response.json()

            # Should have message field
            assert "message" in data
            assert "Read-only mode enabled" in data["message"]

        finally:
            # Restore disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

    def test_disable_returns_message(self, api_client: TrinityApiClient, created_agent):
        """Disabling read-only mode returns informative message."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        try:
            # First enable
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            # Then disable
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

            assert_status(response, 200)
            data = response.json()

            # Should have message field
            assert "message" in data
            assert "disabled" in data["message"].lower()

        finally:
            # Ensure disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )


class TestReadOnlyModeAgentState:
    """Tests for read-only mode with different agent states."""

    pytestmark = pytest.mark.smoke

    def test_read_only_status_readable_for_running_agent(self, api_client: TrinityApiClient, created_agent):
        """Read-only status can be read for running agent."""
        # First verify agent is running
        status_response = api_client.get(f"/api/agents/{created_agent['name']}")
        if status_response.status_code != 200:
            pytest.skip("Agent not available")

        agent_data = status_response.json()
        if agent_data.get("status") != "running":
            pytest.skip("Agent not running")

        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")
        assert_status(response, 200)
        data = response.json()
        assert "enabled" in data
        assert "config" in data


class TestReadOnlyModeResponseFormat:
    """Tests for read-only mode response format consistency."""

    pytestmark = pytest.mark.smoke

    def test_get_response_has_agent_name(self, api_client: TrinityApiClient, created_agent):
        """GET response includes agent_name field."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(response, 200)
        data = response.json()
        assert "agent_name" in data
        assert data["agent_name"] == created_agent['name']

    def test_put_response_has_status_field(self, api_client: TrinityApiClient, created_agent):
        """PUT response includes status field."""
        current = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if current.status_code == 503:
            pytest.skip("Agent server not ready")

        try:
            response = api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": True}
            )

            assert_status(response, 200)
            data = response.json()
            assert "status" in data
            assert data["status"] == "updated"

        finally:
            # Restore disabled state
            api_client.put(
                f"/api/agents/{created_agent['name']}/read-only",
                json={"enabled": False}
            )

    def test_get_put_consistency(self, api_client: TrinityApiClient, created_agent):
        """GET and PUT return consistent data for enabled field."""
        get_response = api_client.get(f"/api/agents/{created_agent['name']}/read-only")

        if get_response.status_code == 503:
            pytest.skip("Agent server not ready")

        assert_status(get_response, 200)
        get_data = get_response.json()

        # PUT with same value (no actual change)
        put_response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": get_data["enabled"]}
        )
        assert_status(put_response, 200)
        put_data = put_response.json()

        # Both should have same enabled value
        assert get_data["enabled"] == put_data["enabled"]


class TestReadOnlyModeValidation:
    """Tests for read-only mode input validation."""

    pytestmark = pytest.mark.smoke

    def test_put_enabled_string_handled(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/read-only handles string value for enabled."""
        response = api_client.put(
            f"/api/agents/{created_agent['name']}/read-only",
            json={"enabled": "true"}
        )

        # Should either accept (JSON parsing coerces) or reject (type validation)
        # Also accept 503 if agent not ready
        assert_status_in(response, [200, 400, 422, 503])

    def test_put_invalid_json(self, api_client: TrinityApiClient, created_agent):
        """PUT /api/agents/{name}/read-only rejects invalid JSON."""
        response = api_client._client.put(
            f"{api_client.config.base_url}/api/agents/{created_agent['name']}/read-only",
            headers={
                **api_client._get_headers(),
                "Content-Type": "application/json"
            },
            content="not valid json"
        )

        assert_status(response, 422)
