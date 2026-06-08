#!/usr/bin/env python3
"""
Lint check: ban bare `sys.modules` mutations in tests/.

Issue #762: cross-file sys.modules pollution is a recurring class of test bug.
Test files that do top-level `sys.modules["X"] = stub` (or `del`, `setdefault`,
`pop`, `update`) leak stubs across the pytest session and break unrelated tests
that re-import the same module later.

Allowed patterns:
  - inside an explicit allowlist of conftest paths
    (tests/conftest.py, tests/*/conftest.py)
  - via `monkeypatch.setitem(sys.modules, ...)` / `monkeypatch.delitem(...)`
  - named snapshot/restore helpers identified by the `_STUBBED_MODULE_NAMES`
    + `_restore_sys_modules` fixture pair already established in
    tests/unit/test_telegram_webhook_backfill.py
  - files listed in tests/lint_sys_modules_baseline.txt (pre-existing
    violations grandfathered in at adoption time, Issue #762; new violations
    in baselined files are NOT permitted — only the existing line count is)

Banned at any scope outside the allowlist (module level, function body, fixture
body):
  - `sys.modules[key] = value`
  - `sys.modules.setdefault(key, value)`
  - `sys.modules.update(...)`
  - `del sys.modules[key]`
  - `sys.modules.pop(key)`

CLI:
  python tests/lint_sys_modules.py
        Exit 0 if zero NEW violations vs baseline; 1 otherwise.
  python tests/lint_sys_modules.py --regenerate-baseline
        Overwrite tests/lint_sys_modules_baseline.txt with current state.
        Use only when intentionally accepting more violations or after a
        cleanup that retires some.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, NamedTuple


TESTS_ROOT = Path(__file__).resolve().parent
BASELINE_FILE = TESTS_ROOT / "lint_sys_modules_baseline.txt"

BANNED_METHODS = {"setdefault", "update", "pop"}
FIX_HINT = (
    "use monkeypatch.setitem(sys.modules, ...) or "
    "monkeypatch.delitem(sys.modules, ..., raising=False), "
    "or move to conftest.py. "
    "For import-time stubs that monkeypatch can't reach (e.g. preloads "
    "of heavy backend deps before any fixture runs), declare a top-level "
    "`_STUBBED_MODULE_NAMES = [...]` list + an autouse `_restore_sys_modules` "
    "fixture in this file (precedent: tests/unit/test_telegram_webhook_backfill.py)"
)


class Finding(NamedTuple):
    path: Path
    lineno: int
    col: int
    message: str


def _is_allowlisted_file(path: Path) -> bool:
    """Allow any conftest.py under tests/."""
    return path.name == "conftest.py" and path.is_relative_to(TESTS_ROOT)


def _is_sys_modules_subscript(node: ast.expr) -> bool:
    """Match `sys.modules[...]`."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "modules"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "sys"
    )


def _is_sys_modules_attr_call(node: ast.expr, methods: Iterable[str]) -> bool:
    """Match `sys.modules.<method>(...)` for a method in `methods`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in set(methods)
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "modules"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "sys"
    )


def _has_stubbed_module_names_helper(tree: ast.Module) -> bool:
    """Detect the named snapshot/restore helper exception.

    Matches a module that defines BOTH:
      - a top-level name `_STUBBED_MODULE_NAMES` (assigned)
      - a function/fixture named `_restore_sys_modules`
    This is the precedent set by tests/unit/test_telegram_webhook_backfill.py
    and is a self-contained snapshot/restore pattern the lint should permit.
    """
    has_names = False
    has_fixture = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_STUBBED_MODULE_NAMES":
                    has_names = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_restore_sys_modules":
                has_fixture = True
    return has_names and has_fixture


def _check_file(path: Path) -> list[Finding]:
    if _is_allowlisted_file(path):
        return []

    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    helper_exception = _has_stubbed_module_names_helper(tree)
    if helper_exception:
        return []

    findings: list[Finding] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_sys_modules_subscript(target):
                    findings.append(
                        Finding(
                            path=path,
                            lineno=node.lineno,
                            col=node.col_offset,
                            message=(
                                f"bare assignment to sys.modules[...]: {FIX_HINT}"
                            ),
                        )
                    )
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if _is_sys_modules_subscript(target):
                    findings.append(
                        Finding(
                            path=path,
                            lineno=node.lineno,
                            col=node.col_offset,
                            message=(
                                f"bare `del sys.modules[...]`: {FIX_HINT}"
                            ),
                        )
                    )
        if _is_sys_modules_attr_call(node, BANNED_METHODS):
            findings.append(
                Finding(
                    path=path,
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"bare `sys.modules.{node.func.attr}(...)`: {FIX_HINT}"
                    ),
                )
            )

    return findings


def iter_test_files(root: Path) -> Iterable[Path]:
    # Skip directories that contain third-party / generated code so the
    # baseline doesn't drift on dep upgrades or machine-local venvs.
    EXCLUDED_DIR_PARTS = {".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
    for path in sorted(root.rglob("*.py")):
        if path.name in {"lint_sys_modules.py", "test_lint_sys_modules.py"}:
            continue
        if EXCLUDED_DIR_PARTS.intersection(path.parts):
            continue
        yield path


def collect_findings(root: Path = TESTS_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_test_files(root):
        findings.extend(_check_file(path))
    return findings


def findings_per_file(findings: list[Finding]) -> Counter:
    """Count violations per relative path."""
    counter: Counter = Counter()
    for f in findings:
        rel = f.path.relative_to(TESTS_ROOT.parent).as_posix()
        counter[rel] += 1
    return counter


def load_baseline(baseline_path: Path = BASELINE_FILE) -> Counter:
    """Baseline format: lines of `<count> <relative_path>`.
    Lines starting with `#` are comments. Blank lines ignored."""
    counter: Counter = Counter()
    if not baseline_path.exists():
        return counter
    for line in baseline_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            count_str, rel = line.split(maxsplit=1)
            counter[rel] = int(count_str)
        except ValueError:
            continue
    return counter


def write_baseline(counter: Counter, baseline_path: Path = BASELINE_FILE) -> None:
    lines = [
        "# Auto-generated baseline for tests/lint_sys_modules.py (Issue #762).",
        "# Format: <count> <path-relative-to-repo-root>",
        "# Regenerate with: python tests/lint_sys_modules.py --regenerate-baseline",
        "# Goal: reduce these counts over time; never grow them.",
        "",
    ]
    for rel in sorted(counter):
        lines.append(f"{counter[rel]} {rel}")
    baseline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def diff_against_baseline(
    current: Counter, baseline: Counter
) -> tuple[list[str], list[str]]:
    """Return (new_or_grew, retired_or_shrank) summaries."""
    new_or_grew: list[str] = []
    retired_or_shrank: list[str] = []
    for path in sorted(set(current) | set(baseline)):
        cur = current.get(path, 0)
        base = baseline.get(path, 0)
        if cur > base:
            new_or_grew.append(
                f"  {path}: {base} → {cur} (+{cur - base})"
            )
        elif cur < base:
            retired_or_shrank.append(
                f"  {path}: {base} → {cur} (-{base - cur})"
            )
    return new_or_grew, retired_or_shrank


def main(argv: list[str]) -> int:
    if "--regenerate-baseline" in argv:
        findings = collect_findings()
        counter = findings_per_file(findings)
        write_baseline(counter)
        print(
            f"Wrote baseline with {sum(counter.values())} violation(s) "
            f"across {len(counter)} file(s) → {BASELINE_FILE.name}"
        )
        return 0

    findings = collect_findings()
    current = findings_per_file(findings)
    baseline = load_baseline()

    new_or_grew, retired_or_shrank = diff_against_baseline(current, baseline)

    if retired_or_shrank:
        print("Violations retired or reduced (👍 below baseline — consider regenerating):")
        for line in retired_or_shrank:
            print(line)
        print()

    if not new_or_grew:
        total = sum(current.values())
        baseline_total = sum(baseline.values())
        print(
            f"OK: {total} violation(s) in {len(current)} file(s); "
            f"baseline allows {baseline_total} — no new violations."
        )
        return 0

    # Print only the offending entries with line numbers.
    new_files = {entry.split(":")[0].strip() for entry in new_or_grew}
    for f in findings:
        rel = f.path.relative_to(TESTS_ROOT.parent).as_posix()
        # Print every finding for files that grew. Reviewers need to see
        # which specific lines pushed the count over the baseline.
        if any(rel in s for s in new_or_grew):
            print(f"{rel}:{f.lineno}:{f.col}: {f.message}")

    print()
    print("New or grown violations vs baseline:")
    for line in new_or_grew:
        print(line)
    print(
        f"\nFAIL: {len(new_or_grew)} file(s) exceed baseline. "
        f"Fix the new occurrences (use monkeypatch.setitem / monkeypatch.delitem) "
        f"or, if you intentionally accept more, run "
        f"`python tests/lint_sys_modules.py --regenerate-baseline`."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
