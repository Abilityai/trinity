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

If the merge-base cannot build its install (its own lifecycle raises: the
base branch is itself broken, and this PR may be the one that repairs it), the
guard walks back the merge-base's first-parent ancestors, at most
`MAX_BASE_WALKBACK` of them, to the newest one that builds, upgrades from that
one instead, and prints a warning naming every commit it skipped. This is not
fail-open: the checkout still has to upgrade an install the older tree built,
a longer range that covers every migration the skipped commits register as
well as the PR's own, so a gap is still reported. When no tree within the bound
builds, or a lifecycle times out, the guard exits 2.

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
    it no longer imports, it counts as a tree that cannot build: the walk-back
    above applies, and exit 2 when it is exhausted.
  * Open-source runner only. It exercises `db/migrations.py` and `db/schema.py`
    and never the enterprise runner (`enterprise/backend/_migrations.py`), so
    the tables only that runner creates, and their drift, are out of scope.

Usage:
    check_sqlite_upgrade_parity.py <base_sha> <head_sha>
    # or, with no args, reads PR_BASE_SHA / PR_HEAD_SHA from the environment.
    # Run from inside the repository; the checkout's src/backend is the head.

Exit codes: 0 = pass, 1 = an upgraded install misses something schema.py
declares (each object is named) or the checkout's lifecycle fails on it,
2 = the check could not run (no base/head, merge-base unresolvable, the
merge-base's tree missing, neither it nor any first-parent ancestor within
`MAX_BASE_WALKBACK` building its install, the checkout's db modules not
importing). Exit 2 never means "no drift". Pure stdlib; no
third-party deps of its own.
"""
from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

BACKEND_REL = "src/backend"
REQUIRED_FILES = ("db/schema.py", "db/migrations.py")
NULL_SHA = "0" * 40
SUBPROCESS_TIMEOUT_S = 600

# How many first-parent ancestors of the merge-base are tried, newest first,
# when the merge-base itself cannot build its install (a broken base branch).
# Past this bound the guard exits 2. A PR that has to go further raises it here,
# in review, rather than by editing branch protection.
MAX_BASE_WALKBACK = 20

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


def check_trees(
    base_backend: Path,
    head_backend: Path,
    workdir: Path,
    *,
    base_label: str = "base",
    older_bases: Iterable[tuple[str, Path]] = (),
) -> int:
    """Build an install with `base_backend`, upgrade it with `head_backend`, compare.

    If `base_backend` cannot build its install, each of `older_bases` (label,
    backend), consumed lazily and in order, is tried instead; the first that
    builds is upgraded, and a warning names every tree skipped.

    The comparison core, free of git so the self-test can drive it over
    synthetic two-file backends.
    """
    head_backend, workdir = head_backend.resolve(), workdir.resolve()
    head_out = workdir / "head.json"
    failures: list[tuple[str, str]] = []  # (label, lifecycle output tail) per tree that could not build
    built: tuple[str, Path, Path] | None = None
    try:
        _require_backend(head_backend, "head")
        candidates = itertools.chain([(base_label, base_backend)], older_bases)
        for attempt, (label, backend) in enumerate(candidates):
            backend = backend.resolve()
            _require_backend(backend, label)
            attempt_dir = workdir / f"base-{attempt}"
            attempt_dir.mkdir()
            db_path, base_out = attempt_dir / "upgrade.db", attempt_dir / "base.json"
            rc, output = _run_lifecycle(backend, db_path, base_out, "base")
            if rc == 0 and base_out.is_file():
                built = (label, db_path, base_out)
                break
            failures.append((label, output))
    except CannotRun as exc:
        print(f"sqlite-upgrade-parity: CANNOT RUN — {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if built is None:
        tried = ", ".join(label for label, _ in failures)
        print(
            f"sqlite-upgrade-parity: CANNOT RUN — no tree within the walk-back bound "
            f"(MAX_BASE_WALKBACK = {MAX_BASE_WALKBACK}) built its install, so there is nothing "
            f"to upgrade. Tried: {tried}. This is infrastructure (for example a dependency the "
            "old code imports is gone, or the base branch has been broken for longer than the "
            "bound), not schema drift; it fails closed rather than pass. If this PR removes or "
            "major-bumps a dependency the old code imports, first land a PR that stops "
            "importing it (or works with both versions), then the bump; if the base branch is "
            "broken past the bound, raise MAX_BASE_WALKBACK in the fix PR."
            f"\n--- {base_label} lifecycle output (tail) ---\n{failures[0][1]}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN
    base_label, db_path, base_out = built
    if failures:
        print(
            f"sqlite-upgrade-parity: WARNING — {failures[0][0]} failed to build its install (is "
            f"the base branch itself broken?), so the install is built by {base_label}, the "
            "newest that builds; skipped: "
            f"{', '.join(label.rsplit(' ', 1)[-1] for label, _ in failures)}. The checkout "
            "still has to upgrade over this longer range, so a gap is still a gap."
            f"\n--- {failures[0][0]} lifecycle output (tail) ---\n{failures[0][1][-1500:]}",
            file=sys.stderr,
            flush=True,
        )

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


def _extract_tree(sha: str, dest: Path) -> Path:
    """`git archive` `sha`'s src/backend under `dest`; return that backend dir."""
    dest.mkdir(parents=True)
    try:
        archive = _git_bytes(["archive", "--format=tar", sha, BACKEND_REL])
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise CannotRun(f"git archive {sha} {BACKEND_REL} failed: {(detail or '').strip()}") from exc
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise CannotRun(f"extracting {sha}:{BACKEND_REL} failed: {exc}") from exc
    return dest / BACKEND_REL


def _ancestor_trees(merge_base: str, root: Path) -> Iterator[tuple[str, Path]]:
    """Yield the merge-base's first-parent ancestors, newest first, at most
    MAX_BASE_WALKBACK, each extracted only when the previous one failed."""
    try:
        revs = _git(["rev-list", "--first-parent", f"--max-count={MAX_BASE_WALKBACK + 1}", merge_base])
    except subprocess.CalledProcessError as exc:
        raise CannotRun(f"git rev-list --first-parent {merge_base} failed: {(exc.stderr or '').strip()}") from exc
    for sha in revs.split()[1:]:
        yield f"first-parent ancestor {sha[:12]}", _extract_tree(sha, root / sha)


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
        try:
            base_backend = _extract_tree(merge_base, tmp_path / "base")
        except CannotRun as exc:
            print(f"sqlite-upgrade-parity: CANNOT RUN — {exc}", file=sys.stderr)
            return EXIT_CANNOT_RUN
        workdir = tmp_path / "work"
        workdir.mkdir()
        print(f"sqlite-upgrade-parity: merge-base {merge_base} (base {base}, head {head}).", flush=True)
        return check_trees(
            base_backend,
            head_backend,
            workdir,
            base_label=f"merge-base {merge_base[:12]}",
            older_bases=_ancestor_trees(merge_base, tmp_path / "ancestors"),
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
