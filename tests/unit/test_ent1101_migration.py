"""External MCP access backfill — the fresh-vs-upgraded migration
(abilityai/trinity-enterprise#1101).

The switch defaults OFF. An install that already has users was serving `/mcp`
to off-box clients before the switch existed, so the data migration writes
`mcp_external_access='true'` for it — and only for it. A fresh database has no
`users` row at migration time (the admin is provisioned after `init_database`),
so nothing is written and the default applies. An existing row is never
touched: insert-only, both tracks, one decision function
(`db.migrations.run_mcp_external_access_backfill`, the 0112 shape).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
sys.path.insert(0, str(BACKEND))

pytestmark = pytest.mark.unit

KEY = "mcp_external_access"
REVISION = "0118_mcp_external_access_backfill"
PREVIOUS = "0117_operator_queue_wake_state"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, email TEXT,
            role TEXT DEFAULT 'user', created_at TEXT, updated_at TEXT
        );
        """
    )
    yield c
    c.close()


def _row(conn):
    return conn.execute("SELECT value, updated_at FROM system_settings WHERE key = ?", (KEY,)).fetchone()


def _migrate(conn):
    from db.migrations import _migrate_mcp_external_access_backfill
    _migrate_mcp_external_access_backfill(conn.cursor(), conn)


def test_an_install_with_users_is_switched_on(conn):
    conn.execute("INSERT INTO users (username, role) VALUES ('alice', 'user')")
    _migrate(conn)
    value, updated_at = _row(conn)
    assert value == "true"
    assert updated_at.endswith("Z") and "T" in updated_at, "the house utc_now_iso() stamp"


def test_a_fresh_database_writes_nothing(conn):
    _migrate(conn)
    assert _row(conn) is None, "no users → default (off) applies"


def test_an_existing_row_is_never_touched(conn):
    conn.execute("INSERT INTO users (username, role) VALUES ('alice', 'admin')")
    conn.execute("INSERT INTO system_settings (key, value, updated_at) VALUES (?, 'false', 'kept')", (KEY,))
    _migrate(conn)
    assert _row(conn) == ("false", "kept")


def test_idempotent(conn):
    conn.execute("INSERT INTO users (username, role) VALUES ('alice', 'user')")
    _migrate(conn)
    first = _row(conn)
    _migrate(conn)
    assert _row(conn) == first


def test_first_pass_skips_before_the_tables_exist():
    """`run_all_migrations` runs once BEFORE init_schema on a fresh install."""
    c = sqlite3.connect(":memory:")
    try:
        from db.migrations import _migrate_mcp_external_access_backfill
        _migrate_mcp_external_access_backfill(c.cursor(), c)  # must not raise
    finally:
        c.close()


def test_the_shared_decision_function_drives_both_tracks(conn):
    """One decision function, two drivers: the run() adapter shape of 0112."""
    from db.migrations import run_mcp_external_access_backfill
    conn.execute("INSERT INTO users (username, role) VALUES ('alice', 'user')")

    def _run(sql, params, fetch=False):
        cur = conn.execute(sql, params)
        return cur.fetchall() if fetch else None

    assert run_mcp_external_access_backfill(_run, now_iso="2026-11-05T00:00:00Z") is True
    assert _row(conn) == ("true", "2026-11-05T00:00:00Z")
    assert run_mcp_external_access_backfill(_run, now_iso="later") is False


def test_registered_on_both_tracks_and_chained_after_0117():
    from db import migrations
    names = [n for n, _ in migrations.MIGRATIONS]
    assert "mcp_external_access_backfill" in names
    assert names.index("mcp_external_access_backfill") > names.index("access_requests_requester_identity")
    versions = BACKEND / "migrations" / "versions"
    rev = (versions / f"{REVISION}.py").read_text()
    assert f'revision = "{REVISION}"' in rev
    assert f'down_revision = "{PREVIOUS}"' in rev
    # The Alembic half drives the same decision function — the PG install is
    # never answered by a second copy of the rule (the 0112 shape).
    assert "run_mcp_external_access_backfill" in rev
    # Nothing else revises 0115: the version-line keeps ONE head (#2068).
    revisers = [p.name for p in versions.glob("*.py") if f'down_revision = "{PREVIOUS}"' in p.read_text()]
    assert revisers == [f"{REVISION}.py"]
