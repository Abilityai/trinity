"""#4052 — `from conftest import ...` in a unit file names `tests/conftest.py`
whatever `sys.path` looks like.

About 60 unit files insert `tests/unit` at `sys.path[0]`. After one of them,
a bare `from conftest import ensure_schema_tables` resolved to
`tests/unit/conftest.py`, re-executed it as a top-level module, and raised
ImportError (`test_2610_room_jsonl_keepset` after `test_ent901_withdraw_ask`).
`tests/unit/conftest.py` now pins the name to the parent file by path.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_UNIT = Path(__file__).resolve().parent


def test_conftest_resolves_to_the_parent_file_with_tests_unit_first(monkeypatch):
    monkeypatch.syspath_prepend(str(_UNIT))

    import conftest
    from conftest import ensure_schema_tables, stubbed_modules  # noqa: F401

    assert Path(conftest.__file__).resolve() == _UNIT.parent / "conftest.py"


def test_the_unit_conftest_is_not_executed_twice():
    import sys

    unit_conftests = [
        name for name, module in list(sys.modules.items())
        if Path(getattr(module, "__file__", None) or "/").resolve() == _UNIT / "conftest.py"
    ]
    assert len(unit_conftests) == 1, f"tests/unit/conftest.py loaded as {unit_conftests}"
