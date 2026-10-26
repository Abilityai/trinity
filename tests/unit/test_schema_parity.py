"""
Schema Parity Test (test_schema_parity.py)

Issue #713 — Fail PRs that add a migration without updating schema.py.
Issue #3395 — and fail the reverse: an object schema.py declares that the
fresh-install lifecycle never produces.

Builds two in-memory SQLite snapshots and compares them in BOTH directions:

  DB_schema = init_schema(empty)                                # canonical declarative
  DB_full   = run_all_migrations() + init_schema() + run_all_migrations()  # init_database() lifecycle

DB_full replays the SQLite branch of ``database.py::init_database`` on an
empty database: first migration pass, ``init_schema``, second migration pass.
If ``schema.py`` is faithful, the two snapshots are identical (modulo the
``schema_migrations`` tracker table that ``run_all_migrations`` creates
internally). Any drift fails with a clear message naming the offending
table / column / index / trigger.

* ``TestSchemaParity`` (forward, ``DB_full - DB_schema``): a migration created
  something ``schema.py`` does not declare.
* ``TestReverseSchemaParity`` (reverse, ``DB_schema - DB_full``): ``schema.py``
  declares something a fresh install does not end up with. Two ways that
  happens: an old creating migration's ``CREATE TABLE`` loses a column that
  ``init_schema``'s ``IF NOT EXISTS`` then never adds back, or a rebuild
  migration on the second pass (after ``init_schema``) drops indexes or
  triggers, since ``_atomic_rebuild(..., indexes=())`` recreates none.

The full ``init_database()`` lifecycle is required: many migrations
short-circuit when their target table already exists (e.g.
``_migrate_audit_log_table`` returns early when ``audit_log`` exists).
Running migrations on an empty DB *first* forces each migration to create
the full table+indexes+triggers it intended to, exposing schema.py omissions
that the early-return would otherwise hide.

Shape and limits. Both fixtures are the fresh-install shape (an empty
database, no rows, no prior install), so this file says nothing about a
database that already exists. In particular the reverse check is blind to a
column whose migration was never registered when its table is created only
by ``init_schema``: on a fresh install that migration hits "no such table",
is skipped, and ``init_schema`` then creates the table with the column, so
both snapshots agree while every existing install never gets the column.
That existing-install shape is covered by the merge-base upgrade guard, not
here. Only names (and, forward, column types) are compared, not index or
trigger SQL or column defaults / NOT NULL / primary keys.
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

    Mirrors the SQLite branch of ``database.py::init_database``. The first migrations
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


class TestReverseSchemaParity:
    """Every object ``schema.py`` declares must exist after a fresh install (#3395).

    The other shape this runs is the fresh install: both fixtures start from an
    empty database, and the drift caught here (a creating migration's
    ``CREATE TABLE`` losing a column, a second-pass rebuild dropping indexes or
    triggers) exists only on that shape.
    """

    def test_no_schema_only_tables(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(schema_snapshot["tables"] - production_snapshot["tables"])
        assert not missing, (
            "reverse schema drift: tables declared in init_schema() are absent "
            f"after init_database() on a fresh install: {missing}. A migration "
            "that runs after init_schema() dropped or renamed them."
        )

    def test_no_schema_only_columns(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        # Tables missing outright are reported by test_no_schema_only_tables.
        missing_cols = sorted(
            f"{table}.{col_name} ({col_type})"
            for table, schema_cols in schema_snapshot["columns"].items()
            if table in production_snapshot["columns"]
            for col_name, col_type in schema_cols.items()
            if col_name not in production_snapshot["columns"][table]
        )
        assert not missing_cols, (
            "reverse schema drift: columns declared in init_schema() are absent "
            "after init_database() on a fresh install:\n  " + "\n  ".join(missing_cols)
            + "\nThe fresh-install lifecycle never creates them: register their "
            "migration in MIGRATIONS (src/backend/db/migrations.py), or fix the "
            "creating migration's CREATE TABLE."
        )

    def test_no_schema_only_indexes(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(schema_snapshot["indexes"] - production_snapshot["indexes"])
        assert not missing, (
            "reverse schema drift: indexes declared in init_schema() are absent "
            f"after init_database() on a fresh install: {missing}. A migration "
            "that runs after init_schema() dropped them (check the indexes= "
            "argument of _atomic_rebuild in src/backend/db/migrations.py)."
        )

    def test_no_schema_only_triggers(self, schema_snapshot: dict, production_snapshot: dict) -> None:
        missing = sorted(schema_snapshot["triggers"] - production_snapshot["triggers"])
        assert not missing, (
            "reverse schema drift: triggers declared in init_schema() are absent "
            f"after init_database() on a fresh install: {missing}. A migration "
            "that runs after init_schema() dropped them (a table rebuild through "
            "_atomic_rebuild drops its triggers; recreate them after the swap)."
        )
