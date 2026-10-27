"""
Migration System Unit Tests (test_migrations.py)

Tests for the database migration runner (Issue #236).
Covers: error propagation, tracking table, idempotency, and skip-if-applied logic.

These are pure unit tests — no backend or Docker required.
Uses an in-memory SQLite database.
"""

import ast
import importlib.util
import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_backend = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_module(rel_path, name):
    path = _backend / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_migrations_mod = _load_module("db/migrations.py", "migrations")
_schema_mod = _load_module("db/schema.py", "schema")

run_all_migrations = _migrations_mod.run_all_migrations
MIGRATIONS = _migrations_mod.MIGRATIONS
init_schema = _schema_mod.init_schema

pytestmark = pytest.mark.unit


def _make_db():
    """Return an in-memory SQLite connection and cursor with a full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # init_schema creates all base tables (CREATE TABLE IF NOT EXISTS).
    # Running migrations after this simulates an upgrade on an existing install.
    init_schema(cursor, conn)
    return conn, cursor


class TestMigrationTracking:
    """schema_migrations table is created and tracks applied migrations."""

    def test_tracking_table_created_on_first_run(self):
        conn, cursor = _make_db()
        run_all_migrations(cursor, conn)

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        assert cursor.fetchone() is not None

    def test_all_migrations_recorded_after_run(self):
        conn, cursor = _make_db()
        run_all_migrations(cursor, conn)

        cursor.execute("SELECT name FROM schema_migrations")
        applied_names = {row[0] for row in cursor.fetchall()}
        expected_names = {name for name, _ in MIGRATIONS}
        assert applied_names == expected_names

    def test_applied_at_is_timezone_aware_iso8601(self):
        conn, cursor = _make_db()
        run_all_migrations(cursor, conn)

        cursor.execute("SELECT applied_at FROM schema_migrations LIMIT 1")
        row = cursor.fetchone()
        assert row is not None
        dt = datetime.fromisoformat(row[0])
        assert dt.tzinfo is not None


class TestIdempotency:
    """Running migrations twice is safe — already-applied migrations are skipped."""

    def test_second_run_does_not_call_any_migration_fn(self):
        conn, cursor = _make_db()
        run_all_migrations(cursor, conn)

        call_log = []

        patched = [
            (name, _make_tracking_fn(name, call_log, fn))
            for name, fn in MIGRATIONS
        ]
        with patch.object(_migrations_mod, "MIGRATIONS", patched):
            run_all_migrations(cursor, conn)

        assert call_log == [], f"Expected no migrations on second run, got: {call_log}"

    def test_insert_or_ignore_survives_duplicate_tracking_row(self):
        """INSERT OR IGNORE: if another worker already recorded the migration, no IntegrityError."""
        conn, cursor = _make_db()

        # Seed tracking table as if another worker already ran the first migration
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        ts = datetime.now(timezone.utc).isoformat()
        first_name = MIGRATIONS[0][0]
        cursor.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (first_name, ts),
        )
        conn.commit()

        # run_all_migrations must not raise IntegrityError even though the row already exists
        run_all_migrations(cursor, conn)

        cursor.execute("SELECT COUNT(*) FROM schema_migrations")
        count = cursor.fetchone()[0]
        assert count == len(MIGRATIONS)

    def test_partial_completion_resumes_from_first_unapplied(self):
        """If only migrations 0..N-1 are recorded, the next run applies N..end."""
        conn, cursor = _make_db()

        # Manually seed only the first 5 migrations as applied
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        ts = datetime.now(timezone.utc).isoformat()
        for name, _ in MIGRATIONS[:5]:
            cursor.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, ts),
            )
        conn.commit()

        run_all_migrations(cursor, conn)

        cursor.execute("SELECT COUNT(*) FROM schema_migrations")
        count = cursor.fetchone()[0]
        assert count == len(MIGRATIONS)


class TestFailurePropagation:
    """Migration failures raise instead of being swallowed."""

    def test_unexpected_error_raises(self):
        conn, cursor = _make_db()

        def bad_migration(cursor, conn):
            raise RuntimeError("intentional test failure")

        with patch.object(_migrations_mod, "MIGRATIONS", [("bad", bad_migration)]):
            with pytest.raises(RuntimeError, match="intentional test failure"):
                run_all_migrations(cursor, conn)

    def test_unexpected_operational_error_raises(self):
        """OperationalError that is NOT 'no such table' must propagate."""
        conn, cursor = _make_db()

        def bad_migration(cursor, conn):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(_migrations_mod, "MIGRATIONS", [("locked", bad_migration)]):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                run_all_migrations(cursor, conn)

    def test_no_such_table_is_skipped_not_raised(self):
        """'no such table' OperationalError is skipped (fresh install behavior)."""
        conn, cursor = _make_db()

        def fresh_install_migration(cursor, conn):
            raise sqlite3.OperationalError("no such table: nonexistent_table")

        with patch.object(_migrations_mod, "MIGRATIONS", [("fresh", fresh_install_migration)]):
            # Should NOT raise
            run_all_migrations(cursor, conn)

    def test_fresh_install_skip_recorded_on_second_run(self):
        """Migrations skipped on first pass (no such table) are recorded when re-run after
        init_schema creates the tables — keeping the health check accurate on fresh installs."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        called = []

        def migration_needs_table(cursor, conn):
            called.append("run")
            # Raises on first call (table doesn't exist), succeeds on second
            if len(called) == 1:
                raise sqlite3.OperationalError("no such table: some_table")

        with patch.object(_migrations_mod, "MIGRATIONS", [("needs_table", migration_needs_table)]):
            # First pass: skipped, not recorded
            run_all_migrations(cursor, conn)
            cursor.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='needs_table'")
            assert cursor.fetchone()[0] == 0, "Should not be recorded after skip"

            # Simulate init_schema creating the table
            cursor.execute("CREATE TABLE some_table (id INTEGER PRIMARY KEY)")
            conn.commit()

            # Second pass: succeeds and is recorded
            run_all_migrations(cursor, conn)
            cursor.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='needs_table'")
            assert cursor.fetchone()[0] == 1, "Should be recorded after second pass succeeds"

    def test_failing_migration_logs_error(self):
        conn, cursor = _make_db()

        def bad_migration(cursor, conn):
            raise ValueError("schema corruption")

        with patch.object(_migrations_mod, "MIGRATIONS", [("bad", bad_migration)]):
            with patch.object(_migrations_mod.logger, "error") as mock_log:
                with pytest.raises(ValueError):
                    run_all_migrations(cursor, conn)

                mock_log.assert_called_once()
                call_args = mock_log.call_args[0]
                assert "bad" in call_args[1]

    def test_failed_migration_not_recorded_in_tracking_table(self):
        conn, cursor = _make_db()

        def bad_migration(cursor, conn):
            raise RuntimeError("fail")

        with patch.object(_migrations_mod, "MIGRATIONS", [("not_recorded", bad_migration)]):
            with pytest.raises(RuntimeError):
                run_all_migrations(cursor, conn)

        cursor.execute(
            "SELECT name FROM schema_migrations WHERE name = 'not_recorded'"
        )
        assert cursor.fetchone() is None

    def test_subsequent_migrations_not_run_after_failure(self):
        conn, cursor = _make_db()
        ran = []

        def fail(cursor, conn):
            raise RuntimeError("stop here")

        def should_not_run(cursor, conn):
            ran.append("should_not_run")

        with patch.object(_migrations_mod, "MIGRATIONS", [("failing", fail), ("after", should_not_run)]):
            with pytest.raises(RuntimeError):
                run_all_migrations(cursor, conn)

        assert ran == []


class TestMigrationsConstant:
    """MIGRATIONS list sanity checks."""

    def test_migrations_list_is_nonempty(self):
        assert len(MIGRATIONS) > 0

    def test_migration_names_are_unique(self):
        names = [name for name, _ in MIGRATIONS]
        assert len(names) == len(set(names)), "Duplicate migration names found"

    def test_all_entries_are_callable(self):
        for name, fn in MIGRATIONS:
            assert callable(fn), f"Migration '{name}' is not callable"

    def test_fan_out_id_migration_present(self):
        """Regression: execution_fan_out_id must be in MIGRATIONS (caused the original outage)."""
        names = [name for name, _ in MIGRATIONS]
        assert "execution_fan_out_id" in names

    def test_every_migrate_function_is_registered_and_vice_versa(self):
        """The registry is a bijection with the module's `_migrate*` functions (#3395).

        `run_all_migrations` runs only what `MIGRATIONS` lists, so a `_migrate*`
        function that is written but never registered never runs on an existing
        install. No schema diff can see the data-only case (an `UPDATE`
        backfill), and a column the function adds is masked on a fresh install
        whenever `init_schema` creates the table. This generalises
        `test_fan_out_id_migration_present` from one name to every name, in both
        directions: every `_migrate*` function defined in `db/migrations.py` is
        registered exactly once, and every registered function is one of them.

        Rule: a helper in `db/migrations.py` that is deliberately not a
        migration must not use the `_migrate` prefix.

        The functions are discovered from the loaded module, never from a
        hand-written list, and compared by object identity against the
        `MIGRATIONS` captured at import (other tests `patch.object` the module
        attribute). The non-vacuity floor keeps the test from passing on an
        empty or partial discovery, e.g. after `db/migrations.py` is split into
        a package: the discovered names must be non-empty and equal the
        top-level `def _migrate*` names parsed from the file's source.

        Other shape: none of the delivery brief's six cases (fresh install,
        empty data, another origin, flag off, clock ahead, restricted
        container) applies to a module-introspection test; the second case is
        the converse direction (a registered function that is not a
        `_migrate*` function of this module).
        """
        module_file = Path(_migrations_mod.__file__).resolve()
        defined = {
            fn
            for name, fn in inspect.getmembers(_migrations_mod, inspect.isfunction)
            if name.startswith("_migrate")
            and Path(fn.__code__.co_filename).resolve() == module_file
        }
        registered = {fn for _, fn in MIGRATIONS}

        # Non-vacuity: discovery is non-empty and agrees with a second,
        # independent derivation from the same file, so an empty or partial
        # discovery cannot make the bijection pass.
        source_names = {
            node.name
            for node in ast.parse(module_file.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_migrate")
        }
        assert defined, f"no _migrate* functions discovered in {module_file}"
        assert registered, "MIGRATIONS is empty"
        assert {fn.__name__ for fn in defined} == source_names, (
            f"introspection of {module_file} disagrees with its source: "
            f"discovered {len(defined)}, source defines {len(source_names)}"
        )

        problems = []
        unregistered = sorted(fn.__name__ for fn in defined - registered)
        if unregistered:
            problems.append(
                "_migrate* functions defined in db/migrations.py but not registered "
                f"in MIGRATIONS (existing installs never run them): {unregistered}"
            )
        foreign = sorted(
            f"{name} -> {fn.__module__}.{fn.__qualname__}"
            for name, fn in MIGRATIONS
            if fn not in defined
        )
        if foreign:
            problems.append(
                "MIGRATIONS entries whose function is not a _migrate* function "
                f"defined in db/migrations.py: {foreign}"
            )
        entries_by_fn = {}
        for name, fn in MIGRATIONS:
            entries_by_fn.setdefault(fn, []).append(name)
        repeated = sorted(
            f"{fn.__name__} <- {names}"
            for fn, names in entries_by_fn.items()
            if len(names) > 1
        )
        if repeated:
            problems.append(
                f"functions registered under more than one MIGRATIONS entry: {repeated}"
            )
        assert not problems, "\n".join(problems)


# --- helpers ---

def _make_tracking_fn(name, log, original_fn):
    def wrapper(cursor, conn):
        log.append(name)
        return original_fn(cursor, conn)
    return wrapper
