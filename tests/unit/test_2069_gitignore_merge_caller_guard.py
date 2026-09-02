"""#2069 — `_GITIGNORE_PATTERNS` stays the single source of truth (AC#3 / R4).

The acceptance criterion forbids a fourth call site with its own copy of the
fleet-wide ignore list: every moment the list is applied must go through
`_build_gitignore_merge_command`, and that command is the ONLY thing that reads
`_GITIGNORE_PATTERNS`.

This guard is an AST writer-SET check (the `test_ent109_git_env_seam.py`
precedent — a grep would read the constant's docstring mentions as writers, and
a `black` reflow could split a call across lines). It pins TWO sets:

  (a) the functions that CALL `_build_gitignore_merge_command` — the three merge
      moments: the Push migration, the init-path merge, and the #2069 creation
      seed. A fourth caller means a fourth application point to reason about.

  (b) the functions that LOAD `_GITIGNORE_PATTERNS` (an `ast.Name` load) — ONLY
      `_build_gitignore_merge_command`. This is the load-bearing half: it catches
      the bypass where someone reaches for the generic
      `_build_gitignore_append_command(git_dir, _GITIGNORE_PATTERNS)` (already
      called at `:719` with a DIFFERENT list) or hand-rolls a shell append,
      neither of which would trip a callers-only guard.

Scope: this parse is `git_service.py`-only. `services/compatibility/fixes.py` is
a legitimate separate cross-module consumer of `_GITIGNORE_PATTERNS` (the
gitignore auto-fix, #668) and is deliberately out of scope here — if the scan is
ever widened beyond this file, allowlist it.

Issue: abilityai/trinity#2069 (Epic #1045)
Target: src/backend/services/git_service/gitignore.py
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# #1028: git_service is a package. The single-source-of-truth contract spans
# it — `initialize_git_in_container` (provisioning) calls the builder through
# the sibling module object (`gitignore._build_gitignore_merge_command`), so
# the guard walks EVERY module and matches both the bare and the qualified
# call. A guard that scanned one file would silently lose a caller the moment
# it moved, which is precisely the erosion it exists to prevent.
_GIT_SERVICE_PKG = _PROJECT_ROOT / "src" / "backend" / "services" / "git_service"
_GIT_SERVICE_SRC = _GIT_SERVICE_PKG / "gitignore.py"  # pattern-load half stays module-local

_MERGE_BUILDER = "_build_gitignore_merge_command"
_PATTERN_CONST = "_GITIGNORE_PATTERNS"

# The three legitimate application points, and only these.
_EXPECTED_MERGE_CALLERS = {
    "_migrate_workspace_gitignore",  # on-Push migration (#462)
    "initialize_git_in_container",  # init-path merge (step 2)
    "merge_gitignore_after_clone",  # #2069 creation-time seed
}
# The ONE function allowed to read the constant.
_EXPECTED_PATTERN_LOADERS = {_MERGE_BUILDER}


def _tree() -> ast.AST:
    return ast.parse(_GIT_SERVICE_SRC.read_text())


def _fns_calling(target: str) -> set:
    """Every function in the PACKAGE containing a call to `target` — bare
    (same-module) or qualified through a sibling module object
    (`gitignore.<target>`, the #1028 cross-module call shape)."""
    found = set()
    for path in sorted(_GIT_SERVICE_PKG.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if isinstance(callee, ast.Name) and callee.id == target:
                    found.add(fn.name)
                elif isinstance(callee, ast.Attribute) and callee.attr == target:
                    found.add(fn.name)
    return found


def _fns_loading_name(target: str) -> set:
    """Every function with an `ast.Name` LOAD of `target` (a docstring mention is
    an `ast.Constant`, not a Name — so prose can't spoof this)."""
    tree = _tree()
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Name)
                and node.id == target
                and isinstance(node.ctx, ast.Load)
            ):
                found.add(fn.name)
    return found


def test_merge_command_callers_are_exactly_the_three_moments():
    """A FOURTH caller of `_build_gitignore_merge_command` means a fourth
    application point that has to be reasoned about (and readiness/idempotence
    re-checked). The count is the guard — pinning only the known three would stay
    green through a new one."""
    callers = _fns_calling(_MERGE_BUILDER)
    assert callers == _EXPECTED_MERGE_CALLERS, (
        "the _build_gitignore_merge_command caller set changed. #2069 adds "
        "merge_gitignore_after_clone; a further caller must be reviewed for the "
        f"single-source-of-truth contract (AC#3). Found: {sorted(callers)}"
    )


def test_pattern_constant_is_read_only_by_the_merge_builder():
    """The load-bearing half (R4): `_GITIGNORE_PATTERNS` must be read ONLY by
    `_build_gitignore_merge_command`. A new writer that feeds the constant into
    the generic `_build_gitignore_append_command` (already called at :719 with a
    DIFFERENT list) or hand-rolls a shell append would bypass the callers-only
    check — this pins it shut."""
    loaders = _fns_loading_name(_PATTERN_CONST)
    assert loaders == _EXPECTED_PATTERN_LOADERS, (
        "_GITIGNORE_PATTERNS is now read outside _build_gitignore_merge_command. "
        "Every application of the fleet-wide list must go through that one "
        f"builder (AC#3, single source of truth). Found: {sorted(loaders)}"
    )
