"""
First-time setup routes for the Trinity backend.

Provides endpoints for initial admin password setup on first launch.
These endpoints require NO authentication and only work before setup is completed.
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from database import db
from dependencies import hash_password
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/setup", tags=["setup"])


class SetAdminPasswordRequest(BaseModel):
    """Request body for setting admin password."""
    password: str
    confirm_password: str


@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup is complete. No auth required.

    Returns:
        - setup_completed: Whether the admin password has been set
        - dev_mode_hint: Hint for dev mode (if applicable)
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    return {
        "setup_completed": setup_completed
    }


@router.post("/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest, request: Request):
    """
    Set admin password on first launch. No auth required, only works once.

    This endpoint is only available before setup is completed.
    Once setup_completed=true is set, this endpoint returns 403.

    Requirements:
    - Password must be at least 8 characters
    - Password and confirm_password must match

    Returns:
        - success: true if password was set
    """
    # Check setup not already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        await log_audit_event(
            event_type="setup",
            action="admin_password",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            result="blocked",
            severity="warning",
            details={"reason": "Setup already completed"}
        )
        raise HTTPException(
            status_code=403,
            detail="Setup already completed. Password cannot be changed through this endpoint."
        )

    # Validate password
    if len(data.password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters"
        )

    if data.password != data.confirm_password:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Hash the password and update admin user
    hashed_password = hash_password(data.password)

    # Update admin user's password in database
    db.update_user_password('admin', hashed_password)

    # Mark setup as completed
    db.set_setting('setup_completed', 'true')

    await log_audit_event(
        event_type="setup",
        action="admin_password",
        user_id="admin",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        result="success",
        details={"message": "Admin password set via first-time setup"}
    )

    return {"success": True}
