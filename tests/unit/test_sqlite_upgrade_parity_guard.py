"""Self-test for the SQLite upgrade-parity guard (#3395).

`scripts/ci/check_sqlite_upgrade_parity.py` builds an install with the PR's
merge-base code, upgrades it with the PR's code, and requires every object the
PR's `schema.py` declares to arrive, with `migration_health` at `(n, n, None)`.
The CI step runs it over real git history; these tests drive the same
comparison core over SYNTHETIC two-file backends (`db/schema.py` plus
`db/migrations.py` in `tmp_path`), so they need no git and no backend
dependencies, and run in the unit tier on every PR.

The five cases the guard exists for:
  * base `t(a)`, head `t(a, b)` with its migration registered -> pass;
  * the migration written but never registered -> gap, names `t.b`;
  * no migration at all, only the schema.py column -> gap, names `t.b`;
  * head adds a brand-new table only `init_schema` creates -> pass (the
    fresh-install shape of a new table must not read as drift);
  * a base tree that cannot build its install -> exit 2, never a pass.

Plus the failure modes around them: a head lifecycle that raises on the
upgraded install is a gap (exit 1), a registered migration left unrecorded is
a gap, and every git-side failure (no base/head, no repository, an
unresolvable merge-base) fails closed with exit 2. One test runs `main()`
end to end over a throwaway git repository.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_GUARD_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_sqlite_upgrade_parity.py"
)
_spec = importlib.util.spec_from_file_location("check_sqlite_upgrade_parity", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# A minimal copy of the real runner's contract: a ledger table, registered
# migrations applied in order, `no such table` swallowed as a fresh-install
# skip (recorded only once a later pass succeeds), and `migration_health`.
_RUNNER_SRC = '''
import sqlite3

def run_all_migrations(cursor, conn):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in cursor.execute("SELECT name FROM schema_migrations").fetchall()}
    for name, fn in MIGRATIONS:
        if name in applied:
            continue
        try:
            fn(cursor, conn)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                continue
            raise
        cursor.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, 'fixed')", (name,))
        conn.commit()

def migration_health(cursor):
    names = {row[0] for row in cursor.execute("SELECT name FROM schema_migrations").fetchall()}
    first = next((n for n, _ in MIGRATIONS if n not in names), None)
    return len(names), len(MIGRATIONS), first
'''

_ADD_B = '''
def _migrate_t_add_b(cursor, conn):
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(t)").fetchall()}
    if "b" not in cols:
        cursor.execute("ALTER TABLE t ADD COLUMN b TEXT")
'''


def _backend(root: Path, *, tables: list[str], migrations: str = "", registry: str = "") -> Path:
    """Write a two-file backend under `root` and return its directory."""
    db = root / "db"
    db.mkdir(parents=True)
    body = "\n".join(f"    cursor.execute({ddl!r})" for ddl in tables)
    (db / "schema.py").write_text(
        f"def init_schema(cursor, conn):\n{body}\n    conn.commit()\n"
    )
    (db / "migrations.py").write_text(
        f"{migrations}\nMIGRATIONS = [{registry}]\n{_RUNNER_SRC}"
    )
    return root


T_A = "CREATE TABLE IF NOT EXISTS t (a TEXT)"
T_AB = "CREATE TABLE IF NOT EXISTS t (a TEXT, b TEXT)"


def _check(tmp_path: Path, base: Path, head: Path) -> int:
    work = tmp_path / "work"
    work.mkdir()
    return guard.check_trees(base, head, work)


@pytest.fixture
def base_t_a(tmp_path: Path) -> Path:
    return _backend(tmp_path / "base", tables=[T_A])


# --- the five cases -----------------------------------------------------------


def test_registered_migration_passes(tmp_path, base_t_a, capsys):
    head = _backend(
        tmp_path / "head",
        tables=[T_AB],
        migrations=_ADD_B,
        registry='("t_add_b", _migrate_t_add_b)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_PASS
    out = capsys.readouterr().out
    assert "1 migration(s) newly recorded on upgrade" in out
    assert "migration_health = (1, 1, None)" in out


def test_written_but_unregistered_migration_is_a_gap(tmp_path, base_t_a, capsys):
    head = _backend(tmp_path / "head", tables=[T_AB], migrations=_ADD_B, registry="")
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    assert "column t.b:" in capsys.readouterr().err


def test_column_with_no_migration_at_all_is_a_gap(tmp_path, base_t_a, capsys):
    head = _backend(tmp_path / "head", tables=[T_AB])
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    err = capsys.readouterr().err
    assert "column t.b:" in err
    assert "never gets it on upgrade" in err


def test_new_init_schema_only_table_is_not_a_false_red(tmp_path, base_t_a, capsys):
    head = _backend(
        tmp_path / "head",
        tables=[T_A, "CREATE TABLE IF NOT EXISTS u (x TEXT)", "CREATE INDEX IF NOT EXISTS idx_u_x ON u(x)"],
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_PASS
    assert "declares 2 tables" in capsys.readouterr().out


def test_unbuildable_base_fails_closed(tmp_path, capsys):
    base = _backend(tmp_path / "base", tables=[T_A])
    (base / "db" / "schema.py").write_text("import a_module_that_is_gone\n")
    head = _backend(tmp_path / "head", tables=[T_A])
    assert _check(tmp_path, base, head) == guard.EXIT_CANNOT_RUN
    err = capsys.readouterr().err
    assert "CANNOT RUN" in err
    assert "a_module_that_is_gone" in err


# --- the failure modes around them ---------------------------------------------


def test_head_lifecycle_raising_on_the_install_is_a_gap(tmp_path, base_t_a, capsys):
    # The headline shape's loudest form: an index on a column no migration adds.
    head = _backend(tmp_path / "head", tables=[T_AB, "CREATE INDEX IF NOT EXISTS idx_t_b ON t(b)"])
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    err = capsys.readouterr().err
    assert "lifecycle" in err
    assert "no such column" in err


def test_registered_migration_left_unrecorded_is_a_gap(tmp_path, base_t_a, capsys):
    head = _backend(
        tmp_path / "head",
        tables=[T_A],
        migrations="def _migrate_ghost(cursor, conn):\n    cursor.execute('ALTER TABLE nowhere ADD COLUMN z TEXT')\n",
        registry='("ghost", _migrate_ghost)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    assert "'ghost' is registered but not recorded" in capsys.readouterr().err


def test_missing_backend_file_fails_closed(tmp_path, base_t_a):
    head = _backend(tmp_path / "head", tables=[T_A])
    (head / "db" / "migrations.py").unlink()
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_CANNOT_RUN


def test_no_base_or_head_fails_closed(monkeypatch, capsys):
    monkeypatch.delenv("PR_BASE_SHA", raising=False)
    monkeypatch.delenv("PR_HEAD_SHA", raising=False)
    assert guard.main(["check_sqlite_upgrade_parity.py"]) == guard.EXIT_CANNOT_RUN
    assert "Failing closed" in capsys.readouterr().err


def test_outside_a_repository_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert guard.main(["x", "a" * 40, "b" * 40]) == guard.EXIT_CANNOT_RUN


# --- main() over real git -------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_main_over_a_git_history(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    backend = repo / "src" / "backend"
    _backend(backend, tables=[T_A])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (backend / "db" / "schema.py").write_text(
        f"def init_schema(cursor, conn):\n    cursor.execute({T_AB!r})\n    conn.commit()\n"
    )
    _git(repo, "commit", "-q", "-am", "head adds t.b with no migration")
    head_sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)

    assert guard.main(["x", base_sha, head_sha]) == guard.EXIT_GAP
    assert "column t.b:" in capsys.readouterr().err

    assert guard.main(["x", base_sha, "f" * 40]) == guard.EXIT_CANNOT_RUN
    assert "merge-base" in capsys.readouterr().err
