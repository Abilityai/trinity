"""
Schema Parity Test (test_schema_parity.py)

Issue #713 — Fail PRs that add a migration without updating schema.py.

Builds two in-memory SQLite snapshots and asserts they match:

  DB_schema = init_schema(empty)                                # canonical declarative
  DB_full   = run_all_migrations() + init_schema() + run_all_migrations()  # init_database() lifecycle

If ``schema.py`` is faithful, the two snapshots are identical (modulo the
``schema_migrations`` tracker table that ``run_all_migrations`` creates
internally). Any drift fails with a clear message naming the offending
table / column / index / trigger.

The full ``init_database()`` lifecycle is required: many migrations
short-circuit when their target table already exists (e.g.
``_migrate_audit_log_table`` at ``migrations.py:1356-1364`` returns early
when ``audit_log`` exists). Running migrations on an empty DB *first*
forces each migration to create the full table+indexes+triggers it
intended to, exposing schema.py omissions that the early-return would
otherwise hide.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_module(rel_path: str, name: str):
    path = _BACKEND / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema_mod = _load_module("db/schema.py", "schema_for_parity")
_migrations_mod = _load_module("db/migrations.py", "migrations_for_parity")
init_schema = _schema_mod.init_schema
run_all_migrations = _migrations_mod.run_all_migrations

pytestmark = pytest.mark.unit

# Tracker table that ``run_all_migrations`` creates internally; ``schema.py``
# is not expected to define it.
INTERNAL_TABLES = {"schema_migrations"}


def _snapshot(cursor: sqlite3.Cursor) -> dict:
    """Return ``{tables, columns, indexes, triggers}`` for the open DB."""
    tables = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    } - INTERNAL_TABLES

    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk).
    # Map name -> type, uppercased so "INTEGER" / "integer" / "Int" compare equal.
    columns: dict[str, dict[str, str]] = {}
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        columns[table] = {row[1]: (row[2] or "").upper() for row in cursor.fetchall()}

    indexes = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND tbl_name NOT IN ('schema_migrations')"
        ).fetchall()
        if row[0] is not None
    }

    triggers = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name NOT IN ('schema_migrations')"
        ).fetchall()
    }

    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "triggers": triggers,
    }


@pytest.fixture(scope="module")
def schema_snapshot() -> dict:
    """Snapshot of ``init_schema()`` on an empty DB — the canonical declarative state."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    init_schema(cur, conn)
    return _snapshot(cur)


@pytest.fixture(scope="module")
def production_snapshot() -> dict:
    """Snapshot of the full ``init_database()`` lifecycle:
    ``run_all_migrations`` -> ``init_schema`` -> ``run_all_migrations``.

    Mirrors ``src/backend/database.py:139-164`` exactly. The first migrations
    pass on an empty DB lets every migration create the table+indexes+triggers
    it intended to, exposing any short-circuit-hidden drift in schema.py.
    """
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    run_all_migrations(cur, conn)
    init_schema(cur, conn)
    run_all_migrations(cur, conn)
    return _snapshot(cur)


class TestSchemaParity:
    """``schema.py`` must be a faithful encoding of ``init_database()``'s end state."""

    def test_no_missing_tables(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(production_snapshot["tables"] - schema_snapshot["tables"])
        assert not missing, (
            "schema.py drift detected: tables exist after init_database() but "
            f"not in init_schema(): {missing}. Add CREATE TABLE definitions "
            "to src/backend/db/schema.py."
        )

    def test_no_missing_columns(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing_cols: list[str] = []
        type_drift: list[str] = []
        for table, prod_cols in production_snapshot["columns"].items():
            schema_cols = schema_snapshot["columns"].get(table, {})
            for col_name, prod_type in prod_cols.items():
                if col_name not in schema_cols:
                    missing_cols.append(f"{table}.{col_name} ({prod_type})")
                elif schema_cols[col_name] != prod_type:
                    type_drift.append(
                        f"{table}.{col_name}: schema.py={schema_cols[col_name]} "
                        f"vs migrations={prod_type}"
                    )
        assert not missing_cols, (
            "schema.py drift detected: columns exist after init_database() but "
            "not in init_schema():\n  " + "\n  ".join(missing_cols)
            + "\nAdd them to the CREATE TABLE in src/backend/db/schema.py."
        )
        assert not type_drift, (
            "schema.py drift detected: column types differ between "
            "init_schema() and migrations:\n  " + "\n  ".join(type_drift)
            + "\nUpdate src/backend/db/schema.py to match the migration's type."
        )

    def test_no_missing_indexes(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(production_snapshot["indexes"] - schema_snapshot["indexes"])
        assert not missing, (
            "schema.py drift detected: indexes exist after init_database() but "
            f"not in init_schema(): {missing}. Add CREATE INDEX statements "
            "to src/backend/db/schema.py."
        )

    def test_no_missing_triggers(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(production_snapshot["triggers"] - schema_snapshot["triggers"])
        assert not missing, (
            "schema.py drift detected: triggers exist after init_database() but "
            f"not in init_schema(): {missing}. Add CREATE TRIGGER statements "
            "to src/backend/db/schema.py."
        )
