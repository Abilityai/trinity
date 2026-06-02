"""
Platform-level image generation service (IMG-001).

Two-step pipeline:
1. Prompt refinement — Gemini 2.0 Flash (text) rewrites the raw prompt using best practices
2. Image generation — Gemini 3.1 Flash Image Preview produces the actual image

Other code (routers, services, MCP tools, agents) calls:
    await image_generation_service.generate_image("a red apple", use_case="thumbnail")
"""

import base64
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import GEMINI_API_KEY
from services.image_generation_prompts import (
    USE_CASE_PROMPTS,
    VALID_ASPECT_RATIOS,
    VALID_USE_CASES,
)

logger = logging.getLogger(__name__)

# Gemini API configuration
GEMINI_TEXT_MODEL = "gemini-2.0-flash"
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Timeouts
PROMPT_REFINEMENT_TIMEOUT = 30.0  # seconds
IMAGE_GENERATION_TIMEOUT = 120.0  # seconds — image gen can be slow


@dataclass
class ImageGenerationResult:
    """Result of an image generation request."""
    success: bool
    image_data: Optional[bytes] = None
    mime_type: str = "image/png"
    refined_prompt: Optional[str] = None
    original_prompt: Optional[str] = None
    model_used: str = GEMINI_IMAGE_MODEL
    use_case: str = "general"
    aspect_ratio: str = "1:1"
    error: Optional[str] = None
    # #957: coarse classification so the router can pick a meaningful HTTP
    # status and the frontend can render an actionable message instead of a
    # raw upstream error. Kinds:
    #   not_configured  — GEMINI_API_KEY missing
    #   invalid_input   — use_case / aspect_ratio rejected before API call
    #   safety_filter   — upstream returned no image (prompt blocked)
    #   rate_limited    — upstream HTTP 429
    #   upstream_error  — upstream HTTP 5xx or unparseable response
    #   timeout         — httpx timeout / connect error
    #   unknown         — anything else
    error_kind: Optional[str] = None


def _classify_exception(exc: BaseException) -> str:
    """Map a generation-path exception to one of the error_kind values."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "upstream_error"
    msg = str(exc)
    if "no image data" in msg or "safety filter" in msg.lower():
        return "safety_filter"
    if "API error 429" in msg:
        return "rate_limited"
    if "API error 5" in msg:  # 500/502/503/504
        return "upstream_error"
    if "API error 4" in msg:  # 400/401/403 — surfaced as upstream for now
        return "upstream_error"
    return "unknown"


class ImageGenerationService:
    """Platform image generation service using Gemini models."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(IMAGE_GENERATION_TIMEOUT, connect=10.0),
            )
        return self._client

    @property
    def available(self) -> bool:
        """Whether the service has a configured API key."""
        return bool(GEMINI_API_KEY)

    async def generate_image(
        self,
        prompt: str,
        use_case: str = "general",
        aspect_ratio: str = "1:1",
        refine_prompt: bool = True,
        agent_name: Optional[str] = None,
    ) -> ImageGenerationResult:
        """Generate an image from a text prompt.

        Args:
            prompt: Raw text description of the desired image.
            use_case: One of "general", "thumbnail", "diagram", "social".
            aspect_ratio: One of "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3".
            refine_prompt: If True, refine the prompt via Gemini text model first.
            agent_name: Optional agent name for logging/tracking.

        Returns:
            ImageGenerationResult with image bytes or error.
        """
        if not self.available:
            return ImageGenerationResult(
                success=False,
                original_prompt=prompt,
                use_case=use_case,
                aspect_ratio=aspect_ratio,
                error="GEMINI_API_KEY not configured",
                error_kind="not_configured",
            )

        if use_case not in VALID_USE_CASES:
            return ImageGenerationResult(
                success=False,
                original_prompt=prompt,
                use_case=use_case,
                aspect_ratio=aspect_ratio,
                error=f"Invalid use_case: {use_case}. Must be one of: {VALID_USE_CASES}",
                error_kind="invalid_input",
            )

        if aspect_ratio not in VALID_ASPECT_RATIOS:
            return ImageGenerationResult(
                success=False,
                original_prompt=prompt,
                use_case=use_case,
                aspect_ratio=aspect_ratio,
                error=f"Invalid aspect_ratio: {aspect_ratio}. Must be one of: {VALID_ASPECT_RATIOS}",
                error_kind="invalid_input",
            )

        log_prefix = f"[IMG {agent_name or 'platform'}]"

        # Step 1: Prompt refinement (optional)
        refined = prompt
        if refine_prompt:
            try:
                refined = await self.refine_prompt(prompt, use_case, aspect_ratio)
                logger.info(f"{log_prefix} Refined prompt: {refined[:100]}...")
            except Exception as e:
                logger.warning(f"{log_prefix} Prompt refinement failed, using raw prompt: {e}")
                refined = prompt

        # Step 2: Image generation
        try:
            image_bytes, mime_type = await self._call_gemini_image(refined, aspect_ratio)
            logger.info(
                f"{log_prefix} Generated image: {len(image_bytes)} bytes, "
                f"use_case={use_case}, aspect_ratio={aspect_ratio}"
            )
            return ImageGenerationResult(
                success=True,
                image_data=image_bytes,
                mime_type=mime_type,
                refined_prompt=refined,
                original_prompt=prompt,
                model_used=GEMINI_IMAGE_MODEL,
                use_case=use_case,
                aspect_ratio=aspect_ratio,
            )
        except Exception as e:
            kind = _classify_exception(e)
            logger.error(
                "image_generation_failed",
                extra={
                    "agent_name": agent_name or "platform",
                    "use_case": use_case,
                    "aspect_ratio": aspect_ratio,
                    "error_kind": kind,
                    "exception_type": type(e).__name__,
                    "error_message": str(e)[:500],
                    "prompt_length": len(prompt),
                },
            )
            return ImageGenerationResult(
                success=False,
                refined_prompt=refined if refined != prompt else None,
                original_prompt=prompt,
                use_case=use_case,
                aspect_ratio=aspect_ratio,
                error=str(e),
                error_kind=kind,
            )

    async def refine_prompt(
        self,
        raw_prompt: str,
        use_case: str,
        aspect_ratio: str,
    ) -> str:
        """Refine a raw prompt using Gemini text model with best practices.

        Args:
            raw_prompt: The user's original description.
            use_case: The target use case for best practices selection.
            aspect_ratio: Target aspect ratio (for context in refinement).

        Returns:
            Refined prompt string optimized for image generation.
        """
        system_prompt = USE_CASE_PROMPTS.get(use_case, USE_CASE_PROMPTS["general"])
        user_message = (
            f"Aspect ratio: {aspect_ratio}\n\n"
            f"Raw description to refine:\n{raw_prompt}"
        )

        refined = await self._call_gemini_text(system_prompt, user_message)
        return refined.strip()

    async def _call_gemini_text(self, system_prompt: str, user_message: str) -> str:
        """Call Gemini text model for prompt refinement.

        Args:
            system_prompt: System instruction with best practices.
            user_message: The user's message to refine.

        Returns:
            The model's text response.

        Raises:
            RuntimeError: If the API call fails.
        """
        url = f"{GEMINI_API_BASE}/{GEMINI_TEXT_MODEL}:generateContent"

        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_message}],
                }
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 512,
            },
        }

        response = await self._http.post(
            url,
            json=payload,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=PROMPT_REFINEMENT_TIMEOUT,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini text API error {response.status_code}: {response.text[:500]}"
            )

        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini text response structure: {e}")

    async def _call_gemini_image(
        self,
        prompt: str,
        aspect_ratio: str,
        reference_image: Optional[bytes] = None,
        reference_mime_type: str = "image/png",
    ) -> tuple[bytes, str]:
        """Call Gemini image model to generate an image.

        Args:
            prompt: The refined prompt for image generation.
            aspect_ratio: Target aspect ratio.
            reference_image: Optional reference image bytes for style/likeness guidance.
            reference_mime_type: MIME type of the reference image.

        Returns:
            Tuple of (image_bytes, mime_type).

        Raises:
            RuntimeError: If the API call fails or returns no image.
        """
        url = f"{GEMINI_API_BASE}/{GEMINI_IMAGE_MODEL}:generateContent"

        parts = []
        if reference_image:
            parts.append({
                "inlineData": {
                    "mimeType": reference_mime_type,
                    "data": base64.b64encode(reference_image).decode("utf-8"),
                }
            })
        parts.append({"text": prompt})

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "responseModalities": ["image", "text"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                },
            },
        }

        response = await self._http.post(
            url,
            json=payload,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=IMAGE_GENERATION_TIMEOUT,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini image API error {response.status_code}: {response.text[:500]}"
            )

        data = response.json()

        # Find the image part in the response
        try:
            candidates = data["candidates"]
            for candidate in candidates:
                parts = candidate.get("content", {}).get("parts", [])
                for part in parts:
                    if "inlineData" in part:
                        inline = part["inlineData"]
                        image_b64 = inline["data"]
                        mime_type = inline.get("mimeType", "image/png")
                        return base64.b64decode(image_b64), mime_type
        except (KeyError, IndexError):
            pass

        raise RuntimeError(
            "Gemini image API returned no image data. "
            "The prompt may have been blocked by safety filters."
        )

    async def generate_variation(
        self,
        prompt: str,
        reference_image: bytes,
        reference_mime_type: str = "image/png",
        aspect_ratio: str = "1:1",
        agent_name: Optional[str] = None,
    ) -> ImageGenerationResult:
        """Generate an image variation using a reference image.

        Args:
            prompt: Text prompt describing the desired image (already refined).
            reference_image: Reference image bytes for style/likeness guidance.
            reference_mime_type: MIME type of the reference image.
            aspect_ratio: Target aspect ratio.
            agent_name: Optional agent name for logging.

        Returns:
            ImageGenerationResult with image bytes or error.
        """
        if not self.available:
            return ImageGenerationResult(
                success=False,
                original_prompt=prompt,
                aspect_ratio=aspect_ratio,
                error="GEMINI_API_KEY not configured",
                error_kind="not_configured",
            )

        log_prefix = f"[IMG {agent_name or 'platform'}]"

        variation_prompt = (
            f"Generate a new variation of this portrait. Keep the same subject identity, "
            f"features, and overall style but create a fresh natural variation — slightly "
            f"different expression, micro-changes in lighting angle, or subtle pose shift. "
            f"The result should look like a different photo from the same session.\n\n"
            f"Original prompt: {prompt}"
        )

        try:
            image_bytes, mime_type = await self._call_gemini_image(
                variation_prompt,
                aspect_ratio,
                reference_image=reference_image,
                reference_mime_type=reference_mime_type,
            )
            logger.info(
                f"{log_prefix} Generated variation: {len(image_bytes)} bytes, "
                f"aspect_ratio={aspect_ratio}"
            )
            return ImageGenerationResult(
                success=True,
                image_data=image_bytes,
                mime_type=mime_type,
                refined_prompt=variation_prompt,
                original_prompt=prompt,
                model_used=GEMINI_IMAGE_MODEL,
                use_case="avatar",
                aspect_ratio=aspect_ratio,
            )
        except Exception as e:
            kind = _classify_exception(e)
            logger.error(
                "image_variation_failed",
                extra={
                    "agent_name": agent_name or "platform",
                    "aspect_ratio": aspect_ratio,
                    "error_kind": kind,
                    "exception_type": type(e).__name__,
                    "error_message": str(e)[:500],
                },
            )
            return ImageGenerationResult(
                success=False,
                original_prompt=prompt,
                use_case="avatar",
                aspect_ratio=aspect_ratio,
                error=str(e),
                error_kind=kind,
            )

    async def generate_emotion_variation(
        self,
        emotion_prompt: str,
        reference_image: bytes,
        reference_mime_type: str = "image/png",
        aspect_ratio: str = "1:1",
        agent_name: Optional[str] = None,
    ) -> ImageGenerationResult:
        """Generate an emotion variant of an avatar using a reference image.

        Unlike generate_variation(), this uses a caller-supplied emotion prompt
        instead of building a generic variation prompt.

        Args:
            emotion_prompt: Full prompt describing the desired emotion/expression.
            reference_image: Reference image bytes for identity preservation.
            reference_mime_type: MIME type of the reference image.
            aspect_ratio: Target aspect ratio.
            agent_name: Optional agent name for logging.

        Returns:
            ImageGenerationResult with image bytes or error.
        """
        if not self.available:
            return ImageGenerationResult(
                success=False,
                original_prompt=emotion_prompt,
                aspect_ratio=aspect_ratio,
                error="GEMINI_API_KEY not configured",
                error_kind="not_configured",
            )

        log_prefix = f"[IMG {agent_name or 'platform'}]"

        try:
            image_bytes, mime_type = await self._call_gemini_image(
                emotion_prompt,
                aspect_ratio,
                reference_image=reference_image,
                reference_mime_type=reference_mime_type,
            )
            logger.info(
                f"{log_prefix} Generated emotion variation: {len(image_bytes)} bytes"
            )
            return ImageGenerationResult(
                success=True,
                image_data=image_bytes,
                mime_type=mime_type,
                refined_prompt=emotion_prompt,
                original_prompt=emotion_prompt,
                model_used=GEMINI_IMAGE_MODEL,
                use_case="avatar",
                aspect_ratio=aspect_ratio,
            )
        except Exception as e:
            kind = _classify_exception(e)
            logger.error(
                "image_emotion_variation_failed",
                extra={
                    "agent_name": agent_name or "platform",
                    "aspect_ratio": aspect_ratio,
                    "error_kind": kind,
                    "exception_type": type(e).__name__,
                    "error_message": str(e)[:500],
                },
            )
            return ImageGenerationResult(
                success=False,
                original_prompt=emotion_prompt,
                use_case="avatar",
                aspect_ratio=aspect_ratio,
                error=str(e),
                error_kind=kind,
            )

    async def close(self):
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_image_generation_service: Optional[ImageGenerationService] = None


def get_image_generation_service() -> ImageGenerationService:
    """Get the global ImageGenerationService instance."""
    global _image_generation_service
    if _image_generation_service is None:
        _image_generation_service = ImageGenerationService()
    return _image_generation_service
