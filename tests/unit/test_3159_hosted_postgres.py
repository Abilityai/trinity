"""Hosted installs start on a bundled PostgreSQL (#3159, HOST-022).

SQLite reached end-of-support on 2026-09-01 (#1278). Every one-click channel
(DigitalOcean, AWS, Vultr, compose-only templates) converges on
``docker-compose.hosted.yml`` + ``start.sh --hosted``, so the fix and its guard
live there.

The start.sh cases execute the shipped ``ensure_hosted_database`` function
under bash with ``docker`` stubbed; the compose cases read the raw YAML so the
unexpanded ``${VAR...}`` forms are what is checked.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_HOSTED = _ROOT / "docker-compose.hosted.yml"
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_ENV_FILE_SH = _ROOT / "scripts" / "deploy" / "env-file.sh"
_PROVISION = _ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh"

_PG_URL = "postgresql://trinity:${POSTGRES_PASSWORD}@postgres:5432/trinity"
_DB_URL_ENTRY = "DATABASE_URL=${DATABASE_URL-" + _PG_URL + "}"


@pytest.fixture(scope="module")
def hosted() -> dict:
    return yaml.safe_load(_HOSTED.read_text(encoding="utf-8"))


# --- compose ----------------------------------------------------------------

def test_postgres_service_shape(hosted: dict) -> None:
    pg = hosted["services"]["postgres"]
    assert pg["image"] == "postgres:16-alpine"
    assert pg["networks"] == ["trinity-platform"], (
        "postgres must sit on trinity-platform-network only — agents never reach it (#589)"
    )
    assert "pg_isready" in str(pg["healthcheck"]["test"])
    assert pg["logging"] == hosted["services"]["redis"]["logging"], "use *default-logging (#1871)"
    assert "postgres-data:/var/lib/postgresql/data" in pg["volumes"]
    assert "postgres-data" in hosted["volumes"]
    assert "no-new-privileges:true" in pg["security_opt"]
    assert "ports" not in pg, "postgres must not publish a host port"


def test_postgres_password_is_required(hosted: dict) -> None:
    env = hosted["services"]["postgres"]["environment"]
    pw = env["POSTGRES_PASSWORD"] if isinstance(env, dict) else next(
        e.split("=", 1)[1] for e in env if e.startswith("POSTGRES_PASSWORD=")
    )
    assert pw.startswith("${POSTGRES_PASSWORD:?"), (
        "compose-only consumers (#2283) must fail to render without POSTGRES_PASSWORD"
    )


@pytest.mark.parametrize("service", ["backend", "scheduler"])
def test_waits_for_postgres_healthy(hosted: dict, service: str) -> None:
    dep = hosted["services"][service]["depends_on"]["postgres"]
    assert dep == {"condition": "service_healthy"}


@pytest.mark.parametrize("service", ["backend", "scheduler"])
def test_database_url_defaults_to_bundled_postgres(hosted: dict, service: str) -> None:
    """`-` (no colon): unset → bundled Postgres, set-but-empty → SQLite.

    start.sh writes `DATABASE_URL=` for an existing SQLite install, so a `:-`
    default would flip that install onto an empty database."""
    env = hosted["services"][service]["environment"]
    assert _DB_URL_ENTRY in env, f"{service} DATABASE_URL must be {_DB_URL_ENTRY!r}"


# --- packer -----------------------------------------------------------------

def test_snapshot_prepulls_postgres(hosted: dict) -> None:
    image = hosted["services"]["postgres"]["image"]
    assert re.search(rf"^docker pull {re.escape(image)}$", _PROVISION.read_text(), re.MULTILINE), (
        f"01-provision.sh must pre-pull {image} so first boot pulls nothing"
    )


# --- start.sh ---------------------------------------------------------------

def _extract(func: str) -> str:
    m = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", _START.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"start.sh no longer defines a top-level `{func}()`"
    return m.group(0)


def _run(tmp_path: Path, *, hosted: str = "1", db_file: bool = False, pg_volume: bool = False,
         env_lines: str = "", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "volume" ] && [ "$2" = "inspect" ]; then exit "${SHIM_VOLUME_RC:-1}"; fi\n'
        "exit 1\n"
    )
    docker.chmod(0o755)
    data = tmp_path / "trinity-data"
    data.mkdir(exist_ok=True)
    if db_file:
        (data / "trinity.db").write_bytes(b"SQLite format 3\x00")
    env_file = tmp_path / ".env"
    if not env_file.exists():
        env_file.write_text(env_lines)
    script = "\n".join([
        "set -e",
        f". '{_ENV_FILE_SH}'",
        _extract("env_value"),
        _extract("compose_project_name"),
        _extract("ensure_hosted_database"),
        f"HOSTED={hosted}",
        "ensure_hosted_database",
    ])
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "SHIM_VOLUME_RC": "0" if pg_volume else "1",
    }
    env.update(extra_env or {})
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, env=env)


def _env(tmp_path: Path) -> dict:
    out = {}
    for line in (tmp_path / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


@_BASH
def test_fresh_install_gets_postgres(tmp_path: Path) -> None:
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = _env(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{48}", env["POSTGRES_PASSWORD"])
    assert env["DATABASE_URL"] == f"postgresql://trinity:{env['POSTGRES_PASSWORD']}@postgres:5432/trinity"


@_BASH
def test_rerun_is_idempotent(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 0
    first = (tmp_path / ".env").read_text()
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / ".env").read_text() == first, "a rerun must not regenerate the password or URL"


@_BASH
def test_existing_sqlite_install_stays_on_sqlite(tmp_path: Path) -> None:
    proc = _run(tmp_path, db_file=True, env_lines="REDIS_PASSWORD=x\n")
    assert proc.returncode == 0, proc.stderr
    env = _env(tmp_path)
    assert env["DATABASE_URL"] == "", "an existing trinity.db must pin SQLite with an explicit empty DATABASE_URL"
    assert env["POSTGRES_PASSWORD"], "the compose file still needs a password to render"
    assert "docs/migrations/SQLITE_TO_POSTGRES.md" in proc.stdout
    # and stays there on every later update
    proc = _run(tmp_path, db_file=True)
    assert proc.returncode == 0, proc.stderr
    assert _env(tmp_path)["DATABASE_URL"] == ""
    assert "docs/migrations/SQLITE_TO_POSTGRES.md" in proc.stdout


@_BASH
def test_operator_database_url_is_kept(tmp_path: Path) -> None:
    url = "postgresql://u:p@db.example.com:5432/trinity"
    proc = _run(tmp_path, db_file=True, env_lines=f"DATABASE_URL={url}\n")
    assert proc.returncode == 0, proc.stderr
    assert _env(tmp_path)["DATABASE_URL"] == url
    assert "SQLITE_TO_POSTGRES" not in proc.stdout


@_BASH
def test_shell_database_url_counts_as_decided(tmp_path: Path) -> None:
    proc = _run(tmp_path, extra_env={"DATABASE_URL": ""})
    assert proc.returncode == 0, proc.stderr
    assert "DATABASE_URL" not in _env(tmp_path)


@_BASH
def test_populated_volume_without_password_refuses(tmp_path: Path) -> None:
    proc = _run(tmp_path, pg_volume=True)
    assert proc.returncode != 0
    assert "POSTGRES_PASSWORD" in proc.stderr
    assert "POSTGRES_PASSWORD" not in _env(tmp_path)


@_BASH
def test_dev_mode_is_untouched(tmp_path: Path) -> None:
    proc = _run(tmp_path, hosted="0")
    assert proc.returncode == 0, proc.stderr
    assert _env(tmp_path) == {}


def test_called_after_the_data_switch_guard_and_before_pull() -> None:
    """A refused run must write nothing, and `compose pull` renders the file."""
    text = _START.read_text()
    call = text.index("\nensure_hosted_database\n")
    assert text.index("# --- Refuse a silent data switch") < call
    assert call < text.index('docker compose "${COMPOSE_FILES[@]}" pull')


def test_backend_pg_dump_can_dump_the_hosted_server(hosted: dict) -> None:
    """Nightly backups (#2216) run pg_dump in the backend image; pg_dump dumps
    servers up to its own major version only."""
    server = int(re.search(r"postgres:(\d+)", hosted["services"]["postgres"]["image"]).group(1))
    m = re.search(r"postgresql-client-(\d+)", (_ROOT / "docker" / "backend" / "Dockerfile").read_text())
    assert m and int(m.group(1)) >= server, "backend pg_dump is older than the hosted PostgreSQL server"


def test_env_example_keeps_database_url_commented() -> None:
    """start.sh copies .env.example to .env on a new install; an uncommented
    DATABASE_URL line there would read as a decision and skip PostgreSQL."""
    text = (_ROOT / ".env.example").read_text()
    assert not re.search(r"^\s*(export\s+)?DATABASE_URL=", text, re.MULTILINE)
