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
import json
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


#: `system_settings` key holding a bcrypt of the `ADMIN_PASSWORD` value the boot
#: step last applied (or found already in force), as JSON
#: ``{"username": ..., "hash": ...}`` (#3825). It is what lets boot tell "the
#: operator put a new value in `.env`" (re-apply) from "the admin changed the
#: password in the app" (leave it). Reserved: the generic settings routes neither
#: return nor accept it, because a key-only caller could otherwise read a crackable
#: hash of the env value, or delete the row so the next restart re-applies it.
ADMIN_ENV_FINGERPRINT_KEY = "admin_env_fingerprint"

#: What boot does with `ADMIN_PASSWORD` against an EXISTING admin row.
ENV_APPLY = "apply"   # write the env value's hash to the admin row
ENV_SKIP = "skip"     # the admin changed it in the app since this env value was applied
ENV_KEEP = "keep"     # nothing to write to the admin row


def fingerprint_value(username: str, hashed: str) -> str:
    """The JSON stored under `ADMIN_ENV_FINGERPRINT_KEY`."""
    return json.dumps({"username": username, "hash": hashed})


def _fingerprint_matches(env_value, fingerprint_raw, username, verify) -> bool:
    """True when the stored fingerprint is for this admin and this env value.

    A missing, unparsable, other-user or unverifiable fingerprint is "no
    fingerprint", never an error: boot must not fail on it, and treating it as
    absent re-applies `.env`, which is today's behaviour."""
    try:
        record = json.loads(fingerprint_raw) if fingerprint_raw else None
        if not isinstance(record, dict) or record.get("username") != username:
            return False
        hashed = record.get("hash")
        return isinstance(hashed, str) and bool(verify(env_value, hashed))
    except Exception:
        return False


def env_apply_decision(env_value, stored_hash, fingerprint_raw, username, verify):
    """Decide what boot does with `ADMIN_PASSWORD` for an existing admin (#3825).

    Returns ``(action, record_fingerprint)``. Shared by the SQLite and the
    PostgreSQL boot paths, which cannot share a query; `verify(plain, hashed)` is
    injected so this stays stdlib-only.

    Before #3825 boot re-applied the env value whenever it no longer verified
    against the stored hash, so a password changed in the app reverted on the
    next restart. Now it re-applies only when the env value differs from the one
    it last applied. Editing `.env` to a NEW value and restarting still rotates
    (and recovers) the password; putting back the value that was already applied
    does not, and boot says so.
    """
    if not env_value:
        return ENV_KEEP, False
    if not stored_hash:
        return ENV_APPLY, True
    if not stored_hash.startswith("$2"):
        # A legacy plaintext row: migrate it only when it IS the env value.
        if stored_hash == env_value:
            return ENV_APPLY, True
        return ENV_KEEP, False
    try:
        in_force = bool(verify(env_value, stored_hash))
    except Exception:
        in_force = False
    known = _fingerprint_matches(env_value, fingerprint_raw, username, verify)
    if in_force:
        return ENV_KEEP, not known
    if known:
        return ENV_SKIP, False
    return ENV_APPLY, True
