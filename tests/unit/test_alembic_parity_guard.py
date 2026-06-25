"""Unit tests for the cross-track Alembic parity guard (#1342).

The guard fails a PR that adds SQLite-track schema DDL without a paired net-new
Alembic revision. These tests exercise the pure decision functions against
synthetic unified diffs — the same shapes the CI step feeds from `git diff` —
covering the acceptance criteria:

  * a SQLite-only schema change FAILS,
  * a properly dual-tracked change PASSES,
  * comment / data-only / down-migration edits do NOT trip it (false-positive guard).
"""
import importlib.util
from pathlib import Path

import pytest

# Load the guard module from scripts/ci/ (not an importable package).
_GUARD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_alembic_parity.py"
_spec = importlib.util.spec_from_file_location("check_alembic_parity", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _diff(path: str, added: list[str]) -> str:
    """Build a minimal unified diff adding `added` lines to `path`."""
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,0 +1,%d @@\n" % len(added)
    ) + body


NEW_REVISION = "A\tsrc/backend/migrations/versions/0005_agent_loops_new_cols.py"

# A net-new MIGRATIONS list entry — the "I'm shipping a schema change" signal.
MIGRATIONS_ENTRY = '    ("agent_loops_new_cols", _migrate_agent_loops_new_cols),'


def _migration_diff(ddl_lines: list[str], *, entry: bool = True) -> str:
    """A migrations.py diff: optional new MIGRATIONS entry + DDL body lines."""
    added = ([MIGRATIONS_ENTRY] if entry else []) + ddl_lines
    return _diff("src/backend/db/migrations.py", added)


# --- DDL detection: positives -------------------------------------------------

def test_add_column_in_migrations_is_ddl():
    diff = _diff(
        "src/backend/db/migrations.py",
        ['        cursor.execute("ALTER TABLE agent_loops ADD COLUMN foo TEXT")'],
    )
    assert guard.diff_has_ddl(diff) is True


def test_create_table_in_schema_is_ddl():
    diff = _diff(
        "src/backend/db/schema.py",
        ["    CREATE TABLE widgets ("],
    )
    assert guard.diff_has_ddl(diff) is True


def test_create_index_in_schema_is_ddl():
    diff = _diff(
        "src/backend/db/schema.py",
        ["    CREATE INDEX idx_widgets_name ON widgets(name);"],
    )
    assert guard.diff_has_ddl(diff) is True


def test_new_column_in_tables_metadata_is_ddl():
    diff = _diff(
        "src/backend/db/tables.py",
        ['    Column("foo", Text),'],
    )
    assert guard.diff_has_ddl(diff) is True


def test_new_table_in_tables_metadata_is_ddl():
    diff = _diff(
        "src/backend/db/tables.py",
        ["widgets = Table("],
    )
    assert guard.diff_has_ddl(diff) is True


# --- DDL detection: negatives (false-positive guard) --------------------------

def test_comment_mentioning_alter_table_is_not_ddl():
    diff = _diff(
        "src/backend/db/migrations.py",
        ["    # this migration would ALTER TABLE if it were not a no-op"],
    )
    assert guard.diff_has_ddl(diff) is False


def test_data_only_migration_is_not_ddl():
    diff = _diff(
        "src/backend/db/migrations.py",
        ['        cursor.execute("UPDATE agent_ownership SET require_email = 1")'],
    )
    assert guard.diff_has_ddl(diff) is False


def test_column_body_line_without_keyword_is_not_ddl():
    # A column row inside an existing CREATE TABLE string (schema.py) carries no
    # DDL keyword on its own; the paired migrations.py ADD COLUMN is the signal.
    diff = _diff("src/backend/db/schema.py", ["    new_col TEXT,"])
    assert guard.diff_has_ddl(diff) is False


def test_created_at_does_not_match_create():
    diff = _diff("src/backend/db/schema.py", ["    created_at TEXT NOT NULL,"])
    assert guard.diff_has_ddl(diff) is False


def test_unrelated_file_ddl_is_ignored():
    # DDL-looking text in a non-watched file must not trip the guard.
    diff = _diff("src/backend/services/foo.py", ["    sql = 'ALTER TABLE x ADD COLUMN y'"])
    assert guard.diff_has_ddl(diff) is False


# --- revision detection -------------------------------------------------------

def test_added_revision_detected():
    assert guard.added_revision_files([NEW_REVISION]) == [
        "src/backend/migrations/versions/0005_agent_loops_new_cols.py"
    ]


def test_modified_revision_does_not_count():
    line = "M\tsrc/backend/migrations/versions/0004_agent_ownership_voice_name.py"
    assert guard.added_revision_files([line]) == []


def test_versions_init_excluded():
    line = "A\tsrc/backend/migrations/versions/__init__.py"
    assert guard.added_revision_files([line]) == []


def test_rename_status_with_score_handled():
    # git emits e.g. "R100\told\tnew" — last column is the path; not an add.
    line = "R100\tsrc/backend/migrations/versions/0004_x.py\tsrc/backend/migrations/versions/0004_y.py"
    assert guard.added_revision_files([line]) == []


# --- MIGRATIONS-entry detection (conjunct 1) ----------------------------------

def test_new_migrations_entry_detected():
    diff = _diff("src/backend/db/migrations.py", [MIGRATIONS_ENTRY])
    assert guard.added_migration_entries(diff) == [MIGRATIONS_ENTRY.strip()]


def test_commented_migrations_entry_not_detected():
    diff = _diff("src/backend/db/migrations.py", ['    # ("old", _migrate_old),'])
    assert guard.added_migration_entries(diff) == []


def test_entry_in_other_file_not_detected():
    diff = _diff("src/backend/db/schema.py", ['    ("x", _migrate_x),'])
    assert guard.added_migration_entries(diff) == []


# --- end-to-end decision (acceptance criteria) --------------------------------

def test_sqlite_only_schema_change_blocks():
    """AC: a SQLite-only schema change (new migration + DDL) FAILS the check."""
    ddl = _migration_diff(
        ['        cursor.execute("ALTER TABLE agent_loops ADD COLUMN bar TEXT")']
    )
    name_status = ["M\tsrc/backend/db/migrations.py", "M\tsrc/backend/db/schema.py"]
    assert guard.would_block(ddl, name_status) is True


def test_dual_tracked_change_passes():
    """AC: a properly dual-tracked change PASSES."""
    ddl = _migration_diff(
        ['        cursor.execute("ALTER TABLE agent_loops ADD COLUMN bar TEXT")']
    )
    name_status = [
        "M\tsrc/backend/db/migrations.py",
        "M\tsrc/backend/db/schema.py",
        "M\tsrc/backend/db/tables.py",
        NEW_REVISION,
    ]
    assert guard.would_block(ddl, name_status) is False


def test_comment_only_change_passes():
    """AC: comment edits to watched files do not trip the guard."""
    ddl = _diff("src/backend/db/migrations.py", ["    # tidy up a docstring; ALTER TABLE mention"])
    name_status = ["M\tsrc/backend/db/migrations.py"]
    assert guard.would_block(ddl, name_status) is False


def test_data_only_migration_passes_without_revision():
    """AC: data-only migration (new entry, no DDL keyword) needs no revision."""
    ddl = _migration_diff(
        ['        cursor.execute("DELETE FROM idempotency_keys WHERE created_at < ?")']
    )
    name_status = ["M\tsrc/backend/db/migrations.py"]
    assert guard.would_block(ddl, name_status) is False


def test_runner_refactor_with_rebuild_ddl_passes():
    """Real #1263 class: a migration-runner refactor that re-emits CREATE TABLE
    for an existing table (table rebuild) but registers NO new MIGRATIONS entry
    must NOT trip the guard, even though it carries DDL keywords."""
    ddl = _migration_diff(
        [
            "            CREATE TABLE agent_sharing_new (",
            '        cursor.execute(f"ALTER TABLE {new} RENAME TO {table}")',
            '                "CREATE INDEX IF NOT EXISTS idx_agent_skills_agent ON agent_skills(agent_name)",',
        ],
        entry=False,  # the refactor adds no new MIGRATIONS entry
    )
    name_status = ["M\tsrc/backend/db/migrations.py"]
    assert guard.is_schema_change(ddl) is False
    assert guard.would_block(ddl, name_status) is False


# --- diff parsing -------------------------------------------------------------

def test_iter_added_lines_tracks_file_and_skips_headers():
    diff = (
        _diff("src/backend/db/tables.py", ['    Column("a", Text),'])
        + _diff("src/backend/db/schema.py", ["    CREATE TABLE z ("])
    )
    pairs = list(guard.iter_added_lines(diff))
    files = {p for p, _ in pairs}
    assert files == {"src/backend/db/tables.py", "src/backend/db/schema.py"}
    # the `+++ b/...` header lines must not be yielded as added content
    assert all(not c.startswith("++") for _, c in pairs)


def test_evidence_lists_offending_lines():
    diff = _diff(
        "src/backend/db/migrations.py",
        ['        cursor.execute("ALTER TABLE agent_loops ADD COLUMN bar TEXT")'],
    )
    evidence = guard.find_ddl_evidence(diff)
    assert len(evidence) == 1
    assert "ADD COLUMN" in evidence[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
