"""Provider API keys beyond Anthropic/GitHub — Resend (email) and Gemini (#2715).

Carved out of `credentials.py` after the trinity-enterprise#580/#581/#582 port
pushed it past the 800-line critical threshold (#1028). Included on the package
router right after `credentials` and, like every sibling, BEFORE `generic` —
`/api-keys/resend` and `/api-keys/gemini` are specific paths the `/{key}`
catch-all would otherwise swallow (Invariant #4).
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from models import ApiKeyTest, ApiKeyUpdate, ResendKeyRequest, User
from database import db
from dependencies import get_current_user, assert_admin
from services.platform_audit_service import platform_audit_service, AuditEventType
from services import platform_keys_service
from services.settings_service import (
    clear_secret_setting,
    get_gemini_api_key,
    set_secret_setting,
    settings_service,
)

from . import credentials

logger = logging.getLogger(__name__)

router = APIRouter()


async def _audit_key_change(request: Request, current_user: User, setting: str, action: str, **extra) -> None:
    # Key value never logged — only which setting changed and how.
    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="settings_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"setting": setting, "action": action, **extra},
    )


def _validated_resend_body(body: ResendKeyRequest) -> tuple:
    """(key, from_address to check/keep, from_address to store or None); 400 on a bad field."""
    key = body.api_key.strip()
    new_from = (body.from_address or '').strip() or None  # blank = keep the one in force
    error = platform_keys_service.resend_key_error(key)
    if not error and new_from:
        error = platform_keys_service.from_address_error(new_from)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return key, new_from or settings_service.get_email_from_address(), new_from


@router.put("/api-keys/resend")
async def update_resend_key(
    body: ResendKeyRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Set the Resend key (+ optional sender address) that email-code sign-in sends through.

    Admin-only. Key stored AES-256-GCM encrypted (`resend_api_key_encrypted`);
    the sender is the plain `email_from_address` setting. A key saved here
    selects Resend as the provider, overriding `EMAIL_PROVIDER` (a fresh
    install's `.env` says `console`). Takes effect on the next send.
    """
    assert_admin(current_user)
    key, _from_in_force, new_from = _validated_resend_body(body)
    set_secret_setting('resend_api_key', key)
    if new_from:
        settings_service.set_email_from_address(new_from)
    await _audit_key_change(request, current_user, "resend_api_key", "update",
                            from_address_changed=bool(new_from))
    return {
        "success": True,
        "masked": credentials.mask_api_key(key),
        "from_address": settings_service.get_email_from_address(),
    }


@router.delete("/api-keys/resend")
async def delete_resend_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Remove the Resend key AND the stored sender — email reverts to the `.env` config."""
    assert_admin(current_user)
    deleted = clear_secret_setting('resend_api_key')
    from_deleted = settings_service.clear_email_from_address()
    if deleted or from_deleted:
        await _audit_key_change(request, current_user, "resend_api_key", "delete")
    return {
        "success": True,
        "deleted": deleted,
        "fallback_configured": bool(os.getenv('RESEND_API_KEY', '')),
    }


@router.post("/api-keys/resend/test")
async def test_resend_key(
    body: ResendKeyRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Check a Resend key AND that Resend will send from the sender address.

    Admin-only. Reads the account's domains; sends nothing. Format errors come
    back as `{valid: false, error}` like the other key tests, not a 400.
    """
    assert_admin(current_user)
    try:
        key, from_address, _new = _validated_resend_body(body)
    except HTTPException as e:
        return {"valid": False, "error": e.detail}
    return await platform_keys_service.check_resend_key(key, from_address)


@router.put("/api-keys/gemini")
async def update_gemini_key(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Set the platform Gemini key (voice features, generated agent avatars).

    Admin-only. Stored through the ent#435 `google_api_key` secret
    (`google_api_key_encrypted`) — the platform already treats a Google API
    key as its Gemini key. Resolved per call, so no restart.
    """
    assert_admin(current_user)
    key = body.api_key.strip()
    error = platform_keys_service.gemini_key_error(key)
    if error:
        raise HTTPException(status_code=400, detail=error)
    set_secret_setting('google_api_key', key)
    await _audit_key_change(request, current_user, "google_api_key", "update")
    return {"success": True, "masked": credentials.mask_api_key(key)}


@router.delete("/api-keys/gemini")
async def delete_gemini_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Remove the stored Gemini key — falls back to `GEMINI_API_KEY`/`GOOGLE_API_KEY` env."""
    assert_admin(current_user)
    deleted = clear_secret_setting('google_api_key')
    if deleted:
        await _audit_key_change(request, current_user, "google_api_key", "delete")
    return {
        "success": True,
        "deleted": deleted,
        "fallback_configured": bool(get_gemini_api_key()),
    }


@router.post("/api-keys/gemini/test")
async def test_gemini_key(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Check a Gemini key with a model-list call (no generation). Admin-only."""
    assert_admin(current_user)
    key = body.api_key.strip()
    error = platform_keys_service.gemini_key_error(key)
    if error:
        return {"valid": False, "error": error}
    return await platform_keys_service.check_gemini_key(key)
