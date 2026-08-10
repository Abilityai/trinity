"""The enterprise-load block must not call a real failure "normal" (ent#196).

`main.py` wraps the enterprise import + registration in one try. It used to
catch bare `ImportError` and print:

    Trinity Enterprise submodule not present — OSS-only build
    (this is normal; enterprise modules are an optional private submodule)

That line is correct for an OSS clone. But `ImportError` also covers a *mounted*
submodule whose module failed to import — e.g. importing a name from an OSS
table that hasn't landed yet. Observed live: the submodule was present and
correctly mounted, a module failed, and the operator was told "this is normal"
while a paid feature was silently absent. Diagnosing it required hand-running
`register_enterprise()` to see the real traceback.

The benign case is now narrowed to its actual signature — a `ModuleNotFoundError`
naming the `enterprise` package itself. These tests pin the classification so a
future edit can't widen it back.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"


def _classify(exc: BaseException) -> str:
    """The exact predicate main.py uses, applied to an exception."""
    if isinstance(exc, ModuleNotFoundError):
        return "benign" if (exc.name or "").split(".")[0] == "enterprise" else "loud"
    return "loud"


def test_absent_submodule_is_still_reported_as_normal():
    """An OSS clone has no `enterprise` package — that must stay quiet."""
    try:
        import enterprise.backend  # noqa: F401
        pytest.skip("enterprise submodule is mounted in this checkout")
    except ModuleNotFoundError as e:
        assert _classify(e) == "benign"


def test_missing_name_inside_a_mounted_module_is_loud():
    """The ent#196 case: the package is present, a module imports a name that
    doesn't exist. Python raises a plain ImportError (NOT ModuleNotFoundError),
    so it must never take the benign branch."""
    exc = ImportError("cannot import name 'product_events' from 'db.tables'")
    assert not isinstance(exc, ModuleNotFoundError)
    assert _classify(exc) == "loud"


def test_missing_third_party_dependency_is_loud():
    """A module needing an uninstalled package is a real failure, not an
    absent submodule."""
    assert _classify(ModuleNotFoundError("No module named 'pyotp'", name="pyotp")) == "loud"


def test_nested_enterprise_module_absence_is_benign():
    """`enterprise.backend` missing is the same OSS-clone signature as
    `enterprise` missing."""
    assert _classify(
        ModuleNotFoundError("No module named 'enterprise.backend'", name="enterprise.backend")
    ) == "benign"


def test_main_py_does_not_catch_bare_importerror_for_the_benign_message():
    """Static guard: the reassuring message must sit behind the narrowed
    `ModuleNotFoundError` + name check, never a bare `except ImportError`."""
    src = MAIN_PY.read_text()
    block_start = src.index("from client_portal import register_enterprise")
    block = src[block_start:block_start + 2500]

    assert "except ModuleNotFoundError" in block, (
        "the benign branch must key off ModuleNotFoundError"
    )
    assert re.search(r"except ImportError\s*:", block) is None, (
        "a bare `except ImportError` here re-buries a real module failure under "
        '"submodule not present — this is normal" (ent#196)'
    )
    assert 'split(".")[0] == "enterprise"' in block, (
        "the benign branch must verify the missing module IS the enterprise package"
    )
