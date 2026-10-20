#!/usr/bin/env python3
"""Every test directory under tests/ must be owned by exactly one tier (#2888).

`run-full.sh` derives the api tier's `--ignore=` list from `TIER_DIRS` +
`NON_TIER_DIRS`, and that derived list is only as good as its inputs: a new
directory that nobody wired would be swept into `api` a second time. So the
list is checked against the tree, and an unwired directory fails the run by
name.

The check used to live inline in the runner as `for _d in tests/*/`, evaluated
AFTER the script had `cd`'d into `tests/` — so the glob looked for
`tests/tests/*/`, matched nothing, stayed a literal `*` under bash's default
globbing, failed classification, and `exit 1`'d the run before the `api`,
`standalone` and `postgres` tiers. Every full-suite run for three weeks was
truncated by its own guard. It is a separate program now for two reasons: it
takes the directory to scan as an explicit argument rather than trusting the
caller's cwd, and it can be tested — the runner cannot be run under pytest, so
the inline guard was the one part of the harness with no test at all.

Only a directory that CONTAINS tests is a finding. `__pycache__`, `reports/`,
the venv, and a stale local checkout holding nothing but `node_modules` are
not collected by anything, so naming them would make the guard fail every run
on every machine — which is the same silent-truncation failure with the sign
flipped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Never a finding, whatever they contain: not test directories by construction.
_ALWAYS_IGNORED = frozenset({"__pycache__", "reports", "node_modules"})


def _holds_tests(directory: Path) -> bool:
    """Is there a ``test_*.py`` anywhere pytest would look? Walks with the same
    pruning pytest applies by default (``node_modules``, dot-dirs, caches), so
    a vendored tree's own test files do not turn a junk directory into a tier."""
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _ALWAYS_IGNORED and not d.startswith(".")]
        if any(f.startswith("test_") and f.endswith(".py") for f in files):
            return True
    return False


def unclassified_test_dirs(tests_dir: Path, known: set[str]) -> list[str]:
    """Directories directly under ``tests_dir`` that hold a ``test_*.py`` and
    are in neither owner list. Sorted, so the message is stable."""
    found: list[str] = []
    for child in sorted(tests_dir.iterdir()):
        name = child.name
        if not child.is_dir() or name in known or name in _ALWAYS_IGNORED:
            continue
        if name.startswith("."):
            continue
        if not _holds_tests(child):
            continue
        found.append(name)
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_test_dirs.py TESTS_DIR KNOWN_DIR...", file=sys.stderr)
        return 2
    tests_dir = Path(argv[1])
    if not tests_dir.is_dir():
        print(f"check_test_dirs: not a directory: {tests_dir}", file=sys.stderr)
        return 2
    known = set(argv[2:])
    # An empty owner set is a wiring error, not "nothing is owned".
    if not known:
        print("check_test_dirs: no owner directories given", file=sys.stderr)
        return 2
    # Any owner that is not on disk is a stale entry — a renamed tier directory
    # would otherwise keep its `--ignore=` line while its new name got swept
    # into `api`. The pytest `--ignore` of a missing path is silent, so this is
    # the only place it can be caught.
    stale = sorted(d for d in known if not (tests_dir / d).is_dir())
    unclassified = unclassified_test_dirs(tests_dir, known)
    if not stale and not unclassified:
        return 0
    print("\n\033[1;31m== tests/run-full.sh: test-directory wiring is wrong\033[0m")
    if unclassified:
        print("   unclassified test directories:")
        for name in unclassified:
            print(f"     {name}/")
        print("   Add each to TIER_DIRS (with its own run_tier line) or to NON_TIER_DIRS.")
        print("   Left unwired they are collected a SECOND time by the api tier.")
    if stale:
        print("   owner entries with no directory on disk:")
        for name in stale:
            print(f"     {name}/")
        print("   Remove them from TIER_DIRS / NON_TIER_DIRS, or restore the directory.")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
