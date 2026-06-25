"""
Tests for platform default model setting (#831).

Covers:
- feature-flags endpoint includes platform_default_model
- settings_service.get_platform_default_model() returns fallback when no row
- settings_service.get_platform_default_model() returns DB value when set
- task_execution_service resolves None model → platform default

Run against a live backend: TRINITY_API_URL=http://localhost:8000
"""
import os
import time
import pytest
import httpx

BASE_URL = os.getenv("TRINITY_API_URL", "http://localhost:8000")
USERNAME = os.getenv("TRINITY_TEST_USERNAME", "admin")
PASSWORD = os.getenv("TRINITY_TEST_PASSWORD", "password")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_auth_headers():
    resp = httpx.post(
        f"{BASE_URL}/api/token",
        data={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Unit-style tests (backend service — no live server required)
# ---------------------------------------------------------------------------

class TestGetPlatformDefaultModelUnit:
    """Tests for settings_service.get_platform_default_model() in isolation."""

    def test_returns_fallback_when_no_db_row(self, monkeypatch):
        """When system_settings has no platform_default_model row, return hardcoded default."""
        import sys
        import types

        # Provide a minimal stub for the `database` module so we can import
        # settings_service without a running database.
        if "database" not in sys.modules:
            db_stub = types.ModuleType("database")
            db_stub.db = types.SimpleNamespace(
                get_setting_value=lambda key, default=None: default
            )
            sys.modules["database"] = db_stub

        # Clear the module cache so our monkeypatched db takes effect.
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        result = svc.get_platform_default_model()
        assert result == "claude-sonnet-4-6"

    def test_returns_db_value_when_set(self, monkeypatch):
        """When system_settings has a platform_default_model row, return that value."""
        import sys
        import types

        db_stub = types.ModuleType("database")
        db_stub.db = types.SimpleNamespace(
            get_setting_value=lambda key, default=None: (
                "claude-opus-4-7" if key == "platform_default_model" else default
            )
        )
        sys.modules["database"] = db_stub
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        result = svc.get_platform_default_model()
        assert result == "claude-opus-4-7"

    def test_ttl_cache_returns_cached_value(self, monkeypatch):
        """TTL cache returns the cached value within 60s without a new DB read."""
        import sys
        import types

        call_count = [0]

        def counting_get(key, default=None):
            if key == "platform_default_model":
                call_count[0] += 1
            return "claude-sonnet-4-6" if key == "platform_default_model" else default

        db_stub = types.ModuleType("database")
        db_stub.db = types.SimpleNamespace(get_setting_value=counting_get)
        sys.modules["database"] = db_stub
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        # Only one DB read due to TTL cache
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Integration tests (live backend required)
# ---------------------------------------------------------------------------

class TestFeatureFlagsEndpoint:
    """GET /api/settings/feature-flags must include platform_default_model."""

    def test_feature_flags_includes_platform_default_model(self):
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "platform_default_model" in data, (
            "feature-flags response missing 'platform_default_model' key"
        )
        assert isinstance(data["platform_default_model"], str)
        assert len(data["platform_default_model"]) > 0

    def test_feature_flags_default_is_claude_sonnet(self):
        """Out-of-box default must be claude-sonnet-4-6 unless overridden in DB."""
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        # Accept either the code default or a valid admin override. The override
        # set must stay in lockstep with the options offered in Settings.vue's
        # platform-default dropdown (#1080 added claude-opus-4-8).
        assert data["platform_default_model"] in (
            "claude-sonnet-4-6",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
        ), f"Unexpected default: {data['platform_default_model']}"

    def test_feature_flags_unauthenticated_returns_401(self):
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", timeout=10)
        assert resp.status_code == 401

    def test_feature_flags_includes_workspace_available(self):
        """feature-flags must expose workspace_available key (#860)."""
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "workspace_available" in data, (
            "feature-flags response missing 'workspace_available' key"
        )
        assert isinstance(data["workspace_available"], bool)

    def test_workspace_available_false_by_default(self):
        """workspace_available must be False unless WORKSPACE_ENABLED is set (#860)."""
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        # In CI/test environments WORKSPACE_ENABLED is not set, so this must be False.
        # If GEMINI_API_KEY is also absent, voice_available=False makes workspace_available
        # False regardless — both conditions confirm the default-off behaviour.
        assert data["workspace_available"] is False, (
            "workspace_available should default to False unless explicitly enabled"
        )

    def test_feature_flags_includes_mcp_agent_chat_pull_enabled(self):
        """feature-flags must expose mcp_agent_chat_pull_enabled (#946 observability)."""
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "mcp_agent_chat_pull_enabled" in data, (
            "feature-flags response missing 'mcp_agent_chat_pull_enabled' key"
        )
        assert isinstance(data["mcp_agent_chat_pull_enabled"], bool)

    def test_mcp_agent_chat_pull_enabled_false_by_default(self):
        """The #946 pull pilot must default OFF unless MCP_AGENT_CHAT_PULL_ENABLED is set."""
        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["mcp_agent_chat_pull_enabled"] is False, (
            "mcp_agent_chat_pull_enabled should default to False (pilot is opt-in)"
        )


class TestPlatformDefaultModelSetting:
    """Admin can read/write platform_default_model via /api/settings/{key}."""

    def test_admin_can_read_platform_default_model(self):
        headers = get_auth_headers()
        resp = httpx.get(
            f"{BASE_URL}/api/settings/platform_default_model",
            headers=headers,
            timeout=10,
        )
        # Either 200 (row exists) or 404 (no row yet — fallback used)
        assert resp.status_code in (200, 404)

    def test_admin_can_set_and_retrieve_platform_default_model(self):
        headers = get_auth_headers()
        # Set to opus
        put_resp = httpx.put(
            f"{BASE_URL}/api/settings/platform_default_model",
            json={"value": "claude-opus-4-7"},
            headers=headers,
            timeout=10,
        )
        assert put_resp.status_code in (200, 201)

        # Verify via feature-flags
        ff_resp = httpx.get(
            f"{BASE_URL}/api/settings/feature-flags",
            headers=headers,
            timeout=10,
        )
        assert ff_resp.status_code == 200
        assert ff_resp.json()["platform_default_model"] == "claude-opus-4-7"

        # Reset to sonnet
        httpx.put(
            f"{BASE_URL}/api/settings/platform_default_model",
            json={"value": "claude-sonnet-4-6"},
            headers=headers,
            timeout=10,
        )
