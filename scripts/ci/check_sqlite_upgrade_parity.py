#!/usr/bin/env python3
"""SQLite upgrade-parity guard: an existing install, upgraded, ends at schema.py (#3395).

`tests/unit/test_schema_parity.py` models only a FRESH install: an empty
database through `run_all_migrations` -> `init_schema` -> `run_all_migrations`.
On that path a migration whose target table does not exist yet raises
`no such table`, which the runner swallows as a fresh-install skip, and
`init_schema` then creates the table with every column `schema.py` lists. So a
column that `schema.py` declares but no registered migration adds is present on
every fresh install and absent on every EXISTING one, and no fresh-install
comparison can see it. That holds for every table `init_schema` creates and no
migration does (`schedule_executions`, `users`, `chat_messages`, ...): 213 of
the 306 `ALTER TABLE <t> ADD COLUMN` / `_safe_add_column(cursor, "<t>", ...)`
sites in `db/migrations.py` target one of them (counted on dev at 40b329593).

This guard models the existing install instead:

  1. Resolve the merge-base of the PR's base and head.
  2. `git archive` the merge-base's `src/backend` into a temp dir and, in a
     subprocess rooted there, run that tree's lifecycle on a temp FILE
     database. This is the install the merge-base would have left: its tables,
     its indexes, its `schema_migrations` ledger.
  3. In a second subprocess rooted at the checkout's `src/backend`, run the
     checkout's lifecycle on that same database: the upgrade.
  4. Require that every table, column, index and trigger `init_schema` declares
     (built on an empty in-memory database by the checkout's code) exists in
     the upgraded database, and that `migration_health` reports `(n, n, None)`:
     every registered migration recorded, no ledger name the registry no longer
     knows. The second half is stricter than `/health` (which checks only
     `applied < expected`) on purpose: a renamed migration re-runs on every
     existing install, and a removed one leaves `/health` counting a name that
     is no longer registered, so either is reported rather than passed.

Each tree runs in its own interpreter, so the two copies of `db.migrations`
never meet in one `sys.modules`. Both load `db/schema.py` and `db/migrations.py`
by file path, as the parity pytest does, so the `db` package `__init__` (and its
domain imports) is never executed.

In CI the checkout of a `pull_request` event is the merge of the PR into the
base tip, i.e. what `dev` becomes; the PR head SHA is used only to find the
merge-base. A table the PR adds is created by the checkout's `init_schema` on
upgrade, exactly as on a real install, so a new table is never a false red.

Stated limits:
  * Names only. Column types, defaults, NOT NULL, keys and the SQL of indexes
    and triggers are not compared.
  * Missing objects only. An object the checkout stops declaring survives on
    existing installs and is not reported.
  * No rows. The install it builds is empty: `init_database`'s seeding steps
    (retention rows, admin user, setup and install-source markers) are not run,
    so a backfill, a rebuild's row copy, or a constraint an existing row would
    violate is never exercised.
  * Incremental: merge-base -> checkout. It does not prove an upgrade from
    every historical release.
  * The schema part of `database.py::init_database` (migrations, `init_schema`,
    migrations, on a `sqlite3.Row` connection as `get_db_connection` opens it)
    is restated here, not imported.
  * The merge-base's code runs under the checkout's installed dependencies. If
    it no longer imports, the guard fails closed (exit 2) and says so.

Usage:
    check_sqlite_upgrade_parity.py <base_sha> <head_sha>
    # or, with no args, reads PR_BASE_SHA / PR_HEAD_SHA from the environment.
    # Run from inside the repository; the checkout's src/backend is the head.

Exit codes: 0 = pass, 1 = an upgraded install misses something schema.py
declares (each object is named) or the checkout's lifecycle fails on it,
2 = the check could not run (no base/head, merge-base unresolvable, the
merge-base's tree missing or failing to build its install, the checkout's
db modules not importing). Exit 2 never means "no drift". Pure stdlib; no
third-party deps of its own.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

BACKEND_REL = "src/backend"
REQUIRED_FILES = ("db/schema.py", "db/migrations.py")
NULL_SHA = "0" * 40
SUBPROCESS_TIMEOUT_S = 600

EXIT_PASS = 0
EXIT_GAP = 1
EXIT_CANNOT_RUN = 2

# The runner's exit code when db/schema.py or db/migrations.py does not import.
RUNNER_LOAD_FAILED = 3

# Runs inside each subprocess: argv = backend_dir db_path out_path role.
# The `head` role also reports migration health and the declared snapshot.
_RUNNER = r"""
import importlib.util
import json
import sqlite3
import sys

backend, db_path, out_path, role = sys.argv[1:5]
sys.path.insert(0, backend)
LOAD_FAILED = 3


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, backend + "/" + rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot(cur):
    tables = sorted(
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ).fetchall()
    )
    columns = {}
    for t in tables:
        columns[t] = sorted(r[1] for r in cur.execute(f'PRAGMA table_info("{t}")').fetchall())
    indexes = sorted(
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' AND tbl_name != 'schema_migrations'"
        ).fetchall()
        if r[0] is not None
    )
    triggers = sorted(
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name != 'schema_migrations'"
        ).fetchall()
    )
    return {"tables": tables, "columns": columns, "indexes": indexes, "triggers": triggers}


try:
    schema = _load("db/schema.py", "_upgrade_parity_schema")
    migrations = _load("db/migrations.py", "_upgrade_parity_migrations")
except Exception:
    import traceback

    traceback.print_exc()
    sys.exit(LOAD_FAILED)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row  # as db/connection.py::get_db_connection opens it
cur = conn.cursor()
migrations.run_all_migrations(cur, conn)
schema.init_schema(cur, conn)
migrations.run_all_migrations(cur, conn)
conn.commit()

result = {"snapshot": _snapshot(cur)}
result["ledger_size"] = cur.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
if role == "head":
    applied, expected, first_pending = migrations.migration_health(cur)
    registered = {name for name, _ in migrations.MIGRATIONS}
    ledger = {r[0] for r in cur.execute("SELECT name FROM schema_migrations").fetchall()}
    result["health"] = [applied, expected, first_pending]
    result["unknown_ledger_names"] = sorted(ledger - registered)
    ref = sqlite3.connect(":memory:")
    ref_cur = ref.cursor()
    schema.init_schema(ref_cur, ref)
    result["declared"] = _snapshot(ref_cur)
    ref.close()
conn.close()

with open(out_path, "w") as fh:
    json.dump(result, fh)
"""


class CannotRun(Exception):
    """The guard could not evaluate; maps to exit 2."""


def _run_lifecycle(backend: Path, db_path: Path, out_path: Path, role: str) -> tuple[int, str]:
    """Run `_RUNNER` against `backend` in a fresh interpreter.

    Returns (returncode, combined output tail). A timeout is raised as
    CannotRun: a hung lifecycle is not evidence either way.
    """
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RUNNER, str(backend), str(db_path), str(out_path), role],
            cwd=str(backend),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CannotRun(f"the {role} lifecycle timed out after {SUBPROCESS_TIMEOUT_S}s") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output[-4000:]


def _require_backend(backend: Path, label: str) -> None:
    missing = [rel for rel in REQUIRED_FILES if not (backend / rel).is_file()]
    if missing:
        raise CannotRun(f"{label} tree at {backend} has no {', '.join(missing)}")


def find_gaps(declared: dict[str, Any], upgraded: dict[str, Any]) -> list[str]:
    """Every object `declared` (schema.py) has that `upgraded` lacks, named."""
    gaps: list[str] = []
    up_tables = set(upgraded["tables"])
    for table in sorted(set(declared["tables"]) - up_tables):
        gaps.append(
            f"table {table}: schema.py declares it but the upgraded install has none "
            "(a migration that runs after init_schema dropped it?)"
        )
    for table in sorted(set(declared["tables"]) & up_tables):
        have = set(upgraded["columns"].get(table, []))
        for column in sorted(set(declared["columns"].get(table, [])) - have):
            gaps.append(
                f"column {table}.{column}: schema.py declares it, but an install that "
                f"already has {table} never gets it on upgrade. Write the migration that "
                "adds it and register it in MIGRATIONS (src/backend/db/migrations.py)."
            )
    for index in sorted(set(declared["indexes"]) - set(upgraded["indexes"])):
        gaps.append(
            f"index {index}: missing after upgrade (a migration that runs after "
            "init_schema rebuilt its table without it? check _atomic_rebuild(indexes=...))"
        )
    for trigger in sorted(set(declared["triggers"]) - set(upgraded["triggers"])):
        gaps.append(
            f"trigger {trigger}: missing after upgrade (a migration that runs after "
            "init_schema rebuilt its table without it?)"
        )
    return gaps


def health_gaps(health: list[Any], unknown_ledger_names: list[str]) -> list[str]:
    """`migration_health` must be (n, n, None) on the upgraded install."""
    applied, expected, first_pending = health
    gaps: list[str] = []
    if first_pending is not None:
        gaps.append(
            f"migration {first_pending!r} is registered but not recorded after upgrade "
            f"({applied}/{expected} applied): /health would answer 503 on this install."
        )
    if unknown_ledger_names:
        gaps.append(
            "schema_migrations records names MIGRATIONS no longer registers: "
            f"{', '.join(unknown_ledger_names)}. A registered migration was renamed (it "
            "re-runs on every existing install) or removed (/health keeps counting its "
            "name). Keep the registered name."
        )
    if not gaps and applied != expected:
        gaps.append(f"migration_health is ({applied}, {expected}, None); expected ({expected}, {expected}, None).")
    return gaps


def check_trees(base_backend: Path, head_backend: Path, workdir: Path, *, base_label: str = "base") -> int:
    """Build an install with `base_backend`, upgrade it with `head_backend`, compare.

    The comparison core, free of git so the self-test can drive it over
    synthetic two-file backends.
    """
    base_backend, head_backend, workdir = base_backend.resolve(), head_backend.resolve(), workdir.resolve()
    try:
        _require_backend(base_backend, f"{base_label}")
        _require_backend(head_backend, "head")
    except CannotRun as exc:
        print(f"sqlite-upgrade-parity: CANNOT RUN — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    db_path = workdir / "upgrade.db"
    base_out = workdir / "base.json"
    head_out = workdir / "head.json"

    try:
        rc, output = _run_lifecycle(base_backend, db_path, base_out, "base")
    except CannotRun as exc:
        print(f"sqlite-upgrade-parity: CANNOT RUN — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if rc != 0 or not base_out.is_file():
        print(
            f"sqlite-upgrade-parity: CANNOT RUN — the {base_label} tree failed to build its "
            "install, so there is nothing to upgrade. This is infrastructure (for example a "
            "dependency the old code imports is gone), not schema drift; it fails closed "
            "rather than pass. If this PR removes or major-bumps a dependency that code "
            "imports, land the bump on its own first; if the branch is old, update it from "
            f"its base.\n--- {base_label} lifecycle output (tail) ---\n{output}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    try:
        rc, output = _run_lifecycle(head_backend, db_path, head_out, "head")
    except CannotRun as exc:
        print(f"sqlite-upgrade-parity: CANNOT RUN — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if rc == RUNNER_LOAD_FAILED:
        print(
            "sqlite-upgrade-parity: CANNOT RUN — the checkout's db/schema.py or "
            "db/migrations.py does not import in this environment (a missing dependency or "
            f"env var?), so no upgrade was attempted.\n--- head output (tail) ---\n{output}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN
    if rc != 0 or not head_out.is_file():
        print(
            "sqlite-upgrade-parity: FAIL — the checkout's init_database lifecycle "
            f"raised on an install built by the {base_label} tree. Every existing install "
            "would fail the same way at boot (for example an index on a column no "
            f"migration adds).\n--- head lifecycle output (tail) ---\n{output}",
            file=sys.stderr,
        )
        return EXIT_GAP

    base_ledger = json.loads(base_out.read_text())["ledger_size"]
    head = json.loads(head_out.read_text())
    gaps = find_gaps(head["declared"], head["snapshot"])
    gaps += health_gaps(head["health"], head["unknown_ledger_names"])
    declared = head["declared"]
    print(
        f"sqlite-upgrade-parity: upgraded an install built by the {base_label} tree; "
        f"schema.py declares {len(declared['tables'])} tables, "
        f"{sum(len(c) for c in declared['columns'].values())} columns, "
        f"{len(declared['indexes'])} indexes, {len(declared['triggers'])} triggers; "
        f"{head['ledger_size'] - base_ledger} migration(s) newly recorded on upgrade; "
        f"migration_health = {tuple(head['health'])}.",
        flush=True,
    )
    if gaps:
        print(
            "\nsqlite-upgrade-parity: FAIL — an existing install upgraded by this change "
            "does not end where a fresh one does:",
            file=sys.stderr,
        )
        for gap in gaps:
            print(f"  - {gap}", file=sys.stderr)
        return EXIT_GAP
    print("sqlite-upgrade-parity: PASS — the upgraded install has every object schema.py declares.", flush=True)
    return EXIT_PASS


def _git(args: list[str]) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _git_bytes(args: list[str]) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3):
        print("usage: check_sqlite_upgrade_parity.py [<base> <head>]", file=sys.stderr)
        return EXIT_CANNOT_RUN
    base = argv[1] if len(argv) == 3 else os.environ.get("PR_BASE_SHA", "")
    head = argv[2] if len(argv) == 3 else os.environ.get("PR_HEAD_SHA", "")
    if not base or not head or base == NULL_SHA:
        print(
            "sqlite-upgrade-parity: CANNOT RUN — no PR base/head (pass <base> <head> or set "
            "PR_BASE_SHA / PR_HEAD_SHA). Failing closed.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    try:
        repo = Path(_git(["rev-parse", "--show-toplevel"]).strip())
        merge_base = _git(["merge-base", base, head]).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or str(exc)
        print(
            f"sqlite-upgrade-parity: CANNOT RUN — could not resolve the merge-base of {base} and "
            f"{head} (a shallow clone needs fetch-depth: 0): {stderr.strip()}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    head_backend = repo / BACKEND_REL
    with tempfile.TemporaryDirectory(prefix="sqlite-upgrade-parity-") as tmp:
        tmp_path = Path(tmp)
        base_root = tmp_path / "base"
        base_root.mkdir()
        try:
            archive = _git_bytes(["archive", "--format=tar", merge_base, BACKEND_REL])
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            print(
                f"sqlite-upgrade-parity: CANNOT RUN — git archive {merge_base} {BACKEND_REL} "
                f"failed: {(detail or '').strip()}",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
                tar.extractall(base_root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            print(
                f"sqlite-upgrade-parity: CANNOT RUN — extracting {merge_base}:{BACKEND_REL} "
                f"failed: {exc}",
                file=sys.stderr,
            )
            return EXIT_CANNOT_RUN
        workdir = tmp_path / "work"
        workdir.mkdir()
        print(f"sqlite-upgrade-parity: merge-base {merge_base} (base {base}, head {head}).", flush=True)
        return check_trees(
            base_root / BACKEND_REL,
            head_backend,
            workdir,
            base_label=f"merge-base {merge_base[:12]}",
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
