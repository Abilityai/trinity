"""
System settings routes for the Trinity backend.

Provides endpoints for managing system-wide configuration like the Trinity prompt.
Admin-only access for modification, read access for all authenticated users.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request

from models import User
from database import db, SystemSetting, SystemSettingUpdate
from dependencies import get_current_user
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/settings", tags=["settings"])


def require_admin(current_user: User):
    """Verify user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("", response_model=List[SystemSetting])
async def get_all_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all system settings.

    Admin-only endpoint to view all configuration values.
    """
    require_admin(current_user)

    try:
        settings = db.get_all_settings()

        await log_audit_event(
            event_type="system_settings",
            action="list",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return settings
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {str(e)}")


@router.get("/{key}")
async def get_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific setting by key.

    Returns the setting value or 404 if not found.
    Admin-only for most settings.
    """
    require_admin(current_user)

    try:
        setting = db.get_setting(key)

        if not setting:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

        await log_audit_event(
            event_type="system_settings",
            action="read",
            user_id=current_user.username,
            resource=f"setting:{key}",
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return setting
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get setting: {str(e)}")


@router.put("/{key}", response_model=SystemSetting)
async def update_setting(
    key: str,
    body: SystemSettingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Create or update a system setting.

    Admin-only endpoint. Creates the setting if it doesn't exist.
    """
    require_admin(current_user)

    try:
        setting = db.set_setting(key, body.value)

        await log_audit_event(
            event_type="system_settings",
            action="update",
            user_id=current_user.username,
            resource=f"setting:{key}",
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"key": key, "value_length": len(body.value)}
        )

        return setting
    except Exception as e:
        await log_audit_event(
            event_type="system_settings",
            action="update",
            user_id=current_user.username,
            resource=f"setting:{key}",
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to update setting: {str(e)}")


@router.delete("/{key}")
async def delete_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a system setting.

    Admin-only endpoint. Returns success even if setting didn't exist.
    """
    require_admin(current_user)

    try:
        deleted = db.delete_setting(key)

        await log_audit_event(
            event_type="system_settings",
            action="delete",
            user_id=current_user.username,
            resource=f"setting:{key}",
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"deleted": deleted}
        )

        return {"success": True, "deleted": deleted}
    except Exception as e:
        await log_audit_event(
            event_type="system_settings",
            action="delete",
            user_id=current_user.username,
            resource=f"setting:{key}",
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to delete setting: {str(e)}")
