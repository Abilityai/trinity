"""#2381 — a fresh install must not leave the first-run endpoint open on a real admin.

The bug: `init_database` asks "does an admin exist?" (the `setup_completed_backfill`
migration) during `run_all_migrations`, which on a fresh install runs while `users`
is still empty — so it answers "no", does nothing, and is then RECORDED as applied
so it never asks again. `_ensure_admin_user` creates the admin one step later. Net
result: a real admin exists while `setup_completed` says false, and the
unauthenticated `POST /api/setup/admin-password` — which gates only on that flag —
overwrites that admin's password hash and binds the caller's email to it.

Two halves are under test, and they must agree:

* `routers/setup.py` refuses whenever a usable admin exists (the security fix), and
* `database.py` writes `setup_completed=true` whenever one exists (the honesty fix).

They cannot share a query — one runs per-request on the ORM, the other at import
time on a raw sqlite3 cursor — so they share `utils/admin_identity`, and
`test_predicate_halves_agree` pins that they cannot drift apart.

The one flow that must NOT break: an install with no admin (blank ADMIN_PASSWORD)
still gets the wizard, because login is gated on the same flag and the wizard is
its only way in.

Import isolation: `routers.setup` / `database` pull the backend chain, so every
import is deferred out of collection — see the long note in
`test_setup_operator_profile.py`.
"""
import asyncio
import sqlite3

import pytest
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
HASH = "$2b$12$abcdefghijklmnopqrstuv"


# --------------------------------------------------------------------------
# lazy imports (collection stays backend-free)
# --------------------------------------------------------------------------

def _setup():
    import routers.setup as m
    return m


def _database():
    import database as m
    return m


def _identity():
    import utils.admin_identity as m
    return m


# --------------------------------------------------------------------------
# Layer 1 — the endpoint refuses when a usable admin already exists
# --------------------------------------------------------------------------

class FakeDB:
    """Minimal stand-in. `users` empty == an install with no admin."""

    def __init__(self, users=None, settings=None):
        self.settings = {"setup_completed": "false"}
        self.settings.update(settings or {})
        self.users = users or {}
        self.password_writes = []

    def get_user_by_username(self, username):
        return self.users.get(username)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed):
        self.password_writes.append((username, hashed))
        self.users.setdefault(username, {"username": username})["password"] = hashed
        return True

    def update_user(self, username, updates):
        self.users.setdefault(username, {"username": username}).update(updates)
        return self.users[username]


class ExplodingDB(FakeDB):
    def get_user_by_username(self, username):
        raise sqlite3.OperationalError("database is locked")


@pytest.fixture
def patch_setup(monkeypatch):
    def _apply(db):
        setup = _setup()
        monkeypatch.setattr(setup, "db", db)
        monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
        monkeypatch.setattr(setup, "hash_password", lambda p: "hashed:" + p)
        return db
    return _apply


def _call(db):
    setup = _setup()
    data = setup.SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email="me@acme.com"
    )
    return asyncio.run(setup.set_admin_password(data, None, BackgroundTasks()))


def test_refuses_when_admin_already_provisioned(patch_setup):
    """The #2381 exploit, verbatim: flag false, real admin present."""
    db = patch_setup(FakeDB(users={"admin": {"username": "admin", "password": HASH}}))

    with pytest.raises(HTTPException) as exc:
        _call(db)

    assert exc.value.status_code == 403
    # The admin's password hash must be untouched — that is the whole point.
    assert db.password_writes == []
    assert db.users["admin"]["password"] == HASH
    # And the caller's email must not have been bound to the admin account.
    assert "email" not in db.users["admin"]


def test_still_open_when_no_admin_exists(patch_setup):
    """The flow that must NOT break: blank ADMIN_PASSWORD, nobody can log in.

    Login is gated on `setup_completed` too, so refusing here would brick the
    install — no wizard AND no login.
    """
    db = patch_setup(FakeDB())

    result = _call(db)

    assert result["success"] is True
    assert db.settings["setup_completed"] == "true"
    assert db.password_writes and db.password_writes[0][0] == "admin"


def test_open_when_admin_row_has_no_usable_hash(patch_setup):
    """A row with an empty hash is not a login anyone can perform."""
    db = patch_setup(FakeDB(users={"admin": {"username": "admin", "password": ""}}))

    assert _call(db)["success"] is True


def test_admin_lookup_failure_fails_closed(patch_setup):
    """A DB error refuses rather than falling through to the flag.

    Failing open would restore the vulnerability on exactly the transient
    conditions an attacker can retry against.
    """
    db = patch_setup(ExplodingDB())

    with pytest.raises(HTTPException) as exc:
        _call(db)
    assert exc.value.status_code == 403


def test_honours_admin_username_env(patch_setup, monkeypatch):
    """ADMIN_USERNAME=root must be seen as the admin.

    With the old hardcoded "admin", this install was refused nothing: the check
    missed the real admin, and `update_user_password` (an upsert) then INSERTed a
    SECOND role='admin' account for the caller.
    """
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    db = patch_setup(FakeDB(users={"root": {"username": "root", "password": HASH}}))

    with pytest.raises(HTTPException) as exc:
        _call(db)
    assert exc.value.status_code == 403
    assert "admin" not in db.users  # no second account minted


def test_refusal_precedes_password_hashing(patch_setup, monkeypatch):
    """bcrypt must not run on a request that was going to be refused.

    The route is unauthenticated and unrate-limited, so a deliberately expensive
    hash above the gate is both a DoS lever and a timing signal.
    """
    setup = _setup()
    db = patch_setup(FakeDB(users={"admin": {"username": "admin", "password": HASH}}))
    calls = []
    monkeypatch.setattr(setup, "hash_password", lambda p: calls.append(p) or "x")
    monkeypatch.setattr(
        setup, "validate_password_strength", lambda p: calls.append("validate") or []
    )

    with pytest.raises(HTTPException):
        _call(db)
    assert calls == []


# --------------------------------------------------------------------------
# Layer 2 — `setup_completed` becomes honest, on both backends
# --------------------------------------------------------------------------

def _mkdb():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "username TEXT UNIQUE, password_hash TEXT, role TEXT)"
    )
    cur.execute(
        "CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn, cur


def _flag(cur):
    cur.execute("SELECT value FROM system_settings WHERE key = 'setup_completed'")
    row = cur.fetchone()
    return row[0] if row else None


def test_sqlite_sets_flag_when_admin_provisioned():
    conn, cur = _mkdb()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
        (HASH,),
    )
    conn.commit()

    _database()._mark_setup_completed_if_provisioned(cur, conn)

    assert _flag(cur) == "true"


def test_sqlite_fixes_an_already_broken_install():
    """The population that matters: already booted, admin present, flag absent.

    A CREATE-branch-only fix would leave every currently-exposed instance
    exposed, and the recorded backfill migration will never run again. This is a
    boot-time reconciliation precisely so those installs converge on restart.
    """
    conn, cur = _mkdb()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
        (HASH,),
    )
    conn.commit()
    assert _flag(cur) is None  # the #2381 state

    _database()._mark_setup_completed_if_provisioned(cur, conn)

    assert _flag(cur) == "true"


def test_sqlite_leaves_flag_alone_without_admin():
    conn, cur = _mkdb()

    _database()._mark_setup_completed_if_provisioned(cur, conn)

    assert _flag(cur) is None


def test_sqlite_leaves_flag_alone_for_hashless_row():
    conn, cur = _mkdb()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('admin', NULL, 'admin')"
    )
    conn.commit()

    _database()._mark_setup_completed_if_provisioned(cur, conn)

    assert _flag(cur) is None


def test_sqlite_honours_admin_username_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    conn, cur = _mkdb()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('root', ?, 'admin')",
        (HASH,),
    )
    conn.commit()

    _database()._mark_setup_completed_if_provisioned(cur, conn)

    assert _flag(cur) == "true"


def test_sqlite_never_raises():
    """`init_database` runs at import — a raise here crash-loops the backend."""
    conn = sqlite3.connect(":memory:")  # no tables at all
    _database()._mark_setup_completed_if_provisioned(conn.cursor(), conn)


def test_engine_path_sets_flag(monkeypatch):
    """PostgreSQL had NO writer for this key — no Alembic revision touches it."""
    database = _database()
    written = {}

    class _Users:
        def get_user_by_username(self, username):
            return {"username": username, "password": HASH}

    class _Settings:
        def get_setting_value(self, key, default=None):
            return default

        def set_setting(self, key, value):
            written[key] = value

    monkeypatch.setattr(database, "UserOperations", _Users)
    monkeypatch.setattr(database, "SettingsOperations", _Settings)

    database._mark_setup_completed_if_provisioned_engine()

    assert written == {"setup_completed": "true"}


def test_engine_path_leaves_flag_alone_without_admin(monkeypatch):
    database = _database()
    written = {}

    class _Users:
        def get_user_by_username(self, username):
            return None

    class _Settings:
        def get_setting_value(self, key, default=None):
            return default

        def set_setting(self, key, value):
            written[key] = value

    monkeypatch.setattr(database, "UserOperations", _Users)
    monkeypatch.setattr(database, "SettingsOperations", _Settings)

    database._mark_setup_completed_if_provisioned_engine()

    assert written == {}


def test_engine_path_never_raises(monkeypatch):
    database = _database()

    class _Boom:
        def __init__(self):
            raise RuntimeError("engine not ready")

    monkeypatch.setattr(database, "UserOperations", _Boom)
    database._mark_setup_completed_if_provisioned_engine()


# --------------------------------------------------------------------------
# The two halves must keep answering the same question
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hash_value,provisioned",
    [
        (HASH, True),
        ("", False),
        ("   ", False),
        (None, False),
    ],
)
def test_predicate_halves_agree(hash_value, provisioned, patch_setup):
    """One policy, two call sites: the router and the boot reconciliation.

    They run against different drivers and cannot share a query, so drift here
    is what re-creates #2381 — a flag that disagrees with reality.
    """
    identity = _identity()
    assert identity.is_usable_password_hash(hash_value) is provisioned

    # Router half: refuses exactly when the policy says provisioned.
    db = patch_setup(FakeDB(users={"admin": {"username": "admin", "password": hash_value}}))
    if provisioned:
        with pytest.raises(HTTPException):
            _call(db)
    else:
        assert _call(db)["success"] is True

    # Boot half: writes the flag exactly when the policy says provisioned.
    conn, cur = _mkdb()
    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')",
        (hash_value,),
    )
    conn.commit()
    _database()._mark_setup_completed_if_provisioned(cur, conn)
    assert (_flag(cur) == "true") is provisioned


def test_admin_username_default_and_override(monkeypatch):
    identity = _identity()
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    assert identity.admin_username() == "admin"
    monkeypatch.setenv("ADMIN_USERNAME", "  root  ")
    assert identity.admin_username() == "root"
    # Blank must not resolve to "" — that matches no row and would silently
    # reopen the endpoint on every install that sets the var to nothing.
    monkeypatch.setenv("ADMIN_USERNAME", "   ")
    assert identity.admin_username() == "admin"
