"""
User management routes for the Trinity backend.

Admin-only endpoints for listing users and managing their roles.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models import User
from database import db
from dependencies import require_admin

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"admin", "creator", "operator", "user"}


class UserRoleUpdate(BaseModel):
    role: str


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
