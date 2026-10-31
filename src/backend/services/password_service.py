"""Change a signed-in person's own password (#3825).

Only an account that already HAS a password can change it. Every account a
person creates by email sign-in (or SSO) has none, and this must never be a way
to add one: a session that could set a password on an email-only account would
mint a second, longer-lived sign-in method out of a stolen session.

A change also signs the account out everywhere else (the per-user session
cutoff in `dependencies.revoke_user_sessions`) and hands the caller a fresh
token, so the person who made the change stays signed in and nobody else does.

MCP API keys are not sessions and the cutoff does not reach them, so a key
minted from a session opened with a leaked password would outlive the change.
The person can revoke the keys they created in the same request (opt-in: those
keys often drive real integrations), and is told how many still work if not.
Only person-minted scopes count; an agent's own key, the system key and a
connector key are never touched here.

Wrong current passwords are capped per account in their OWN bucket, not the
login one: sharing it would let anyone holding a stolen session lock the real
owner out of signing in.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from jose import jwt

from config import ACCESS_TOKEN_EXPIRE_MINUTES
from database import db
from dependencies import (
    OPS_SCOPE, OPS_WRITE_SCOPE, PORTAL_DELEGATE_SCOPE, create_access_token, hash_password,
    revoke_user_sessions,
    verify_password,
)
from redis_breaker_util import get_breaker_redis
from utils.admin_identity import admin_username, is_usable_password_hash
from utils.password_validation import validate_password_strength

logger = logging.getLogger(__name__)

#: Wrong current passwords allowed per account per window.
CHANGE_ATTEMPT_LIMIT = 5
CHANGE_ATTEMPT_WINDOW_SECONDS = 15 * 60
_ATTEMPT_PREFIX = "pwchange:fail:"

#: Key scopes a person mints for themselves (`POST /api/mcp/keys`). The ones a
#: password change may revoke, and the ones it reports as still working.
#: Must equal `db/mcp_keys.py::_USER_CREATABLE_SCOPES` (pinned by
#: `test_3180_ops_write_scope.py`): a mintable scope missing here survives a
#: "revoke keys" password change after a suspected leak.
PERSON_KEY_SCOPES = frozenset({"user", OPS_SCOPE, OPS_WRITE_SCOPE, PORTAL_DELEGATE_SCOPE})


@dataclass
class PasswordChangeError(Exception):
    """A refusal the router maps to HTTP: `status`, a stable `code`, a sentence."""
    status: int
    code: str
    message: str
    errors: List[str] = field(default_factory=list)


@dataclass
class PasswordChangeResult:
    access_token: str
    other_sessions_signed_out: bool
    admin_env_stale: bool
    revoked_key_ids: List[str] = field(default_factory=list)
    active_key_count: int = 0


def _reserve_attempt(username: str) -> bool:
    """Spend one attempt BEFORE the password is checked; False when the cap is
    already spent. Redis INCR is atomic, so requests racing across workers
    cannot all read "under the cap" and each get a guess. `EXPIRE NX` starts the
    window on the attempt that created the key and leaves a running window
    alone; both run in one MULTI, so no key INCR creates is ever left without a
    TTL (a lockout that never resets). A counter with a TTL, not a lock — hence
    no `SET NX` (#1920)."""
    r = get_breaker_redis()
    if r is None:
        return True  # fail-open, like the login limiter
    key = f"{_ATTEMPT_PREFIX}{username}"
    try:
        pipe = r.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, CHANGE_ATTEMPT_WINDOW_SECONDS, nx=True)
        count, _ = pipe.execute()
        return int(count) <= CHANGE_ATTEMPT_LIMIT
    except Exception as exc:  # pragma: no cover — fail-open
        logger.warning(f"[auth] change-attempt limiter failed (fail-open): {exc}")
        return True


def _release_attempt(username: str) -> None:
    """Give back a reserved attempt: the current password was right."""
    r = get_breaker_redis()
    if r is None:
        return
    key = f"{_ATTEMPT_PREFIX}{username}"
    try:
        # Same MULTI shape as the reserve: if the window lapsed in between,
        # DECR re-creates the key, and it must not outlive a window either.
        pipe = r.pipeline(transaction=True)
        pipe.decr(key)
        pipe.expire(key, CHANGE_ATTEMPT_WINDOW_SECONDS, nx=True)
        pipe.execute()
    except Exception:  # pragma: no cover — best-effort
        pass


def _clear_attempts(username: str) -> None:
    r = get_breaker_redis()
    if r is None:
        return
    try:
        r.delete(f"{_ATTEMPT_PREFIX}{username}")
    except Exception:  # pragma: no cover — best-effort
        pass


def _settle_person_keys(username: str, revoke: bool):
    """Revoke (when asked) the person-minted keys still active; return
    ``(revoked_ids, still_active_count)``. Ids come from the caller's OWN list,
    so `revoke_mcp_api_key`'s admin ownership bypass is never what scopes it."""
    keys = [k for k in db.list_mcp_api_keys(username)
            if k.is_active and k.scope in PERSON_KEY_SCOPES and not k.agent_name]
    if not revoke:
        return [], len(keys)
    revoked = [k.id for k in keys if db.revoke_mcp_api_key(k.id, username)]
    return revoked, len(keys) - len(revoked)


def change_own_password(username: str, current: str, new: str, token_mode: Optional[str],
                        revoke_keys: bool = False) -> PasswordChangeResult:
    """Verify `current`, store `new`, sign every other session out.

    Raises `PasswordChangeError` for every refusal; nothing is written on one."""
    user = db.get_user_by_username(username)
    stored = user.get("password") if user else None
    if not is_usable_password_hash(stored):
        raise PasswordChangeError(
            409, "no_password",
            "This account signs in with an emailed code and has no password to change.")
    if not stored.startswith("$2"):
        raise PasswordChangeError(
            409, "password_not_changeable",
            "This account's password is stored in a legacy format. Restart the "
            "platform with ADMIN_PASSWORD set so it is migrated, then change it here.")

    if not _reserve_attempt(username):
        raise PasswordChangeError(
            429, "too_many_attempts",
            "Too many wrong current passwords. Try again in 15 minutes.")
    if not verify_password(current, stored):
        raise PasswordChangeError(400, "invalid_current_password", "The current password is wrong.")
    _release_attempt(username)

    errors = validate_password_strength(new)
    if errors:
        raise PasswordChangeError(422, "weak_password", "The new password does not meet the requirements.", errors)
    if verify_password(new, stored):
        raise PasswordChangeError(422, "password_unchanged", "The new password is the same as the current one.")

    db.update_user_password(username, hash_password(new))
    _clear_attempts(username)

    # The fresh token is minted BEFORE the cutoff and named in it, so it is the
    # one session that survives.
    fresh = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        mode=token_mode or "admin",
    )
    signed_out = revoke_user_sessions(username, keep_jti=jwt.get_unverified_claims(fresh).get("jti"))
    revoked, still_active = _settle_person_keys(username, revoke_keys)
    return PasswordChangeResult(
        access_token=fresh,
        other_sessions_signed_out=signed_out,
        # `.env` still holds the old value: the MCP server's legacy login and
        # scripts that read it will fail until it is updated too.
        admin_env_stale=username == admin_username() and os.getenv("ADMIN_PASSWORD", "") not in ("", new),
        revoked_key_ids=revoked,
        active_key_count=still_active,
    )
