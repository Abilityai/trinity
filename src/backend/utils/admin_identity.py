"""Who the platform's provisioned admin is — the one policy both halves share (#2381).

Two very different layers need the same answer to "does this install already have
a real admin account?":

* ``database.py::init_database`` asks it at import time, on a RAW sqlite3 cursor,
  before the app object exists, to decide whether ``setup_completed`` should be
  true.
* ``routers/setup.py`` asks it per request, through the dialect-agnostic ORM
  accessors, to decide whether the unauthenticated first-run endpoint may still
  overwrite the admin password.

They cannot share a *query* — one has no engine, the other has no cursor — so
they share the *policy* instead: which username the admin lives under, and what
counts as a usable password hash. Keeping those two facts in one place is what
stops the halves drifting into disagreeing about whether an install is
provisioned, which is precisely the shape of #2381: a derived flag that
disagreed with reality, trusted by an unauthenticated endpoint.

Stdlib only. No imports from ``db`` / ``services`` / ``config`` — ``database.py``
imports this at module import time, and every one of those would be a cycle.
"""
import os

#: The username `_ensure_admin_user` provisions when `ADMIN_USERNAME` is unset.
DEFAULT_ADMIN_USERNAME = "admin"


def admin_username() -> str:
    """The username the platform provisions its admin account under.

    ``_ensure_admin_user`` honours ``ADMIN_USERNAME``, so anything asking "does
    an admin exist?" must ask about the same name. Before #2381, two callers
    hardcoded ``"admin"`` instead: ``routers/setup.py`` and the
    ``setup_completed_backfill`` migration. On an ``ADMIN_USERNAME=root``
    install that made the setup endpoint miss the real admin entirely and
    *create a second* ``role='admin'`` account for whoever called it.

    The recorded migration is deliberately left alone — it has already run and
    been recorded on every existing install, so editing it changes nothing.
    """
    return (os.getenv("ADMIN_USERNAME") or "").strip() or DEFAULT_ADMIN_USERNAME


def is_usable_password_hash(value) -> bool:
    """True when ``value`` is a password hash a login could actually verify.

    Deliberately not ``value is not None``: ``users.password_hash`` is nullable,
    and a row created by a path that never set a password carries an empty
    string. In either case nobody can log into the account, so the install is
    NOT provisioned and the first-run wizard must stay open — that is the one
    flow #2381 must not break (a blank-``ADMIN_PASSWORD`` dev install has no
    admin, and the wizard is its only way in).
    """
    return isinstance(value, str) and value.strip() != ""


#: trinity-enterprise#709 — the `system_settings` row that says the admin's
#: password was last set from the UI (Settings → Change password). Once it reads
#: ``ADMIN_PASSWORD_SOURCE_UI``, the boot-time seeding in ``database.py`` leaves a
#: usable stored hash alone instead of re-syncing it to ``ADMIN_PASSWORD`` — the
#: DB wins, and ``.env`` only seeds a missing or unusable hash. Without it, a
#: restart with the old value still in ``.env`` would silently revert a rotation.
ADMIN_PASSWORD_SOURCE_KEY = "admin_password_source"
ADMIN_PASSWORD_SOURCE_UI = "ui"


def env_may_resync_admin_password(stored_hash, password_source) -> bool:
    """Whether boot may overwrite the admin's stored hash with ``ADMIN_PASSWORD``.

    One rule for both boot paths (SQLite cursor and engine), so they cannot
    drift: the env value wins unless the password was set from the UI AND the
    stored hash is one a login could verify (#709).
    """
    if password_source == ADMIN_PASSWORD_SOURCE_UI and is_usable_password_hash(stored_hash):
        return False
    return True
