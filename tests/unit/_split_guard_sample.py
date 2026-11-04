"""Polluters and a victim for `test_3390_module_split_guard.py` (#3390).

`pytest unit/` never collects this file (no `test_` prefix). The guard's own test
passes it explicitly to a subprocess, in file order, with `split_guard_sample_pkg`
(a two-file package it writes to a temporary directory) on PYTHONPATH: once with
the unit conftest's guard on, where the victim must pass, and once with
`TRINITY_SPLIT_GUARD=off`, where the same victim must fail.
"""

import importlib
import importlib.util
import sys

import pytest

# Loaded at collection, like the backend modules the real polluters evict: a
# module first imported inside the polluting test is a copy the guard cannot
# rank, and it reports that instead of repairing it.
import split_guard_sample_pkg.mod  # noqa: F401

_PKG = "split_guard_sample_pkg"
_MOD = f"{_PKG}.mod"


def test_1_an_eviction_leaves_the_package_attribute_on_the_fresh_copy():
    original = sys.modules[_MOD]
    package = sys.modules[_PKG]
    with pytest.MonkeyPatch.context() as mp:
        mp.delitem(sys.modules, _MOD)
        fresh = importlib.import_module(_MOD)
    # The context put the entry back; nothing put the attribute back. This is the
    # state the next test inherits unless the guard repairs it.
    assert sys.modules[_MOD] is original
    assert package.mod is fresh


def test_2_the_next_test_patches_the_module_the_code_reads(monkeypatch):
    monkeypatch.setattr(f"{_MOD}.VALUE", "patched")  # resolved through the package attribute
    reader = importlib.import_module(_MOD)  # what `from split_guard_sample_pkg.mod import VALUE` reads
    assert reader.VALUE == "patched", "SPLIT-SENTINEL: the patch landed on a copy sys.modules does not hold"


def test_3_a_test_that_leaves_its_own_copy_in_sys_modules():
    importlib.import_module(_MOD)
    spec = importlib.util.find_spec(_MOD)
    private = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private)
    # Left behind on purpose: the attribute keeps the original, the entry is a
    # copy the guard has no way to rank against it.
    sys.modules[_MOD] = private
