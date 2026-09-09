#!/usr/bin/env python3
"""Parse the Architecture Map table in `docs/memory/architecture.md` (#2306, #2642).

The map — which on-demand area file owns which code paths, and why a change
there should read it first — lives in exactly one place: the table in the
always-loaded core. This module is the one reader of that table. There is
deliberately no sidecar map: a second copy is a second thing to drift, and the
table is already the reviewed artifact.

Loading an area file is a *deliberate* step (#2642): a skill or a reader
resolves the owner of a path with `owner_for()` and reads it explicitly, at the
point the reasoning needs it. Nothing injects area files ambiently — the
`PreToolUse` hook that once promised to was never wired anywhere, and would
fire too late regardless (an edit comes after the reasoning that needed the
file).

Consumers: the map-lint half of `tests/unit/test_2306_architecture_split.py`
(bijection between rows and files on disk, every row carries owned paths and a
cited consequence) and dev-tooling skills that need path → area resolution.

    python3 scripts/docs/architecture_map.py src/backend/services/cleanup_service.py
    reliability.md
"""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

DOC_REL = "docs/memory/architecture.md"
AREA_REL = "docs/memory/architecture"
MAP_HEADING = "## Architecture Map"

# `| [`file.md`](architecture/file.md) | `glob`<br>`glob` | consequence |`
ROW = re.compile(r"^\|\s*\[`([^`]+)`\]\([^)]+\)\s*\|(.+?)\|(.+?)\|\s*$")
GLOB = re.compile(r"`([^`]+)`")


def repo_root(start: str | Path = ".") -> Path | None:
    p = Path(start).resolve()
    for cand in (p, *p.parents):
        if (cand / DOC_REL).is_file():
            return cand
    return None


def parse_map(doc: Path) -> list[tuple[str, list[str], str]]:
    """-> [(area_filename, [owned globs], consequence)] from the core's own table."""
    text = doc.read_text(encoding="utf-8")
    start = text.index(MAP_HEADING)
    section = text[start:]
    nxt = section.find("\n## ", 1)
    if nxt != -1:
        section = section[:nxt]
    out = []
    for line in section.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        fname, owns_cell, why = m.group(1), m.group(2), m.group(3)
        globs = GLOB.findall(owns_cell)
        if fname.endswith(".md") and globs:
            out.append((fname, globs, why.strip()))
    return out


def match_score(rel: str, pattern: str) -> int:
    """0 = no match, else the pattern's specificity (its literal prefix length).

    Most-specific-wins matters: `backend.md` owns `src/backend/services/**` as a
    catalog, but `cleanup_service.py` is documented in `reliability.md`. Naming
    both would point at the catalog on every single service edit.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-2]
        ok = rel.startswith(prefix) or rel == pattern[:-3]
    else:
        ok = fnmatch.fnmatch(rel, pattern)
    if not ok:
        return 0
    return len(pattern.split("*")[0])


def owner_for(rel: str, doc: Path) -> list[tuple[str, str]]:
    """-> [(area_filename, consequence)] for the most specific owner(s) of a repo-relative path.

    Empty when no row owns the path. More than one entry only when two rows tie
    on specificity, which the map is expected to avoid.
    """
    scored = []
    for fname, globs, why in parse_map(doc):
        best = max((match_score(rel, g) for g in globs), default=0)
        if best:
            scored.append((best, fname, why))
    if not scored:
        return []
    top = max(s for s, _, _ in scored)
    return [(f, why) for s, f, why in scored if s == top]


def main(argv: list[str]) -> int:
    root = repo_root()
    if root is None:
        print(f"not inside a checkout containing {DOC_REL}", file=sys.stderr)
        return 2
    doc = root / DOC_REL
    if not argv:
        for fname, globs, _ in parse_map(doc):
            print(f"{fname}\t{' '.join(globs)}")
        return 0
    rc = 0
    for arg in argv:
        try:
            rel = str(Path(arg).resolve().relative_to(root))
        except ValueError:
            rel = arg
        owners = owner_for(rel, doc)
        if owners:
            print(" ".join(f for f, _ in owners))
        else:
            print("-")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
