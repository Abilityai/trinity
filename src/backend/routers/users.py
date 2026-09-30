# mcp: none — user management is an admin grant surface (ROLE-001), human-only
"""
User management routes for the Trinity backend.

Admin-only endpoints for listing users and managing their roles.
"""
import re

from fastapi import APIRouter, Depends, HTTPException

from fastapi import Request, status
from fastapi.responses import JSONResponse

from models import (
    User,
    UserRoleUpdate,
    UpdateMyEmailRequest,
    RequestEmailBindCodeRequest,
    GitHubPATRequest,
    UserPreferenceWrite,
    UserPreferenceRecord,
    UserPreferencesResponse,
)
from database import db
from db.users import EmailInUseError
from services.platform_audit_service import AuditEventType, platform_audit_service
from dependencies import (
    require_admin,
    get_current_user,
    reject_non_interactive_principal,
    require_interactive,
)
from services import user_preferences_service
from services.user_preferences_service import PreferenceConflict, PreferenceError

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"admin", "creator", "operator", "user"}

# Permissive email-shape check (mirrors routers/setup.py): one @, a dot in the
# domain, no spaces.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")

# trinity-enterprise#720 — binding a sign-in email requires proving the mailbox.
# Every sign-in path resolves the account by email ALONE, so whoever holds the
# address on a `users` row holds that identity: what is shared with it, its
# Workspace threads, and the real person's next sign-in. The 409 alone only saw
# `users` rows, so any signed-in human could claim an address nobody had signed
# in with yet — a sharee who never signed up, a Workspace-only client.
_BIND_CODE_MINUTES = 10
_BIND_CODE_MAX_PER_WINDOW = 3   # the sign-in code limit (routers/auth.py)


def _bind_purpose(user_id) -> str:
    """A bind code completes THIS account's bind only, and is never a sign-in code."""
    return f"email_bind:{user_id}"


def _bind_attempt_scope(user_id, email: str) -> str:
    """The OTP failure-counter key for one account binding one address.

    Scoped to the bind (`otp_attempts:bind:{user_id}:{email}`), never the bare
    address: wrong bind guesses must not lock the address's real owner out of
    email sign-in (`otp_attempts:{email}`) — the cross-surface lockout the
    portal's `portal:` prefix avoids too (ent#311)."""
    return f"bind:{user_id}:{email}"


def _email_can_be_delivered() -> bool:
    """False when the provider is `console`: the code only reaches the server log."""
    from services.settings_service import settings_service
    return settings_service.get_email_provider() != "console"


def _refuse(status_code: int, code: str, message: str):
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _valid_new_email(raw: str, current_user: User) -> str:
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email):
        _refuse(400, "invalid_email", "Invalid email address")
    existing = db.get_user_by_email(email)
    if existing and existing.get("username") != current_user.username:
        _refuse(409, "email_in_use", "That email is already associated with another account")
    return email


@router.post("/me/email/code")
async def request_email_bind_code(
    body: RequestEmailBindCodeRequest,
    request: Request,
    current_user: User = Depends(require_interactive),
):
    """Send a 6-digit code to the address the caller wants to bind (ent#720).

    Interactive sessions only: an identity change is a person's act. The code is
    tied to the caller's account (`purpose`), so it completes only their bind and
    never signs anyone in.
    """
    email = _valid_new_email(body.email, current_user)
    if not _email_can_be_delivered() and current_user.role != "admin":
        _refuse(409, "email_verification_unavailable",
                "Email verification isn't available on this instance yet. Ask an admin to configure email.")
    # The allowance belongs to the CALLER, across every address: counting the
    # address instead would let one account spend another's bind allowance,
    # and bind rows counted by sign-in would suppress the owner's sign-in codes.
    purpose = _bind_purpose(current_user.id)
    if db.count_recent_codes_for_purpose(purpose, minutes=_BIND_CODE_MINUTES) >= _BIND_CODE_MAX_PER_WINDOW:
        _refuse(429, "too_many_codes", "Too many codes requested. Try again in a few minutes.")

    code = db.create_login_code(email, expiry_minutes=_BIND_CODE_MINUTES, purpose=purpose)
    from services.email_service import EmailService
    sent = await EmailService().send_verification_code(
        email, code["code"], context_label="Trinity sign-in email confirmation")
    if not sent:
        _refuse(502, "email_send_failed", "We couldn't send the code. Try again in a moment.")
    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION, event_action="email_bind_code_sent",
        source="api", actor_user=current_user, target_type="user", target_id=current_user.username,
        endpoint=request.scope["path"], request_id=getattr(request.state, "request_id", None),
    )
    return {"sent": True, "expires_in_seconds": code["expires_in_seconds"]}


@router.put("/me/email")
async def update_my_email(
    body: UpdateMyEmailRequest,
    request: Request,
    current_user: User = Depends(require_interactive),
):
    """Bind a sign-in email to the current account (#82 FR-3, ent#720 FR-4).

    Requires the code `POST /me/email/code` sent to the new address. The one
    exception is the #82 transition on an install that cannot deliver mail
    (provider `console`): an interactive ADMIN may bind without a code, and the
    bind is audited as unverified.

    Signed-in session only (trinity-enterprise#711): email sign-in resolves the
    account by this column, so binding it is a sign-in identity change and no
    MCP key may make it.
    """
    email = _valid_new_email(body.email, current_user)
    code = (body.code or "").strip()
    verified = False
    if code:
        # Cap wrong guesses exactly as email sign-in does (OTP_MAX_ATTEMPTS=5 in
        # 10 minutes, pentest 3.1.5): past the cap even the right code is
        # refused, so a caller cannot mint 3 live codes and guess without limit.
        from routers import auth as auth_limits
        scope = _bind_attempt_scope(current_user.id, email)
        try:
            auth_limits.check_otp_rate_limit(scope)
        except HTTPException as e:
            if e.status_code != 429:
                raise
            _refuse(429, "too_many_attempts",
                    "Too many wrong codes for that address. Try again in a few minutes.")
        verified = bool(db.verify_login_code(email, code, purpose=_bind_purpose(current_user.id)))
        auth_limits.record_otp_attempt(scope, success=verified)
        if not verified:
            _refuse(400, "invalid_code", "That code is wrong or has expired. Request a new one.")
    elif _email_can_be_delivered():
        _refuse(400, "code_required", "Enter the code we sent to that address.")
    elif current_user.role != "admin":
        _refuse(409, "email_verification_unavailable",
                "Email verification isn't available on this instance yet. Ask an admin to configure email.")

    try:
        updated = db.update_user(current_user.username, {"email": email})
    except EmailInUseError:
        _refuse(409, "email_in_use", "That email is already associated with another account")
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")

    await platform_audit_service.log(
        event_type=AuditEventType.AUTHENTICATION,
        event_action="email_bound" if verified else "email_bind_unverified",
        source="api", actor_user=current_user, target_type="user", target_id=current_user.username,
        endpoint=request.scope["path"], request_id=getattr(request.state, "request_id", None),
        details={"verified": verified},
    )
    return {"success": True, "email": email, "verified": verified}


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
    current_user: User = Depends(require_interactive),
):
    """Store the caller's personal GitHub PAT (validated + encrypted at rest).

    Signed-in session only: future agent creations inherit this credential.

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
async def clear_my_github_pat(current_user: User = Depends(require_interactive)):
    """Clear the caller's personal GitHub PAT — reverts them to the global PAT.

    Signed-in session only, like setting it.

    Agents already created under it keep their own persisted per-agent copy
    (#347) and are unaffected; only future creations fall back to the platform
    token (ent#162 AC #10).
    """
    db.clear_user_github_pat(current_user.id)
    return {"configured": False, "message": "Personal GitHub token cleared."}


# ---------------------------------------------------------------------------
# Per-user UI preferences (trinity-enterprise#413, OSS-core) — the caller's OWN
# record, always. The Dashboard Grid's layout / tile prefs / org toggles are
# the first keys; the allowlist lives in services/user_preferences_service.py.
#
# Gated `reject_non_interactive_principal` on all three routes, the GET
# included, which deviates from that helper's "never for read surfaces an MCP
# key legitimately drives" note on purpose: a dashboard arrangement has no
# machine consumer. An agent-scoped key resolves to its OWNER on REST, so
# without the gate any agent could read and rewrite the operator's board.
# ---------------------------------------------------------------------------


@router.get("/me/preferences", response_model=UserPreferencesResponse)
async def get_my_preferences(current_user: User = Depends(get_current_user)):
    """Every stored UI preference of the caller, in one round trip."""
    reject_non_interactive_principal(current_user)
    return {"preferences": user_preferences_service.get_all(current_user.id)}


@router.put("/me/preferences/{key}", response_model=UserPreferenceRecord)
async def put_my_preference(
    key: str,
    body: UserPreferenceWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Conditionally store one preference (insert-only or compare-and-set).

    409 carries the live record in `detail.current` (or `null` when the row
    is gone) so the client can adopt or retry without another GET.
    """
    reject_non_interactive_principal(current_user)
    # Cheap header check before the parsed payload is re-serialized. A HINT,
    # not the enforcement — the exact byte check lives in the service (the
    # routers/canvas.py two-stage shape).
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > user_preferences_service.MAX_VALUE_BYTES * 2:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Preference value exceeds {user_preferences_service.MAX_VALUE_BYTES} bytes",
                )
        except ValueError:
            pass
    try:
        return user_preferences_service.put(
            current_user.id, key, body.value, body.base_updated_at
        )
    except PreferenceConflict as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": {"message": e.detail, "current": e.current}},
        )
    except PreferenceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.delete("/me/preferences/{key}")
async def delete_my_preference(key: str, current_user: User = Depends(get_current_user)):
    """Remove one of the caller's preferences (the Grid's "Reset")."""
    reject_non_interactive_principal(current_user)
    try:
        return {"deleted": user_preferences_service.delete(current_user.id, key)}
    except PreferenceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


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
