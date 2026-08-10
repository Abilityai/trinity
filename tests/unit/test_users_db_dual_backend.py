"""
Dual-backend pilot tests for the configurable database backend (#300).

Proves the migrated ``db/users.py`` (SQLAlchemy Core) runs identically on
SQLite and PostgreSQL from one codebase. The same ``UserOperations`` CRUD
suite is parametrized over both engines:

  * SQLite — always runs (ephemeral file, zero-config).
  * PostgreSQL — runs only when a server is reachable via ``TEST_POSTGRES_URL``
    (or ``postgresql://trinity:trinity@localhost:5432/trinity`` if a local
    postgres is up); otherwise that parametrization is skipped, so the suite
    stays green in environments without postgres.

Tables are created with ``metadata.create_all`` from ``db/tables.py`` — the
dialect-agnostic schema registry that emits AUTOINCREMENT on SQLite and SERIAL
on PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (same shim as test_sync_state_db.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# tests-side `utils` package (api_client.py, …) shadows the backend's `utils`
# (helpers.py); cleared per-test via monkeypatch.delitem so db.users' import of
# `utils.helpers` resolves to the backend. Done in the fixture (not at module
# import) so all sys.modules mutation goes through monkeypatch (sys.modules
# pollution lint).
# #2080: emptied. These named the `tests/utils` shadow of the backend
# `utils` package; the helpers are now `tests/testkit`, so deleting
# `utils` here would evict the REAL backend package mid-session.
_SHADOWS: tuple[str, ...] = ()

pytestmark = pytest.mark.unit

# sqlalchemy is a hard dep of the migrated module; skip cleanly if absent.
sqlalchemy = pytest.importorskip("sqlalchemy")

DEFAULT_PG_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://trinity:trinity@localhost:5432/trinity"
)

# Synthetic package: load engine/tables/users from db/ WITHOUT importing the
# real `db` package (db/__init__.py pulls heavy modules — pytz, etc.). A unique
# name avoids clobbering any real `db.*` entries in the shared test session.
_PILOT_PKG = "db300pilot"


def _load_pilot(monkeypatch):
    """(Re)load engine, tables, users under the synthetic pilot package.

    All sys.modules mutation goes through monkeypatch so it auto-restores at
    test end (and satisfies the sys.modules pollution lint)."""
    for _shadow in _SHADOWS:
        monkeypatch.delitem(sys.modules, _shadow, raising=False)
    for key in [k for k in list(sys.modules) if k == _PILOT_PKG or k.startswith(_PILOT_PKG + ".")]:
        monkeypatch.delitem(sys.modules, key, raising=False)
    pkg = types.ModuleType(_PILOT_PKG)
    pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, _PILOT_PKG, pkg)

    def _load(rel_path: str, name: str):
        spec = importlib.util.spec_from_file_location(name, _BACKEND / rel_path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, mod)  # register before exec so relative imports resolve
        spec.loader.exec_module(mod)
        return mod

    engine_mod = _load("db/engine.py", f"{_PILOT_PKG}.engine")
    tables_mod = _load("db/tables.py", f"{_PILOT_PKG}.tables")
    users_mod = _load("db/users.py", f"{_PILOT_PKG}.users")
    return engine_mod, tables_mod, users_mod


def _pg_reachable(url: str) -> bool:
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def _backend_params():
    params = [pytest.param("sqlite", id="sqlite")]
    if _pg_reachable(DEFAULT_PG_URL):
        params.append(pytest.param("postgres", id="postgres"))
    else:
        params.append(
            pytest.param(
                "postgres",
                id="postgres",
                marks=pytest.mark.skip(
                    reason=f"no PostgreSQL reachable at {DEFAULT_PG_URL} "
                    "(set TEST_POSTGRES_URL to run the postgres leg)"
                ),
            )
        )
    return params


@pytest.fixture(params=_backend_params())
def user_ops(request, tmp_path, monkeypatch):
    """Fresh UserOperations bound to either an ephemeral SQLite file or postgres."""
    backend = request.param

    if backend == "sqlite":
        db_file = tmp_path / "trinity.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    else:
        monkeypatch.setenv("DATABASE_URL", DEFAULT_PG_URL)

    engine_mod, tables_mod, users_mod = _load_pilot(monkeypatch)
    engine = engine_mod.get_engine()  # cached per-URL → fresh for this backend

    # Clean slate: drop+recreate the users table for this backend.
    tables_mod.metadata.drop_all(engine)
    tables_mod.metadata.create_all(engine)

    ops = users_mod.UserOperations()

    yield ops

    tables_mod.metadata.drop_all(engine)
    engine_mod.dispose_engines()
    # sys.modules entries auto-restored by monkeypatch.


def _make_user(ops, username="alice@example.com", role="user", **kw):
    from db_models import UserCreate

    data = UserCreate(
        username=username,
        password=kw.get("password", "hash-123"),
        role=role,
        auth0_sub=kw.get("auth0_sub"),
        name=kw.get("name", "Alice"),
        picture=kw.get("picture"),
        email=kw.get("email", username),
    )
    return ops.create_user(data)


def test_create_and_get_by_username(user_ops):
    created = _make_user(user_ops)
    assert created["id"] is not None
    assert created["username"] == "alice@example.com"

    fetched = user_ops.get_user_by_username("alice@example.com")
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["password"] == "hash-123"  # backward-compat alias
    assert fetched["role"] == "user"
    assert fetched["suspended_at"] is None


def test_get_by_id_and_email(user_ops):
    created = _make_user(user_ops, username="bob@example.com", email="bob@example.com")
    by_id = user_ops.get_user_by_id(created["id"])
    by_email = user_ops.get_user_by_email("bob@example.com")
    assert by_id["username"] == "bob@example.com"
    assert by_email["id"] == created["id"]


def test_missing_user_returns_none(user_ops):
    assert user_ops.get_user_by_username("nobody@example.com") is None
    assert user_ops.get_user_by_id(999999) is None


def test_update_user_fields(user_ops):
    _make_user(user_ops)
    updated = user_ops.update_user("alice@example.com", {"name": "Alice B", "role": "creator"})
    assert updated["name"] == "Alice B"
    assert updated["role"] == "creator"
    # Non-whitelisted keys are ignored.
    same = user_ops.update_user("alice@example.com", {"password": "nope"})
    assert same["password"] == "hash-123"


def test_update_user_role_validation(user_ops):
    _make_user(user_ops)
    assert user_ops.update_user_role("alice@example.com", "operator")["role"] == "operator"
    assert user_ops.update_user_role("ghost@example.com", "operator") is None
    with pytest.raises(ValueError):
        user_ops.update_user_role("alice@example.com", "superadmin")


def test_update_password_updates_then_creates(user_ops):
    # Update path (existing user)
    _make_user(user_ops, username="admin", email="admin")
    assert user_ops.update_user_password("admin", "newhash") is True
    assert user_ops.get_user_by_username("admin")["password"] == "newhash"

    # Create path (user does not exist yet → created as admin)
    assert user_ops.update_user_password("freshadmin", "h2") is True
    created = user_ops.get_user_by_username("freshadmin")
    assert created is not None
    assert created["role"] == "admin"


def test_update_last_login(user_ops):
    _make_user(user_ops)
    assert user_ops.get_user_by_username("alice@example.com")["last_login"] is None
    user_ops.update_last_login("alice@example.com")
    assert user_ops.get_user_by_username("alice@example.com")["last_login"] is not None


def test_get_or_create_auth0_user(user_ops):
    # Creates on first call
    u1 = user_ops.get_or_create_auth0_user("auth0|x", "carol@example.com", name="Carol")
    assert u1["auth0_sub"] == "auth0|x"
    # Idempotent on second call (same sub)
    u2 = user_ops.get_or_create_auth0_user("auth0|x", "carol@example.com", name="Carol C")
    assert u2["id"] == u1["id"]
    assert u2["name"] == "Carol C"  # profile updated


def test_list_users_orders_newest_first(user_ops):
    _make_user(user_ops, username="u1@example.com", email="u1@example.com")
    _make_user(user_ops, username="u2@example.com", email="u2@example.com")
    rows = user_ops.list_users()
    assert {r["username"] for r in rows} >= {"u1@example.com", "u2@example.com"}
    # list_users projects without password_hash
    assert "password" not in rows[0]
