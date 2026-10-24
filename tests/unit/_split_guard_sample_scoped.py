"""A module fixture that holds its own copy for this file's tests (#3390).

The `test_fleet_status_resilience.py` shape: the copy sits in sys.modules while
the package attribute keeps the original, and the fixture puts the original back
in its own teardown. That is a split for the length of this file and none after
it, so the guard must not fail any test here. Run by
`test_3390_module_split_guard.py` ahead of `_split_guard_sample.py`, in the same
subprocess; `pytest unit/` never collects it.
"""

import importlib.util
import sys

import pytest

import split_guard_sample_pkg.mod  # noqa: F401 - loaded at collection, like a backend module

_MOD = "split_guard_sample_pkg.mod"


@pytest.fixture(scope="module")
def private_copy():
    spec = importlib.util.find_spec(_MOD)
    private = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private)
    mp = pytest.MonkeyPatch()
    mp.setitem(sys.modules, _MOD, private)
    yield private
    mp.undo()


def test_scoped_1_reads_the_files_own_copy(private_copy):
    assert sys.modules[_MOD] is private_copy


def test_scoped_2_still_reads_it(private_copy):
    assert sys.modules[_MOD] is private_copy
