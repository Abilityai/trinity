#!/usr/bin/env python3
"""Alembic single-head guard: one head per version-line, both tracks (#2068).

An Alembic version-line must resolve to **exactly one head**. Two revisions
declaring the same `down_revision` are two heads, and `alembic upgrade head`
(singular — which is what both of Trinity's runners call) then raises
`CommandError: Multiple head revisions are present`. Git shows no conflict when
this lands: each revision file is independently valid, and the defect only
exists in the *relationship* between two files that were never edited together.

Why the failure is worth a dedicated guard rather than leaving it to a real
`alembic upgrade`:

  * On the OSS track a multi-head graph aborts `init_database()`, so the backend
    crash-loops and `/health` answers 503 — loud, but only at boot.
  * On the enterprise version-line the same fault is **silent**: the runner is
    invoked from the module registration seam, whose broad `except` degrades the
    instance to OSS-only. Containers stay green, `/health` stays healthy, gated
    routes answer 403, and the only evidence is one ERROR line at boot (#2068).

  Because `upgrade` resolves its target BEFORE applying anything, a two-head
  graph applies **zero** revisions — so every revision merged since the fork
  silently stops arriving too, not just the one that forked.

Method — the graph is read by PARSING, never by importing:

  * `ast.parse` + `ast.literal_eval` over each `*.py`, reading only the
    module-level `revision` / `down_revision` literals. No migration module is
    ever imported, so no `env.py`, `db.engine` or `DATABASE_URL` import chain is
    touched and no code from the parsed files executes.
  * A head is a revision that appears in no other revision's `down_revision`.
  * A **tuple/list** `down_revision` (an `alembic merge` revision) contributes
    **every** member as a parent, which is what lets a merge revision correctly
    collapse a forked graph back to one head. This is not optional: the guard
    ships alongside exactly such a revision.
  * `depends_on` is deliberately ignored — it expresses ordering, not parentage,
    and does not participate in head resolution.

False-positive guards (both exit 0 and say `skipped` out loud):

  1. the version directory does not exist — the normal state of an OSS clone or
     fork, where the private submodule is never initialised (`.gitmodules` sets
     `update = none`, so a plain `--init` skips it);
  2. the directory exists but holds no revision files — a partially-initialised
     checkout, or a path pointed one level too shallow.

Fail-closed condition: `*.py` revision files ARE present but none yields a
parseable `revision` → **fail**. A guard that reports green when it understood
nothing is worse than no guard at all.

Out of scope (stated so a green run is not over-read): this checks head COUNT
only. It does not verify that every `down_revision` resolves to a revision that
exists, and it does not execute the migrations — the `pg-migrations` job's real
`alembic upgrade head` remains the authority on whether the OSS line applies.

A BYTE-IDENTICAL copy of this file is vendored into the private enterprise
submodule's own repository, whose migration workflow runs the same assertion at
the branch point (public CI never checks that submodule out, so the arm below
skips there). Keep the two in sync — that workflow cross-checks this parser
against `alembic heads`, which is the only place both are available.

Usage:
    check_alembic_heads.py <versions-dir> [<versions-dir> ...]

Exit codes: 0 = pass (one head, or skipped), 1 = multi-head / unparseable,
2 = usage error. Pure stdlib; no third-party deps, no alembic install required.
"""
from __future__ import annotations

import ast
import sys
from collections import deque
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence


class Revision(NamedTuple):
    """One parsed revision file."""

    revision: str
    down_revisions: tuple[str, ...]
    filename: str


_REVISION = "revision"
_DOWN_REVISION = "down_revision"


# --- parsing ------------------------------------------------------------------


def _literal_assignments(source: str) -> dict[str, Any]:
    """Module-level literal assignments to `revision` / `down_revision`.

    Handles both the plain (`revision = "x"`) and annotated
    (`revision: str = "x"`) templates. Anything non-literal — a name, a call, an
    f-string — is skipped rather than guessed at, and a file that does not parse
    at all yields nothing. Reading via `ast` rather than a regex is what makes
    docstring prose that happens to contain `down_revision = ...` inert.
    """
    values: dict[str, Any] = {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return values

    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: Sequence[ast.expr] = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in (_REVISION, _DOWN_REVISION):
                continue
            try:
                values[target.id] = ast.literal_eval(value)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
    return values


def _normalize_down(value: Any) -> tuple[str, ...]:
    """Normalize a `down_revision` literal to a tuple of parent ids.

    `None` (a base revision) → `()`. A string → one parent. A tuple/list (an
    `alembic merge` revision) → EVERY member, which is the whole reason a merge
    revision resolves a fork.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (tuple, list)):
        return tuple(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    return ()


def parse_revision_source(source: str) -> tuple[str | None, tuple[str, ...]]:
    """Return `(revision_id, parent_ids)` for one revision file's source."""
    values = _literal_assignments(source)
    revision = values.get(_REVISION)
    if not isinstance(revision, str) or not revision.strip():
        return None, ()
    return revision.strip(), _normalize_down(values.get(_DOWN_REVISION))


def revision_files(directory: Path | str) -> list[Path]:
    """Candidate revision files: `*.py` excluding `__init__.py`.

    Excluding `__init__.py` matters for the fail-closed rule — a versions
    directory holding only a package marker must read as "no revisions here"
    (skip), not as "revision files that refused to parse" (fail).
    """
    return sorted(
        path
        for path in Path(directory).glob("*.py")
        if path.name != "__init__.py"
    )


def collect_graph(directory: Path | str) -> list[Revision]:
    """Parse every revision file in `directory` (unparseable files omitted)."""
    graph: list[Revision] = []
    for path in revision_files(directory):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        revision, parents = parse_revision_source(source)
        if revision is None:
            continue
        graph.append(Revision(revision, parents, path.name))
    return graph


# --- graph analysis -----------------------------------------------------------


def find_heads(revisions: Iterable[Revision]) -> list[str]:
    """Revisions that no other revision declares as a parent, sorted."""
    revisions = list(revisions)
    parents: set[str] = set()
    for rev in revisions:
        parents.update(rev.down_revisions)
    return sorted({rev.revision for rev in revisions} - parents)


def _ancestor_depths(by_id: dict[str, Revision], start: str) -> dict[str, int]:
    """Breadth-first ancestor walk from `start`, id → shortest distance."""
    depths: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        current, depth = queue.popleft()
        if current in depths:
            continue
        depths[current] = depth
        node = by_id.get(current)
        if node is None:
            continue
        for parent in node.down_revisions:
            if parent not in depths:
                queue.append((parent, depth + 1))
    return depths


def shared_parent(by_id: dict[str, Revision], heads: Sequence[str]) -> str | None:
    """The nearest revision that is an ancestor of every head (the fork point)."""
    if len(heads) < 2:
        return None
    depth_maps = [_ancestor_depths(by_id, head) for head in heads]
    common = set(depth_maps[0])
    for depths in depth_maps[1:]:
        common &= set(depths)
    common -= set(heads)
    if not common:
        return None
    return min(
        sorted(common),
        key=lambda rid: max(depths[rid] for depths in depth_maps),
    )


# --- the check ----------------------------------------------------------------


def check_directory(directory: Path | str) -> int:
    """Assert one head in `directory`. 0 = pass or skip, 1 = fail."""
    path = Path(directory)
    label = str(directory)

    if not path.is_dir():
        print(
            f"alembic-heads: {label} — version directory absent "
            "(submodule not initialised) — skipped."
        )
        return 0

    files = revision_files(path)
    if not files:
        print(
            f"alembic-heads: {label} — no revision files present "
            "(submodule not initialised) — skipped."
        )
        return 0

    graph = collect_graph(path)
    if not graph:
        print(
            f"\nalembic-heads: FAIL — {label} holds {len(files)} revision file(s) "
            "but NONE declares a parseable `revision`.\n"
            "The guard understood nothing here, which it reports as a failure "
            "rather than a pass: a vacuously-green head check is worse than no "
            "check. Confirm the files are Alembic revisions and that `revision` "
            "is a module-level string literal.",
            file=sys.stderr,
        )
        return 1

    heads = find_heads(graph)
    if len(heads) == 1:
        print(
            f"alembic-heads: {label} — {len(graph)} revision(s), "
            f"1 head ({heads[0]}) — PASS."
        )
        return 0

    by_id = {rev.revision: rev for rev in graph}
    by_id_files = {rev.revision: rev.filename for rev in graph}

    if not heads:
        print(
            f"\nalembic-heads: FAIL — {label} resolves to 0 heads across "
            f"{len(graph)} revision(s).\n"
            "Every revision is named as some other revision's parent, so the "
            "graph contains a CYCLE. Alembic cannot resolve `head` at all.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nalembic-heads: FAIL — {label} resolves to {len(heads)} heads "
        f"across {len(graph)} revision(s); exactly 1 is required.",
        file=sys.stderr,
    )
    for head in heads:
        print(f"  • {head}  ({by_id_files.get(head, '?')})", file=sys.stderr)
    fork = shared_parent(by_id, heads)
    if fork:
        print(
            f"\nThey fork at: {fork}  ({by_id_files.get(fork, '?')})",
            file=sys.stderr,
        )
    else:
        print("\nThe heads share no common ancestor in this directory.", file=sys.stderr)
    print(
        "\n`alembic upgrade head` is singular and resolves its target BEFORE "
        "applying anything,\nso this graph applies ZERO revisions — every "
        "revision since the fork stops arriving,\nnot only the one that forked. "
        "Fix by chaining the newer revision off the real head,\nor — if the "
        "forked revision may already be applied somewhere — by adding a merge\n"
        "revision (`alembic merge -m \"…\" <head-a> <head-b>`), whose tuple "
        "`down_revision`\nconverges the line from any starting state. See "
        "Architectural Invariant #3.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str]) -> int:
    directories = argv[1:]
    if not directories:
        print(
            "usage: check_alembic_heads.py <versions-dir> [<versions-dir> ...]",
            file=sys.stderr,
        )
        return 2
    # Evaluate EVERY directory before returning, so one failure does not hide
    # the state of the others in the CI log.
    results = [check_directory(directory) for directory in directories]
    return max(results)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
