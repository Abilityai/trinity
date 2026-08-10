#!/usr/bin/env python3
"""Emit the tests that must run in their OWN pytest process (#2080).

Some tests assert properties of a pristine interpreter — route ORDER on a
freshly imported `main`, a `sys.modules` state no other module has touched.
Collected alongside 8k siblings they cannot assert anything, so they
`pytest.skip(..., allow_module_level=True)` and tell the reader to run them
standalone. Nothing ever did: "documented as standalone" meant "not executed",
which is the shape of missing coverage that looks like a green suite.

The list is DERIVED from the tests' own instruction rather than maintained
here. Each such module documents itself with a literal

    Run standalone: pytest tests/unit/test_x.py

and this scanner extracts those paths. A new standalone test is picked up with
no edit to the runner, and a stale entry cannot outlive the file it names —
the two failure modes a hand-kept list in the harness would have.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent.parent
_REPO = _TESTS.parent

# `pytest <path>` as written in the skip message. Tolerates the repo-relative
# and tests-relative spellings both forms appear in.
_RUN_STANDALONE = re.compile(r"Run standalone:\s*pytest\s+(?P<path>[\w./\-]+\.py)")


def discover() -> list[str]:
    found: set[str] = set()
    for path in sorted(_TESTS.rglob("test_*.py")):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        # Collapse Python's implicit string concatenation first: these
        # messages are written across several quoted lines, so the sentinel and
        # the path it names are usually separated by `"\n        "`.
        flat = re.sub(r'"\s*\n\s*"', "", text)
        for match in _RUN_STANDALONE.finditer(flat):
            named = match.group("path")
            candidate = (_REPO / named) if named.startswith("tests/") else (_TESTS / named)
            # Trust the file that exists, not the string: a renamed module
            # would otherwise emit a path that silently collects nothing, and
            # `pytest` exits 5 for that — a "no tests ran" the runner treats
            # as a failure precisely so it cannot pass unnoticed.
            found.add(str(candidate.resolve() if candidate.exists() else path.resolve()))
    return sorted(found)


if __name__ == "__main__":
    paths = discover()
    if not paths:
        print("no standalone-documented tests found", file=sys.stderr)
    for p in paths:
        print(p)
