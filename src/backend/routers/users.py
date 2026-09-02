# mcp: none — user management is an admin grant surface (ROLE-001), human-only
"""
User management routes for the Trinity backend.

Admin-only endpoints for listing users and managing their roles.
"""
import re

from fastapi import APIRouter, Depends, HTTPException

from models import User, UserRoleUpdate, UpdateMyEmailRequest, GitHubPATRequest
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


# ---------------------------------------------------------------------------
# Per-user GitHub PAT (ent#162) — self-service, on the caller's OWN account.
#
# A user stores one GitHub token here; agent creation then resolves per-agent →
# this owner's per-user → global (services/settings_service.resolve_github_pat),
# so a non-admin is not confined to the admin PAT's repo scope. Any authenticated
# user may manage their own credential (NOT admin-gated) — it is theirs. The
# token is never echoed back on read (status only), mirroring the per-agent
# `GET /{agent}/github-pat` and the ElevenLabs key surface.
# ---------------------------------------------------------------------------


@router.get("/me/github-pat")
async def get_my_github_pat_status(current_user: User = Depends(get_current_user)):
    """Personal GitHub PAT status — configured flag only, never the token."""
    from services.settings_service import get_github_pat

    return {
        "configured": db.has_user_github_pat(current_user.id),
        # Lets the UI say "your agents fall back to the platform token" when the
        # user has none of their own but a global PAT exists.
        "has_global": bool(get_github_pat()),
    }


@router.put("/me/github-pat")
async def set_my_github_pat(
    body: GitHubPATRequest,
    current_user: User = Depends(get_current_user),
):
    """Store the caller's personal GitHub PAT (validated + encrypted at rest).

    Honest validation (ent#162): a token GitHub *rejects* is a 400; a token we
    simply could not verify because GitHub was unreachable is a 503 — we do not
    tell the user their token is bad when we never got an answer.
    """
    from services.github_service import GitHubService

    pat = (body.pat or "").strip()
    if not pat:
        raise HTTPException(status_code=400, detail="PAT cannot be empty")

    status, username = await GitHubService(pat).validate_token_detailed()
    if status == "invalid":
        raise HTTPException(
            status_code=400,
            detail="GitHub rejected this token. Check it hasn't expired and has repo scope.",
        )
    if status == "unreachable":
        raise HTTPException(
            status_code=503,
            detail="Couldn't reach GitHub to verify the token. Try again shortly.",
        )

    if not db.set_user_github_pat(current_user.id, pat):
        raise HTTPException(status_code=500, detail="Failed to save GitHub token")

    return {
        "configured": True,
        "github_username": username,
        "message": "Personal GitHub token saved. New agents you create from a repo will use it.",
    }


@router.delete("/me/github-pat")
async def clear_my_github_pat(current_user: User = Depends(get_current_user)):
    """Clear the caller's personal GitHub PAT — reverts them to the global PAT.

    Agents already created under it keep their own persisted per-agent copy
    (#347) and are unaffected; only future creations fall back to the platform
    token (ent#162 AC #10).
    """
    db.clear_user_github_pat(current_user.id)
    return {"configured": False, "message": "Personal GitHub token cleared."}


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
