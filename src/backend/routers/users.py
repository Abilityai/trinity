"""
User management routes for the Trinity backend.

Admin-only endpoints for listing users and managing their roles.
"""
import re

from fastapi import APIRouter, Depends, HTTPException

from models import User, UserRoleUpdate, UpdateMyEmailRequest
from database import db
from dependencies import require_admin, get_current_user

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"admin", "creator", "operator", "user"}

# Permissive email-shape check (mirrors routers/setup.py): one @, a dot in the
# domain, no spaces. Identity binding only — no verification mail is sent.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


@router.put("/me/email")
async def update_my_email(
    body: UpdateMyEmailRequest,
    current_user: User = Depends(get_current_user),
):
    """Bind a sign-in email to the current account (#82 Phase 1 transition).

    The migration path for an existing admin created before #82 — whose stored
    email is still the placeholder 'admin' — to register a real email and then
    sign in with email + password, exactly like a fresh install captures at
    first run. No verification mail is sent; binding the identity is independent
    of whether an email provider is configured.
    """
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Don't let one account claim another account's sign-in identity.
    existing = db.get_user_by_email(email)
    if existing and existing.get("username") != current_user.username:
        raise HTTPException(
            status_code=409,
            detail="That email is already associated with another account",
        )

    updated = db.update_user(current_user.username, {"email": email})
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "email": email}


@router.get("")
async def list_users(current_user: User = Depends(require_admin)):
    """
    List all users with their roles.

    Admin-only endpoint.
    """
    users = db.list_users()
    # Strip password hashes from response
    return [
        {
            "id": u["id"],
            "username": u["username"],
            "email": u.get("email"),
            "role": u["role"],
            "name": u.get("name"),
            "picture": u.get("picture"),
            "created_at": u.get("created_at"),
            "last_login": u.get("last_login"),
            "suspended_at": u.get("suspended_at"),  # #995 — NULL = active
        }
        for u in users
    ]


@router.put("/{username}/role")
async def update_user_role(
    username: str,
    body: UserRoleUpdate,
    current_user: User = Depends(require_admin),
):
    """
    Change a user's role.

    Admin-only endpoint. Cannot demote yourself.
    """
    if username == current_user.username:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    if body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}"
        )

    try:
        updated = db.update_user_role(username, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    return {
        "username": updated["username"],
        "role": updated["role"],
    }
