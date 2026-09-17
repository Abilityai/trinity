"""#2859 — evicting `services.git_service` from sys.modules must take its submodules with it.

#1028 (#2487) made `services.git_service` a package. A test that does
`sys.modules.pop("services.git_service")` and re-imports it gets a NEW package object,
but Python binds a submodule onto its parent only when the submodule is first LOADED —
and the submodules are still cached — so the new package has no `.token_scrub`,
`.conflicts`, ... attribute. Every later `git_service.<sub>` read in another file then
raises AttributeError (13 failures in test_ent615_token_free_remotes on every dev push
since #2487). Two guards here:

1. the mechanism, on a throwaway package, so the trap is documented executably;
2. a static scan of tests/unit for the parent-only eviction — the regression guard
   the issue asks for: it FAILS when a test pops only the package key.
"""

from __future__ import annotations

import ast
import importlib
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS_UNIT = Path(__file__).resolve().parent
FAMILY = "services.git_service"


# ---------------------------------------------------------------- 1. mechanism

@pytest.fixture
def throwaway_package(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg2859"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .sub import MARK\n")
    (pkg / "sub.py").write_text("MARK = 'loaded'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "pkg2859"
    for k in [k for k in list(sys.modules) if k == "pkg2859" or k.startswith("pkg2859.")]:
        monkeypatch.delitem(sys.modules, k, raising=False)


def test_popping_only_the_parent_loses_the_submodule_attribute(throwaway_package, monkeypatch):
    """The trap, executably: parent-only eviction → re-imported package lacks `.sub`."""
    name = throwaway_package
    mod = importlib.import_module(name)
    assert hasattr(mod, "sub"), "first import binds the submodule"

    monkeypatch.delitem(sys.modules, name)     # the pre-#2859 pattern: parent only
    reimported = importlib.import_module(name)
    assert reimported is not mod
    assert f"{name}.sub" in sys.modules, "submodule still cached — that is the leak"
    assert not hasattr(reimported, "sub"), (
        "if this ever passes, CPython changed import semantics and the guard below is moot"
    )


def test_evicting_the_family_rebinds_the_submodule(throwaway_package, monkeypatch):
    """The fix: evict every `<pkg>*` key → re-import loads submodules fresh and binds them."""
    name = throwaway_package
    importlib.import_module(name)
    for k in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
        monkeypatch.delitem(sys.modules, k)
    reimported = importlib.import_module(name)
    assert hasattr(reimported, "sub")
    assert reimported.sub.MARK == "loaded"


# ---------------------------------------------------------- 2. static guard

def _parent_only_evictions(path: Path) -> list[int]:
    """Line numbers of `sys.modules.pop("services.git_service", …)`,
    `del sys.modules["services.git_service"]` and
    `monkeypatch.delitem(sys.modules, "services.git_service")` — the literal
    package key evicted on its own. A loop over `startswith("services.git_service")`
    is the sanctioned shape and is not matched (its key is a Name, not a Constant)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []

    def is_sys_modules(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "modules"
                and isinstance(node.value, ast.Name) and node.value.id == "sys")

    def is_family_key(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == FAMILY

    for node in ast.walk(tree):
        # sys.modules.pop("services.git_service", ...)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "pop" and is_sys_modules(node.func.value)
                and node.args and is_family_key(node.args[0])):
            hits.append(node.lineno)
        # monkeypatch.delitem(sys.modules, "services.git_service")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delitem" and len(node.args) >= 2
                and is_sys_modules(node.args[0]) and is_family_key(node.args[1])):
            hits.append(node.lineno)
        # del sys.modules["services.git_service"]
        if isinstance(node, ast.Delete):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and is_sys_modules(t.value)
                        and is_family_key(t.slice)):
                    hits.append(node.lineno)
    return sorted(hits)


# test_2075 keeps `monkeypatch.delitem(sys.modules, "services.git_service")` as the
# *recorded-absence* trick right after a full-family sweep, and its autouse fixture
# restores the whole family on teardown — the pattern is safe there by construction.
_ALLOWLIST = {"test_2075_detect_git_dir.py"}


def test_no_test_evicts_only_the_git_service_package():
    offenders = {}
    for path in sorted(TESTS_UNIT.glob("test_*.py")):
        if path.name in _ALLOWLIST or path.name == Path(__file__).name:
            continue
        lines = _parent_only_evictions(path)
        if lines:
            offenders[path.name] = lines
    assert not offenders, (
        "parent-only eviction of the services.git_service package leaves its submodules "
        "cached and breaks the next file's `git_service.<sub>` reads (#2859). Evict the "
        "family instead:\n"
        "    for k in [k for k in list(sys.modules) if k.startswith('services.git_service')]:\n"
        "        del sys.modules[k]\n"
        f"offenders: {offenders}"
    )


def test_the_guard_catches_the_parent_only_pattern(tmp_path):
    """The guard must be red on the exact line the four polluters used to carry."""
    sample = tmp_path / "test_sample.py"
    sample.write_text(textwrap.dedent('''
        import sys
        def _load():
            sys.modules.pop("services.git_service", None)
            import services.git_service.trinity_files as gs
            return gs
        def _load2(monkeypatch):
            monkeypatch.delitem(sys.modules, "services.git_service")
        def _load3():
            del sys.modules["services.git_service"]
        def _fine():
            for k in [k for k in list(sys.modules) if k.startswith("services.git_service")]:
                del sys.modules[k]
    '''))
    assert _parent_only_evictions(sample) == [4, 8, 10]
