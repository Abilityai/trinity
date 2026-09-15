"""#1596 — git-sync bloat mitigations (backend slice).

Covers the observability chain (agent .git size persisted to agent_sync_state)
and the default-.gitignore conventions that stop bulk data/deps/caches from being
auto-committed. The agent-side maintenance repack + .git-size measurement live in
the base image (docker/base-image/agent_server/routers/git.py) and are exercised
by the image build / live sync, not here.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402

pytestmark = pytest.mark.unit


def _ops():
    from db.sync_state import SyncStateOperations
    return SyncStateOperations()


# #2800: the 44 GiB round-trip below can only FAIL on PostgreSQL — SQLite's
# INTEGER is 64-bit, PostgreSQL's is int4 (ceiling 2 GiB). `schema-parity.yml`
# selects on this marker and runs it with TEST_POSTGRES_URL set, so the
# [postgres] leg is a required gate instead of a leg nobody ever ran (it shipped
# red for two months because the tier ran only `-m requires_postgres`).
@pytest.mark.requires_postgres
class TestGitDirBytesRoundTrip:
    def test_upsert_and_read_git_dir_bytes(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=47244640256)  # ~44 GiB
        row = ops.get("a1")
        assert row["git_dir_bytes"] == 47244640256

    def test_git_dir_bytes_is_64_bit_on_postgres(self, db_backend):
        """#2800: the declared type, not just one value that happened to fit.

        The round-trip above proves a 44 GiB value persists; this proves WHY, so
        a future `schema.py` edit that quietly reverts the column to INTEGER is
        named by column type rather than by a NumericValueOutOfRange stack.
        """
        if db_backend != "postgres":
            pytest.skip("declared-type assertion is PostgreSQL-only (SQLite INTEGER is already 64-bit)")
        from sqlalchemy import text
        from db.engine import get_engine

        with get_engine().connect() as conn:
            data_type = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'agent_sync_state' AND column_name = 'git_dir_bytes'"
            )).scalar()
        assert data_type == "bigint", f"git_dir_bytes is {data_type!r} on PostgreSQL — must be bigint (#2800)"

    def test_partial_update_preserves_git_dir_bytes(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=1000)
        # A later failed sync that doesn't re-measure must keep the last value.
        ops.upsert("a1", last_sync_status="failed", last_error_summary="push failed")
        row = ops.get("a1")
        assert row["git_dir_bytes"] == 1000
        assert row["last_sync_status"] == "failed"

    def test_list_all_surfaces_the_field(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=42)
        rows = {r["agent_name"]: r for r in ops.list_all()}
        assert rows["a1"]["git_dir_bytes"] == 42


class TestDefaultGitignoreConventions:
    def test_bulk_data_patterns_present(self):
        from services.git_service import _GITIGNORE_PATTERNS
        for pat in (
            "node_modules/", ".venv/", "venv/", "__pycache__/",
            "*.pyc", ".pytest_cache/", "*.sqlite", "*.sqlite3", "*.db",
        ):
            assert pat in _GITIGNORE_PATTERNS, f"{pat} missing from default .gitignore (#1596)"

    def test_still_ignores_credentials_and_content(self):
        # Guard against a copy-paste that drops the pre-existing safety rules.
        from services.git_service import _GITIGNORE_PATTERNS
        for pat in (".env", ".mcp.json", "content/", "*.pem"):
            assert pat in _GITIGNORE_PATTERNS


class TestGitDirBytesSqliteDeclaredTypeMigration:
    """#2800: the SQLite half is a declared-type rebuild, and it must keep the rows.

    SQLite has no ALTER COLUMN TYPE, so the migration re-creates
    `agent_sync_state` via the #1160 rename-swap. Three things must hold on a
    pre-#2800 file: the column now reads BIGINT (schema-parity compares declared
    types), every row survives verbatim, and the one index is back.
    """

    @staticmethod
    def _migrations():
        spec = importlib.util.spec_from_file_location(
            "migrations_for_2800", _BACKEND / "db" / "migrations.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_rebuild_redeclares_bigint_and_preserves_rows(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute(
            """
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
                git_dir_bytes INTEGER,
                pack_count INTEGER,
                loose_objects INTEGER,
                maintenance_failures INTEGER DEFAULT 0,
                last_check_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (agent_name) REFERENCES agent_ownership(agent_name)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_state_status "
            "ON agent_sync_state(last_sync_status, consecutive_failures)"
        )
        cur.execute(
            "INSERT INTO agent_sync_state (agent_name, last_sync_status, consecutive_failures, "
            "git_dir_bytes, pack_count, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("a1", "failed", 3, 47244640256, 21, "2026-09-15T00:00:00Z"),
        )
        conn.commit()

        mig = self._migrations()
        mig._migrate_agent_sync_state_git_dir_bytes_bigint(cur, conn)

        declared = {row[1]: row[2].upper() for row in cur.execute("PRAGMA table_info(agent_sync_state)")}
        assert declared["git_dir_bytes"] == "BIGINT"
        assert declared["pack_count"] == "INTEGER"  # only the byte column moved
        row = cur.execute(
            "SELECT agent_name, last_sync_status, consecutive_failures, git_dir_bytes, pack_count, updated_at "
            "FROM agent_sync_state"
        ).fetchall()
        assert row == [("a1", "failed", 3, 47244640256, 21, "2026-09-15T00:00:00Z")]
        indexes = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_sync_state_status" in indexes
        assert not cur.execute(
            "SELECT name FROM sqlite_master WHERE name='agent_sync_state_new'"
        ).fetchone()

        # Idempotent: a second run sees BIGINT and touches nothing.
        mig._migrate_agent_sync_state_git_dir_bytes_bigint(cur, conn)
        assert cur.execute("SELECT COUNT(*) FROM agent_sync_state").fetchone()[0] == 1

    def test_refuses_to_drop_an_unknown_column(self):
        """A rename-swap copies only the columns it names; an unknown one must stop it, not vanish."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY, git_dir_bytes INTEGER, "
            "updated_at TEXT NOT NULL, future_col TEXT)"
        )
        cur.execute("INSERT INTO agent_sync_state VALUES ('a1', 1, 'now', 'keep me')")
        conn.commit()
        with pytest.raises(RuntimeError, match="future_col"):
            self._migrations()._migrate_agent_sync_state_git_dir_bytes_bigint(cur, conn)
        # Nothing touched: column and row both still there, no orphan _new table.
        assert cur.execute("SELECT future_col FROM agent_sync_state").fetchone() == ("keep me",)
        assert not cur.execute("SELECT 1 FROM sqlite_master WHERE name='agent_sync_state_new'").fetchone()

    def test_noop_before_the_column_exists(self):
        """Pre-#1596 file: the add-column migration runs first; this one must not rebuild a table it cannot describe."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY, updated_at TEXT NOT NULL)")
        conn.commit()
        self._migrations()._migrate_agent_sync_state_git_dir_bytes_bigint(cur, conn)
        assert "git_dir_bytes" not in {row[1] for row in cur.execute("PRAGMA table_info(agent_sync_state)")}
