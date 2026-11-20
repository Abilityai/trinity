"""The setup admin's email is on the email allow-list (#4693).

`POST /api/auth/email/request` mints a code only for an allow-listed address,
and every platform email redeemer re-checks the list (#2381). First-run setup
stored the admin's email on the admin account but never allow-listed it, so the
instance's own admin was told a code was on its way and none ever was.

The rule lives here, at the writers of the admin's email, not at the request
route: that route's anti-enumeration contract (#186) stays byte-for-byte as it
was, and the allow-list stays the one gate every redeemer asks — accepting "any
address bound to a user" there would also let a de-listed account back in.

Writers: setup (both forms), the admin's own email change, and a one-time
boot backfill for installs set up before this fix. The backfill runs once per
install, behind a stored marker (also set by setup), so an operator who removes
the admin's address from the allow-list is not overruled by a restart. The row's `default_role` is
`user`: the admin's address resolves to the existing admin row, so the role is
used only if that address ever creates a NEW account (after the admin moves to
another address), and that account must not be an admin.

Never raises, and never logs the address.
"""
import logging

from database import db
from utils.admin_identity import admin_username

logger = logging.getLogger(__name__)

ADMIN_ALLOWLIST_SOURCE = "setup"
BACKFILL_MARKER = "admin_email_allowlist_backfilled"


def mark_backfill_done() -> None:
    try:
        db.set_setting(BACKFILL_MARKER, "true")
    except Exception as e:
        logger.warning("Could not record the admin email backfill: %s", type(e).__name__)


def backfill_admin_email_once() -> bool:
    """Boot: allow-list the admin's email once per install, on a completed setup.

    The marker is set whether or not a row was written. Returns True when a row
    was written."""
    try:
        if db.get_setting_value("setup_completed", "false") != "true":
            return False
        if db.get_setting_value(BACKFILL_MARKER) == "true":
            return False
    except Exception as e:
        logger.warning("Could not read the admin email backfill state: %s", type(e).__name__)
        return False
    written = allow_list_admin_email()
    mark_backfill_done()
    return written


def allow_list_admin_email(email=None) -> bool:
    """Allow-list `email` (default: the admin account's current email).

    Returns True when a row was written, False when there was nothing to do or
    the write failed."""
    try:
        name = admin_username()
        if email is None:
            admin = db.get_user_by_username(name)
            email = (admin or {}).get("email")
        email = (email or "").strip().lower()
        if "@" not in email or db.is_email_whitelisted(email):
            return False
        return bool(db.add_to_whitelist(
            email, added_by=name, source=ADMIN_ALLOWLIST_SOURCE, default_role="user",
        ))
    except Exception as e:  # another worker won the insert, or the DB is down
        logger.warning("Could not allow-list the admin email: %s", type(e).__name__)
        return False
