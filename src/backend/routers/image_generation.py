"""
Image generation router (IMG-001).

REST endpoints for the platform image generation service.
"""

import base64
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from dependencies import get_current_user
from models import ImageGenerateRequest, User
from config import GEMINI_TEXT_MODEL
from services.image_generation_service import (
    get_image_generation_service,
    GEMINI_IMAGE_MODEL,
)
from services.image_generation_prompts import VALID_ASPECT_RATIOS, VALID_USE_CASES

router = APIRouter(prefix="/api/images", tags=["images"])
logger = logging.getLogger(__name__)


@router.post("/generate")
async def generate_image(
    request: ImageGenerateRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate an image from a text prompt.

    Uses a two-step pipeline:
    1. Prompt refinement via Gemini text model (optional)
    2. Image generation via Gemini image model

    Returns base64-encoded image data and the refined prompt used.
    """
    service = get_image_generation_service()

    if not service.available:
        return JSONResponse(
            status_code=501,
            content={"detail": "Image generation not available: GEMINI_API_KEY not configured"},
        )

    result = await service.generate_image(
        prompt=request.prompt,
        use_case=request.use_case or "general",
        aspect_ratio=request.aspect_ratio or "1:1",
        refine_prompt=request.refine_prompt if request.refine_prompt is not None else True,
        agent_name=f"user:{current_user.username}",
    )

    if not result.success:
        return JSONResponse(
            status_code=422,
            content={
                "detail": result.error,
                "refined_prompt": result.refined_prompt,
                "original_prompt": result.original_prompt,
            },
        )

    return {
        "image_base64": base64.b64encode(result.image_data).decode("ascii"),
        "mime_type": result.mime_type,
        "refined_prompt": result.refined_prompt,
        "original_prompt": result.original_prompt,
        "model_used": result.model_used,
        "use_case": result.use_case,
        "aspect_ratio": result.aspect_ratio,
    }


@router.get("/models")
async def get_models(
    current_user: User = Depends(get_current_user),
):
    """Get available image generation models and configuration options."""
    service = get_image_generation_service()

    return {
        "available": service.available,
        "models": {
            "text_refinement": GEMINI_TEXT_MODEL,
            "image_generation": GEMINI_IMAGE_MODEL,
        },
        "default_model": GEMINI_IMAGE_MODEL,
        "use_cases": VALID_USE_CASES,
        "aspect_ratios": VALID_ASPECT_RATIOS,
    }
