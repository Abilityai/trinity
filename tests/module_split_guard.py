"""
A package attribute and its `sys.modules` entry must name the same module (#3390).

Importing `pkg.mod` stores the module twice: as `sys.modules["pkg.mod"]` and as
the attribute `mod` on the package `pkg`. A test that evicts `pkg.mod` and
imports it again rebinds both to a fresh copy, and every eviction tool undoes
only the first. `monkeypatch.delitem(sys.modules, ...)`, `patch.dict(sys.modules,
...)` and a hand-written snapshot/restore of `sys.modules` put the entry back
and leave the attribute on the copy the test built. From then on the process
runs two copies of one file, and which one a caller gets depends on how the
import is spelled:

  * `from pkg.mod import x`, `importlib.import_module("pkg.mod")` and
    `unittest.mock.patch("pkg.mod.x")` read `sys.modules`;
  * `from pkg import mod`, `import pkg.mod as m` and
    `monkeypatch.setattr("pkg.mod.x", ...)` read the attribute.

So a later test patches one copy while the code under test reads the other. It
fails on the real dependency's behaviour, in a full run and never alone: #3272's
40 failures, and #3470, #2572 and trinity-enterprise#752 before it.

`tests/unit/conftest.py` runs a `Guard` around every test:

  * After the test's last teardown, an attribute the test rebound is put back
    when the `sys.modules` entry is still the module that was there before the
    test. Then the attribute's old value is known, and restoring it is exactly
    the restore the eviction tool missed.
  * Once a test file's tests and fixtures have all finished, any split it left
    that the guard could not repair fails its last test. Only the test knows
    which of the two copies it meant to keep.
  * Splits that existed before the first test are left alone. Collection-time
    loaders register a private copy of a module under its real name (the #762
    class). Pointing the attribute at that copy broke four tests in
    `test_ent692_skill_sources_mcp.py`.

`evict()` is the eviction that leaves no split behind, in any harness. Out of
reach: a module first imported during a polluting test that bound the fresh
copy, and a package attribute left behind when a test pops a module it does not
restore. Neither shows as a split.
"""

from __future__ import annotations

import sys
from types import ModuleType

# A module's namespace, read without calling its `__getattribute__`: on a lazy
# module that call would run the import it defers.
_namespace = ModuleType.__dict__["__dict__"].__get__


def _is_module(obj: object) -> bool:
    # `type()`, not `isinstance()`: a `Mock(spec=module)` reports the module class.
    kind = type(obj)
    return kind is ModuleType or issubclass(kind, ModuleType)


def scan(modules=None) -> dict:
    """Every dotted name whose package attribute is another copy of the same module.

    Returns `{name: (attribute, entry)}`. Both sides must be real modules. An
    entry that is a stub (`Mock`, `SimpleNamespace`, or a `types.ModuleType` built
    by hand, with no `__spec__`) belongs to the #762 stub-leak class, which the
    conftest restores separately. An attribute that is a different module under
    the child's name is not a split either: `alembic.autogenerate.compare` binds
    `alembic.util` as `util`.
    """
    modules = sys.modules if modules is None else modules
    found = {}
    for name, entry in list(modules.items()):
        try:
            dot = name.rfind(".")
            if dot < 0:
                continue
            parent = modules.get(name[:dot])
            if not _is_module(parent):
                continue
            attribute = _namespace(parent).get(name[dot + 1:])
            if attribute is entry or not _is_module(attribute) or not _is_module(entry):
                continue
            if _namespace(attribute).get("__name__") != name:
                continue
            if _namespace(entry).get("__spec__") is None:
                continue
        except Exception:  # noqa: BLE001 - a module the guard cannot read is not one it may fail a test over
            continue
        found[name] = (attribute, entry)
    return found


class Guard:
    """Wraps one test at a time: `before_test()` at setup, `after_test()` after teardown."""

    def __init__(self, modules=None):
        self._modules = sys.modules if modules is None else modules
        self._splits = None  # the splits as the current test found them
        self._before = None  # the module table as the current test found it
        self._file_splits = None  # the splits as the current test file found them

    def before_test(self) -> None:
        if self._splits is None:
            self._splits = scan(self._modules)
        self._before = dict(self._modules)
        if self._file_splits is None:
            self._file_splits = dict(self._splits)

    def after_test(self, *, last_in_module: bool) -> tuple[list[str], list[str]]:
        """Repair what is certain and return `(healed, unrepairable)`.

        `unrepairable` is only ever non-empty when `last_in_module` is true: a
        module-scoped fixture may hold a copy in `sys.modules` for its file's tests
        and put the original back in its own teardown, so a replaced entry is
        judged once that teardown has run.
        """
        if self._before is None:
            self._splits = scan(self._modules)
            return [], []
        before, self._before = self._before, None
        current = scan(self._modules)
        healed = []
        for name, (attribute, entry) in list(current.items()):
            start = self._splits.get(name)
            if start is not None and start[0] is attribute and start[1] is entry:
                continue  # split this way before the test began
            if entry is not before.get(name):
                continue  # the test replaced the entry: judged when its file ends
            restore = entry if start is None else start[0]
            parent_name, _, child = name.rpartition(".")
            try:
                setattr(self._modules[parent_name], child, restore)
            except Exception:  # noqa: BLE001 - leave it to the end-of-file judgement
                continue
            healed.append(name)
            if start is None:
                del current[name]
            else:
                current[name] = (restore, entry)
        self._splits = current
        unrepairable = []
        if last_in_module:
            for name, pair in current.items():
                at_start = self._file_splits.get(name)
                if at_start is None or at_start[0] is not pair[0] or at_start[1] is not pair[1]:
                    unrepairable.append(name)
            self._file_splits = None
        return sorted(healed), sorted(unrepairable)


def unbind(name: str, modules=None) -> bool:
    """Drop the package attribute that holds a copy of `name`; return whether one went.

    For a module just popped from `sys.modules`: otherwise `from pkg import mod`
    keeps handing out the popped copy while `from pkg.mod import x` imports a
    fresh one.
    """
    modules = sys.modules if modules is None else modules
    parent_name, _, child = name.rpartition(".")
    parent = modules.get(parent_name) if parent_name else None
    if not _is_module(parent):
        return False
    namespace = _namespace(parent)
    held = namespace.get(child)
    if not _is_module(held) or _namespace(held).get("__name__") != name:
        return False
    del namespace[child]
    return True


def evict(monkeypatch, *names: str) -> None:
    """`monkeypatch.delitem(sys.modules, name)` that also restores the package attribute.

    The re-import that follows an eviction rebinds `pkg.<mod>`. Registering the
    attribute with the same monkeypatch first means teardown puts both halves back
    (#3468's per-site fix, as one call). A package takes its loaded submodules with
    it: evicted alone it would re-import without their attributes (#2859).
    """
    modules = sys.modules
    targets = []
    for name in names:
        targets.append(name)
        entry = modules.get(name)
        if _is_module(entry) and "__path__" in _namespace(entry):
            prefix = f"{name}."
            targets.extend(sorted(k for k in list(modules) if isinstance(k, str) and k.startswith(prefix)))
    targets = list(dict.fromkeys(targets))
    # Register every attribute first, while each parent is still in sys.modules.
    for name in targets:
        parent_name, _, child = name.rpartition(".")
        parent = modules.get(parent_name) if parent_name else None
        if _is_module(parent):
            namespace = _namespace(parent)
            if child in namespace:
                monkeypatch.setattr(parent, child, namespace[child])
    for name in targets:
        monkeypatch.delitem(modules, name, raising=False)
