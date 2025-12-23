"""
System settings routes for the Trinity backend.

Provides endpoints for managing system-wide configuration like the Trinity prompt.
Admin-only access for modification, read access for all authenticated users.
"""
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from models import User
from database import db, SystemSetting, SystemSettingUpdate
from dependencies import get_current_user
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================================
# Ops Settings Configuration
# ============================================================================

# Default values for ops settings (as specified in requirements)
OPS_SETTINGS_DEFAULTS = {
    "ops_context_warning_threshold": "75",  # Context % to trigger warning
    "ops_context_critical_threshold": "90",  # Context % to trigger reset/action
    "ops_idle_timeout_minutes": "30",  # Minutes before stuck detection
    "ops_cost_limit_daily_usd": "50.0",  # Daily cost limit (0 = unlimited)
    "ops_max_execution_minutes": "10",  # Max chat execution time
    "ops_alert_suppression_minutes": "15",  # Suppress duplicate alerts
    "ops_log_retention_days": "7",  # Days to keep container logs
    "ops_health_check_interval": "60",  # Seconds between health checks
}

# Descriptions for each ops setting
OPS_SETTINGS_DESCRIPTIONS = {
    "ops_context_warning_threshold": "Context usage percentage to trigger a warning (default: 75)",
    "ops_context_critical_threshold": "Context usage percentage to trigger critical alert or action (default: 90)",
    "ops_idle_timeout_minutes": "Minutes of inactivity before an agent is considered stuck (default: 30)",
    "ops_cost_limit_daily_usd": "Maximum daily cost limit in USD per agent (0 = unlimited) (default: 50.0)",
    "ops_max_execution_minutes": "Maximum allowed execution time for a single chat in minutes (default: 10)",
    "ops_alert_suppression_minutes": "Minutes to suppress duplicate alerts for same agent+type (default: 15)",
    "ops_log_retention_days": "Number of days to retain container logs (default: 7)",
    "ops_health_check_interval": "Seconds between automated health checks (default: 60)",
}


class OpsSettingsUpdate(BaseModel):
    """Request body for updating ops settings."""
    settings: Dict[str, str]


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


# ============================================================================
# Ops Settings Endpoints
# ============================================================================

@router.get("/ops/config")
async def get_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get all ops-related settings with their current values and defaults.

    Admin-only. Returns both stored values and defaults for ops settings.
    Useful for displaying the ops configuration panel.
    """
    require_admin(current_user)

    try:
        # Get current values from database
        all_settings = db.get_settings_dict()

        # Build response with defaults and current values
        ops_config = {}
        for key, default_value in OPS_SETTINGS_DEFAULTS.items():
            current_value = all_settings.get(key, default_value)
            ops_config[key] = {
                "value": current_value,
                "default": default_value,
                "description": OPS_SETTINGS_DESCRIPTIONS.get(key, ""),
                "is_default": current_value == default_value
            }

        await log_audit_event(
            event_type="system_settings",
            action="read_ops",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {
            "settings": ops_config
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get ops settings: {str(e)}")


@router.put("/ops/config")
async def update_ops_settings(
    body: OpsSettingsUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Update multiple ops settings at once.

    Admin-only. Only accepts valid ops setting keys.
    Invalid keys are ignored with a warning.
    """
    require_admin(current_user)

    try:
        updated = []
        ignored = []

        for key, value in body.settings.items():
            if key in OPS_SETTINGS_DEFAULTS:
                db.set_setting(key, value)
                updated.append(key)
            else:
                ignored.append(key)

        await log_audit_event(
            event_type="system_settings",
            action="update_ops",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"updated": updated, "ignored": ignored}
        )

        return {
            "success": True,
            "updated": updated,
            "ignored": ignored if ignored else None
        }
    except Exception as e:
        await log_audit_event(
            event_type="system_settings",
            action="update_ops",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to update ops settings: {str(e)}")


@router.post("/ops/reset")
async def reset_ops_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Reset all ops settings to their default values.

    Admin-only. Removes all ops settings from the database,
    causing them to fall back to defaults.
    """
    require_admin(current_user)

    try:
        deleted = []
        for key in OPS_SETTINGS_DEFAULTS.keys():
            if db.delete_setting(key):
                deleted.append(key)

        await log_audit_event(
            event_type="system_settings",
            action="reset_ops",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"reset": deleted}
        )

        return {
            "success": True,
            "message": "Ops settings reset to defaults",
            "reset": deleted
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset ops settings: {str(e)}")


def get_ops_setting(key: str, as_type: type = str):
    """
    Helper function to get an ops setting value with proper type conversion.

    Used internally by the ops module to retrieve settings.
    Returns the default if not set.
    """
    default = OPS_SETTINGS_DEFAULTS.get(key, "")
    value = db.get_setting_value(key, default)

    if as_type == int:
        return int(value)
    elif as_type == float:
        return float(value)
    elif as_type == bool:
        return value.lower() in ("true", "1", "yes")
    return value


# ============================================================================
# API Keys Management Endpoints
# ============================================================================

import os
import httpx


class ApiKeyUpdate(BaseModel):
    """Request body for updating an API key."""
    api_key: str


class ApiKeyTest(BaseModel):
    """Request body for testing an API key."""
    api_key: str


def get_anthropic_api_key() -> str:
    """Get Anthropic API key from settings, fallback to env var."""
    key = db.get_setting_value('anthropic_api_key', None)
    if key:
        return key
    return os.getenv('ANTHROPIC_API_KEY', '')


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing only last 4 characters."""
    if not key or len(key) < 8:
        return "****"
    return f"...{key[-4:]}"


@router.get("/api-keys")
async def get_api_keys_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get status of configured API keys.

    Admin-only. Returns masked key info for security.
    """
    require_admin(current_user)

    try:
        # Get Anthropic key
        anthropic_key = get_anthropic_api_key()
        anthropic_configured = bool(anthropic_key)

        # Check if it's from settings or env
        key_from_settings = bool(db.get_setting_value('anthropic_api_key', None))

        await log_audit_event(
            event_type="system_settings",
            action="read_api_keys",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success"
        )

        return {
            "anthropic": {
                "configured": anthropic_configured,
                "masked": mask_api_key(anthropic_key) if anthropic_configured else None,
                "source": "settings" if key_from_settings else ("env" if anthropic_configured else None)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get API keys status: {str(e)}")


@router.put("/api-keys/anthropic")
async def update_anthropic_key(
    body: ApiKeyUpdate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Set or update the Anthropic API key.

    Admin-only. Key is stored in system settings.
    """
    require_admin(current_user)

    try:
        # Validate format
        key = body.api_key.strip()
        if not key.startswith('sk-ant-'):
            raise HTTPException(
                status_code=400,
                detail="Invalid API key format. Anthropic keys start with 'sk-ant-'"
            )

        # Store in settings
        db.set_setting('anthropic_api_key', key)

        await log_audit_event(
            event_type="system_settings",
            action="update_anthropic_key",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"key_masked": mask_api_key(key)}
        )

        return {
            "success": True,
            "masked": mask_api_key(key)
        }
    except HTTPException:
        raise
    except Exception as e:
        await log_audit_event(
            event_type="system_settings",
            action="update_anthropic_key",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="failed",
            severity="error",
            details={"error": str(e)}
        )
        raise HTTPException(status_code=500, detail=f"Failed to update API key: {str(e)}")


@router.delete("/api-keys/anthropic")
async def delete_anthropic_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Delete the Anthropic API key from settings.

    Admin-only. Will fall back to env var if configured.
    """
    require_admin(current_user)

    try:
        deleted = db.delete_setting('anthropic_api_key')

        await log_audit_event(
            event_type="system_settings",
            action="delete_anthropic_key",
            user_id=current_user.username,
            ip_address=request.client.host if request.client else None,
            result="success",
            details={"deleted": deleted}
        )

        # Check if env var fallback exists
        env_key = os.getenv('ANTHROPIC_API_KEY', '')

        return {
            "success": True,
            "deleted": deleted,
            "fallback_configured": bool(env_key)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete API key: {str(e)}")


@router.post("/api-keys/anthropic/test")
async def test_anthropic_key(
    body: ApiKeyTest,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Test if an Anthropic API key is valid.

    Admin-only. Makes a lightweight API call to validate the key.
    """
    require_admin(current_user)

    try:
        key = body.api_key.strip()

        # Validate format first
        if not key.startswith('sk-ant-'):
            return {
                "valid": False,
                "error": "Invalid format. Anthropic keys start with 'sk-ant-'"
            }

        # Make a lightweight API call to test the key
        # Using the models endpoint which is simple and doesn't create any resources
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01"
                },
                timeout=10.0
            )

            if response.status_code == 200:
                await log_audit_event(
                    event_type="system_settings",
                    action="test_anthropic_key",
                    user_id=current_user.username,
                    ip_address=request.client.host if request.client else None,
                    result="success",
                    details={"valid": True}
                )
                return {"valid": True}
            elif response.status_code == 401:
                return {
                    "valid": False,
                    "error": "Invalid API key"
                }
            else:
                return {
                    "valid": False,
                    "error": f"API returned status {response.status_code}"
                }

    except httpx.TimeoutException:
        return {
            "valid": False,
            "error": "Request timed out. Please try again."
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Error testing key: {str(e)}"
        }
