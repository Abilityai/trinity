"""
Image Generation API Tests (IMG-001)

Tests for the platform image generation REST endpoints:
- POST /api/images/generate — Generate image from prompt
- GET /api/images/models — List available models and options

Test tiers:
- SMOKE: Endpoint structure, auth, validation (no GEMINI_API_KEY needed)
- SLOW: Actual image generation (requires GEMINI_API_KEY configured on backend)

Note: Most smoke tests work regardless of whether GEMINI_API_KEY is set.
The /models endpoint always works. The /generate endpoint returns 501 if
the key is not configured, which is a valid and testable response.
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
# GET /api/images/models Tests (SMOKE)
# =============================================================================

class TestImageModelsEndpoint:
    """Tests for GET /api/images/models."""

    @pytest.mark.smoke
    def test_get_models_authenticated(self, api_client: TrinityApiClient):
        """GET /api/images/models returns model info for authenticated user."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["available", "models", "default_model", "use_cases", "aspect_ratios"])

    @pytest.mark.smoke
    def test_get_models_returns_valid_use_cases(self, api_client: TrinityApiClient):
        """GET /api/images/models returns expected use cases."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        data = response.json()

        expected_use_cases = ["general", "thumbnail", "diagram", "social", "avatar"]
        assert data["use_cases"] == expected_use_cases

    @pytest.mark.smoke
    def test_get_models_returns_valid_aspect_ratios(self, api_client: TrinityApiClient):
        """GET /api/images/models returns expected aspect ratios."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        data = response.json()

        expected_ratios = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
        assert data["aspect_ratios"] == expected_ratios

    @pytest.mark.smoke
    def test_get_models_returns_model_names(self, api_client: TrinityApiClient):
        """GET /api/images/models includes text and image model names."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        data = response.json()

        assert_has_fields(data["models"], ["text_refinement", "image_generation"])
        assert "gemini" in data["models"]["text_refinement"]
        assert "gemini" in data["models"]["image_generation"]
        assert data["default_model"] == data["models"]["image_generation"]

    @pytest.mark.smoke
    def test_text_refinement_model_not_retired(self, api_client: TrinityApiClient):
        """Regression guard (#1130): the text-refinement model must not be a
        Gemini generation Google has retired (gemini-2.0-* / gemini-1.x 404)."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        model = response.json()["models"]["text_refinement"]
        assert not model.startswith("gemini-2.0"), f"retired model configured: {model}"
        assert not model.startswith("gemini-1."), f"retired model configured: {model}"

    @pytest.mark.smoke
    def test_get_models_availability_is_boolean(self, api_client: TrinityApiClient):
        """GET /api/images/models 'available' field is a boolean."""
        response = api_client.get("/api/images/models")
        assert_status(response, 200)
        data = response.json()
        assert isinstance(data["available"], bool)

    @pytest.mark.smoke
    def test_get_models_unauthenticated(self, unauthenticated_client: TrinityApiClient):
        """GET /api/images/models without auth returns 401."""
        response = unauthenticated_client.get("/api/images/models", auth=False)
        assert_status(response, 401)


# =============================================================================
# POST /api/images/generate — Validation Tests (SMOKE)
# =============================================================================

class TestImageGenerateValidation:
    """Tests for POST /api/images/generate input validation.

    These tests verify endpoint behavior regardless of GEMINI_API_KEY.
    When key is not set, endpoint returns 501. When set, it validates inputs.
    """

    @pytest.mark.smoke
    def test_generate_unauthenticated(self, unauthenticated_client: TrinityApiClient):
        """POST /api/images/generate without auth returns 401."""
        response = unauthenticated_client.post(
            "/api/images/generate",
            json={"prompt": "A red apple"},
            auth=False,
        )
        assert_status(response, 401)

    @pytest.mark.smoke
    def test_generate_missing_prompt(self, api_client: TrinityApiClient):
        """POST /api/images/generate without prompt returns 422."""
        response = api_client.post(
            "/api/images/generate",
            json={},
        )
        assert_status(response, 422)

    @pytest.mark.smoke
    def test_generate_empty_body(self, api_client: TrinityApiClient):
        """POST /api/images/generate with empty body returns 422."""
        response = api_client.post("/api/images/generate", json=None)
        assert_status(response, 422)

    @pytest.mark.smoke
    def test_generate_accepts_prompt(self, api_client: TrinityApiClient):
        """POST /api/images/generate with valid prompt returns 200 or 501."""
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": "A red apple on a white background"},
            timeout=120.0,
        )
        # 200 = generated (GEMINI_API_KEY set and working)
        # 501 = GEMINI_API_KEY not configured
        # 422 = generation failed (e.g., safety filter, API error)
        assert_status_in(response, [200, 422, 501])

        if response.status_code == 200:
            data = assert_json_response(response)
            assert_has_fields(data, [
                "image_base64", "mime_type", "refined_prompt",
                "model_used", "use_case", "aspect_ratio",
            ])

    @pytest.mark.smoke
    def test_generate_with_all_options(self, api_client: TrinityApiClient):
        """POST /api/images/generate accepts all optional parameters."""
        response = api_client.post(
            "/api/images/generate",
            json={
                "prompt": "A sunset over mountains",
                "use_case": "thumbnail",
                "aspect_ratio": "16:9",
                "refine_prompt": False,
            },
            timeout=120.0,
        )
        assert_status_in(response, [200, 422, 501])

        if response.status_code == 200:
            data = response.json()
            assert data["use_case"] == "thumbnail"
            assert data["aspect_ratio"] == "16:9"

    @pytest.mark.smoke
    def test_generate_invalid_use_case(self, api_client: TrinityApiClient):
        """POST /api/images/generate with invalid use_case returns 422 or 501."""
        response = api_client.post(
            "/api/images/generate",
            json={
                "prompt": "A red apple",
                "use_case": "invalid_case",
            },
            timeout=30.0,
        )
        # 422 = invalid use_case (GEMINI_API_KEY set)
        # 501 = GEMINI_API_KEY not configured (validation skipped)
        assert_status_in(response, [422, 501])

        if response.status_code == 422:
            data = assert_json_response(response)
            assert "detail" in data
            assert "use_case" in data["detail"].lower() or "invalid" in data["detail"].lower()

    @pytest.mark.smoke
    def test_generate_invalid_aspect_ratio(self, api_client: TrinityApiClient):
        """POST /api/images/generate with invalid aspect_ratio returns 422 or 501."""
        response = api_client.post(
            "/api/images/generate",
            json={
                "prompt": "A red apple",
                "aspect_ratio": "5:7",
            },
            timeout=30.0,
        )
        assert_status_in(response, [422, 501])

        if response.status_code == 422:
            data = assert_json_response(response)
            assert "detail" in data

    @pytest.mark.smoke
    def test_generate_refine_prompt_false(self, api_client: TrinityApiClient):
        """POST /api/images/generate with refine_prompt=false skips refinement."""
        response = api_client.post(
            "/api/images/generate",
            json={
                "prompt": "A simple red circle on white",
                "refine_prompt": False,
            },
            timeout=120.0,
        )
        assert_status_in(response, [200, 422, 501])

        if response.status_code == 200:
            data = response.json()
            # When refinement is skipped, refined_prompt equals original
            assert data["refined_prompt"] == "A simple red circle on white"

    @pytest.mark.smoke
    def test_generate_default_values(self, api_client: TrinityApiClient):
        """POST /api/images/generate uses correct defaults."""
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": "A cat"},
            timeout=120.0,
        )
        assert_status_in(response, [200, 422, 501])

        if response.status_code == 200:
            data = response.json()
            assert data["use_case"] == "general"
            assert data["aspect_ratio"] == "1:1"


# =============================================================================
# POST /api/images/generate — Response Structure Tests
# =============================================================================

class TestImageGenerateResponse:
    """Tests for response structure when generation succeeds.

    These tests only run meaningfully when GEMINI_API_KEY is configured.
    They gracefully skip/pass when the key is not set (501 response).
    """

    @pytest.mark.smoke
    def test_response_contains_base64_image(self, api_client: TrinityApiClient):
        """Successful generation returns valid base64 image data."""
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": "A solid blue square"},
            timeout=120.0,
        )

        if response.status_code == 501:
            pytest.skip("GEMINI_API_KEY not configured on backend")
        if response.status_code == 422:
            pytest.skip(f"Generation failed: {response.text[:200]}")

        assert_status(response, 200)
        data = response.json()

        # Verify base64 is decodable
        import base64
        try:
            image_bytes = base64.b64decode(data["image_base64"])
            assert len(image_bytes) > 100, "Image data too small to be valid"
        except Exception as e:
            raise AssertionError(f"image_base64 is not valid base64: {e}")

    @pytest.mark.smoke
    def test_response_mime_type_is_image(self, api_client: TrinityApiClient):
        """Successful generation returns an image mime type."""
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": "A solid green circle"},
            timeout=120.0,
        )

        if response.status_code in [501, 422]:
            pytest.skip("Generation not available or failed")

        assert_status(response, 200)
        data = response.json()
        assert data["mime_type"].startswith("image/"), \
            f"Expected image/* mime type, got {data['mime_type']}"

    @pytest.mark.smoke
    def test_response_includes_original_prompt(self, api_client: TrinityApiClient):
        """Response includes the original prompt that was submitted."""
        original = "A bright yellow star shape"
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": original},
            timeout=120.0,
        )

        if response.status_code in [501, 422]:
            pytest.skip("Generation not available or failed")

        assert_status(response, 200)
        data = response.json()
        assert data["original_prompt"] == original

    @pytest.mark.smoke
    def test_response_refined_prompt_differs(self, api_client: TrinityApiClient):
        """When refinement is on, refined_prompt should differ from original."""
        original = "cat"
        response = api_client.post(
            "/api/images/generate",
            json={"prompt": original, "refine_prompt": True},
            timeout=120.0,
        )

        if response.status_code in [501, 422]:
            pytest.skip("Generation not available or failed")

        assert_status(response, 200)
        data = response.json()
        # Refined prompt should be longer/more detailed than "cat"
        assert len(data["refined_prompt"]) > len(original), \
            f"Refined prompt should be longer than '{original}', got: {data['refined_prompt']}"

    @pytest.mark.smoke
    def test_each_use_case_accepted(self, api_client: TrinityApiClient):
        """All documented use cases are accepted by the endpoint."""
        # First check what use cases are valid
        models_resp = api_client.get("/api/images/models")
        assert_status(models_resp, 200)
        use_cases = models_resp.json()["use_cases"]

        for use_case in use_cases:
            response = api_client.post(
                "/api/images/generate",
                json={"prompt": "test", "use_case": use_case},
                timeout=120.0,
            )
            # Should not get 422 for valid use cases (501 OK if key not set)
            assert response.status_code != 422 or "use_case" not in response.text.lower(), \
                f"Valid use_case '{use_case}' was rejected"

    @pytest.mark.smoke
    def test_each_aspect_ratio_accepted(self, api_client: TrinityApiClient):
        """All documented aspect ratios are accepted by the endpoint."""
        models_resp = api_client.get("/api/images/models")
        assert_status(models_resp, 200)
        ratios = models_resp.json()["aspect_ratios"]

        for ratio in ratios:
            response = api_client.post(
                "/api/images/generate",
                json={"prompt": "test", "aspect_ratio": ratio},
                timeout=120.0,
            )
            assert response.status_code != 422 or "aspect_ratio" not in response.text.lower(), \
                f"Valid aspect_ratio '{ratio}' was rejected"
