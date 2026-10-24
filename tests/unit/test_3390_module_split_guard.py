"""#3390: no test may leave a package attribute and its sys.modules entry on two copies.

`tests/module_split_guard.py` explains the split and the guard's rules. Coverage,
in three layers:

  * the rules, on a private module table, so nothing here touches the real one;
  * `evict()` and the eviction it replaces, through the real import system and a
    real `MonkeyPatch` undo;
  * the wiring, end to end: `_split_guard_sample.py` runs in a subprocess under
    the real unit conftest, where the polluter's split is repaired before the
    victim runs and a copy left in sys.modules fails at teardown, while
    `_split_guard_sample_scoped.py`'s module-scoped copy fails nothing; then again
    with `TRINITY_SPLIT_GUARD=off`, where the same victim fails. The second run is
    what stops the first one passing for the wrong reason.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import os
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import pytest

import module_split_guard as guard

pytestmark = pytest.mark.unit

_UNIT = Path(__file__).resolve().parent
# Run in this order: the module-scoped copy first, then the polluters.
_SAMPLES = (_UNIT / "_split_guard_sample_scoped.py", _UNIT / "_split_guard_sample.py")


def _module(name: str, *, spec: bool = True) -> types.ModuleType:
    module = types.ModuleType(name)
    if spec:
        module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    return module


def _table(*names: str) -> dict:
    """A module table holding `names`, each bound on its parent as an import binds it."""
    table = {name: _module(name) for name in names}
    for name in names:
        parent, _, child = name.rpartition(".")
        if parent in table:
            setattr(table[parent], child, table[name])
    return table


def _reimport(table: dict, name: str) -> types.ModuleType:
    """What an eviction, a re-import and the eviction's undo leave behind: the
    entry is the original again, the package attribute is the fresh copy."""
    fresh = _module(name)
    parent, _, child = name.rpartition(".")
    setattr(table[parent], child, fresh)
    return fresh


# --------------------------------------------------------------------- scan()

class TestScan:
    def test_reports_an_attribute_left_on_a_fresh_copy(self):
        table = _table("pkg", "pkg.mod")
        fresh = _reimport(table, "pkg.mod")
        assert guard.scan(table) == {"pkg.mod": (fresh, table["pkg.mod"])}

    def test_a_consistent_table_reports_nothing(self):
        assert guard.scan(_table("pkg", "pkg.mod", "pkg.sub", "pkg.sub.leaf")) == {}

    def test_another_module_bound_under_the_childs_name_is_not_a_split(self):
        # `alembic.autogenerate.compare` binds `alembic.util` as `util`.
        table = _table("alembic", "alembic.util", "alembic.compare", "alembic.compare.util")
        table["alembic.compare"].util = table["alembic.util"]
        assert guard.scan(table) == {}

    @pytest.mark.parametrize("stub", [
        types.SimpleNamespace(),
        Mock(spec=types.ModuleType),
        _module("pkg.mod", spec=False),
    ], ids=["namespace", "module-spec mock", "hand-built module"])
    def test_a_stub_in_sys_modules_is_the_762_class_not_a_split(self, stub):
        table = _table("pkg", "pkg.mod")
        table["pkg.mod"] = stub
        assert guard.scan(table) == {}

    def test_a_stub_on_the_attribute_is_not_a_split(self):
        table = _table("pkg", "pkg.mod")
        table["pkg"].mod = Mock(spec=types.ModuleType)
        assert guard.scan(table) == {}

    @pytest.mark.parametrize("parent", [
        types.SimpleNamespace(mod=None),
        Mock(spec=types.ModuleType),
    ], ids=["namespace", "module-spec mock"])
    def test_a_parent_that_is_not_a_module_is_skipped(self, parent):
        table = _table("pkg.mod")
        table["pkg"] = parent
        assert guard.scan(table) == {}

    def test_reads_a_parent_without_its_getattribute_or_getattr(self):
        class _Lazy(types.ModuleType):
            def __getattribute__(self, item):
                raise AssertionError(f"scan reached __getattribute__({item!r})")

        table = _table("pkg.mod")
        lazy = _Lazy("pkg")
        types.ModuleType.__dict__["__dict__"].__get__(lazy)["mod"] = _module("pkg.mod")
        table["pkg"] = lazy
        assert list(guard.scan(table)) == ["pkg.mod"]


# --------------------------------------------------------------------- Guard

class TestGuard:
    def test_heals_an_attribute_the_test_rebound(self):
        table = _table("pkg", "pkg.mod")
        original = table["pkg.mod"]
        g = guard.Guard(table)
        g.before_test()
        _reimport(table, "pkg.mod")

        assert g.after_test(last_in_module=False) == (["pkg.mod"], [])
        assert table["pkg"].mod is original

    def test_leaves_a_split_that_existed_before_the_first_test(self):
        # A collection-time loader registered its own copy; the canonical one is on
        # the attribute. Healing toward the entry broke four ent692 tests.
        table = _table("pkg", "pkg.mod")
        canonical = table["pkg.mod"]
        table["pkg.mod"] = _module("pkg.mod")
        g = guard.Guard(table)
        g.before_test()

        assert g.after_test(last_in_module=True) == ([], [])
        assert table["pkg"].mod is canonical

    def test_puts_back_an_already_split_attribute_the_test_moved(self):
        table = _table("pkg", "pkg.mod")
        canonical = table["pkg"].mod
        table["pkg.mod"] = _module("pkg.mod")
        g = guard.Guard(table)
        g.before_test()
        _reimport(table, "pkg.mod")

        assert g.after_test(last_in_module=True) == (["pkg.mod"], [])
        assert table["pkg"].mod is canonical

    def test_a_replaced_entry_is_reported_once_its_module_ends_and_never_healed(self):
        table = _table("pkg", "pkg.mod")
        attribute = table["pkg"].mod
        g = guard.Guard(table)
        g.before_test()
        table["pkg.mod"] = _module("pkg.mod")  # the test left its own copy behind

        assert g.after_test(last_in_module=False) == ([], [])
        g.before_test()
        assert g.after_test(last_in_module=True) == ([], ["pkg.mod"])
        assert table["pkg"].mod is attribute

    def test_a_module_scoped_copy_restored_when_its_module_ends_is_not_reported(self):
        # test_fleet_status_resilience: a module fixture holds a private copy for the
        # module's tests and puts the original back in its own teardown.
        table = _table("pkg", "pkg.mod")
        original = table["pkg.mod"]
        g = guard.Guard(table)
        g.before_test()
        table["pkg.mod"] = _module("pkg.mod")
        assert g.after_test(last_in_module=False) == ([], [])
        g.before_test()
        table["pkg.mod"] = original
        assert g.after_test(last_in_module=True) == ([], [])

    def test_a_split_on_a_module_born_during_the_test_is_reported(self):
        table = _table("pkg")
        g = guard.Guard(table)
        g.before_test()
        table["pkg.mod"] = _module("pkg.mod")
        table["pkg"].mod = _module("pkg.mod")

        assert g.after_test(last_in_module=True) == ([], ["pkg.mod"])

    def test_a_reported_split_is_not_reported_again(self):
        table = _table("pkg", "pkg.mod")
        g = guard.Guard(table)
        g.before_test()
        table["pkg.mod"] = _module("pkg.mod")
        assert g.after_test(last_in_module=True) == ([], ["pkg.mod"])

        g.before_test()
        assert g.after_test(last_in_module=True) == ([], [])

    def test_after_test_without_before_test_judges_nothing(self):
        table = _table("pkg", "pkg.mod")
        _reimport(table, "pkg.mod")
        assert guard.Guard(table).after_test(last_in_module=True) == ([], [])


# --------------------------------------------------------------------- unbind()

class TestUnbind:
    def test_drops_the_binding_to_a_popped_module(self):
        table = _table("pkg", "pkg.mod")
        table.pop("pkg.mod")
        assert guard.unbind("pkg.mod", table) is True
        assert not hasattr(table["pkg"], "mod")

    def test_keeps_another_module_bound_under_the_same_name(self):
        table = _table("alembic", "alembic.util", "alembic.compare")
        table["alembic.compare"].util = table["alembic.util"]
        assert guard.unbind("alembic.compare.util", table) is False
        assert table["alembic.compare"].util is table["alembic.util"]

    def test_a_missing_parent_is_a_no_op(self):
        assert guard.unbind("pkg.mod", {}) is False

    def test_a_mock_parent_is_left_alone(self):
        # `isinstance(Mock(spec=module), ModuleType)` is True; its namespace is not a module's.
        assert guard.unbind("pkg.mod", {"pkg": Mock(spec=types.ModuleType)}) is False


class TestTheConftestsOwnPops:
    """The unit conftest pops `_POP_PREFIXES` modules around every test; a popped
    backend module must not stay reachable through its package attribute."""

    @staticmethod
    def _conftest(config):
        for plugin in config.pluginmanager.get_plugins():
            path = getattr(plugin, "__file__", None)
            if path and Path(path).resolve() == _UNIT / "conftest.py":
                return plugin
        pytest.fail("the unit conftest is not loaded")

    def test_a_popped_backend_module_takes_its_package_binding_with_it(self, request):
        import services.cleanup_service  # noqa: F401 - a _POP_PREFIXES module outside the baseline

        services = sys.modules["services"]
        assert "cleanup_service" in vars(services)

        self._conftest(request.config)._restore_unit_sys_modules()

        assert "services.cleanup_service" not in sys.modules
        assert "cleanup_service" not in vars(services), (
            "`from services import cleanup_service` still hands out the copy the conftest popped"
        )


# --------------------------------------------------------------------- evict()

def _write_package(root: Path, name: str, init: str = "") -> None:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text(init)
    (package / "mod.py").write_text('VALUE = "real"\n')


class TestEvict:
    def test_a_plain_delitem_leaves_the_attribute_on_the_fresh_copy(self, tmp_path, monkeypatch):
        """The mechanism, through the real import system (#3272's shape)."""
        _write_package(tmp_path, "split3390_plain")
        monkeypatch.syspath_prepend(str(tmp_path))
        original = importlib.import_module("split3390_plain.mod")
        package = sys.modules["split3390_plain"]

        with pytest.MonkeyPatch.context() as mp:
            mp.delitem(sys.modules, "split3390_plain.mod")
            fresh = importlib.import_module("split3390_plain.mod")

        assert sys.modules["split3390_plain.mod"] is original
        assert package.mod is fresh, "CPython no longer rebinds the parent on import; revisit #3390"
        package.mod = original  # leave nothing for the conftest's guard to report

    def test_evict_restores_both_halves(self, tmp_path, monkeypatch):
        _write_package(tmp_path, "split3390_evict")
        monkeypatch.syspath_prepend(str(tmp_path))
        original = importlib.import_module("split3390_evict.mod")
        package = sys.modules["split3390_evict"]

        with pytest.MonkeyPatch.context() as mp:
            guard.evict(mp, "split3390_evict.mod")
            assert "split3390_evict.mod" not in sys.modules
            fresh = importlib.import_module("split3390_evict.mod")
            assert fresh is not original and package.mod is fresh

        assert sys.modules["split3390_evict.mod"] is original
        assert package.mod is original

    def test_evicting_a_package_takes_its_loaded_submodules(self, tmp_path, monkeypatch):
        """#2859: a package evicted alone re-imports without its submodule attributes."""
        _write_package(tmp_path, "split3390_family", init="from .mod import VALUE\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        original = importlib.import_module("split3390_family")

        with pytest.MonkeyPatch.context() as mp:
            guard.evict(mp, "split3390_family")
            assert "split3390_family.mod" not in sys.modules
            fresh = importlib.import_module("split3390_family")
            assert fresh is not original and hasattr(fresh, "mod")

        assert sys.modules["split3390_family"] is original
        assert sys.modules["split3390_family.mod"] is original.mod

    def test_a_name_that_is_not_loaded_is_a_no_op(self, monkeypatch):
        guard.evict(monkeypatch, "split3390_never_imported.mod")
        assert "split3390_never_imported.mod" not in sys.modules

    def test_a_stubbed_parent_is_not_treated_as_a_package(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "split3390_mock_parent", Mock(spec=types.ModuleType))
        monkeypatch.setitem(sys.modules, "split3390_mock_parent.mod", types.ModuleType("split3390_mock_parent.mod"))
        guard.evict(monkeypatch, "split3390_mock_parent.mod")
        assert "split3390_mock_parent.mod" not in sys.modules


# --------------------------------------------------------------------- wiring

def _run_sample(tmp_path: Path, guard_mode: str) -> dict[str, tuple[str, str]]:
    """Run the samples under the real unit conftest; return {test name: (outcome, message)}."""
    _write_package(tmp_path, "split_guard_sample_pkg")
    report = tmp_path / "report.xml"
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"}
        and not key.startswith("PYTEST_XDIST")
    }
    env.update({
        "PYTHONPATH": os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH")])),
        # The conftest only sets these when absent, and the parent process has
        # them: without a fresh value the child would initialise the parent's DB.
        "TRINITY_DB_PATH": str(tmp_path / "trinity.db"),
        "TRINITY_RETAINED_RESULTS_DIR": str(tmp_path / "retained"),
        "TRINITY_CHAT_SESSION_FILE": str(tmp_path / "chat-session.json"),
        "TRINITY_SPLIT_GUARD": guard_mode,
    })
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *map(str, _SAMPLES),
         "-c", str(_UNIT / "pytest.ini"), "--rootdir", str(_UNIT),
         "-q", "-p", "no:randomly", "-p", "no:cacheprovider",
         "--timeout=60", f"--junitxml={report}"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
    )
    assert report.exists(), f"the sample run produced no report:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"
    outcomes = {}
    for case in ET.parse(report).iter("testcase"):
        verdicts = [child for child in case if child.tag in {"failure", "error"}]
        outcomes[case.get("name")] = (
            verdicts[0].tag if verdicts else "passed",
            " ".join(f"{v.get('message', '')} {v.text or ''}" for v in verdicts),
        )
    return outcomes


class TestWiring:
    def test_the_conftest_repairs_the_split_before_the_next_test_and_fails_a_left_copy(self, tmp_path):
        outcomes = _run_sample(tmp_path, "heal")

        # A file's own module-scoped copy is judged after its fixture puts the
        # original back, not after its first test.
        assert outcomes["test_scoped_1_reads_the_files_own_copy"] == ("passed", "")
        assert outcomes["test_scoped_2_still_reads_it"] == ("passed", "")
        assert outcomes["test_1_an_eviction_leaves_the_package_attribute_on_the_fresh_copy"][0] == "passed"
        assert outcomes["test_2_the_next_test_patches_the_module_the_code_reads"] == ("passed", "")
        verdict, message = outcomes["test_3_a_test_that_leaves_its_own_copy_in_sys_modules"]
        assert verdict == "error", outcomes
        assert "split_guard_sample_pkg.mod" in message and "#3390" in message, message

    def test_with_the_guard_off_the_same_victim_sees_the_wrong_copy(self, tmp_path):
        outcomes = _run_sample(tmp_path, "off")

        assert outcomes["test_1_an_eviction_leaves_the_package_attribute_on_the_fresh_copy"][0] == "passed"
        verdict, message = outcomes["test_2_the_next_test_patches_the_module_the_code_reads"]
        assert verdict == "failure" and "SPLIT-SENTINEL" in message, outcomes
        assert outcomes["test_3_a_test_that_leaves_its_own_copy_in_sys_modules"] == ("passed", "")
