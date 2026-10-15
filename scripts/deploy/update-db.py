"""Database steps of an update, run INSIDE the running backend container (#3007).

`scripts/deploy/update.sh` pipes this file into the backend with a mode:

    docker exec -i trinity-backend python - backup < scripts/deploy/update-db.py
    docker exec -i trinity-backend python - schema < scripts/deploy/update-db.py

and reads the LAST line of stdout, a JSON object whose `status` is `ok` or
`failed`. The exit status mirrors it.

It runs in whichever backend is live — before an update that is the OLD release,
which may pre-date any helper in `src/backend` — so the SQLite arms are stdlib
only and must stay that way.

backup
  SQLite: an online `sqlite3` backup (never a raw copy of a live file), checked
  with `PRAGMA quick_check`, written to a tmp name and renamed into
  `<db dir>/backups/pre-migration-<UTC YYYYMMDD-HHMMSS>.db` — the #2216
  boot-hook name, so the nightly prune reclaims it.
  PostgreSQL: a fresh `pg_dump -Fc` on EVERY update (the nightly artifact is
  day-keyed, so reusing it would hand rollback a copy hours older than the
  update), verified by its PGDMP magic and renamed into
  `<backups>/pre-update-<UTC YYYYMMDD-HHMMSS>.dump`. `pre-update-*.dump` is in
  `db/backup_primitives.ARTIFACT_PATTERNS`, so the same retention prune
  (retention days + MIN_KEEP newest) reclaims it — no second policy. The URL
  is normalised by the #2216 service's own `_pg_conninfo_and_password`
  (driver suffix dropped, password moved to PGPASSWORD — never argv).

schema
  The schema position: every applied migration the database records, hashed.
  SQLite: `schema_migrations.name` (+ `enterprise_schema_migrations` rows when
  present). PostgreSQL: `alembic_version` (+ the enterprise table) via `psql`.
  Two positions differ exactly when a migration ran between them, which is
  what decides whether rolling the CODE back alone is safe.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone


def _emit(result, code):
    print(json.dumps(result, default=str))
    sys.exit(code)


def _fail(msg):
    _emit({"status": "failed", "error": msg}, 1)


def _database():
    """('sqlite', path) or ('postgresql', url)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url and not url.startswith("sqlite"):
        return "postgresql", url
    path = os.environ.get("TRINITY_DB_PATH", "/data/trinity.db")
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):] or path
    return "sqlite", path


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _backup_dir(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def _sqlite_connect_ro(db_path):
    if not os.path.isfile(db_path):
        _fail("database file not found: %s" % db_path)
    # mode=ro: a missing or mistyped path must never create an empty DB.
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)


def _pg_conn():
    try:
        from services.db_backup_service import _pg_conninfo_and_password
    except Exception as exc:  # noqa: BLE001
        _fail("this backend cannot normalise DATABASE_URL for libpq (%s: %s); take a pg_dump "
              "yourself and re-run with --skip-backup" % (type(exc).__name__, exc))
    conninfo, password = _pg_conninfo_and_password(os.environ["DATABASE_URL"])
    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password  # env only — argv is world-readable via /proc
    return conninfo, env


# --- backup ---------------------------------------------------------------------

def _sqlite_backup(db_path):
    src = _sqlite_connect_ro(db_path)
    backup_dir = _backup_dir(db_path)
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    final = os.path.join(backup_dir, "pre-migration-%s.db" % _stamp())
    tmp = "%s.tmp.%d" % (final, os.getpid())
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
            src.close()
        if check != "ok":
            raise RuntimeError("quick_check on the copy returned %r" % check)
        os.replace(tmp, final)
    except Exception as exc:  # noqa: BLE001 — every failure is reported, not raised
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _fail("%s: %s" % (type(exc).__name__, exc))
    _emit({"status": "ok", "path": final, "bytes": os.path.getsize(final)}, 0)


def _pg_backup():
    conninfo, env = _pg_conn()
    backup_dir = _backup_dir(os.environ.get("TRINITY_DB_PATH", "/data/trinity.db"))
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    final = os.path.join(backup_dir, "pre-update-%s.dump" % _stamp())
    tmp = "%s.tmp.%d" % (final, os.getpid())
    try:
        try:
            proc = subprocess.run(["pg_dump", "-Fc", "-d", conninfo, "-f", tmp],
                                  env=env, capture_output=True, timeout=3600)
        except FileNotFoundError:
            raise RuntimeError("pg_dump not found in this backend image (it predates #2216)")
        if proc.returncode != 0:
            raise RuntimeError("pg_dump exited %d: %s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace")[:500]))
        with open(tmp, "rb") as fh:
            if fh.read(5) != b"PGDMP":
                raise RuntimeError("pg_dump output is missing the PGDMP magic bytes")
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    except Exception as exc:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _fail("%s: %s" % (type(exc).__name__, exc))
    _emit({"status": "ok", "path": final, "bytes": os.path.getsize(final)}, 0)


# --- schema position --------------------------------------------------------------

def _position(lines):
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]
    return digest


def _sqlite_schema(db_path):
    conn = _sqlite_connect_ro(db_path)
    try:
        lines = ["oss:%s" % r[0] for r in conn.execute("SELECT name FROM schema_migrations")]
        oss = len(lines)
        has_ent = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enterprise_schema_migrations'"
        ).fetchone()
        ent = []
        if has_ent:
            ent = ["ent:%s" % "|".join(str(c) for c in r)
                   for r in conn.execute("SELECT * FROM enterprise_schema_migrations")]
    except sqlite3.Error as exc:
        _fail("could not read the migration history: %s" % exc)
    finally:
        conn.close()
    _emit({"status": "ok", "backend": "sqlite", "position": _position(lines + ent),
           "oss": oss, "enterprise": len(ent)}, 0)


def _psql(conninfo, env, sql):
    try:
        proc = subprocess.run(["psql", "-X", "-At", "-d", conninfo, "-c", sql],
                              env=env, capture_output=True, timeout=60)
    except FileNotFoundError:
        _fail("psql not found in this backend image")
    return proc.returncode, proc.stdout.decode("utf-8", "replace").splitlines()


def _pg_schema():
    conninfo, env = _pg_conn()
    rc, heads = _psql(conninfo, env, "SELECT version_num FROM alembic_version")
    if rc != 0:
        _fail("could not read alembic_version")
    lines = ["alembic:%s" % h for h in heads if h]
    rc, has_ent = _psql(conninfo, env, "SELECT to_regclass('enterprise_schema_migrations') IS NOT NULL")
    ent = []
    if rc == 0 and has_ent and has_ent[0] == "t":
        rc, rows = _psql(conninfo, env, "SELECT * FROM enterprise_schema_migrations")
        if rc != 0:
            _fail("could not read enterprise_schema_migrations")
        ent = ["ent:%s" % r for r in rows if r]
    _emit({"status": "ok", "backend": "postgresql", "position": _position(lines + ent),
           "oss": ",".join(sorted(h for h in heads if h)), "enterprise": len(ent)}, 0)


def main(argv):
    mode = argv[1] if len(argv) > 1 else "backup"
    kind, target = _database()
    if mode == "backup":
        _pg_backup() if kind == "postgresql" else _sqlite_backup(target)
    elif mode == "schema":
        _pg_schema() if kind == "postgresql" else _sqlite_schema(target)
    else:
        _fail("unknown mode %r (expected backup or schema)" % mode)


main(sys.argv)
