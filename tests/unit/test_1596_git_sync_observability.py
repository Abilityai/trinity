"""#1596 — git-sync bloat mitigations (backend slice).

Covers the observability chain (agent .git size persisted to agent_sync_state)
and the default-.gitignore conventions that stop bulk data/deps/caches from being
auto-committed. The agent-side maintenance repack + .git-size measurement live in
the base image (docker/base-image/agent_server/routers/git.py) and are exercised
by the image build / live sync, not here.
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


class TestGitDirBytesNeedsNoSqliteMigration:
    """#2800 on the SQLite track is deliberately NOTHING.

    INTEGER and BIGINT are the same 64-bit INTEGER affinity in SQLite, so an
    upgraded file that still declares INTEGER stores the same values as a fresh
    file declaring BIGINT. The first version of this fix shipped a rename-swap
    rebuild of the live table at boot on the claim that the schema-parity suite
    would otherwise go red — a negative control (registration removed) showed
    it stays green, because both parity fixtures build from empty and never see
    a pre-#2800 file. A boot-time DROP TABLE for a CI benefit that does not
    exist is the wrong trade, so the migration was dropped and this pins that it
    stays dropped for a REASON rather than being re-added by the next reader of
    the Alembic revision's "mirrors" sentence.
    """

    def test_no_sqlite_migration_is_registered_for_the_widening(self):
        src = (_BACKEND / "db" / "migrations.py").read_text(encoding="utf-8")
        assert "agent_sync_state_git_dir_bytes_bigint" not in src.split("MIGRATIONS = [")[1], (
            "a SQLite migration for #2800 was re-registered — read the note beside "
            "_migrate_agent_sync_state_git_dir_bytes before keeping it"
        )

    def test_a_pre_2800_sqlite_file_stores_a_64_bit_value_unchanged(self):
        """The property the dropped rebuild was NOT needed for: INTEGER affinity
        already holds the value that overflows int4 on PostgreSQL."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY, git_dir_bytes INTEGER, updated_at TEXT NOT NULL)")
        big = 44 * 1024 ** 3   # the 44 GiB repo from the report; > 2**31
        cur.execute("INSERT INTO agent_sync_state VALUES ('a', ?, 'now')", (big,))
        assert cur.execute("SELECT git_dir_bytes FROM agent_sync_state").fetchone()[0] == big
