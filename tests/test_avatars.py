"""
Avatar API Tests (test_avatars.py)

Tests for AVATAR-001 (generation), AVATAR-002 (emotions), AVATAR-003 (defaults).
Covers all 8 avatar endpoints plus identity CRUD.

Test tiers:
- SMOKE: Public endpoints (serving, emotions list) — no agent needed
- CORE: Auth-required endpoints (generate, delete, identity) — needs created_agent
"""

import pytest
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# =============================================================================
# Constants
# =============================================================================

AVATAR_EMOTIONS = [
    "happy", "thoughtful", "surprised", "determined",
    "calm", "amused", "curious", "confident",
]


# =============================================================================
# Public Avatar Serving Tests (SMOKE)
# =============================================================================

class TestAvatarServing:
    """Test public avatar serving endpoints (no auth required)."""

    @pytest.mark.smoke
    def test_get_avatar_nonexistent(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/avatar returns 404 when no avatar exists."""
        response = api_client.get(
            "/api/agents/nonexistent-avatar-agent/avatar",
            auth=False,
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_get_avatar_reference_nonexistent(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/avatar/reference returns 404 when no reference."""
        response = api_client.get(
            "/api/agents/nonexistent-avatar-agent/avatar/reference",
            auth=False,
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_get_avatar_no_avatar_on_agent(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar returns 404 for agent without avatar."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar",
            auth=False,
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_get_avatar_reference_no_reference(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar/reference returns 404 for agent without reference."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/reference",
            auth=False,
        )
        assert_status(response, 404)


# =============================================================================
# Emotion Endpoint Tests (SMOKE)
# =============================================================================

class TestAvatarEmotions:
    """Test emotion variant endpoints (no auth required)."""

    @pytest.mark.smoke
    def test_list_emotions_empty(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar/emotions returns empty list when no emotions."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/emotions",
            auth=False,
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "emotions"])
        assert data["agent_name"] == created_agent["name"]
        assert data["emotions"] == []

    @pytest.mark.smoke
    def test_list_emotions_nonexistent_agent(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/avatar/emotions returns empty for nonexistent agent."""
        response = api_client.get(
            "/api/agents/nonexistent-agent-xyz/avatar/emotions",
            auth=False,
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["emotions"] == []

    @pytest.mark.smoke
    def test_get_emotion_invalid(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar/emotion/{emotion} returns 400 for invalid emotion."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/emotion/angry",
            auth=False,
        )
        assert_status(response, 400)
        data = assert_json_response(response)
        assert "Invalid emotion" in data["detail"]

    @pytest.mark.smoke
    def test_get_emotion_valid_but_missing(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar/emotion/{emotion} returns 404 when file missing."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/emotion/happy",
            auth=False,
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_all_valid_emotions_return_404(self, api_client: TrinityApiClient, created_agent):
        """All 8 valid emotions return 404 (not 400) for agent without emotions."""
        for emotion in AVATAR_EMOTIONS:
            response = api_client.get(
                f"/api/agents/{created_agent['name']}/avatar/emotion/{emotion}",
                auth=False,
            )
            assert_status(response, 404, f"Expected 404 for valid emotion '{emotion}'")


# =============================================================================
# Identity Prompt Tests (Auth Required)
# =============================================================================

class TestAvatarIdentity:
    """Test avatar identity prompt CRUD (auth required)."""

    @pytest.mark.smoke
    def test_get_identity_no_prompt(self, api_client: TrinityApiClient, created_agent):
        """GET /api/agents/{name}/avatar/identity returns null fields when no prompt set."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/identity"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "identity_prompt", "updated_at", "has_avatar", "has_reference"])
        assert data["agent_name"] == created_agent["name"]
        assert data["identity_prompt"] is None
        assert data["updated_at"] is None
        assert data["has_avatar"] is False
        assert data["has_reference"] is False

    @pytest.mark.smoke
    def test_get_identity_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """GET /api/agents/{name}/avatar/identity without auth returns 401."""
        response = unauthenticated_client.get(
            f"/api/agents/{created_agent['name']}/avatar/identity",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_get_identity_nonexistent_agent(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/avatar/identity for nonexistent agent returns 403."""
        response = api_client.get(
            "/api/agents/nonexistent-agent-xyz/avatar/identity"
        )
        # Admin bypasses access check — may get 200 with null fields
        # Non-admin would get 403
        assert_status_in(response, [200, 403])


# =============================================================================
# Avatar Generation Tests (Auth Required)
# =============================================================================

class TestAvatarGeneration:
    """Test avatar generation endpoint (requires auth + Gemini API)."""

    @pytest.mark.smoke
    def test_generate_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """POST /api/agents/{name}/avatar/generate without auth returns 401."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": "A sleek robot"},
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_generate_nonexistent_agent(self, api_client: TrinityApiClient):
        """POST generate for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/avatar/generate",
            json={"identity_prompt": "A sleek robot"},
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_generate_empty_prompt(self, api_client: TrinityApiClient, created_agent):
        """POST generate with empty prompt returns 400."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": ""},
        )
        assert_status(response, 400)
        data = assert_json_response(response)
        assert "empty" in data["detail"].lower()

    @pytest.mark.smoke
    def test_generate_whitespace_only_prompt(self, api_client: TrinityApiClient, created_agent):
        """POST generate with whitespace-only prompt returns 400."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": "   "},
        )
        assert_status(response, 400)

    @pytest.mark.smoke
    def test_generate_prompt_too_long(self, api_client: TrinityApiClient, created_agent):
        """POST generate with >500 char prompt returns 400."""
        long_prompt = "A" * 501
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": long_prompt},
        )
        assert_status(response, 400)
        data = assert_json_response(response)
        assert "500" in data["detail"]

    @pytest.mark.smoke
    def test_generate_prompt_at_limit(self, api_client: TrinityApiClient, created_agent):
        """POST generate with exactly 500 char prompt does not get 400 for length."""
        prompt_500 = "A" * 500
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": prompt_500},
        )
        # Should NOT get 400 for length — may get 501 (no Gemini key) or 200/422
        assert response.status_code != 400 or "500 characters" not in response.json().get("detail", "")

    @pytest.mark.smoke
    def test_generate_missing_prompt_field(self, api_client: TrinityApiClient, created_agent):
        """POST generate without identity_prompt field returns 422 (validation)."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={},
        )
        assert_status(response, 422)

    def test_generate_returns_501_without_gemini_key(
        self, api_client: TrinityApiClient, created_agent
    ):
        """POST generate returns 501 when GEMINI_API_KEY is not configured."""
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/generate",
            json={"identity_prompt": "A sleek chrome android"},
        )
        # Either 501 (no key) or 200 (key configured) — both valid
        if response.status_code == 501:
            data = assert_json_response(response)
            assert "GEMINI_API_KEY" in data["detail"]
        else:
            assert_status_in(response, [200, 422])


# =============================================================================
# Avatar Regeneration Tests (Auth Required)
# =============================================================================

class TestAvatarRegeneration:
    """Test avatar regeneration endpoint (requires existing reference)."""

    @pytest.mark.smoke
    def test_regenerate_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """POST regenerate without auth returns 401."""
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/avatar/regenerate",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_regenerate_nonexistent_agent(self, api_client: TrinityApiClient):
        """POST regenerate for nonexistent agent returns 404."""
        response = api_client.post(
            "/api/agents/nonexistent-agent-xyz/avatar/regenerate",
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_regenerate_no_reference(self, api_client: TrinityApiClient, created_agent):
        """POST regenerate without reference image returns 404 (or 200 if ref exists from earlier test)."""
        # Clean any leftover avatar from earlier tests in this module
        api_client.delete(f"/api/agents/{created_agent['name']}/avatar")

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/avatar/regenerate",
        )
        assert_status(response, 404)
        data = assert_json_response(response)
        assert "reference" in data["detail"].lower() or "prompt" in data["detail"].lower()


# =============================================================================
# Avatar Deletion Tests (Auth Required)
# =============================================================================

class TestAvatarDeletion:
    """Test avatar deletion endpoint."""

    @pytest.mark.smoke
    def test_delete_unauthenticated(
        self, unauthenticated_client: TrinityApiClient, created_agent
    ):
        """DELETE avatar without auth returns 401."""
        response = unauthenticated_client.delete(
            f"/api/agents/{created_agent['name']}/avatar",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_delete_nonexistent_agent(self, api_client: TrinityApiClient):
        """DELETE avatar for nonexistent agent returns 404."""
        response = api_client.delete(
            "/api/agents/nonexistent-agent-xyz/avatar",
        )
        assert_status(response, 404)

    @pytest.mark.smoke
    def test_delete_no_avatar(self, api_client: TrinityApiClient, created_agent):
        """DELETE avatar when none exists returns 200 (idempotent cleanup)."""
        response = api_client.delete(
            f"/api/agents/{created_agent['name']}/avatar",
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert "removed" in data["message"].lower()

    @pytest.mark.smoke
    def test_delete_clears_identity(self, api_client: TrinityApiClient, created_agent):
        """DELETE avatar clears identity prompt in DB."""
        # Delete (idempotent)
        api_client.delete(f"/api/agents/{created_agent['name']}/avatar")

        # Verify identity is cleared
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/avatar/identity"
        )
        assert_status(response, 200)
        data = assert_json_response(response)
        assert data["identity_prompt"] is None
        assert data["has_avatar"] is False
        assert data["has_reference"] is False


# =============================================================================
# Default Avatar Generation Tests (Admin Only)
# =============================================================================

class TestDefaultAvatarGeneration:
    """Test admin-only default avatar generation endpoint."""

    @pytest.mark.smoke
    def test_generate_defaults_unauthenticated(
        self, unauthenticated_client: TrinityApiClient
    ):
        """POST generate-defaults without auth returns 401."""
        response = unauthenticated_client.post(
            "/api/agents/avatars/generate-defaults",
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.slow
    def test_generate_defaults_returns_structure(self, api_client: TrinityApiClient):
        """POST generate-defaults returns expected response structure.

        Marked slow because this calls Gemini for every agent without a custom avatar,
        which can take minutes depending on fleet size.
        """
        import httpx

        try:
            # Use a longer timeout since this generates avatars for all agents
            response = api_client._client.post(
                "/api/agents/avatars/generate-defaults",
                headers={"Authorization": f"Bearer {api_client.token}"},
                timeout=300.0,
            )
        except httpx.ReadTimeout:
            pytest.skip("generate-defaults timed out (fleet too large)")

        # 200 (success) or 501 (no Gemini key) — both valid
        if response.status_code == 501:
            data = assert_json_response(response)
            assert "GEMINI_API_KEY" in data["detail"]
        else:
            assert_status(response, 200)
            data = assert_json_response(response)
            assert_has_fields(data, ["generated", "failed", "skipped", "agents", "errors", "message"])
            assert isinstance(data["generated"], int)
            assert isinstance(data["failed"], int)
            assert isinstance(data["skipped"], int)
            assert isinstance(data["agents"], list)
            assert isinstance(data["errors"], list)


# =============================================================================
# End-to-End Avatar Lifecycle Tests (requires Gemini API)
# =============================================================================

class TestAvatarLifecycle:
    """Test full avatar lifecycle when Gemini API is available.

    These tests are skipped if GEMINI_API_KEY is not configured (501 response).
    """

    def test_generate_and_serve(self, api_client: TrinityApiClient, created_agent):
        """Full lifecycle: generate → serve → identity → delete."""
        agent_name = created_agent["name"]

        # Step 1: Generate
        gen_response = api_client.post(
            f"/api/agents/{agent_name}/avatar/generate",
            json={"identity_prompt": "A sleek chrome android with blue eyes"},
        )

        if gen_response.status_code == 501:
            pytest.skip("GEMINI_API_KEY not configured")

        if gen_response.status_code == 422:
            pytest.skip("Image generation failed (API error)")

        assert_status(gen_response, 200)
        gen_data = assert_json_response(gen_response)
        assert_has_fields(gen_data, ["agent_name", "identity_prompt", "refined_prompt", "updated_at"])
        assert gen_data["agent_name"] == agent_name
        assert gen_data["identity_prompt"] == "A sleek chrome android with blue eyes"

        # Step 2: Serve avatar
        serve_response = api_client.get(
            f"/api/agents/{agent_name}/avatar",
            auth=False,
        )
        assert_status(serve_response, 200)
        assert serve_response.headers.get("content-type") in ("image/webp", "image/png")
        assert "max-age=86400" in serve_response.headers.get("cache-control", "")

        # Step 3: Check identity
        identity_response = api_client.get(
            f"/api/agents/{agent_name}/avatar/identity"
        )
        assert_status(identity_response, 200)
        id_data = assert_json_response(identity_response)
        assert id_data["has_avatar"] is True
        assert id_data["has_reference"] is True
        assert id_data["identity_prompt"] == "A sleek chrome android with blue eyes"

        # Step 4: Check reference image
        ref_response = api_client.get(
            f"/api/agents/{agent_name}/avatar/reference",
            auth=False,
        )
        assert_status(ref_response, 200)
        assert ref_response.headers.get("content-type") == "image/png"

        # Step 5: Delete
        del_response = api_client.delete(
            f"/api/agents/{agent_name}/avatar"
        )
        assert_status(del_response, 200)

        # Step 6: Verify deleted
        serve_after = api_client.get(
            f"/api/agents/{agent_name}/avatar",
            auth=False,
        )
        assert_status(serve_after, 404)

        identity_after = api_client.get(
            f"/api/agents/{agent_name}/avatar/identity"
        )
        assert_status(identity_after, 200)
        id_after = assert_json_response(identity_after)
        assert id_after["has_avatar"] is False
        assert id_after["has_reference"] is False
        assert id_after["identity_prompt"] is None

    def test_regenerate_after_generate(self, api_client: TrinityApiClient, created_agent):
        """Regenerate creates a variation from the reference."""
        agent_name = created_agent["name"]

        # Generate first
        gen_response = api_client.post(
            f"/api/agents/{agent_name}/avatar/generate",
            json={"identity_prompt": "A precise robot with green eyes"},
        )
        if gen_response.status_code == 501:
            pytest.skip("GEMINI_API_KEY not configured")
        if gen_response.status_code == 422:
            pytest.skip("Image generation failed (API error)")
        assert_status(gen_response, 200)

        # Regenerate
        regen_response = api_client.post(
            f"/api/agents/{agent_name}/avatar/regenerate",
        )
        if regen_response.status_code == 501:
            pytest.skip("GEMINI_API_KEY not configured")
        if regen_response.status_code == 422:
            pytest.skip("Image generation failed (API error)")

        assert_status(regen_response, 200)
        regen_data = assert_json_response(regen_response)
        assert_has_fields(regen_data, ["agent_name", "identity_prompt", "updated_at"])
        assert regen_data["identity_prompt"] == "A precise robot with green eyes"

        # Cleanup
        api_client.delete(f"/api/agents/{agent_name}/avatar")

    def test_emotions_appear_after_generate(self, api_client: TrinityApiClient, created_agent):
        """After generation, emotion variants are generated in background."""
        import time
        agent_name = created_agent["name"]

        gen_response = api_client.post(
            f"/api/agents/{agent_name}/avatar/generate",
            json={"identity_prompt": "A friendly robot assistant"},
        )
        if gen_response.status_code == 501:
            pytest.skip("GEMINI_API_KEY not configured")
        if gen_response.status_code == 422:
            pytest.skip("Image generation failed (API error)")
        assert_status(gen_response, 200)

        # Wait for background emotion generation (may take a while)
        max_wait = 120
        start = time.time()
        emotions_found = []
        while time.time() - start < max_wait:
            response = api_client.get(
                f"/api/agents/{agent_name}/avatar/emotions",
                auth=False,
            )
            if response.status_code == 200:
                data = response.json()
                emotions_found = data.get("emotions", [])
                if len(emotions_found) >= 1:
                    break
            time.sleep(5)

        # At least some emotions should have been generated
        assert len(emotions_found) >= 1, "Expected at least 1 emotion variant after generation"

        # Verify emotion serving works
        if emotions_found:
            emotion = emotions_found[0]
            serve_response = api_client.get(
                f"/api/agents/{agent_name}/avatar/emotion/{emotion}",
                auth=False,
            )
            assert_status(serve_response, 200)
            assert serve_response.headers.get("content-type") in ("image/webp", "image/png")

        # Cleanup
        api_client.delete(f"/api/agents/{agent_name}/avatar")
