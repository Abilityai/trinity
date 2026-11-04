"""PostgreSQL is the default and SQLite is refused at boot (#4175, HOST-023).

An existing install with a `trinity.db` must never come up on an empty
PostgreSQL. Compose's default URL hides the "no DATABASE_URL" state from the
backend, so the refusal lives where every PostgreSQL boot passes through.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
_SRC = _ROOT / "src"
_GUIDE = "docs/migrations/SQLITE_TO_POSTGRES.md"
_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml"]


def _python(code: str, cwd: Path, **env: str) -> subprocess.CompletedProcess:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(cwd),
        "REDIS_URL": "redis://a:b@localhost:6379/0",
        "SECRET_KEY": "test",
    }
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True,
                          env={**base, **env}, timeout=120)


# --- SQLite selected inside an image: the real import path ------------------

class TestBackendSqliteRefusal:
    def test_image_refuses_and_creates_no_file(self, tmp_path: Path) -> None:
        db = tmp_path / "sub" / "trinity.db"
        proc = _python("import database", _BACKEND, TRINITY_DB_PATH=str(db), TRINITY_REQUIRE_POSTGRES="1")
        assert proc.returncode != 0
        assert _GUIDE in proc.stderr
        assert not db.parent.exists(), "a refused boot must not create the directory or the file"

    def test_empty_database_url_is_refused_too(self, tmp_path: Path) -> None:
        db = tmp_path / "trinity.db"
        proc = _python("import database", _BACKEND, TRINITY_DB_PATH=str(db),
                       TRINITY_REQUIRE_POSTGRES="1", DATABASE_URL="")
        assert proc.returncode != 0
        assert not db.exists()

    def test_existing_file_is_named_and_left_alone(self, tmp_path: Path) -> None:
        db = tmp_path / "trinity.db"
        sqlite3.connect(db).executescript("CREATE TABLE users (id INTEGER); INSERT INTO users VALUES (1);")
        before = db.read_bytes()
        proc = _python("import database", _BACKEND, TRINITY_DB_PATH=str(db), TRINITY_REQUIRE_POSTGRES="1")
        assert proc.returncode != 0
        assert "TRINITY_ALLOW_SQLITE" in proc.stderr
        assert db.read_bytes() == before

    def test_hatch_boots_on_sqlite(self, tmp_path: Path) -> None:
        db = tmp_path / "trinity.db"
        proc = _python("import database", _BACKEND, TRINITY_DB_PATH=str(db),
                       TRINITY_REQUIRE_POSTGRES="1", TRINITY_ALLOW_SQLITE="1")
        assert proc.returncode == 0, proc.stderr
        assert db.stat().st_size > 0

    def test_outside_an_image_sqlite_still_works(self, tmp_path: Path) -> None:
        db = tmp_path / "trinity.db"
        proc = _python("import database", _BACKEND, TRINITY_DB_PATH=str(db))
        assert proc.returncode == 0, proc.stderr


# --- Empty PostgreSQL beside SQLite data ------------------------------------

@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The real upgrade_to_head on a throwaway engine, with Alembic's commands
    recorded. The target stands in for PostgreSQL: the guard reads only the
    `users` table, through SQLAlchemy."""
    if str(_BACKEND) not in sys.path:
        monkeypatch.syspath_prepend(str(_BACKEND))
    from db import alembic_runner

    target = tmp_path / "target.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
    monkeypatch.setenv("TRINITY_REQUIRE_POSTGRES", "1")
    monkeypatch.delenv("TRINITY_ALLOW_SQLITE", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(alembic_runner.command, "upgrade", lambda cfg, rev: calls.append("upgrade"))
    monkeypatch.setattr(alembic_runner.command, "stamp", lambda cfg, rev: calls.append("stamp"))
    source = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(source))
    return alembic_runner, source, target, calls


def _write_source(source: Path) -> None:
    source.write_bytes(b"SQLite format 3\x00")


def _users(target: Path, rows: int) -> None:
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE users (id INTEGER)")
    conn.executemany("INSERT INTO users VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


class TestEmptyPostgresBesideSqlite:
    def test_no_tables_is_refused(self, runner) -> None:
        mod, source, _, calls = runner
        _write_source(source)
        with pytest.raises(RuntimeError, match="SQLITE_TO_POSTGRES"):
            mod.upgrade_to_head()
        assert calls == [], "nothing may be built on the empty database"

    def test_schema_without_users_is_refused(self, runner) -> None:
        """A failed copy leaves empty tables. Without this, the next boot seeds
        an admin and every later boot passes."""
        mod, source, target, calls = runner
        _write_source(source)
        _users(target, 0)
        with pytest.raises(RuntimeError, match="SQLITE_TO_POSTGRES"):
            mod.upgrade_to_head()
        assert calls == []

    def test_migrated_install_keeping_its_file_boots(self, runner) -> None:
        mod, source, target, calls = runner
        _write_source(source)
        _users(target, 1)
        mod.upgrade_to_head()
        assert "upgrade" in calls

    @pytest.mark.parametrize("content", [None, b""])
    def test_fresh_install_boots(self, runner, content) -> None:
        mod, source, _, calls = runner
        if content is not None:
            source.write_bytes(content)
        mod.upgrade_to_head()
        assert calls == ["upgrade"]

    def test_hatch_does_not_open_the_empty_database(self, runner, monkeypatch) -> None:
        mod, source, _, calls = runner
        _write_source(source)
        monkeypatch.setenv("TRINITY_ALLOW_SQLITE", "1")
        with pytest.raises(RuntimeError, match="DATABASE_URL="):
            mod.upgrade_to_head()
        assert calls == []

    def test_outside_an_image_the_check_is_off(self, runner, monkeypatch) -> None:
        mod, source, _, calls = runner
        _write_source(source)
        monkeypatch.delenv("TRINITY_REQUIRE_POSTGRES")
        mod.upgrade_to_head()
        assert calls == ["upgrade"]

    def test_the_copy_tool_may_build_the_schema(self, runner) -> None:
        mod, source, _, calls = runner
        _write_source(source)
        mod.upgrade_to_head(allow_sqlite_source=True)
        assert calls == ["upgrade"]


def test_percent_in_password_reaches_alembic_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigParser treats `%` as interpolation: an encoded password raised
    ValueError with the full URL in the message."""
    if str(_BACKEND) not in sys.path:
        monkeypatch.syspath_prepend(str(_BACKEND))
    from db import alembic_runner

    url = "postgresql://trinity:p%40ss@postgres:5432/trinity"
    monkeypatch.setenv("DATABASE_URL", url)
    assert alembic_runner._config().get_main_option("sqlalchemy.url") == url


# --- Scheduler --------------------------------------------------------------

_SCHED = "from scheduler.database import SchedulerDatabase; SchedulerDatabase({db!r})"


class TestSchedulerRefusal:
    def test_image_refuses_without_a_postgres_url(self, tmp_path: Path) -> None:
        db = tmp_path / "trinity.db"
        proc = _python(_SCHED.format(db=str(db)), _SRC, TRINITY_REQUIRE_POSTGRES="1")
        assert proc.returncode != 0
        assert _GUIDE in proc.stderr
        assert not db.exists()

    def test_hatch_allows_it(self, tmp_path: Path) -> None:
        proc = _python(_SCHED.format(db=str(tmp_path / "t.db")), _SRC,
                       TRINITY_REQUIRE_POSTGRES="1", TRINITY_ALLOW_SQLITE="1")
        assert proc.returncode == 0, proc.stderr

    def test_postgres_url_passes(self, tmp_path: Path) -> None:
        proc = _python(_SCHED.format(db=str(tmp_path / "t.db")), _SRC,
                       TRINITY_REQUIRE_POSTGRES="1", DATABASE_URL="postgresql://u:p@h:5432/d")
        assert proc.returncode == 0, proc.stderr


# --- Compose and images -----------------------------------------------------

def _compose(name: str) -> dict:
    return yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _COMPOSE_FILES)
class TestComposeDefaults:
    def test_postgres_runs_by_default(self, name: str) -> None:
        pg = _compose(name)["services"]["postgres"]
        assert "profiles" not in pg
        assert pg["networks"] == ["trinity-platform"], "agents never reach it (#589)"
        assert "POSTGRES_PASSWORD:?" in str(pg["environment"]), "no default credential"

    @pytest.mark.parametrize("service", ["backend", "scheduler"])
    def test_app_services_default_to_postgres(self, name: str, service: str) -> None:
        svc = _compose(name)["services"][service]
        url = next(e for e in svc["environment"] if e.startswith("DATABASE_URL="))
        assert url.startswith("DATABASE_URL=${DATABASE_URL-postgresql://"), url
        assert any(e.startswith("TRINITY_ALLOW_SQLITE=") for e in svc["environment"]), (
            "the hatch must reach the container (#1039 packaging class)"
        )
        assert svc["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_dev_scheduler_waits_for_a_healthy_backend() -> None:
    """A scheduler that starts first hits missing tables on a fresh database."""
    dep = _compose("docker-compose.yml")["services"]["scheduler"]["depends_on"]["backend"]
    assert dep["condition"] == "service_healthy"


def test_dev_url_honours_the_configured_names() -> None:
    env = _compose("docker-compose.yml")["services"]["backend"]["environment"]
    url = next(e for e in env if e.startswith("DATABASE_URL="))
    assert "${POSTGRES_USER:-trinity}" in url and "${POSTGRES_DB:-trinity}" in url


@pytest.mark.parametrize("name", ["docker-compose.prod.yml", "docker-compose.hosted.yml"])
def test_existing_postgres_volume_can_be_adopted(name: str) -> None:
    """A standalone container's data volume is reused with no copy (ops#604)."""
    vol = _compose(name)["volumes"]["postgres-data"]
    assert vol["name"] == "${TRINITY_POSTGRES_VOLUME:-${COMPOSE_PROJECT_NAME}_postgres-data}"


@pytest.mark.parametrize("image", ["backend", "scheduler"])
def test_images_require_postgres(image: str) -> None:
    """The refusal is keyed on this line: source pin on top of the executing
    tests above, whose live consumer is the image build."""
    text = (_ROOT / "docker" / image / "Dockerfile").read_text(encoding="utf-8")
    assert "ENV TRINITY_REQUIRE_POSTGRES=1" in text
