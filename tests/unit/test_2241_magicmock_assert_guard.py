"""A bare `MagicMock()` may not stub a module that exports an `assert_*` name (#2241).

`unittest.mock.MagicMock` raises `AttributeError` for any attribute whose name
starts with `assert` — a typo guard against `mock.assert_called_once`:

    AttributeError: 'assert_owns' is not a valid assertion.
    Use a spec for the mock if 'assert_owns' is meant to be an attribute.

The #1310 imperative auth-guard family is named exactly that way —
`assert_admin`, `assert_agent_access`, `assert_agent_owner`,
`assert_owns_or_admin`, `assert_owns` — so a test that stubs `dependencies` with a
bare `MagicMock()` fails at IMPORT time, before a single assertion runs. That is
what took all 14 tests in `tests/test_ip_rate_limit_fix.py` down as setup errors,
silently, from `b2db1c87` onward: the error message points at mock internals, not
at the stub, and nothing on the PR path reaches the api tier.

WHY THIS GUARD EXISTS (the issue asked for a decision, so here it is, on the
record). The trap is not a one-off: Invariant #8 documents and encourages the
guard family, #1683 added a fifth member, and EVERY future test that stubs a
module re-exporting one breaks the same way, with the same misleading message. The
cost of noticing it again is a full-suite run against a tier per-PR CI does not
reach — which is how it stayed hidden for weeks. The cost of this file is a text
scan with no imports and no runtime. That trade is clearly worth taking.

Deliberate limits, so a pass is not over-read:

  * it scans SOURCE TEXT, not behaviour — a stub built across several lines, or
    through a helper, is out of reach. It catches the shape that actually shipped.
  * the exporter list is DERIVED from `src/backend`, never hardcoded, so a sixth
    guard is covered the day it lands.
  * `spec=` / `create_autospec` / a real `types.ModuleType` are all accepted: each
    resolves `assert_*` names on its own terms. Only the bare form is a defect.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_TESTS = _REPO / "tests"

# `def assert_...` at any indentation: the family is module-level today, but a
# guard moved onto a class would export the same trapped name.
_EXPORTS_ASSERT = re.compile(r"^\s*def (assert_\w+)", re.M)


def _module_name(path: Path) -> str:
    """`src/backend/services/x.py` -> `services.x`; `.../dependencies.py` -> `dependencies`."""
    rel = path.relative_to(_BACKEND).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _assert_exporting_modules() -> dict[str, list[str]]:
    """Importable module name -> the `assert_*` names it defines."""
    found: dict[str, list[str]] = {}
    for path in _BACKEND.rglob("*.py"):
        names = _EXPORTS_ASSERT.findall(path.read_text(encoding="utf-8", errors="ignore"))
        if names:
            found[_module_name(path)] = sorted(set(names))
    return found


def test_the_exporter_scan_finds_the_1310_family():
    """The scan is the load-bearing half — if it silently finds nothing, the guard
    below passes vacuously forever."""
    modules = _assert_exporting_modules()
    assert "dependencies" in modules, modules.keys()
    guards = modules["dependencies"]
    for name in ("assert_admin", "assert_agent_access", "assert_agent_owner",
                 "assert_owns", "assert_owns_or_admin"):
        assert name in guards, (name, guards)


def _bare_magicmock_stubs(module_names: set[str]) -> list[str]:
    """Every `sys.modules[...] = MagicMock()`-shaped stub of a trapped module."""
    offenders: list[str] = []
    # `setitem(sys.modules, "mod", MagicMock(...))`, `sys.modules["mod"] = MagicMock(...)`,
    # and `sys.modules.setdefault("mod", MagicMock(...))` — the three forms in use.
    stub_re = re.compile(
        r"""(?:setitem\(\s*sys\.modules\s*,\s*|sys\.modules\[\s*|sys\.modules\.setdefault\(\s*)
            ["'](?P<mod>[\w.]+)["']
            (?:\s*\]\s*=\s*|\s*,\s*)
            (?P<factory>MagicMock\s*\((?P<args>[^)]*)\))""",
        re.X,
    )
    for path in sorted(_TESTS.rglob("*.py")):
        if path.resolve() == Path(__file__).resolve():
            continue  # this file quotes the offending shape on purpose
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in stub_re.finditer(text):
            if m.group("mod") not in module_names:
                continue
            args = m.group("args")
            # `unsafe=True` disables the prefix check; `spec=`/`autospec` give the
            # mock a real attribute surface. Anything else cannot resolve `assert_*`.
            if "unsafe" in args or "spec" in args:
                continue
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_REPO)}:{line} stubs '{m.group('mod')}'")
    return offenders


def test_no_bare_magicmock_stubs_a_module_exporting_assert_names():
    modules = _assert_exporting_modules()
    offenders = _bare_magicmock_stubs(set(modules))
    assert not offenders, (
        "A bare MagicMock() cannot resolve an `assert_*` attribute — importing the "
        "module under test will raise \"'assert_x' is not a valid assertion\" at "
        "collection time and every test in the file becomes a setup error (#2241).\n"
        "Use MagicMock(unsafe=True), or spec/create_autospec, or a real "
        "types.ModuleType with the attributes you need.\n\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


def test_magicmock_really_does_block_the_guard_names():
    """Pin the upstream behaviour this whole file depends on.

    If a future mock release drops the prefix check, this fails and the guard above
    can be deleted rather than maintained on faith.
    """
    from unittest.mock import MagicMock

    with pytest.raises(AttributeError, match="valid assertion"):
        MagicMock().assert_owns

    # And the documented escape hatch still works.
    assert MagicMock(unsafe=True).assert_owns is not None
