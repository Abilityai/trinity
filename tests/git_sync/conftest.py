"""
Local conftest for tests/git_sync/.

The top-level tests/conftest.py wires up HTTP fixtures and auto-logs in
against a live backend, which these unit tests don't need. We override the
backend-dependent fixtures with no-ops so pytest can collect and run the
S7 unit tests in complete isolation.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _restore_real_utils_helpers() -> None:
    """Replace any stub `utils.helpers` in sys.modules with the real backend module.

    Issue #763: `tests/test_validation.py:35` and `tests/test_watchdog_unit.py:54`
    unconditionally assign `sys.modules["utils.helpers"] = _helpers_mod` at module
    import time with a stub that has utc_now / utc_now_iso / to_utc_iso /
    parse_iso_timestamp but is missing `iso_cutoff`. The top-level conftest preloads
    the real `utils.helpers` first, but if either polluter is collected before this
    directory, the real module is silently overwritten. Later, when
    `TestMigrationPreflight` does `sys.modules.pop("db.migrations", None)` and
    re-imports it, the chain hits `db/__init__.py` → `db/schedules.py` →
    `from utils.helpers import iso_cutoff, ...` → ImportError, failing all 4
    preflight tests.

    Restoring the real module here is order-independent: works whether the polluter
    ran before or after, and is idempotent (no-op when already real).
    """
    existing = sys.modules.get("utils.helpers")
    helpers_path = _BACKEND / "utils" / "helpers.py"

    if not helpers_path.exists():
        return

    # Already the real module? (presence of `iso_cutoff` is the discriminator.)
    if existing is not None and hasattr(existing, "iso_cutoff"):
        return

    spec = importlib.util.spec_from_file_location("utils.helpers", str(helpers_path))
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    sys.modules["utils.helpers"] = mod
    spec.loader.exec_module(mod)


def _evict_db_module_cache() -> None:
    """Pop the `db.*` modules so the next import gets a fresh chain.

    Required because `db/__init__.py` and `db/schedules.py` may already be
    cached against the stub `utils.helpers` from a polluter test, even after
    `_restore_real_utils_helpers()` puts the real module back. The test itself
    pops `db.migrations`, but its import chain hits `db/__init__.py` first,
    which is already cached with the bad import. Evict the whole `db` subtree
    so the next `from db.migrations import …` reloads everything from disk.
    """
    for name in [n for n in sys.modules if n == "db" or n.startswith("db.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _pin_module_state_for_db_reimports():
    """Order-independence pin for S7 migration preflight tests (#763).

    Restores the real `utils.helpers` and evicts the `db.*` cache before
    every test in this directory, defending against arbitrary earlier
    tests that polluted `sys.modules`. Cheap and idempotent.
    """
    _restore_real_utils_helpers()
    _evict_db_module_cache()
    yield


@pytest.fixture(scope="session")
def api_client():
    """Override the live-backend api_client fixture."""
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the autouse cleanup_after_test fixture that hits the backend."""
    yield
