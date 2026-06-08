"""
Unit tests for FileSharingMixin (amazing-file-outbound Step 2).

Covers the DB-side toggle + static helpers:
- Default value is False when no row exists
- Default value is False when column is NULL
- set → get round trip
- Flip on, then off — idempotent rewrites
- Volume-name and mount-path conventions
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# Load db/connection.py and db/agent_settings/file_sharing.py directly.
# The regular package import path triggers pydantic via db/__init__.py;
# we route around that the same way the Step 1 migration test does.
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# db.connection reads TRINITY_DB_PATH at import time; we'll point it at a
# temp DB per test via monkeypatch + re-import. Load it lazily below.


@pytest.fixture
def tmp_db_conn(tmp_path, monkeypatch):
    """Provision an empty DB with just agent_ownership and the new column.

    Returns a sqlite3 connection the test can seed with rows.
    """
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            file_sharing_enabled INTEGER DEFAULT 0,
            deleted_at TEXT  -- #834: read paths filter `WHERE deleted_at IS NULL`
        )
        """
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mixin(tmp_db_conn):
    """Load the mixin bound to the tmp DB.

    db/connection.py reads TRINITY_DB_PATH at import time, so we force a
    fresh load after the env var is set.
    """
    # Ensure the env-dependent module is reloaded per test
    sys.modules.pop("_ams_db_connection", None)
    _load("_ams_db_connection", _BACKEND / "db" / "connection.py")
    # The mixin does `from db.connection import get_db_connection`. We register
    # a `db` package pointing at the real src/backend/db directory so that
    # (a) this test's `from db.connection import ...` finds our stub, and
    # (b) later tests that do `from db.X import Y` can still resolve X from
    # the real directory. Without __path__, sys.modules['db'] becomes a
    # non-package and breaks sibling tests.
    original_db = sys.modules.get("db")
    original_db_connection = sys.modules.get("db.connection")
    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    sys.modules["db"] = db_pkg
    sys.modules["db.connection"] = sys.modules["_ams_db_connection"]
    fs_mod = _load(
        "_ams_file_sharing",
        _BACKEND / "db" / "agent_settings" / "file_sharing.py",
    )

    class _Wrapper(fs_mod.FileSharingMixin):
        pass

    wrapper = _Wrapper()

    yield wrapper

    # Restore sys.modules to avoid leaking our stub into later tests that
    # import the real db package.
    if original_db is not None:
        sys.modules["db"] = original_db
    else:
        sys.modules.pop("db", None)
    if original_db_connection is not None:
        sys.modules["db.connection"] = original_db_connection
    else:
        sys.modules.pop("db.connection", None)


def _insert_agent(conn, name, enabled=None):
    if enabled is None:
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (?, 1, 'now')",
            (name,),
        )
    else:
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, file_sharing_enabled) "
            "VALUES (?, 1, 'now', ?)",
            (name, 1 if enabled else 0),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Getter — defaults
# ---------------------------------------------------------------------------


def test_default_false_when_no_row(mixin):
    assert mixin.get_file_sharing_enabled("ghost-agent") is False


def test_default_false_when_column_unset(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    assert mixin.get_file_sharing_enabled("a1") is False


def test_false_when_column_is_zero(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=False)
    assert mixin.get_file_sharing_enabled("a1") is False


def test_true_when_column_is_one(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.get_file_sharing_enabled("a1") is True


# ---------------------------------------------------------------------------
# Setter — round-trip
# ---------------------------------------------------------------------------


def test_set_true_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.get_file_sharing_enabled("a1") is True


def test_set_false_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.set_file_sharing_enabled("a1", False) is True
    assert mixin.get_file_sharing_enabled("a1") is False


def test_set_is_idempotent(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", enabled=True)
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.set_file_sharing_enabled("a1", True) is True
    assert mixin.get_file_sharing_enabled("a1") is True


def test_set_returns_false_when_agent_missing(mixin):
    """No-op UPDATE should report 0 rowcount back to the caller."""
    assert mixin.set_file_sharing_enabled("ghost", True) is False


def test_set_isolated_between_agents(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    _insert_agent(tmp_db_conn, "a2")
    mixin.set_file_sharing_enabled("a1", True)
    assert mixin.get_file_sharing_enabled("a1") is True
    assert mixin.get_file_sharing_enabled("a2") is False


# ---------------------------------------------------------------------------
# Static helpers — volume name + mount path conventions
# ---------------------------------------------------------------------------


def test_volume_name_convention(mixin):
    assert mixin.get_public_volume_name("alpha") == "agent-alpha-public"
    assert mixin.get_public_volume_name("xyz-1") == "agent-xyz-1-public"


def test_mount_path_is_constant(mixin):
    assert mixin.get_public_mount_path() == "/home/developer/public"


def test_mount_path_does_not_depend_on_agent(mixin):
    """Different agents share the same in-container mount point."""
    # Method doesn't take agent_name today — pin the contract so a
    # future change that introduces per-agent paths has to explicitly
    # break this test and justify it.
    import inspect
    sig = inspect.signature(mixin.get_public_mount_path)
    assert list(sig.parameters) == []
