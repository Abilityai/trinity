"""
Unit tests for ImageGenerationService (IMG-001).

Tests the service logic with mocked Gemini API calls.
No backend or network access required.

Module: src/backend/services/image_generation_service.py

Import strategy: Uses importlib to load modules directly by file path,
bypassing services/__init__.py which imports docker_service and other
heavy dependencies not available in the test environment.
"""

import base64
import importlib.util
import json
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Backend path for direct file imports (relative to this file)
_project_root = Path(__file__).resolve().parents[2]
BACKEND_PATH = str(_project_root / 'src' / 'backend')
if BACKEND_PATH not in sys.path:
    sys.path.insert(0, BACKEND_PATH)


# =============================================================================
# Module Loading Helpers
# =============================================================================

def _load_module(name, filename):
    """Load a module directly by file path, bypassing services/__init__.py."""
    spec = importlib.util.spec_from_file_location(
        name,
        f"{BACKEND_PATH}/services/{filename}",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_prompts():
    """Load image_generation_prompts module directly."""
    return _load_module("image_generation_prompts", "image_generation_prompts.py")


def _load_service(api_key=""):
    """Load image_generation_service module with mocked config.GEMINI_API_KEY.

    The service module imports from config and from image_generation_prompts.
    We pre-populate sys.modules to satisfy those imports without triggering
    the services/__init__.py import chain.
    """
    # Load prompts module first (no dependencies)
    prompts_mod = _load_prompts()

    # Create a mock config module with GEMINI_API_KEY
    mock_config = MagicMock()
    mock_config.GEMINI_API_KEY = api_key

    # Pre-populate sys.modules so the service can import its dependencies
    saved_modules = {}
    modules_to_mock = {
        'config': mock_config,
        'services.image_generation_prompts': prompts_mod,
    }
    for mod_name, mod in modules_to_mock.items():
        saved_modules[mod_name] = sys.modules.get(mod_name)
        sys.modules[mod_name] = mod

    # Remove cached service module to force reimport
    for key in list(sys.modules.keys()):
        if 'image_generation_service' in key:
            del sys.modules[key]

    try:
        spec = importlib.util.spec_from_file_location(
            "image_generation_service",
            f"{BACKEND_PATH}/services/image_generation_service.py",
        )
        service_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service_mod)
        return service_mod
    finally:
        # Restore original sys.modules state
        for mod_name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = original


# =============================================================================
# Fake Gemini API Responses
# =============================================================================

FAKE_TEXT_RESPONSE = {
    "candidates": [{
        "content": {
            "parts": [{"text": "A photorealistic red apple with dewdrops on a clean white surface, soft studio lighting"}]
        }
    }]
}

FAKE_IMAGE_B64 = base64.b64encode(b"fake_png_image_data_for_testing").decode("ascii")
FAKE_IMAGE_RESPONSE = {
    "candidates": [{
        "content": {
            "parts": [{
                "inlineData": {
                    "data": FAKE_IMAGE_B64,
                    "mimeType": "image/png",
                }
            }]
        }
    }]
}

FAKE_IMAGE_RESPONSE_NO_IMAGE = {
    "candidates": [{
        "content": {
            "parts": [{"text": "I cannot generate that image."}]
        }
    }]
}


def _make_mock_response(status_code: int, json_data: dict):
    """Create a mock httpx.Response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.text = json.dumps(json_data)
    return mock


# =============================================================================
# Prompt Constants Tests
# =============================================================================

@pytest.mark.unit
class TestImageGenerationPrompts:
    """Test the prompt constants module."""

    def test_use_case_prompts_has_all_cases(self):
        prompts_mod = _load_prompts()
        for case in prompts_mod.VALID_USE_CASES:
            assert case in prompts_mod.USE_CASE_PROMPTS, f"Missing prompt for use case: {case}"

    def test_valid_use_cases_list(self):
        prompts_mod = _load_prompts()
        expected = ["general", "thumbnail", "diagram", "social", "avatar"]
        assert prompts_mod.VALID_USE_CASES == expected

    def test_valid_aspect_ratios_list(self):
        prompts_mod = _load_prompts()
        expected = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"]
        assert prompts_mod.VALID_ASPECT_RATIOS == expected

    def test_prompts_are_nonempty_strings(self):
        prompts_mod = _load_prompts()
        for case, prompt in prompts_mod.USE_CASE_PROMPTS.items():
            assert isinstance(prompt, str), f"Prompt for {case} is not a string"
            assert len(prompt) > 100, f"Prompt for {case} is too short ({len(prompt)} chars)"

    def test_prompts_mention_output_format(self):
        """Each prompt should instruct the model on output format."""
        prompts_mod = _load_prompts()
        for case, prompt in prompts_mod.USE_CASE_PROMPTS.items():
            assert "output" in prompt.lower() or "return" in prompt.lower(), \
                f"Prompt for {case} doesn't mention output format"

    def test_prompts_mention_no_text(self):
        """Each prompt should warn against text in images."""
        prompts_mod = _load_prompts()
        for case, prompt in prompts_mod.USE_CASE_PROMPTS.items():
            assert "text" in prompt.lower(), \
                f"Prompt for {case} doesn't mention text handling"


# =============================================================================
# ImageGenerationResult Tests
# =============================================================================

@pytest.mark.unit
class TestImageGenerationResult:
    """Test the result dataclass."""

    def test_success_result(self):
        mod = _load_service()
        result = mod.ImageGenerationResult(
            success=True,
            image_data=b"fake_image_bytes",
            mime_type="image/png",
            refined_prompt="A detailed prompt",
            original_prompt="simple prompt",
            model_used="gemini-2.5-flash-preview-image-generation",
            use_case="general",
            aspect_ratio="1:1",
        )
        assert result.success is True
        assert result.image_data == b"fake_image_bytes"
        assert result.error is None

    def test_error_result(self):
        mod = _load_service()
        result = mod.ImageGenerationResult(
            success=False,
            error="API key not configured",
        )
        assert result.success is False
        assert result.image_data is None
        assert result.error == "API key not configured"

    def test_default_values(self):
        mod = _load_service()
        result = mod.ImageGenerationResult(success=True)
        assert result.mime_type == "image/png"
        assert result.use_case == "general"
        assert result.aspect_ratio == "1:1"
        assert result.error_kind is None  # #957

    def test_error_kind_field_is_settable(self):
        """#957: error_kind drives router status/detail mapping."""
        mod = _load_service()
        result = mod.ImageGenerationResult(
            success=False,
            error="GEMINI_API_KEY not configured",
            error_kind="not_configured",
        )
        assert result.error_kind == "not_configured"


# =============================================================================
# #957 — _classify_exception
# =============================================================================

@pytest.mark.unit
class TestClassifyException:
    """Map upstream exceptions to coarse error_kind values for the router."""

    def test_timeout_exception_classified_as_timeout(self):
        import httpx
        mod = _load_service()
        kind = mod._classify_exception(httpx.ReadTimeout("read timed out"))
        assert kind == "timeout"

    def test_connect_error_classified_as_upstream(self):
        import httpx
        mod = _load_service()
        kind = mod._classify_exception(httpx.ConnectError("nope"))
        assert kind == "upstream_error"

    def test_safety_filter_runtimeerror(self):
        mod = _load_service()
        kind = mod._classify_exception(
            RuntimeError("Gemini image API returned no image data. ...")
        )
        assert kind == "safety_filter"

    def test_rate_limit_runtimeerror(self):
        mod = _load_service()
        kind = mod._classify_exception(
            RuntimeError("Gemini text API error 429: rate limit")
        )
        assert kind == "rate_limited"

    def test_upstream_5xx(self):
        mod = _load_service()
        kind = mod._classify_exception(
            RuntimeError("Gemini image API error 503: service unavailable")
        )
        assert kind == "upstream_error"

    def test_unknown_default(self):
        mod = _load_service()
        kind = mod._classify_exception(ValueError("something else"))
        assert kind == "unknown"


# =============================================================================
# ImageGenerationService Availability Tests
# =============================================================================

@pytest.mark.unit
class TestImageGenerationServiceAvailability:
    """Test service availability checks."""

    def test_not_available_without_key(self):
        mod = _load_service(api_key="")
        service = mod.ImageGenerationService()
        assert service.available is False

    def test_available_with_key(self):
        mod = _load_service(api_key="test-api-key")
        service = mod.ImageGenerationService()
        assert service.available is True


# =============================================================================
# generate_image Tests (with mocked HTTP)
# =============================================================================

@pytest.mark.unit
class TestGenerateImage:
    """Test the generate_image method with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_returns_error_when_no_api_key(self):
        """generate_image() returns error result when GEMINI_API_KEY is empty."""
        mod = _load_service(api_key="")
        service = mod.ImageGenerationService()
        result = await service.generate_image("A red apple")

        assert result.success is False
        assert "not configured" in result.error.lower()
        assert result.original_prompt == "A red apple"

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_use_case(self):
        """generate_image() returns error for unknown use_case."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()
        result = await service.generate_image("test", use_case="nonexistent")

        assert result.success is False
        assert "use_case" in result.error.lower()

    @pytest.mark.asyncio
    async def test_returns_error_for_invalid_aspect_ratio(self):
        """generate_image() returns error for unknown aspect_ratio."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()
        result = await service.generate_image("test", aspect_ratio="5:7")

        assert result.success is False
        assert "aspect_ratio" in result.error.lower()

    @pytest.mark.asyncio
    async def test_full_pipeline_with_refinement(self):
        """generate_image() calls text model then image model."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.side_effect = [
            _make_mock_response(200, FAKE_TEXT_RESPONSE),
            _make_mock_response(200, FAKE_IMAGE_RESPONSE),
        ]
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image(
            "A red apple",
            use_case="general",
            aspect_ratio="1:1",
            refine_prompt=True,
        )

        assert result.success is True
        assert result.image_data == b"fake_png_image_data_for_testing"
        assert result.mime_type == "image/png"
        assert result.refined_prompt is not None
        assert result.original_prompt == "A red apple"
        assert result.use_case == "general"
        assert result.aspect_ratio == "1:1"
        # Two HTTP calls: text refinement + image generation
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_pipeline_without_refinement(self):
        """generate_image(refine_prompt=False) skips text model call."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image("A red apple", refine_prompt=False)

        assert result.success is True
        assert result.refined_prompt == "A red apple"  # unchanged
        assert result.original_prompt == "A red apple"
        # Only one HTTP call (image only)
        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_prompt_on_refinement_error(self):
        """If prompt refinement fails, raw prompt is used for image gen."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.side_effect = [
            _make_mock_response(500, {"error": "Internal error"}),
            _make_mock_response(200, FAKE_IMAGE_RESPONSE),
        ]
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image("A red apple", refine_prompt=True)

        assert result.success is True
        assert result.image_data is not None
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_error_on_image_gen_failure(self):
        """generate_image() returns error when image API fails."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.side_effect = [
            _make_mock_response(200, FAKE_TEXT_RESPONSE),
            _make_mock_response(500, {"error": "Server error"}),
        ]
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image("A red apple")

        assert result.success is False
        assert result.error is not None
        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_returns_error_when_no_image_in_response(self):
        """generate_image() returns error if API returns text instead of image."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.side_effect = [
            _make_mock_response(200, FAKE_TEXT_RESPONSE),
            _make_mock_response(200, FAKE_IMAGE_RESPONSE_NO_IMAGE),
        ]
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image("A red apple")

        assert result.success is False
        assert "no image data" in result.error.lower() or "safety" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_name_for_logging(self):
        """generate_image() accepts agent_name for logging without error."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.generate_image(
            "test", refine_prompt=False, agent_name="test-agent"
        )
        assert result.success is True


# =============================================================================
# refine_prompt Tests
# =============================================================================

@pytest.mark.unit
class TestRefinePrompt:
    """Test the prompt refinement method."""

    @pytest.mark.asyncio
    async def test_refine_prompt_calls_text_model(self):
        """refine_prompt() calls Gemini text model with best practices."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_TEXT_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.refine_prompt("A cat", "general", "1:1")

        assert isinstance(result, str)
        assert len(result) > 0

        call_args = mock_http.post.call_args
        url = call_args[0][0]
        assert "gemini" in url
        assert "generateContent" in url

        payload = call_args[1]["json"]
        assert "system_instruction" in payload
        assert "contents" in payload
        user_text = payload["contents"][0]["parts"][0]["text"]
        assert "A cat" in user_text
        assert "1:1" in user_text

    @pytest.mark.asyncio
    async def test_refine_prompt_uses_correct_use_case(self):
        """refine_prompt() selects the right best-practices prompt."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_TEXT_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        await service.refine_prompt("test", "thumbnail", "16:9")

        call_args = mock_http.post.call_args
        payload = call_args[1]["json"]
        system_text = payload["system_instruction"]["parts"][0]["text"]
        assert "thumbnail" in system_text.lower()

    @pytest.mark.asyncio
    async def test_refine_prompt_strips_whitespace(self):
        """refine_prompt() strips leading/trailing whitespace from result."""
        response_with_whitespace = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "  refined prompt with spaces  \n"}]
                }
            }]
        }
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, response_with_whitespace)
        mock_http.is_closed = False
        service._client = mock_http

        result = await service.refine_prompt("test", "general", "1:1")

        assert result == "refined prompt with spaces"
        assert not result.startswith(" ")
        assert not result.endswith("\n")


# =============================================================================
# _call_gemini_text Tests
# =============================================================================

@pytest.mark.unit
class TestCallGeminiText:
    """Test the raw text API call method."""

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self):
        """_call_gemini_text() raises RuntimeError on non-200 response."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(429, {"error": "Rate limited"})
        mock_http.is_closed = False
        service._client = mock_http

        with pytest.raises(RuntimeError, match="429"):
            await service._call_gemini_text("system", "user message")

    @pytest.mark.asyncio
    async def test_raises_on_malformed_response(self):
        """_call_gemini_text() raises on unexpected response structure."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, {"candidates": []})
        mock_http.is_closed = False
        service._client = mock_http

        with pytest.raises(RuntimeError, match="Unexpected"):
            await service._call_gemini_text("system", "user message")

    @pytest.mark.asyncio
    async def test_sends_api_key_header(self):
        """_call_gemini_text() includes x-goog-api-key header."""
        mod = _load_service(api_key="my-secret-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_TEXT_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        await service._call_gemini_text("system", "user")

        call_kwargs = mock_http.post.call_args[1]
        assert call_kwargs["headers"]["x-goog-api-key"] == "my-secret-key"


# =============================================================================
# _call_gemini_image Tests
# =============================================================================

@pytest.mark.unit
class TestCallGeminiImage:
    """Test the raw image API call method."""

    @pytest.mark.asyncio
    async def test_returns_bytes_and_mime_type(self):
        """_call_gemini_image() returns (bytes, mime_type) tuple."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        image_bytes, mime_type = await service._call_gemini_image("prompt", "1:1")

        assert isinstance(image_bytes, bytes)
        assert mime_type == "image/png"
        assert image_bytes == b"fake_png_image_data_for_testing"

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self):
        """_call_gemini_image() raises RuntimeError on non-200 response."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(400, {"error": "Bad request"})
        mock_http.is_closed = False
        service._client = mock_http

        with pytest.raises(RuntimeError, match="400"):
            await service._call_gemini_image("prompt", "1:1")

    @pytest.mark.asyncio
    async def test_raises_when_no_image_in_response(self):
        """_call_gemini_image() raises when response has no inlineData."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE_NO_IMAGE)
        mock_http.is_closed = False
        service._client = mock_http

        with pytest.raises(RuntimeError, match="no image data"):
            await service._call_gemini_image("prompt", "1:1")

    @pytest.mark.asyncio
    async def test_sends_aspect_ratio_in_config(self):
        """_call_gemini_image() includes aspect ratio in generation config."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        await service._call_gemini_image("prompt", "16:9")

        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        aspect = payload["generationConfig"]["imageConfig"]["aspectRatio"]
        assert aspect == "16:9"

    @pytest.mark.asyncio
    async def test_requests_image_response_modality(self):
        """_call_gemini_image() requests image+text response modalities."""
        mod = _load_service(api_key="test-key")
        service = mod.ImageGenerationService()

        mock_http = AsyncMock()
        mock_http.post.return_value = _make_mock_response(200, FAKE_IMAGE_RESPONSE)
        mock_http.is_closed = False
        service._client = mock_http

        await service._call_gemini_image("prompt", "1:1")

        call_kwargs = mock_http.post.call_args[1]
        payload = call_kwargs["json"]
        modalities = payload["generationConfig"]["responseModalities"]
        assert "image" in modalities


# =============================================================================
# Singleton Tests
# =============================================================================

@pytest.mark.unit
class TestSingleton:
    """Test the singleton accessor."""

    def test_get_service_returns_instance(self):
        mod = _load_service(api_key="")
        service = mod.get_image_generation_service()
        assert isinstance(service, mod.ImageGenerationService)

    def test_get_service_returns_same_instance(self):
        mod = _load_service(api_key="")
        # Reset singleton state
        mod._image_generation_service = None
        s1 = mod.get_image_generation_service()
        s2 = mod.get_image_generation_service()
        assert s1 is s2
