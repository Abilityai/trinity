"""Copy an install's SQLite database into an empty PostgreSQL (#4175).

    docker compose run --rm backend python -m db.sqlite_to_postgres

Runs from the state the backend refuses to boot in, so it must not import
``database``, whose import runs ``init_database()``.

1. Snapshot the SQLite file with the backup API. The live file is never written.
2. Bring the snapshot to this release's schema with the SQLite runner.
3. Build the PostgreSQL schema with Alembic (OSS and enterprise version-lines).
4. Copy the data in one transaction, reset sequences, verify rowcounts.
5. Write a marker beside the SQLite file, which tells start.sh the copy is done.

Exit codes: 0 ok | 1 usage | 2 rowcount mismatch | 3 schema drift | 4 load error
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
from sqlalchemy.engine import make_url

from db.backup_primitives import sqlite_backup_to
from db.engine import is_sqlite, resolve_database_url, sqlite_db_path

EXIT_USAGE = 1
EXIT_MISMATCH = 2
EXIT_SCHEMA = 3
EXIT_LOAD = 4

COPIED_SUFFIX = ".copied-to-postgres"  # also read by scripts/deploy/start.sh

BATCH = 1000

# SQLite-only bookkeeping with no PostgreSQL counterpart: Alembic owns both on
# that side (`alembic_version`, `enterprise_alembic_version`).
SQLITE_ONLY_OK = {"schema_migrations", "sqlite_sequence", "enterprise_schema_migrations"}

INT_TYPES = {"integer", "bigint", "smallint"}
FLOAT_TYPES = {"double precision", "real", "numeric"}
TEXT_TYPES = {"text", "character varying", "character"}


class CopyError(Exception):
    pass


def _fail(code: int, message: str) -> None:
    print(f"\nCOPY FAILED: {message}", file=sys.stderr)
    sys.exit(code)


def coerce(value, pg_type: str, ctx: str):
    """Coerce a SQLite value for a PostgreSQL column.

    SQLite tolerated bools and strings in INTEGER columns and numbers in TEXT
    columns; PostgreSQL rejects them. Anything that cannot be coerced losslessly
    fails with context.
    """
    if value is None:
        return None
    if pg_type != "bytea":
        if isinstance(value, (bytes, memoryview)):
            has_nul = b"\x00" in bytes(value)
        else:
            has_nul = isinstance(value, str) and "\x00" in value
        if has_nul:
            raise CopyError(f"{ctx}: NUL byte, which PostgreSQL cannot store in {pg_type}")
    if pg_type in INT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise CopyError(f"{ctx}: non-integral float {value!r} for {pg_type} column")
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                raise CopyError(f"{ctx}: non-numeric string {value!r} for {pg_type} column")
        raise CopyError(f"{ctx}: {type(value).__name__} for {pg_type} column")
    if pg_type in TEXT_TYPES:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return str(int(value))
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (bytes, memoryview)):
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                raise CopyError(f"{ctx}: undecodable bytes for text column")
        raise CopyError(f"{ctx}: {type(value).__name__} for text column")
    if pg_type in FLOAT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise CopyError(f"{ctx}: non-numeric string {value!r} for {pg_type} column")
        raise CopyError(f"{ctx}: {type(value).__name__} for {pg_type} column")
    if pg_type == "bytea":
        if isinstance(value, (bytes, memoryview)):
            return psycopg2.Binary(bytes(value))
        if isinstance(value, str):
            return psycopg2.Binary(value.encode("utf-8"))
        raise CopyError(f"{ctx}: {type(value).__name__} for bytea column")
    return value


def prepare_snapshot(source: str) -> str:
    """Snapshot ``source`` beside itself and bring the snapshot to head."""
    from db import migrations
    from db.schema import init_schema

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snapshot = str(Path(source).with_name(f"{Path(source).stem}.pg-copy-{stamp}.db"))
    sqlite_backup_to(source, snapshot)

    conn = sqlite3.connect(snapshot)
    try:
        cursor = conn.cursor()
        # The same sequence as database.init_database().
        migrations.run_all_migrations(cursor, conn)
        init_schema(cursor, conn)
        migrations.run_all_migrations(cursor, conn)
        applied, expected, first_pending = migrations.migration_health(cursor)
        conn.commit()
    finally:
        conn.close()
    if first_pending:
        _fail(EXIT_SCHEMA, f"SQLite schema stopped at {applied}/{expected}, pending: {first_pending}")
    if _module_available("enterprise.backend"):
        _apply_enterprise_sqlite_migrations(snapshot)
    print(f"Snapshot at this release's schema: {snapshot}")
    return snapshot


def _apply_enterprise_sqlite_migrations(snapshot: str) -> None:
    """Run the private module's SQLite schema and migrations on the snapshot.

    A SQLite boot applies them inside register_enterprise. Alembic builds the
    PostgreSQL side on an empty database, so a migration that moves data
    would otherwise run on neither track.
    """
    from fastapi import FastAPI

    from db import connection

    enterprise = importlib.import_module("enterprise.backend")
    # A module's register() imports `database`, whose SQLite branch refuses
    # inside the image unless SQLite is allowed.
    overrides = {"DATABASE_URL": "", "TRINITY_DB_PATH": snapshot, "TRINITY_ALLOW_SQLITE": "1"}
    saved_env = {key: os.environ.get(key) for key in overrides}
    saved_path = connection.DB_PATH
    os.environ.update(overrides)
    connection.DB_PATH = snapshot
    try:
        enterprise.register_enterprise(FastAPI())
        failed = [name for name, _ in enterprise._FAILED]
    finally:
        connection.DB_PATH = saved_path
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if failed:
        _fail(EXIT_SCHEMA, f"enterprise modules failed on the SQLite snapshot: {', '.join(failed)}")
    print("Enterprise SQLite migrations applied to the snapshot")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:  # find_spec raises when a parent package is absent
        return False


def build_postgres_schema() -> None:
    from db.alembic_runner import upgrade_to_head

    upgrade_to_head(allow_sqlite_source=True)
    # Enterprise tables normally appear through register_enterprise at boot,
    # which also seeds, and a seeded target cannot take a copy.
    if _module_available("enterprise.backend"):
        try:
            importlib.import_module("enterprise.backend._alembic_runner").upgrade_to_head()
        except ValueError as e:
            # configparser quotes the URL, password included, in this message.
            if "interpolation" not in str(e):
                raise
            _fail(EXIT_USAGE, "The enterprise module cannot read a DATABASE_URL containing %. "
                              "Update the module, or use a password that needs no URL encoding.")
    print("PostgreSQL schema at head")


def _build_plan(lite, cur) -> tuple[list, list, list]:
    lite_tables = {
        r[0] for r in lite.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'")
    pg_tables = {r[0] for r in cur.fetchall()}

    # A SQLite table absent from PostgreSQL is an orphan of a table later dropped
    # from the schema. Skipping it is safe only while it is empty.
    empty_orphans, data_orphans = [], []
    for table in sorted(lite_tables - pg_tables - SQLITE_ONLY_OK):
        rows = lite.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        (data_orphans if rows else empty_orphans).append(table)
    if data_orphans:
        _fail(EXIT_SCHEMA, f"SQLite tables hold data but do not exist in PostgreSQL: {data_orphans}")

    copy_tables = sorted(lite_tables & pg_tables)
    plan = []
    for table in copy_tables:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))
        pg_cols = {r[0]: r[1] for r in cur.fetchall()}
        lite_cols = [r[1] for r in lite.execute(f'PRAGMA table_info("{table}")')]
        missing = [c for c in lite_cols if c not in pg_cols]
        if missing:
            _fail(EXIT_SCHEMA, f'table "{table}": SQLite columns missing in PostgreSQL: {missing}')
        plan.append((table, lite_cols, pg_cols))
    return plan, copy_tables, empty_orphans


def _load(lite, cur, plan: list) -> None:
    populated = []
    for table, _, _ in plan:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        if cur.fetchone()[0]:
            populated.append(table)
    if populated:
        _fail(EXIT_LOAD, f"PostgreSQL tables are not empty, refusing to merge: {populated}")

    for table, cols, pg_cols in plan:
        collist = ", ".join(f'"{c}"' for c in cols)
        src = lite.execute(f'SELECT {collist} FROM "{table}"')
        total = 0
        while rows := src.fetchmany(BATCH):
            out = [
                tuple(coerce(v, pg_cols[c], f"{table}.{c} (row ~{total + i + 1})") for c, v in zip(cols, row))
                for i, row in enumerate(rows)
            ]
            psycopg2.extras.execute_values(
                cur, f'INSERT INTO "{table}" ({collist}) VALUES %s', out, page_size=BATCH)
            total += len(rows)


def _reset_sequences(cur) -> None:
    """Rows carry explicit ids, so every nextval would otherwise collide."""
    cur.execute(
        "SELECT c.table_name, c.column_name, "
        "       pg_get_serial_sequence(quote_ident(c.table_name), c.column_name) "
        "FROM information_schema.columns c "
        "WHERE c.table_schema='public' AND c.column_default LIKE 'nextval(%'")
    for table, column, sequence in cur.fetchall():
        if sequence is None:
            continue
        cur.execute(f'SELECT MAX("{column}") FROM "{table}"')
        highest = cur.fetchone()[0]
        if highest is None:
            cur.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            cur.execute("SELECT setval(%s, %s, true)", (sequence, highest))


def copy_data(snapshot: str, url: str) -> None:
    dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    lite = sqlite3.connect(f"file:{snapshot}?mode=ro&immutable=1", uri=True)
    pg = psycopg2.connect(dsn)
    pg.autocommit = False
    try:
        cur = pg.cursor()
        plan, copy_tables, empty_orphans = _build_plan(lite, cur)
        _load(lite, cur, plan)
        _reset_sequences(cur)

        mismatches = []
        for table in copy_tables:
            source_rows = lite.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            target_rows = cur.fetchone()[0]
            if source_rows != target_rows:
                mismatches.append(f"{table}: {source_rows} -> {target_rows}")
        if mismatches:
            _fail(EXIT_MISMATCH, f"rowcounts differ: {mismatches}")
        pg.commit()
        if empty_orphans:
            print(f"Skipped empty tables absent from PostgreSQL: {empty_orphans}")
        print(f"Copied and verified {len(copy_tables)} tables")
    finally:
        pg.close()
        lite.close()


def _has_users(snapshot: str) -> bool:
    conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
    finally:
        conn.close()


def main() -> None:
    source = sqlite_db_path()
    url = resolve_database_url()
    if is_sqlite(url):
        if "DATABASE_URL" in os.environ and not os.environ["DATABASE_URL"].strip():
            _fail(EXIT_USAGE, "DATABASE_URL is empty, which selects SQLite. "
                              "Remove the DATABASE_URL= line from .env and run this again.")
        _fail(EXIT_USAGE, "DATABASE_URL must be a postgresql:// URL")
    if not os.path.isfile(source) or os.path.getsize(source) == 0:
        _fail(EXIT_USAGE, f"no SQLite database at {source}")

    snapshot = prepare_snapshot(source)
    if not _has_users(snapshot):
        _fail(EXIT_USAGE, f"{source} has no users, so there is nothing to copy. "
                          "Move it aside and start the stack.")
    build_postgres_schema()
    try:
        copy_data(snapshot, url)
    except CopyError as e:
        _fail(EXIT_LOAD, str(e))
    except psycopg2.Error as e:
        # pgerror only: str(e) on a connection failure can carry the DSN.
        _fail(EXIT_LOAD, f"PostgreSQL error: {e.pgerror or type(e).__name__}")

    Path(f"{source}{COPIED_SUFFIX}").write_text(f"{datetime.now(timezone.utc).isoformat()}\n")
    print(f"Done. Start the stack. Keep {source} and {snapshot} until you have checked the result.")


if __name__ == "__main__":
    main()
