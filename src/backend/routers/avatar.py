"""
Avatar generation and serving router (AVATAR-001, AVATAR-002).

REST endpoints for AI-generated agent avatars and emotion variants.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from database import db
from dependencies import get_current_user
from models import User
from services.agent_auth import agent_httpx_client
from services.image_generation_prompts import AVATAR_EMOTIONS, AVATAR_EMOTION_PROMPTS
from services.image_generation_service import get_image_generation_service
from utils.image_optimize import optimize_avatar

router = APIRouter(prefix="/api/agents", tags=["avatars"])
logger = logging.getLogger(__name__)

AVATAR_DIR = Path("/data/avatars")


# #957: Map image-generation error_kind → (HTTP status, user-facing detail).
# Keeps the Gemini-internal error strings out of the dialog; gives the
# operator/owner an actionable message per failure mode.
_AVATAR_ERROR_HTTP = {
    "not_configured": (
        501,
        "Avatar generation isn't configured on this server. Ask an admin "
        "to set GEMINI_API_KEY.",
    ),
    "invalid_input": (400, "Avatar request was rejected by the image service."),
    "safety_filter": (
        422,
        "The prompt was blocked by the image service's safety filters. "
        "Try rephrasing and avoid descriptions of real people, sensitive "
        "content, or trademarked characters.",
    ),
    "rate_limited": (
        429,
        "Image generation is rate-limited right now. Wait a minute and retry.",
    ),
    "upstream_error": (
        502,
        "Image generation service returned an error. This is usually transient — please retry.",
    ),
    "timeout": (
        504,
        "Image generation timed out. The model can be slow under load — please retry.",
    ),
    "unknown": (
        422,
        "Avatar generation failed. Check server logs for details.",
    ),
}


def _avatar_http_for_result(result) -> tuple[int, str]:
    """Pick the HTTP status + detail for an unsuccessful ImageGenerationResult."""
    kind = getattr(result, "error_kind", None) or "unknown"
    return _AVATAR_ERROR_HTTP.get(kind, _AVATAR_ERROR_HTTP["unknown"])

# Diverse visual styles for default avatars — deterministically assigned from agent name hash
# so each agent gets a unique look even when they share the same Docker type.
_DEFAULT_AVATAR_STYLES = [
    "A sleek humanoid robot with a polished chrome and dark navy metallic face, glowing indigo eyes, minimal geometric features, clean modern design like a luxury android executive",
    "A stylized android with a matte black face panel covered in faintly glowing teal circuit traces, bright cyan eyes, angular geometric features like a futuristic coding machine",
    "A refined robot with a brushed silver metallic face, warm amber glowing eyes behind thin geometric spectacle frames, scholarly and dignified mechanical appearance",
    "A vibrant android with an iridescent holographic face surface that shifts between purple and pink, bright magenta eyes, expressive geometric features with artistic flair",
    "A precise robot with a dark gunmetal face featuring subtle grid lines, sharp green glowing eyes, clean angular features like a high-precision analytical instrument",
    "A weathered bronze steampunk automaton with intricate clockwork gears visible through a translucent faceplate, glowing amber eyes, Victorian-era mechanical elegance",
    "A crystalline android with a faceted translucent face like cut gemstone, refracting soft rainbow light, prismatic violet eyes, ethereal and otherworldly presence",
    "A military-spec robot with matte olive drab armor plating, glowing red tactical visor, angular aggressive features, rugged battlefield command unit aesthetic",
    "A sleek white porcelain android with delicate gold filigree tracery, calm pearl-white eyes with soft glow, serene and graceful ceramic-doll mechanical beauty",
    "A neon-edged cyberpunk android with jet black face and electric blue wireframe contour lines, piercing white eyes, sharp angular features from a dystopian future",
    "A nature-inspired bio-mechanical android with a face textured like dark polished wood with bioluminescent green veins, warm emerald eyes, organic and living machine fusion",
    "A retro-futuristic robot with a rounded brushed copper face, large circular glowing turquoise eyes, friendly bulbous features like a 1960s space-age vision of the future",
]


def _get_style_for_agent(agent_name: str) -> str:
    """Deterministically pick a visual style from the agent name."""
    h = hash(agent_name)
    return _DEFAULT_AVATAR_STYLES[h % len(_DEFAULT_AVATAR_STYLES)]


class AvatarGenerateRequest(BaseModel):
    identity_prompt: str


async def _get_prompt_from_template(agent_name: str) -> Optional[str]:
    """Fetch avatar_prompt or build from description via agent's template.yaml.

    Returns a meaningful identity prompt from the running agent's template,
    or None if the agent is unreachable or has no useful template data.
    """
    try:
        async with agent_httpx_client(agent_name, timeout=5.0) as client:
            resp = await client.get(f"http://agent-{agent_name}:8000/api/template/info")
            if resp.status_code == 200:
                data = resp.json()
                # Priority 1: explicit avatar_prompt from template
                if data.get("avatar_prompt"):
                    return data["avatar_prompt"]
                # Priority 2: build from description + display_name
                desc = data.get("description", "")
                display = data.get("display_name", "")
                if desc:
                    return f"{display or agent_name}: {desc}"
    except Exception:
        pass
    return None


# ---- AVATAR-003: Generate defaults (must come BEFORE /{agent_name} routes) ----

@router.post("/avatars/generate-defaults")
async def generate_default_avatars(
    current_user: User = Depends(get_current_user),
):
    """Generate default avatars for all agents that don't have a custom one.

    Admin-only. Uses the same Gemini pipeline as custom avatars with an
    auto-generated prompt derived from each agent's name and type.
    No emotion variants or reference images for defaults.
    """
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins can generate default avatars")

    service = get_image_generation_service()
    if not service.available:
        raise HTTPException(
            status_code=501,
            detail="Image generation not available: GEMINI_API_KEY not configured",
        )

    # Get agents needing default avatars from DB
    agents_needing = db.get_agents_without_custom_avatar()
    agent_names_needing = {a["agent_name"] for a in agents_needing}

    if not agent_names_needing:
        return {
            "generated": 0,
            "failed": 0,
            "skipped": 0,
            "agents": [],
            "errors": [],
            "message": "All agents already have custom avatars",
        }

    # Get agent types from Docker labels
    from services.docker_service import list_all_agents_fast

    all_agents = list_all_agents_fast()
    agent_type_map = {a.name: a.type for a in all_agents}

    generated = []
    errors = []
    skipped = 0

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    for agent_name in sorted(agent_names_needing):
        # Skip agents that don't exist in Docker
        if agent_name not in agent_type_map:
            skipped += 1
            continue

        # AVATAR-003: Smart prompt priority chain
        # 1. Check if DB already has a seeded prompt (from template on creation)
        existing = db.get_avatar_identity(agent_name)
        if existing and existing.get("identity_prompt"):
            identity_prompt = existing["identity_prompt"]
        else:
            # 2. Try to fetch from running agent's template.yaml
            template_prompt = await _get_prompt_from_template(agent_name)
            if template_prompt:
                identity_prompt = template_prompt
            else:
                # 3. Fallback — pick a unique style from the agent name
                style_desc = _get_style_for_agent(agent_name)
                identity_prompt = f"{style_desc} named {agent_name}"

        try:
            result = await service.generate_image(
                prompt=identity_prompt,
                use_case="avatar",
                aspect_ratio="1:1",
                refine_prompt=True,
                agent_name=agent_name,
            )

            if not result.success:
                errors.append({"agent": agent_name, "error": result.error})
                logger.warning(f"[AVATAR-003] Default avatar failed for {agent_name}: {result.error}")
                continue

            # Save optimized WebP (no reference image, no emotion variants for defaults)
            avatar_path = AVATAR_DIR / f"{agent_name}.webp"
            avatar_path.write_bytes(optimize_avatar(result.image_data))

            now = datetime.now(timezone.utc).isoformat()
            db.set_default_avatar(agent_name, identity_prompt, now)

            generated.append(agent_name)
            logger.info(
                f"[AVATAR-003] Generated default avatar for {agent_name}: "
                f"{len(result.image_data)} bytes"
            )

        except Exception as e:
            errors.append({"agent": agent_name, "error": str(e)})
            logger.warning(f"[AVATAR-003] Default avatar error for {agent_name}: {e}")

    return {
        "generated": len(generated),
        "failed": len(errors),
        "skipped": skipped,
        "agents": generated,
        "errors": errors,
        "message": f"Generated {len(generated)} default avatars"
        + (f", {len(errors)} failed" if errors else "")
        + (f", {skipped} skipped (not in Docker)" if skipped else ""),
    }


@router.get("/{agent_name}/avatar")
async def get_avatar(agent_name: str):
    """Serve cached avatar for an agent. No auth required — avatars are public assets."""
    webp_path = AVATAR_DIR / f"{agent_name}.webp"
    png_path = AVATAR_DIR / f"{agent_name}.png"

    if webp_path.exists():
        return FileResponse(
            webp_path,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if png_path.exists():
        return FileResponse(
            png_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(status_code=404, detail="No avatar found")


@router.get("/{agent_name}/avatar/reference")
async def get_avatar_reference(agent_name: str):
    """Serve reference avatar PNG for an agent. No auth required — avatars are public assets."""
    ref_path = AVATAR_DIR / f"{agent_name}_ref.png"
    if not ref_path.exists():
        raise HTTPException(status_code=404, detail="No reference image found")

    return FileResponse(
        ref_path,
        media_type="image/png",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{agent_name}/avatar/identity")
async def get_avatar_identity(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """Return avatar identity prompt and metadata."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied")

    identity = db.get_avatar_identity(agent_name)
    has_avatar = (AVATAR_DIR / f"{agent_name}.webp").exists() or (AVATAR_DIR / f"{agent_name}.png").exists()
    has_reference = (AVATAR_DIR / f"{agent_name}_ref.png").exists()

    return {
        "agent_name": agent_name,
        "identity_prompt": identity["identity_prompt"] if identity else None,
        "updated_at": identity["updated_at"] if identity else None,
        "has_avatar": has_avatar,
        "has_reference": has_reference,
    }


async def _generate_emotions_background(
    agent_name: str,
    reference_bytes: bytes,
    identity_prompt: str,
):
    """Generate 8 emotion variants in the background (fire-and-forget).

    Iterates sequentially to avoid API rate limits. Each failure is logged
    but does not stop the loop. If the reference file changes or disappears
    mid-loop the task aborts.
    """
    service = get_image_generation_service()
    ref_path = AVATAR_DIR / f"{agent_name}_ref.png"

    logger.info(f"[AVATAR-002] Starting background emotion generation for {agent_name}")

    for emotion in AVATAR_EMOTIONS:
        # Guard: abort if reference was deleted or replaced
        if not ref_path.exists() or ref_path.read_bytes() != reference_bytes:
            logger.info(
                f"[AVATAR-002] Reference changed for {agent_name}, aborting emotion generation"
            )
            return

        expression = AVATAR_EMOTION_PROMPTS[emotion]
        emotion_prompt = (
            f"Generate a portrait of this exact same subject with the following "
            f"facial expression: {expression}. Keep the same identity, features, "
            f"clothing, and style. Only change the expression/emotion. "
            f"Original character: {identity_prompt}"
        )

        try:
            result = await service.generate_emotion_variation(
                emotion_prompt=emotion_prompt,
                reference_image=reference_bytes,
                aspect_ratio="1:1",
                agent_name=agent_name,
            )
            if result.success and result.image_data:
                emotion_path = AVATAR_DIR / f"{agent_name}_emotion_{emotion}.webp"
                emotion_path.write_bytes(optimize_avatar(result.image_data))
                logger.info(
                    f"[AVATAR-002] Saved emotion '{emotion}' for {agent_name}: "
                    f"{len(result.image_data)} bytes"
                )
            else:
                logger.warning(
                    f"[AVATAR-002] Emotion '{emotion}' failed for {agent_name}: "
                    f"{result.error}"
                )
        except Exception as e:
            logger.warning(
                f"[AVATAR-002] Emotion '{emotion}' error for {agent_name}: {e}"
            )

    logger.info(f"[AVATAR-002] Finished emotion generation for {agent_name}")


@router.get("/{agent_name}/avatar/emotions")
async def get_avatar_emotions(agent_name: str):
    """Return which emotion variant PNGs exist on disk for an agent. No auth required."""
    available = []
    for emotion in AVATAR_EMOTIONS:
        if (AVATAR_DIR / f"{agent_name}_emotion_{emotion}.webp").exists():
            available.append(emotion)
        elif (AVATAR_DIR / f"{agent_name}_emotion_{emotion}.png").exists():
            available.append(emotion)
    return {"agent_name": agent_name, "emotions": available}


@router.get("/{agent_name}/avatar/emotion/{emotion}")
async def get_avatar_emotion(agent_name: str, emotion: str):
    """Serve an emotion variant PNG. No auth required."""
    if emotion not in AVATAR_EMOTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid emotion. Must be one of: {AVATAR_EMOTIONS}",
        )

    webp_path = AVATAR_DIR / f"{agent_name}_emotion_{emotion}.webp"
    png_path = AVATAR_DIR / f"{agent_name}_emotion_{emotion}.png"

    if webp_path.exists():
        return FileResponse(
            webp_path,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if png_path.exists():
        return FileResponse(
            png_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    raise HTTPException(status_code=404, detail="Emotion variant not found")


@router.post("/{agent_name}/avatar/generate")
async def generate_avatar(
    agent_name: str,
    request: AvatarGenerateRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate an avatar from an identity prompt using the image generation service."""
    # Only owner/admin can generate
    owner = db.get_agent_owner(agent_name)
    if not owner:
        raise HTTPException(status_code=404, detail="Agent not found")

    is_admin = current_user.role == "admin"
    is_owner = owner["owner_username"] == current_user.username
    if not (is_admin or is_owner):
        raise HTTPException(status_code=403, detail="Only the agent owner can generate avatars")

    identity_prompt = request.identity_prompt.strip()
    if not identity_prompt:
        raise HTTPException(status_code=400, detail="identity_prompt cannot be empty")

    if len(identity_prompt) > 500:
        raise HTTPException(status_code=400, detail="identity_prompt must be 500 characters or less")

    service = get_image_generation_service()
    if not service.available:
        status, detail = _AVATAR_ERROR_HTTP["not_configured"]
        raise HTTPException(status_code=status, detail=detail)

    result = await service.generate_image(
        prompt=identity_prompt,
        use_case="avatar",
        aspect_ratio="1:1",
        refine_prompt=True,
        agent_name=agent_name,
    )

    if not result.success:
        status, detail = _avatar_http_for_result(result)
        raise HTTPException(status_code=status, detail=detail)

    # Save optimized display avatar (.webp) and full-quality reference (.png)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    avatar_path = AVATAR_DIR / f"{agent_name}.webp"
    ref_path = AVATAR_DIR / f"{agent_name}_ref.png"
    avatar_path.write_bytes(optimize_avatar(result.image_data))
    ref_path.write_bytes(result.image_data)  # Full quality PNG for Gemini input

    # Remove legacy .png display avatar if present
    legacy_avatar = AVATAR_DIR / f"{agent_name}.png"
    if legacy_avatar.exists():
        legacy_avatar.unlink()

    # Delete any existing emotion files (AVATAR-002) — new reference invalidates them
    for emotion in AVATAR_EMOTIONS:
        for ext in (".webp", ".png"):
            ep = AVATAR_DIR / f"{agent_name}_emotion_{emotion}{ext}"
            if ep.exists():
                ep.unlink()

    # Kick off background emotion generation (AVATAR-002)
    asyncio.create_task(
        _generate_emotions_background(agent_name, result.image_data, identity_prompt)
    )

    # Update DB
    now = datetime.now(timezone.utc).isoformat()
    db.set_avatar_identity(agent_name, identity_prompt, now)

    logger.info(f"Generated avatar + reference for agent {agent_name}: {len(result.image_data)} bytes")

    return {
        "agent_name": agent_name,
        "identity_prompt": identity_prompt,
        "refined_prompt": result.refined_prompt,
        "updated_at": now,
    }


@router.post("/{agent_name}/avatar/regenerate")
async def regenerate_avatar(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """Regenerate avatar as a variation of the reference image."""
    owner = db.get_agent_owner(agent_name)
    if not owner:
        raise HTTPException(status_code=404, detail="Agent not found")

    is_admin = current_user.role == "admin"
    is_owner = owner["owner_username"] == current_user.username
    if not (is_admin or is_owner):
        raise HTTPException(status_code=403, detail="Only the agent owner can regenerate avatars")

    # Need a reference image and stored prompt
    ref_path = AVATAR_DIR / f"{agent_name}_ref.png"
    if not ref_path.exists():
        raise HTTPException(status_code=404, detail="No reference image found. Generate an avatar first.")

    identity = db.get_avatar_identity(agent_name)
    if not identity or not identity.get("identity_prompt"):
        raise HTTPException(status_code=404, detail="No identity prompt found. Generate an avatar first.")

    service = get_image_generation_service()
    if not service.available:
        status, detail = _AVATAR_ERROR_HTTP["not_configured"]
        raise HTTPException(status_code=status, detail=detail)

    reference_bytes = ref_path.read_bytes()
    result = await service.generate_variation(
        prompt=identity["identity_prompt"],
        reference_image=reference_bytes,
        aspect_ratio="1:1",
        agent_name=agent_name,
    )

    if not result.success:
        status, detail = _avatar_http_for_result(result)
        raise HTTPException(status_code=status, detail=detail)

    # Save as optimized display avatar only (reference stays the same)
    avatar_path = AVATAR_DIR / f"{agent_name}.webp"
    avatar_path.write_bytes(optimize_avatar(result.image_data))

    now = datetime.now(timezone.utc).isoformat()
    db.set_avatar_identity(agent_name, identity["identity_prompt"], now)

    logger.info(f"Regenerated avatar from reference for agent {agent_name}: {len(result.image_data)} bytes")

    return {
        "agent_name": agent_name,
        "identity_prompt": identity["identity_prompt"],
        "updated_at": now,
    }


@router.delete("/{agent_name}/avatar")
async def delete_avatar(
    agent_name: str,
    current_user: User = Depends(get_current_user),
):
    """Remove avatar file and clear DB fields."""
    owner = db.get_agent_owner(agent_name)
    if not owner:
        raise HTTPException(status_code=404, detail="Agent not found")

    is_admin = current_user.role == "admin"
    is_owner = owner["owner_username"] == current_user.username
    if not (is_admin or is_owner):
        raise HTTPException(status_code=403, detail="Only the agent owner can remove avatars")

    # Delete files (display + reference + emotion variants, both formats)
    for ext in (".webp", ".png"):
        p = AVATAR_DIR / f"{agent_name}{ext}"
        if p.exists():
            p.unlink()
    ref_path = AVATAR_DIR / f"{agent_name}_ref.png"
    if ref_path.exists():
        ref_path.unlink()
    for emotion in AVATAR_EMOTIONS:
        for ext in (".webp", ".png"):
            ep = AVATAR_DIR / f"{agent_name}_emotion_{emotion}{ext}"
            if ep.exists():
                ep.unlink()

    # Clear DB
    db.clear_avatar_identity(agent_name)

    logger.info(f"Deleted avatar for agent {agent_name}")

    return {"message": f"Avatar removed for {agent_name}"}
