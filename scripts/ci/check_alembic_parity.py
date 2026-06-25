#!/usr/bin/env python3
"""Cross-track schema-parity guard: SQLite DDL change ⇒ Alembic revision (#1342).

The `schema-parity` pytest suite only validates the **SQLite** track
(`db/migrations.py` ↔ `db/schema.py`). Per Architectural Invariant #3, every
schema change must ALSO ship a new **Alembic** revision under
`src/backend/migrations/versions/` — otherwise PostgreSQL breaks, because
`init_database()` runs `alembic_runner.upgrade_to_head()`, which applies
revision files only (it does not autogenerate from `tables.py`). With no guard,
a SQLite-only schema change merges green yet PG-broken.

Heuristic (documented for the false-positive guard, AC #4):

  A PR that registers a NEW SQLite migration carrying schema DDL MUST also ADD
  a net-new file under `src/backend/migrations/versions/`. Two conjuncts:

  (1) NEW MIGRATION — an added line in `db/migrations.py` registers a net-new
      entry in the `MIGRATIONS` list: `("<name>", _migrate_<fn>),`. This is the
      unambiguous "I'm shipping a schema change" act; runner refactors, lock
      changes, and table-rebuild helpers do NOT add an entry.
  (2) DDL — an *added* (`+`), *non-comment* diff line in
      `db/{migrations,schema,tables}.py` carries a schema keyword:
        - SQL (migrations.py / schema.py): CREATE TABLE/INDEX/TRIGGER,
          ALTER TABLE, ADD/DROP COLUMN, RENAME COLUMN/TO, DROP TABLE/INDEX.
        - SQLAlchemy (tables.py): Column(/Table(/Index(/UniqueConstraint(/
          ForeignKey(/PrimaryKeyConstraint(.

  Requiring BOTH is what keeps the false-positive rate down — it does NOT trip on:
    - comment / docstring-only edits (comment lines are skipped),
    - data-only or down migrations (a new entry, but no DDL keyword),
    - migration-runner refactors / table REBUILDS that re-emit CREATE TABLE for
      an *existing* table without registering a new migration (conjunct 1 false).

  A real column/table add always registers a new `MIGRATIONS` entry AND emits
  `ADD COLUMN` / `CREATE TABLE` (migrations.py) or a new `Column(`/`Table(`
  (tables.py), so it is caught. A schema.py-only DDL edit that forgets the
  SQLite migration is caught separately (red) by the existing schema-parity
  pytest, which then forces the migration — and this guard then forces the
  revision.

Usage:
    check_alembic_parity.py <base_sha> <head_sha>
    # or, with no args, reads PR_BASE_SHA / PR_HEAD_SHA from the environment.

Exit codes: 0 = pass (no DDL, or DDL + revision present), 1 = missing revision,
2 = usage / git error. Pure stdlib; no third-party deps.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Files whose DDL changes require a paired Alembic revision.
SQL_DDL_FILES = ("src/backend/db/migrations.py", "src/backend/db/schema.py")
SQLALCHEMY_DDL_FILE = "src/backend/db/tables.py"
WATCHED_FILES = (*SQL_DDL_FILES, SQLALCHEMY_DDL_FILE)

# Net-new revision target.
REVISION_DIR = "src/backend/migrations/versions/"

# Raw-SQL DDL keywords (case-insensitive). Anchored on word boundaries so e.g.
# "created_at" does not match "CREATE".
_SQL_DDL = re.compile(
    r"\b("
    r"CREATE\s+(?:UNIQUE\s+)?INDEX"
    r"|CREATE\s+TABLE"
    r"|CREATE\s+TRIGGER"
    r"|ALTER\s+TABLE"
    r"|ADD\s+COLUMN"
    r"|DROP\s+COLUMN"
    r"|DROP\s+TABLE"
    r"|DROP\s+INDEX"
    r"|RENAME\s+COLUMN"
    r"|RENAME\s+TO"
    r")\b",
    re.IGNORECASE,
)

# SQLAlchemy schema constructs in tables.py (the Alembic MetaData source).
_SQLALCHEMY_DDL = re.compile(
    r"\b(Column|Table|Index|UniqueConstraint|ForeignKey|PrimaryKeyConstraint)\s*\("
)

# A net-new entry in the `MIGRATIONS = [...]` list in migrations.py:
#   ("agent_loops_tables", _migrate_agent_loops_tables),
# Trinity registers every migration here and names the fn `_migrate_*`.
_MIGRATIONS_ENTRY = re.compile(r"""\(\s*["'][^"']+["']\s*,\s*_migrate_\w+""")


def _patterns_for(path: str) -> re.Pattern | None:
    if path == SQLALCHEMY_DDL_FILE:
        return _SQLALCHEMY_DDL
    if path in SQL_DDL_FILES:
        return _SQL_DDL
    return None


def _is_comment(content: str) -> bool:
    """A line we treat as a comment for DDL-detection purposes."""
    stripped = content.lstrip()
    return stripped.startswith("#")


def iter_added_lines(diff_text: str):
    """Yield (path, content) for every added line of a unified diff.

    Tracks the current file from `+++ b/<path>` headers; skips the `+++`/`---`
    headers themselves. Robust to multi-file diffs.
    """
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                # strip the "b/" prefix git uses
                current = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current is not None:
                yield current, line[1:]


def find_ddl_evidence(diff_text: str) -> list[str]:
    """Return human-readable evidence strings for added DDL lines."""
    evidence: list[str] = []
    for path, content in iter_added_lines(diff_text):
        pattern = _patterns_for(path)
        if pattern is None or _is_comment(content):
            continue
        match = pattern.search(content)
        if match:
            evidence.append(f"{path}: + {content.strip()[:120]}")
    return evidence


def diff_has_ddl(diff_text: str) -> bool:
    return bool(find_ddl_evidence(diff_text))


def added_migration_entries(diff_text: str) -> list[str]:
    """Net-new `MIGRATIONS` list entries added in db/migrations.py."""
    entries: list[str] = []
    for path, content in iter_added_lines(diff_text):
        if path != "src/backend/db/migrations.py" or _is_comment(content):
            continue
        if _MIGRATIONS_ENTRY.search(content):
            entries.append(content.strip())
    return entries


def added_revision_files(name_status_lines: list[str]) -> list[str]:
    """Net-new (`A`) files under the Alembic versions dir, excluding __init__."""
    found: list[str] = []
    for raw in name_status_lines:
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if not status.startswith("A"):
            continue
        if path.startswith(REVISION_DIR) and path.endswith(".py") and not path.endswith("__init__.py"):
            found.append(path)
    return found


def is_schema_change(ddl_diff_text: str) -> bool:
    """A new SQLite migration registering schema DDL (conjuncts 1 AND 2)."""
    return bool(added_migration_entries(ddl_diff_text)) and diff_has_ddl(ddl_diff_text)


def would_block(ddl_diff_text: str, name_status_lines: list[str]) -> bool:
    """Core decision: a DDL-bearing new migration with no net-new revision."""
    return is_schema_change(ddl_diff_text) and not added_revision_files(name_status_lines)


def _git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 2 else os.environ.get("PR_BASE_SHA", "")
    head = argv[2] if len(argv) > 2 else os.environ.get("PR_HEAD_SHA", "")
    null_sha = "0" * 40
    if not base or not head or base == null_sha:
        print("alembic-parity: no PR base/head available — skipping (pass).")
        return 0

    rng = f"{base}...{head}"
    try:
        ddl_diff = _git(["diff", rng, "--", *WATCHED_FILES])
        name_status = _git(["diff", "--name-status", rng]).splitlines()
    except subprocess.CalledProcessError as exc:
        print(f"alembic-parity: git diff failed: {exc.stderr}", file=sys.stderr)
        return 2

    new_migrations = added_migration_entries(ddl_diff)
    evidence = find_ddl_evidence(ddl_diff)
    if not new_migrations:
        print(
            "alembic-parity: no net-new MIGRATIONS entry — runner refactor / rebuild /\n"
            "non-schema edit, nothing to guard (pass)."
        )
        return 0
    if not evidence:
        print(
            "alembic-parity: new migration is data-only (no DDL keyword) — "
            "no Alembic revision required (pass)."
        )
        return 0

    revisions = added_revision_files(name_status)
    print("alembic-parity: new schema migration detected in this PR:")
    for entry in new_migrations[:10]:
        print(f"  + {entry}")
    print("alembic-parity: carrying DDL:")
    for line in evidence[:20]:
        print(f"  • {line}")
    if revisions:
        print("alembic-parity: paired Alembic revision(s) added:")
        for rev in revisions:
            print(f"  ✓ {rev}")
        print("alembic-parity: PASS — both tracks updated.")
        return 0

    print(
        "\nalembic-parity: FAIL — this PR adds schema DDL on the SQLite track but\n"
        f"adds no new Alembic revision under {REVISION_DIR}\n"
        "PostgreSQL applies revision files only (it does NOT autogenerate from\n"
        "tables.py), so this change would break PG. Add a revision per\n"
        "Architectural Invariant #3 (see docs/migrations/SQLITE_TO_POSTGRES.md).\n"
        "If this is a data-only/down migration or a non-DDL edit, ensure the\n"
        "added lines carry no DDL keyword (see the heuristic in this script).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
