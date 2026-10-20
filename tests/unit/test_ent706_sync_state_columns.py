"""The four divergence columns on `agent_sync_state` (trinity-enterprise#706).

Both migration tracks (Invariant #9) and the upsert's set / keep / clear
semantics. `diverged_since` and `dirty_since` are episode clocks: the poller
must be able to say "clear" (None) as distinct from "unchanged", which the
upsert's `_merged()` fallback cannot express — hence the `KEEP` sentinel.

Backend-agnostic via ``db_harness`` (#300) for the upsert cases.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from db_harness import db_backend  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO / "src" / "backend" / "migrations" / "versions"
NEW_COLUMNS = ("diverged_since", "dirty_files", "dirty_since", "last_successful_push_at")

# agent_sync_state exactly as it stood before #706 — what an upgrading
# install's SQLite file holds when the new migration runs.
_PRE_706_DDL = """
CREATE TABLE agent_sync_state (
    agent_name TEXT PRIMARY KEY,
    last_sync_at TEXT,
    last_sync_status TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    last_error_summary TEXT,
    last_remote_sha_main TEXT,
    last_remote_sha_working TEXT,
    ahead_main INTEGER DEFAULT 0,
    behind_main INTEGER DEFAULT 0,
    ahead_working INTEGER DEFAULT 0,
    behind_working INTEGER DEFAULT 0,
    git_dir_bytes BIGINT,
    pack_count INTEGER,
    loose_objects INTEGER,
    maintenance_failures INTEGER DEFAULT 0,
    last_check_at TEXT,
    updated_at TEXT NOT NULL
)
"""


def _cols(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(agent_sync_state)")}


# --------------------------------------------------------------------------
# SQLite track
# --------------------------------------------------------------------------

def test_sqlite_migration_adds_the_columns_and_is_idempotent():
    from db.migrations import _migrate_agent_sync_state_divergence

    conn = sqlite3.connect(":memory:")
    conn.execute(_PRE_706_DDL)
    conn.execute(
        "INSERT INTO agent_sync_state (agent_name, ahead_working, updated_at) "
        "VALUES ('a1', 7, '2026-09-27T00:00:00Z')"
    )
    assert not set(NEW_COLUMNS) & _cols(conn)
    _migrate_agent_sync_state_divergence(conn.cursor(), conn)
    _migrate_agent_sync_state_divergence(conn.cursor(), conn)  # second run: no-op
    assert set(NEW_COLUMNS) <= _cols(conn)
    # No backfill: the clocks start at the first post-upgrade poll.
    row = conn.execute(
        "SELECT ahead_working, diverged_since, dirty_files, dirty_since, "
        "last_successful_push_at FROM agent_sync_state"
    ).fetchone()
    assert row == (7, None, None, None, None)


def test_sqlite_migration_is_registered():
    from db.migrations import MIGRATIONS

    names = [name for name, _ in MIGRATIONS]
    assert names.count("agent_sync_state_divergence") == 1


def test_fresh_schema_carries_the_columns():
    from db.schema import TABLES

    conn = sqlite3.connect(":memory:")
    conn.execute(TABLES["agent_sync_state"])
    assert set(NEW_COLUMNS) <= _cols(conn)


def test_core_table_carries_the_columns():
    from db.tables import agent_sync_state

    assert set(NEW_COLUMNS) <= set(agent_sync_state.c.keys())


# --------------------------------------------------------------------------
# PostgreSQL track (the revision's shape; the live upgrade is proved on a real
# PostgreSQL in the PR evidence — CI runs no PG pytest)
# --------------------------------------------------------------------------

def _revision_file() -> Path:
    matches = sorted(_VERSIONS.glob("*_agent_sync_state_divergence.py"))
    assert len(matches) == 1, matches
    return matches[0]


def test_alembic_revision_adds_and_drops_all_four():
    path = _revision_file()
    tree = ast.parse(path.read_text())
    assigns = {
        t.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert assigns["revision"] == path.stem
    parent = _VERSIONS / f"{assigns['down_revision']}.py"
    assert parent.exists(), f"down_revision {assigns['down_revision']} is not a revision"
    text = path.read_text()
    for col in NEW_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {col} " in text
        assert f"DROP COLUMN IF EXISTS {col}" in text


# --------------------------------------------------------------------------
# Upsert: set / keep / clear
# --------------------------------------------------------------------------

@pytest.fixture
def sync_db(db_backend):  # noqa: F811
    from database import db

    db.create_git_config(
        agent_name="ent706-a", github_repo="owner/ent706-a",
        working_branch="main", instance_id="inst-ent706-a",
    )
    return db


def test_clocks_default_to_keep(sync_db):
    # The sentinel from the module the LIVE ops object was built from — sibling
    # suites evict and re-import `db.*`, so `from db.sync_state import KEEP`
    # can name a different module object than the one `db` is holding.
    KEEP = type(sync_db._sync_state_ops).upsert.__globals__["KEEP"]

    sync_db.upsert_sync_state(
        "ent706-a", diverged_since="2026-09-26T10:00:00Z",
        dirty_since="2026-09-26T11:00:00Z", dirty_files=791,
        last_successful_push_at="2026-09-26T09:00:00Z",
    )
    # A later partial upsert that says nothing about the clocks keeps them.
    sync_db.upsert_sync_state("ent706-a", last_sync_status="success")
    row = sync_db.get_sync_state("ent706-a")
    assert row["diverged_since"] == "2026-09-26T10:00:00Z"
    assert row["dirty_since"] == "2026-09-26T11:00:00Z"
    assert row["dirty_files"] == 791
    assert row["last_successful_push_at"] == "2026-09-26T09:00:00Z"
    # KEEP is also the explicit spelling.
    sync_db.upsert_sync_state("ent706-a", diverged_since=KEEP, dirty_since=KEEP)
    assert sync_db.get_sync_state("ent706-a")["diverged_since"] == "2026-09-26T10:00:00Z"


def test_none_clears_a_clock(sync_db):
    sync_db.upsert_sync_state(
        "ent706-a", diverged_since="2026-09-26T10:00:00Z",
        dirty_since="2026-09-26T11:00:00Z",
    )
    sync_db.upsert_sync_state("ent706-a", diverged_since=None, dirty_since=None)
    row = sync_db.get_sync_state("ent706-a")
    assert row["diverged_since"] is None
    assert row["dirty_since"] is None


def test_zero_dirty_files_is_stored_not_dropped(sync_db):
    sync_db.upsert_sync_state("ent706-a", dirty_files=791)
    sync_db.upsert_sync_state("ent706-a", dirty_files=0)
    assert sync_db.get_sync_state("ent706-a")["dirty_files"] == 0
