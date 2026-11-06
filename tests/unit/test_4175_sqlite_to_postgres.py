"""The SQLite to PostgreSQL copy command (#4175, HOST-023).

Runs from the refused state: the backend will not boot beside an empty
PostgreSQL, so the command builds the schema itself and never imports
`database`.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch):
    if str(_BACKEND) not in sys.path:
        monkeypatch.syspath_prepend(str(_BACKEND))
    from db import sqlite_to_postgres

    return sqlite_to_postgres


def _build_head(path: Path) -> None:
    code = "import database"
    env = {
        "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "PYTHONPATH": str(_BACKEND),
        "REDIS_URL": "redis://a:b@localhost:6379/0", "SECRET_KEY": "test", "TRINITY_DB_PATH": str(path),
    }
    proc = subprocess.run([sys.executable, "-c", code], cwd=_BACKEND, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


class TestPrepareSnapshot:
    def test_live_file_is_untouched_and_pending_migration_runs(self, tool, tmp_path: Path) -> None:
        source = tmp_path / "trinity.db"
        _build_head(source)
        conn = sqlite3.connect(source)
        last = conn.execute("SELECT name FROM schema_migrations ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM schema_migrations WHERE name = ?", (last,))
        conn.commit()
        conn.close()
        before = source.read_bytes()

        snapshot = tool.prepare_snapshot(str(source))

        assert source.read_bytes() == before
        applied = {r[0] for r in sqlite3.connect(snapshot).execute("SELECT name FROM schema_migrations")}
        assert last in applied

    def test_pending_migration_after_the_runner_stops_the_copy(self, tool, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "trinity.db"
        _build_head(source)
        from db import migrations

        monkeypatch.setattr(migrations, "run_all_migrations", lambda cursor, conn: None)
        monkeypatch.setattr(migrations, "migration_health", lambda cursor: (1, 2, "never_applied"))
        with pytest.raises(SystemExit) as exc:
            tool.prepare_snapshot(str(source))
        assert exc.value.code == tool.EXIT_SCHEMA


class TestCoerce:
    @pytest.mark.parametrize("value,pg_type,expected", [
        (None, "integer", None),
        (True, "integer", 1),
        ("7", "bigint", 7),
        (3.0, "integer", 3),
        (5, "text", "5"),
        (True, "text", "1"),
        (b"abc", "text", "abc"),
        ("1.5", "double precision", 1.5),
    ])
    def test_lossless(self, tool, value, pg_type, expected) -> None:
        assert tool.coerce(value, pg_type, "ctx") == expected

    @pytest.mark.parametrize("value,pg_type", [(1.5, "integer"), ("x", "bigint"), ("x", "real"), (b"\xff", "text")])
    def test_lossy_fails_loudly(self, tool, value, pg_type) -> None:
        with pytest.raises(tool.CopyError, match="ctx"):
            tool.coerce(value, pg_type, "ctx")


@pytest.mark.parametrize("name,expected", [("db.engine", True), ("no_such_package.backend", False)])
def test_module_available_on_an_oss_image(tool, name, expected) -> None:
    """An OSS image has no `enterprise` package at all, and find_spec raises
    for a dotted name whose parent is absent."""
    assert tool._module_available(name) is expected


def test_refuses_a_sqlite_target(tool, tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "trinity.db"
    source.write_bytes(b"x")
    monkeypatch.setenv("TRINITY_DB_PATH", str(source))
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(SystemExit) as exc:
        tool.main()
    assert exc.value.code == tool.EXIT_USAGE


def test_missing_source_is_named(tool, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "absent.db"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    with pytest.raises(SystemExit) as exc:
        tool.main()
    assert exc.value.code == tool.EXIT_USAGE


# --- Real PostgreSQL --------------------------------------------------------

def test_nul_byte_names_the_table_and_row(tool) -> None:
    """PostgreSQL text cannot hold NUL, and psycopg2's ValueError names nothing."""
    with pytest.raises(tool.CopyError, match=r"agents\.name \(row ~3\)"):
        tool.coerce("a\x00b", "text", "agents.name (row ~3)")
    with pytest.raises(tool.CopyError, match="NUL"):
        tool.coerce(b"a\x00b", "text", "t.c")
    with pytest.raises(tool.CopyError, match="NUL"):
        tool.coerce("a\x00b", "jsonb", "t.c")
    assert tool.coerce(b"a\x00b", "bytea", "t.c").adapted == b"a\x00b"


def test_source_without_users_is_named_before_postgres_is_touched(tool, tmp_path: Path, monkeypatch, capsys) -> None:
    """A copy of it would stay refused at boot, and a re-run would exit 4."""
    source = tmp_path / "trinity.db"
    _build_head(source)
    monkeypatch.setenv("TRINITY_DB_PATH", str(source))
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@nothing-listens-here:5432/t")
    with pytest.raises(SystemExit) as exc:
        tool.main()
    assert exc.value.code == tool.EXIT_USAGE
    assert "no users" in capsys.readouterr().err


def test_env_py_survives_percent_in_password(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Executes migrations/env.py through Alembic's offline mode."""
    from alembic import command
    from db import alembic_runner

    monkeypatch.setenv("DATABASE_URL", "postgresql://trinity:p%40ss@postgres:5432/trinity")
    command.stamp(alembic_runner._config(), "head", sql=True)
    assert "alembic_version" in capsys.readouterr().out


def test_enterprise_percent_error_does_not_print_the_password(tool, monkeypatch, capsys) -> None:
    secret = "postgresql://trinity:p%40ss@postgres:5432/trinity"

    def boom():
        raise ValueError(f"invalid interpolation syntax in '{secret}' at position 20")

    monkeypatch.setitem(sys.modules, "enterprise.backend._alembic_runner", SimpleNamespace(upgrade_to_head=boom))
    monkeypatch.setattr(tool, "_module_available", lambda name: True)
    # Patch the module `build_postgres_schema` will import (the sys.modules
    # entry), not the `db` package attribute: a test that pops `db.*` from
    # sys.modules leaves that attribute on a stale copy, and the string form
    # then patches the copy while the code imports a fresh, unpatched module.
    monkeypatch.setattr(importlib.import_module("db.alembic_runner"), "upgrade_to_head",
                        lambda **kwargs: None)
    with pytest.raises(SystemExit) as exc:
        tool.build_postgres_schema()
    assert exc.value.code == tool.EXIT_USAGE
    captured = capsys.readouterr()
    assert "p%40ss" not in captured.out + captured.err


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch):
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL not set")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    monkeypatch.setenv("DATABASE_URL", url)
    yield engine
    engine.dispose()


@pytest.mark.requires_postgres
class TestEndToEnd:
    def _seed(self, source: Path) -> None:
        _build_head(source)
        conn = sqlite3.connect(source)
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(users)")}
        values = {"username": "alice", "role": "admin", "created_at": "2026-01-01T00:00:00Z",
                  "updated_at": "2026-01-01T00:00:00Z"}
        for name, col in columns.items():
            if name not in values and col[3] and col[4] is None and not col[5]:
                values[name] = "x"
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO users ({names}) VALUES ({marks})", list(values.values()))
        conn.commit()
        conn.close()

    def test_copy_from_the_refused_state(self, tool, pg, tmp_path: Path, monkeypatch, capsys) -> None:
        source = tmp_path / "trinity.db"
        self._seed(source)
        monkeypatch.setenv("TRINITY_DB_PATH", str(source))
        monkeypatch.setenv("TRINITY_REQUIRE_POSTGRES", "1")

        tool.main()

        assert (tmp_path / "trinity.db.copied-to-postgres").exists(), "start.sh reads this"
        expected = sqlite3.connect(source).execute("SELECT COUNT(*) FROM users").fetchone()[0]
        with pg.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == expected
            assert conn.execute(text("SELECT username FROM users WHERE username='alice'")).scalar() == "alice"
            assert conn.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar() == 1
            max_id = conn.execute(text("SELECT MAX(id) FROM users")).scalar()
            new_id = conn.execute(text("SELECT nextval(pg_get_serial_sequence('users', 'id'))")).scalar()
        assert new_id > max_id, "the next insert must not collide with a copied id"
        assert "postgresql://" not in capsys.readouterr().out, "the URL carries the password"

        # The copied database now boots past the refusal.
        from db import alembic_runner

        alembic_runner.upgrade_to_head()

    def test_second_run_refuses_a_populated_target(self, tool, pg, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "trinity.db"
        self._seed(source)
        monkeypatch.setenv("TRINITY_DB_PATH", str(source))
        tool.main()
        with pytest.raises(SystemExit) as exc:
            tool.main()
        assert exc.value.code == tool.EXIT_LOAD
        assert (tmp_path / "trinity.db.copied-to-postgres").exists()

    def test_sqlite_table_with_rows_and_no_postgres_home_stops_the_copy(self, tool, pg, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "trinity.db"
        self._seed(source)
        conn = sqlite3.connect(source)
        conn.execute("CREATE TABLE only_in_sqlite (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO only_in_sqlite VALUES (1)")
        conn.commit()
        conn.close()
        monkeypatch.setenv("TRINITY_DB_PATH", str(source))
        with pytest.raises(SystemExit) as exc:
            tool.main()
        assert exc.value.code == tool.EXIT_SCHEMA
        assert not (tmp_path / "trinity.db.copied-to-postgres").exists()
        with pg.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == 0

    def test_target_with_rows_that_do_not_conflict_is_refused(self, tool, pg, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "trinity.db"
        self._seed(source)
        monkeypatch.setenv("TRINITY_DB_PATH", str(source))
        tool.build_postgres_schema()
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO system_settings (key, value, updated_at) "
                "VALUES ('already-here', 'x', '2026-01-01T00:00:00Z')"))
        with pytest.raises(SystemExit) as exc:
            tool.main()
        assert exc.value.code == tool.EXIT_LOAD
        assert not (tmp_path / "trinity.db.copied-to-postgres").exists()

    def test_rowcount_mismatch_commits_nothing(self, tool, pg, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "trinity.db"
        self._seed(source)
        monkeypatch.setenv("TRINITY_DB_PATH", str(source))
        real = tool._reset_sequences

        def lose_a_row(cur) -> None:
            cur.execute("DELETE FROM users WHERE username='alice'")
            real(cur)

        monkeypatch.setattr(tool, "_reset_sequences", lose_a_row)
        with pytest.raises(SystemExit) as exc:
            tool.main()
        assert exc.value.code == tool.EXIT_MISMATCH
        assert not (tmp_path / "trinity.db.copied-to-postgres").exists()

        monkeypatch.setattr(tool, "_reset_sequences", real)
        tool.main()  # nothing was committed, so a re-run succeeds
