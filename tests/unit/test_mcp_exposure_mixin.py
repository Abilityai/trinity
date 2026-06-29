"""
Unit tests for McpExposureMixin (#846).

Covers the DB-side toggle, the deleted_at-guarded setter (a soft-deleted agent
must never be flipped into exposed state), and get_mcp_exposed_agents.

Mirrors test_file_sharing_mixin.py: route both the seeding sqlite3 connection
AND the SQLAlchemy engine at the same temp file via DATABASE_URL.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@pytest.fixture
def tmp_db_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            mcp_exposed INTEGER DEFAULT 0,
            deleted_at TEXT
        )
        """
    )
    conn.commit()

    import db.engine as engine_mod
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


@pytest.fixture
def mixin(tmp_db_conn):
    try:
        from db.agent_settings.mcp_exposure import McpExposureMixin
    except ImportError:
        pytest.skip("backend venv required (no `db.agent_settings` import)")

    class _Wrapper(McpExposureMixin):
        pass

    return _Wrapper()


def _insert_agent(conn, name, exposed=None, deleted_at=None):
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, mcp_exposed, deleted_at) "
        "VALUES (?, 1, 'now', ?, ?)",
        (name, 1 if exposed else 0, deleted_at),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Getter — defaults
# --------------------------------------------------------------------------


def test_default_false_when_no_row(mixin):
    assert mixin.get_mcp_exposed("ghost") is False


def test_default_false_when_column_unset(mixin, tmp_db_conn):
    tmp_db_conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) VALUES ('a1', 1, 'now')"
    )
    tmp_db_conn.commit()
    assert mixin.get_mcp_exposed("a1") is False


def test_true_when_column_is_one(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", exposed=True)
    assert mixin.get_mcp_exposed("a1") is True


# --------------------------------------------------------------------------
# Setter — round-trip
# --------------------------------------------------------------------------


def test_set_true_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    assert mixin.set_mcp_exposed("a1", True) is True
    assert mixin.get_mcp_exposed("a1") is True


def test_set_false_then_get(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1", exposed=True)
    assert mixin.set_mcp_exposed("a1", False) is True
    assert mixin.get_mcp_exposed("a1") is False


def test_set_returns_false_when_agent_missing(mixin):
    assert mixin.set_mcp_exposed("ghost", True) is False


def test_set_isolated_between_agents(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "a1")
    _insert_agent(tmp_db_conn, "a2")
    mixin.set_mcp_exposed("a1", True)
    assert mixin.get_mcp_exposed("a1") is True
    assert mixin.get_mcp_exposed("a2") is False


# --------------------------------------------------------------------------
# deleted_at guard — the reason this mixin does NOT copy file_sharing's setter
# --------------------------------------------------------------------------


def test_setter_refuses_soft_deleted_agent(mixin, tmp_db_conn):
    """A soft-deleted agent must never be flipped into exposed state."""
    _insert_agent(tmp_db_conn, "gone", exposed=False, deleted_at="2026-01-01T00:00:00Z")
    assert mixin.set_mcp_exposed("gone", True) is False
    # Re-read raw to confirm the column never changed.
    row = tmp_db_conn.execute(
        "SELECT mcp_exposed FROM agent_ownership WHERE agent_name='gone'"
    ).fetchone()
    assert row["mcp_exposed"] == 0


def test_getter_ignores_soft_deleted_agent(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "gone", exposed=True, deleted_at="2026-01-01T00:00:00Z")
    assert mixin.get_mcp_exposed("gone") is False


# --------------------------------------------------------------------------
# get_mcp_exposed_agents — full-set listing
# --------------------------------------------------------------------------


def test_list_only_exposed_live_agents(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "on1", exposed=True)
    _insert_agent(tmp_db_conn, "off1", exposed=False)
    _insert_agent(tmp_db_conn, "on2", exposed=True)
    _insert_agent(tmp_db_conn, "gone", exposed=True, deleted_at="2026-01-01T00:00:00Z")

    names = sorted(a["agent_name"] for a in mixin.get_mcp_exposed_agents())
    assert names == ["on1", "on2"]


def test_list_empty_when_none_exposed(mixin, tmp_db_conn):
    _insert_agent(tmp_db_conn, "off1", exposed=False)
    assert mixin.get_mcp_exposed_agents() == []
