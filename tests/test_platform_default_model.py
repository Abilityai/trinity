# allow-root-live-test: raw-httpx against a live backend (feature-flags + settings API)
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
        from services.model_catalog import MODEL_CATALOG

        headers = get_auth_headers()
        resp = httpx.get(f"{BASE_URL}/api/settings/feature-flags", headers=headers, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        # Accept the code default or any valid admin override. Sourced from the
        # single catalog (#2086) instead of a hand-maintained tuple that was
        # already stale (missing fable-5/sonnet-5) — the admin dropdown offers
        # exactly the admin_default_selectable set, so the default must be one of
        # them (#1080 keeps Haiku out of this set).
        admin_default_models = {m.id for m in MODEL_CATALOG if m.admin_default_selectable}
        assert data["platform_default_model"] in admin_default_models, (
            f"Unexpected default: {data['platform_default_model']} "
            f"(not admin_default_selectable in the catalog: {sorted(admin_default_models)})"
        )

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
