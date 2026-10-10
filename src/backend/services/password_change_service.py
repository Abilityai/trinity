"""Change your own password (trinity-enterprise#709).

The decisions behind `PUT /api/users/me/password`, with no HTTP in them: the
router owns the gate, the login rate-limit counters and the audit row; this
module owns what counts as a valid change and how it is written.

Every refusal is a `PasswordChangeRefused` carrying a stable `code` the UI
branches on (the endpoint is authenticated, so unlike first-run setup it may
name the exact rule that failed).
"""
from __future__ import annotations

from typing import Optional

from database import db
from dependencies import hash_password, verify_password
from services import mfa_gate
from utils.admin_identity import (
    ADMIN_PASSWORD_SOURCE_KEY,
    ADMIN_PASSWORD_SOURCE_UI,
    admin_username,
)
from utils.password_validation import validate_password_strength


class PasswordChangeRefused(Exception):
    def __init__(self, code: str, message: str, errors: Optional[list] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = errors or []


def load_account(username: str) -> dict:
    """The caller's user row, or a refusal when it has no password to change
    (an email-code-only account)."""
    user = db.get_user_by_username(username)
    if not user or not user.get("password"):
        raise PasswordChangeRefused(
            "no_password",
            "This account signs in with an email code and has no password to change.",
        )
    return user


def current_password_matches(user: dict, current_password: str) -> bool:
    return verify_password(current_password or "", user["password"])


def second_factor_state(user: dict) -> dict:
    """`{"required", "enrolled"}` from the mfa_gate seam; refusal if the
    provider cannot answer (step-up fails closed)."""
    try:
        return mfa_gate.step_up_decision(user)
    except mfa_gate.MfaUnavailable:
        raise PasswordChangeRefused(
            "second_factor_unavailable",
            "Two-factor verification is unavailable right now, so the password can't be changed. Try again later.",
        )


def check_second_factor(user: dict, code: Optional[str]) -> bool:
    """Enforce step 2. Returns whether a second factor was checked.

    Raises a refusal when one is needed and missing, wrong, or unverifiable.
    A wrong code is reported as `second_factor_invalid` so the caller can count
    it against the login limiter.
    """
    state = second_factor_state(user)
    if not state["required"]:
        return False
    if not state["enrolled"]:
        raise PasswordChangeRefused(
            "second_factor_enrollment_required",
            "Your account requires two-factor authentication. Set it up in Settings → Security, then change your password.",
        )
    code = (code or "").strip()
    if not code:
        raise PasswordChangeRefused(
            "second_factor_required",
            "Enter the 6-digit code from your authenticator app.",
        )
    try:
        ok = mfa_gate.verify_code(user, code)
    except mfa_gate.MfaUnavailable:
        raise PasswordChangeRefused(
            "second_factor_unavailable",
            "Two-factor verification is unavailable right now, so the password can't be changed. Try again later.",
        )
    if not ok:
        raise PasswordChangeRefused(
            "second_factor_invalid",
            "That code is wrong or has expired. Enter the current code from your authenticator app.",
        )
    return True


def check_new_password(current_password: str, new_password: str, confirm_password: str) -> None:
    """Step 3: confirm matches, first-run rules pass, and it is actually new."""
    if new_password != confirm_password:
        raise PasswordChangeRefused(
            "password_mismatch",
            "The new password and its confirmation don't match. Type the same password in both fields.",
        )
    errors = validate_password_strength(new_password)
    if errors:
        raise PasswordChangeRefused(
            "password_too_weak",
            f"New password doesn't meet the requirements: {errors[0]}.",
            errors=errors,
        )
    if new_password == current_password:
        raise PasswordChangeRefused(
            "password_unchanged",
            "The new password must be different from the current one.",
        )


def apply_password_change(username: str, new_password: str) -> None:
    """Write the new hash. For the provisioned admin, also record that the
    password now comes from the UI, so the next boot does not revert it to
    `ADMIN_PASSWORD` (see `utils/admin_identity.env_may_resync_admin_password`).

    Marker first: if it cannot be written the password is left unchanged and the
    caller sees the error. Hash first would leave a changed password that the
    next boot silently reverts to `.env`."""
    if username == admin_username():
        db.set_setting(ADMIN_PASSWORD_SOURCE_KEY, ADMIN_PASSWORD_SOURCE_UI)
    db.update_user_password(username, hash_password(new_password))
