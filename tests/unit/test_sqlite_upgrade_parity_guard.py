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
upgraded install is a gap (exit 1); an index, trigger or table a migration
drops after `init_schema` is a gap; a registered migration left unrecorded, a
renamed migration and a duplicate registration are gaps; the restated
lifecycle is pinned (migrations before `init_schema`, the second pass, and a
`sqlite3.Row` connection as production opens it); a head that does not import, and every git-side failure (usage, no
base/head, no repository, an unresolvable merge-base), fail closed with exit 2.
One test runs `main()` end to end over a throwaway git repository. A broken
merge-base (a broken `dev`) is walked back over first-parent ancestors, bounded,
to the newest that builds, so the PR that repairs `dev` is not blocked; a gap
over that longer range is still a gap, and an exhausted walk exits 2.
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


# --- objects a migration drops after init_schema ---------------------------------
#
# A migration that targets a table only the head's init_schema creates is skipped
# on the first pass (`no such table`) and runs on the second, AFTER init_schema.
# If it rebuilds or drops something there, the install never gets it back.

_U = "CREATE TABLE IF NOT EXISTS u (x TEXT)"


def _second_pass(body: str) -> str:
    return (
        "def _migrate_touch_u(cursor, conn):\n"
        "    cursor.execute('SELECT 1 FROM u')\n"
        f"    cursor.execute({body!r})\n"
    )


@pytest.mark.parametrize(
    ("extra_ddl", "drop", "named"),
    [
        ("CREATE INDEX IF NOT EXISTS idx_u_x ON u(x)", "DROP INDEX idx_u_x", "index idx_u_x:"),
        (
            "CREATE TRIGGER IF NOT EXISTS trg_u AFTER INSERT ON u BEGIN SELECT 1; END",
            "DROP TRIGGER trg_u",
            "trigger trg_u:",
        ),
        (None, "DROP TABLE u", "table u:"),
    ],
    ids=["index", "trigger", "table"],
)
def test_object_dropped_after_init_schema_is_a_gap(tmp_path, base_t_a, capsys, extra_ddl, drop, named):
    tables = [T_A, _U] + ([extra_ddl] if extra_ddl else [])
    head = _backend(
        tmp_path / "head",
        tables=tables,
        migrations=_second_pass(drop),
        registry='("touch_u", _migrate_touch_u)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    assert named in capsys.readouterr().err


# --- the ledger -------------------------------------------------------------------

_NOOP = "def _migrate_noop(cursor, conn):\n    pass\n"


def test_renamed_migration_is_a_gap(tmp_path, capsys):
    base = _backend(tmp_path / "base", tables=[T_A], migrations=_NOOP, registry='("old_name", _migrate_noop)')
    head = _backend(tmp_path / "head", tables=[T_A], migrations=_NOOP, registry='("new_name", _migrate_noop)')
    assert _check(tmp_path, base, head) == guard.EXIT_GAP
    err = capsys.readouterr().err
    assert "no longer registers: old_name" in err


def test_duplicate_registration_is_a_gap(tmp_path, base_t_a, capsys):
    # One ledger row for two entries: (1, 2, None), which /health answers with 503.
    head = _backend(
        tmp_path / "head",
        tables=[T_A],
        migrations=_NOOP,
        registry='("noop", _migrate_noop), ("noop", _migrate_noop)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_GAP
    assert "migration_health is (1, 2, None)" in capsys.readouterr().err


# --- the lifecycle order the guard restates -----------------------------------------


def test_migrations_run_before_init_schema(tmp_path, base_t_a):
    # init_schema indexes a column only the migration adds: running init_schema
    # first would raise `no such column` on the existing table.
    head = _backend(
        tmp_path / "head",
        tables=[T_AB, "CREATE INDEX IF NOT EXISTS idx_t_b ON t(b)"],
        migrations=_ADD_B,
        registry='("t_add_b", _migrate_t_add_b)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_PASS


def test_second_migration_pass_records_a_migration_on_a_new_table(tmp_path, base_t_a, capsys):
    # The migration needs `u`, which only the head's init_schema creates: it is
    # skipped on the first pass and recorded only by the second.
    head = _backend(
        tmp_path / "head",
        tables=[T_A, "CREATE TABLE IF NOT EXISTS u (x TEXT, y TEXT)"],
        migrations=(
            "def _migrate_u_y(cursor, conn):\n"
            "    cols = {r[1] for r in cursor.execute('PRAGMA table_info(u)').fetchall()}\n"
            "    if 'y' not in cols:\n"
            "        cursor.execute('ALTER TABLE u ADD COLUMN y TEXT')\n"
        ),
        registry='("u_y", _migrate_u_y)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_PASS
    assert "migration_health = (1, 1, None)" in capsys.readouterr().out


def test_connection_yields_rows_by_name_as_in_production(tmp_path, base_t_a):
    # db/connection.py::get_db_connection sets row_factory = sqlite3.Row, and a
    # migration may index a row by column name. On a plain tuple connection the
    # guard would report that migration as a boot failure.
    head = _backend(
        tmp_path / "head",
        tables=[T_AB],
        migrations=(
            "def _migrate_t_add_b(cursor, conn):\n"
            "    cols = {r['name'] for r in cursor.execute('PRAGMA table_info(t)').fetchall()}\n"
            "    if 'b' not in cols:\n"
            "        cursor.execute('ALTER TABLE t ADD COLUMN b TEXT')\n"
        ),
        registry='("t_add_b", _migrate_t_add_b)',
    )
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_PASS


# --- environment ------------------------------------------------------------------


def test_head_that_does_not_import_cannot_run(tmp_path, base_t_a, capsys):
    head = _backend(tmp_path / "head", tables=[T_A])
    (head / "db" / "migrations.py").write_text("import a_module_that_is_gone\n")
    assert _check(tmp_path, base_t_a, head) == guard.EXIT_CANNOT_RUN
    assert "does not import" in capsys.readouterr().err


def test_relative_paths(tmp_path, monkeypatch):
    _backend(tmp_path / "base", tables=[T_A])
    _backend(tmp_path / "head", tables=[T_AB], migrations=_ADD_B, registry='("t_add_b", _migrate_t_add_b)')
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path)
    assert guard.check_trees(Path("base"), Path("head"), Path("work")) == guard.EXIT_PASS


def test_one_positional_argument_is_a_usage_error(capsys):
    assert guard.main(["x", "a" * 40]) == guard.EXIT_CANNOT_RUN
    assert "usage" in capsys.readouterr().err


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


# --- a broken merge-base (a broken `dev`) -------------------------------------------
#
# The PR that repairs a broken `dev` has the broken commit as its merge-base.
# The guard walks back first-parent ancestors, bounded, to the newest one that
# builds, and upgrades from it; exit 2 only when none within the bound builds.

_BROKEN = "def _migrate_broken(cursor, conn):\n    raise RuntimeError('dev is broken')\n"


def _commit_tree(repo: Path, message: str, **backend: object) -> str:
    db = repo / "src" / "backend" / "db"
    if db.exists():
        for f in db.iterdir():
            f.unlink()
        db.rmdir()
    _backend(repo / "src" / "backend", **backend)  # type: ignore[arg-type]
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


_BROKEN_TREE = {"tables": [T_A], "migrations": _BROKEN, "registry": '("broken", _migrate_broken)'}


def test_broken_merge_base_walks_back_to_the_newest_ancestor_that_builds(tmp_path, monkeypatch, capsys):
    repo = _new_repo(tmp_path)
    good = _commit_tree(repo, "good", tables=[T_A])
    broken = _commit_tree(repo, "A: dev breaks", **_BROKEN_TREE)
    fix = _commit_tree(repo, "B: the fix PR", tables=[T_A])
    monkeypatch.chdir(repo)

    assert guard.main(["x", broken, fix]) == guard.EXIT_PASS
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert f"skipped: {broken[:12]}" in captured.err
    assert "dev is broken" in captured.err
    assert f"first-parent ancestor {good[:12]}" in captured.out


def test_walk_back_still_finds_a_gap_over_the_longer_range(tmp_path, monkeypatch, capsys):
    # Not fail-open: the fix PR adds t.b with no migration; upgrading from the
    # older ancestor still has to deliver it.
    repo = _new_repo(tmp_path)
    _commit_tree(repo, "good", tables=[T_A])
    broken = _commit_tree(repo, "A: dev breaks", **_BROKEN_TREE)
    fix = _commit_tree(repo, "B: fix plus an unmigrated column", tables=[T_AB])
    monkeypatch.chdir(repo)

    assert guard.main(["x", broken, fix]) == guard.EXIT_GAP
    assert "column t.b:" in capsys.readouterr().err


def test_walk_back_is_bounded_and_fails_closed_when_exhausted(tmp_path, monkeypatch, capsys):
    repo = _new_repo(tmp_path)
    good = _commit_tree(repo, "good", tables=[T_A])
    broken1 = _commit_tree(repo, "dev breaks", **_BROKEN_TREE)
    broken2 = _commit_tree(repo, "dev stays broken", **_BROKEN_TREE)
    fix = _commit_tree(repo, "the fix PR", tables=[T_A])
    monkeypatch.chdir(repo)

    monkeypatch.setattr(guard, "MAX_BASE_WALKBACK", 1)
    assert guard.main(["x", broken2, fix]) == guard.EXIT_CANNOT_RUN
    err = capsys.readouterr().err
    assert "CANNOT RUN" in err
    assert broken2[:12] in err and broken1[:12] in err
    assert good[:12] not in err

    monkeypatch.setattr(guard, "MAX_BASE_WALKBACK", 2)
    assert guard.main(["x", broken2, fix]) == guard.EXIT_PASS


def test_broken_root_with_no_ancestor_fails_closed(tmp_path, monkeypatch, capsys):
    repo = _new_repo(tmp_path)
    broken = _commit_tree(repo, "broken root", **_BROKEN_TREE)
    fix = _commit_tree(repo, "the fix PR", tables=[T_A])
    monkeypatch.chdir(repo)

    assert guard.main(["x", broken, fix]) == guard.EXIT_CANNOT_RUN
    assert "CANNOT RUN" in capsys.readouterr().err
