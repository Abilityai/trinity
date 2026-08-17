#!/usr/bin/env python3
"""Lint check: keep self-contained tests OUT of the tests/ root, and keep async
unit tests explicitly marked.

Issue #1895. The per-PR CI unit job runs ``cd tests && pytest unit/`` and
``tests/unit/pytest.ini`` seals that island (``norecursedirs = ..``). So a
``test_*.py`` that sits in the ``tests/`` **root** is collected by NO per-PR job
— its coverage is invisible to the merge gate (this is exactly how #1880
shipped). And once a test lands under ``tests/unit/``, the island runs
pytest-asyncio in **strict** mode (``tests/unit/pytest.ini`` sets no
``asyncio_mode``), so an ``async def test_`` with no ``pytest.mark.asyncio``
marker does not run — on the current toolchain it fails outright, and on older
pytest-asyncio it passes vacuously; either way the coverage is a lie.

This guard is AST-parse-only (it never imports or execs a test module, so it
cannot trigger a test file's import-time side effects). It runs in the existing
``lint-sys-modules`` job (push + PR, not path-filtered) alongside
``lint_sys_modules.py``.

Part 1 — root placement. Every root-level ``tests/test_*.py`` must be one of:
  1. a live-backend test — some test/fixture function takes a live fixture
     (api_client / created_agent / ws_ticket / …) as a parameter it does not
     locally redefine;
  2. explicitly marked ``# allow-root-live-test: <reason>`` (a raw-httpx /
     ws_connect live test that takes no live *fixture* parameter);
  3. grandfathered in ``lint_root_test_placement_baseline.txt`` (the live-in-root
     set at adoption; ratcheted — never grows).
Anything else is a self-contained test in the wrong drawer → move it to
``tests/unit/``.

Part 2 — async markers under tests/unit/. Every ``async def test_`` under
``tests/unit/`` must have a resolvable ``pytest.mark.asyncio`` marker (its own
decorators, its enclosing class's ``pytestmark``, or the module's
``pytestmark``).

CLI:
  python tests/lint_root_test_placement.py
        Exit 0 if no NEW root-placement violation and no unmarked-async
        violation; 1 otherwise.
  python tests/lint_root_test_placement.py --regenerate-baseline
        Overwrite lint_root_test_placement_baseline.txt with the current
        live-in-root set. Use only when intentionally accepting more root files.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

TESTS_ROOT = Path(__file__).resolve().parent
BASELINE_FILE = TESTS_ROOT / "lint_root_test_placement_baseline.txt"
UNIT_DIR = TESTS_ROOT / "unit"

# Fixtures defined in tests/conftest.py that require a live backend/agent. A
# test (or fixture) that requests one of these as a parameter genuinely needs a
# running stack and therefore belongs in the tests/ root, not the unit island.
LIVE_FIXTURES = frozenset(
    {
        "api_client",
        "unauthenticated_client",
        "created_agent",
        "stopped_agent",
        "shared_agent",
        "isolated_agent",
        "module_agent_name",
        "pre_existing_agent",
        "ws_ticket",
    }
)

ALLOW_COMMENT = "allow-root-live-test:"


class RootFinding(NamedTuple):
    path: Path


class AsyncFinding(NamedTuple):
    path: Path
    lineno: int
    test_name: str


# ---------------------------------------------------------------------------
# Shared AST helpers
# ---------------------------------------------------------------------------


def _is_test_func(node: ast.AST) -> bool:
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test")


def _param_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Positional + keyword-only parameter names, excluding self/cls."""
    args = func.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return [n for n in names if n not in ("self", "cls")]


def _iter_funcs(tree: ast.Module) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


# ---------------------------------------------------------------------------
# Part 1 — root placement
# ---------------------------------------------------------------------------


def _local_fixture_shadows(tree: ast.Module) -> set[str]:
    """Live-fixture names the file redefines locally (e.g. canary/idempotency
    ``def api_client(): yield None``). Such a parameter resolves to the local
    override, NOT the live fixture, so it is not a live-backend signal."""
    shadows: set[str] = set()
    for func in _iter_funcs(tree):
        if func.name in LIVE_FIXTURES:
            shadows.add(func.name)
    return shadows


def _requests_live_fixture(tree: ast.Module) -> bool:
    """True if any test/fixture function requests a live fixture it does not
    locally redefine."""
    shadows = _local_fixture_shadows(tree)
    for func in _iter_funcs(tree):
        for name in _param_names(func):
            if name in LIVE_FIXTURES and name not in shadows:
                return True
    return False


def _root_file_ok(path: Path, source: str, baseline: set[str]) -> bool:
    rel = path.relative_to(TESTS_ROOT.parent).as_posix()
    if rel in baseline:
        return True
    if ALLOW_COMMENT in source:
        return True
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Can't parse → don't block; a syntax error surfaces elsewhere.
        return True
    return _requests_live_fixture(tree)


def iter_root_test_files() -> Iterable[Path]:
    """Root-level tests/test_*.py only (never tests/**)."""
    for path in sorted(TESTS_ROOT.glob("test_*.py")):
        if path.name == "test_lint_root_test_placement.py":
            continue  # this guard's own test uses live-fixture samples as data
        yield path


def collect_root_findings(baseline: set[str]) -> list[RootFinding]:
    findings: list[RootFinding] = []
    for path in iter_root_test_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not _root_file_ok(path, source, baseline):
            findings.append(RootFinding(path=path))
    return findings


# ---------------------------------------------------------------------------
# Part 2 — async markers under tests/unit/
# ---------------------------------------------------------------------------


def _expr_has_asyncio_marker(node: ast.expr) -> bool:
    """True if an expression is (or contains) a ``pytest.mark.asyncio`` marker.

    Handles: ``pytest.mark.asyncio``, ``mark.asyncio``, a Call to either, and a
    list/tuple containing any of the above (``pytestmark = [pytest.mark.asyncio,
    pytest.mark.skip(...)]``).
    """
    if isinstance(node, ast.Call):
        return _expr_has_asyncio_marker(node.func)
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_expr_has_asyncio_marker(elt) for elt in node.elts)
    if isinstance(node, ast.Attribute):
        # ...mark.asyncio  (accept whether prefixed with pytest. or not)
        if node.attr == "asyncio" and isinstance(node.value, ast.Attribute):
            return node.value.attr == "mark"
    return False


def _pytestmark_has_asyncio(body: list[ast.stmt]) -> bool:
    """True if a module/class body assigns ``pytestmark`` an asyncio marker."""
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if _expr_has_asyncio_marker(stmt.value):
                        return True
        elif isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id == "pytestmark"
                and stmt.value is not None
                and _expr_has_asyncio_marker(stmt.value)
            ):
                return True
    return False


def _decorator_has_asyncio(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> bool:
    """True if any decorator on a function OR class is a pytest.mark.asyncio
    marker. A class decorated ``@pytest.mark.asyncio`` marks all its methods
    (the form test_backlog.py / many unit files use)."""
    return any(_expr_has_asyncio_marker(dec) for dec in node.decorator_list)


def _collect_unmarked_async(
    body: list[ast.stmt],
    path: Path,
    *,
    module_marked: bool,
    class_marked: bool,
) -> list[AsyncFinding]:
    findings: list[AsyncFinding] = []
    for stmt in body:
        if isinstance(stmt, ast.AsyncFunctionDef) and stmt.name.startswith("test"):
            if module_marked or class_marked or _decorator_has_asyncio(stmt):
                continue
            findings.append(
                AsyncFinding(path=path, lineno=stmt.lineno, test_name=stmt.name)
            )
        elif isinstance(stmt, ast.ClassDef):
            cls_marked = (
                class_marked
                or _decorator_has_asyncio(stmt)
                or _pytestmark_has_asyncio(stmt.body)
            )
            findings.extend(
                _collect_unmarked_async(
                    stmt.body,
                    path,
                    module_marked=module_marked,
                    class_marked=cls_marked,
                )
            )
    return findings


def collect_async_findings() -> list[AsyncFinding]:
    findings: list[AsyncFinding] = []
    if not UNIT_DIR.is_dir():
        return findings
    for path in sorted(UNIT_DIR.glob("test_*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        module_marked = _pytestmark_has_asyncio(tree.body)
        findings.extend(
            _collect_unmarked_async(
                tree.body, path, module_marked=module_marked, class_marked=False
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Baseline I/O (Part 1 only)
# ---------------------------------------------------------------------------


def load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    out: set[str] = set()
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    return out


def write_baseline(paths: Iterable[str]) -> None:
    lines = [
        "# Auto-generated baseline for tests/lint_root_test_placement.py (#1895).",
        "# Root test files grandfathered at adoption — they take no live *fixture*",
        "# parameter, so part 1 flags them, but they stay in tests/ root because:",
        "#   - they hit a live stack over raw httpx/ws (test_public_*, slack), or",
        "#   - they import a dependency the unit venv omits (trinity_cli), or",
        "#   - they are currently red-and-hidden: broken vs a product change while",
        "#     uncollected, so moving them would redden the per-PR gate. Fixing the",
        "#     drift and relocating each is a separate follow-up (see the batch",
        "#     commit messages: cb_probe #1804, github_pat AgentGitConfig,",
        "#     ip_rate_limit assert_owns #1310, audit_log_unit).",
        "# Ratcheted — never grow this list; a NEW self-contained root test must go",
        "# to tests/unit/ instead. Regenerate (only when intentionally accepting",
        "# more): python tests/lint_root_test_placement.py --regenerate-baseline",
        "",
    ]
    lines.extend(sorted(paths))
    BASELINE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_root_findings(findings: list[RootFinding]) -> None:
    for f in findings:
        rel = f.path.relative_to(TESTS_ROOT.parent).as_posix()
        print(
            f"{rel}: self-contained test in tests/ root is collected by NO per-PR "
            f"CI job (#1895) — move it to tests/unit/, or add "
            f"`# allow-root-live-test: <reason>` if it needs a live backend."
        )


def _print_async_findings(findings: list[AsyncFinding]) -> None:
    for f in findings:
        rel = f.path.relative_to(TESTS_ROOT.parent).as_posix()
        print(
            f"{rel}:{f.lineno}: async def {f.test_name} under tests/unit/ has no "
            f"asyncio marker; pytest-asyncio strict mode does not run it (#1895). "
            f"Add @pytest.mark.asyncio (function / class pytestmark / module "
            f"pytestmark)."
        )


def main(argv: list[str]) -> int:
    if "--regenerate-baseline" in argv:
        # Baseline the current live-in-root set (files that fail part 1 with an
        # empty baseline).
        findings = collect_root_findings(baseline=set())
        rels = [f.path.relative_to(TESTS_ROOT.parent).as_posix() for f in findings]
        write_baseline(rels)
        print(
            f"Wrote baseline with {len(rels)} live-in-root file(s) "
            f"→ {BASELINE_FILE.name}"
        )
        return 0

    baseline = load_baseline()
    root_findings = collect_root_findings(baseline)
    async_findings = collect_async_findings()

    if not root_findings and not async_findings:
        print(
            f"OK: no self-contained tests in tests/ root (baseline allows "
            f"{len(baseline)}); no unmarked async under tests/unit/."
        )
        return 0

    if root_findings:
        _print_root_findings(root_findings)
    if async_findings:
        _print_async_findings(async_findings)

    print()
    print(
        f"FAIL: {len(root_findings)} misplaced root test(s), "
        f"{len(async_findings)} unmarked async unit test(s). See messages above "
        f"(#1895). If a root file genuinely needs a live backend, add "
        f"`# allow-root-live-test:` or, only when intentional, run "
        f"`python tests/lint_root_test_placement.py --regenerate-baseline`."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
