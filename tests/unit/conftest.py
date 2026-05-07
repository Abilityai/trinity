"""
Unit test conftest — overrides the parent conftest's autouse fixtures.

These tests run without a backend connection (no Docker, no API).
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

# Issue #589 + #645: backend/config.py raises at import if REDIS_URL lacks
# credentials. Unit tests don't share the parent conftest (the unit/pytest.ini
# sets `norecursedirs = ..`), so set a creds-bearing dummy here. Any test
# that wants to assert the fail-fast behavior (e.g. test_config_fail_fast.py)
# still uses monkeypatch to override.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

# Ensure src/backend is importable before test modules are collected.
#
# BACKLOG-001: tests/unit/test_backlog.py does `from models import ...`, which
# in turn does `from utils.helpers import ...`. pytest auto-adds `tests/` to
# sys.path at position 0, and `tests/utils/__init__.py` exists as a test
# helpers package. Just adding `src/backend` to sys.path isn't enough because
# pytest's entry stays at position 0 — the shadow `utils` wins first.
#
# Fix: load `src/backend/utils/__init__.py` directly via importlib and
# install it under the name `utils` in sys.modules BEFORE any test imports
# backend code. After that, `from utils.helpers import ...` and
# `from utils.api_client import ...` both hit the right place (backend's
# `utils` has `helpers.py`; tests' `utils` has `api_client.py`). Because
# we register under the name `utils`, test helpers must be imported
# via `from utils import api_client` — the existing helpers use absolute
# `from utils.api_client import TrinityApiClient` which still resolves via
# sys.path lookup on attribute access. To avoid breakage we also install
# the backend's `utils` submodules explicitly and leave the test helpers
# alone (unit tests don't use them).
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

_AGENT_SERVER_DIR = (
    Path(__file__).resolve().parent.parent.parent / "docker" / "base-image" / "agent_server"
)
_BASE_IMAGE_DIR = _AGENT_SERVER_DIR.parent
_BASE_IMAGE_STR = str(_BASE_IMAGE_DIR)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)


def _preload_backend_utils():
    """Install src/backend/utils as the canonical `utils` package for unit
    tests. Uses importlib's file-based loader so sys.path ordering can't
    shadow it later.
    """
    utils_init = _BACKEND / "utils" / "__init__.py"
    if not utils_init.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "utils", str(utils_init), submodule_search_locations=[str(_BACKEND / "utils")]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["utils"] = module
    spec.loader.exec_module(module)
    # Preload helpers so `from utils.helpers import X` resolves without
    # triggering sys.path-based lookup.
    helpers_spec = importlib.util.spec_from_file_location(
        "utils.helpers", str(_BACKEND / "utils" / "helpers.py")
    )
    helpers_mod = importlib.util.module_from_spec(helpers_spec)
    sys.modules["utils.helpers"] = helpers_mod
    helpers_spec.loader.exec_module(helpers_mod)
    module.helpers = helpers_mod  # type: ignore[attr-defined]


def _preload_real_agent_server():
    """Install docker/base-image/agent_server as the canonical `agent_server`
    package for unit tests.

    When running the full test suite, pytest collects tests/agent_server/ first
    and registers it as the `agent_server` package in sys.modules (pointing to
    the tests helper package, not the base-image implementation). Unit tests
    that do `from agent_server.models import ExecutionMetadata` then fail with
    ModuleNotFoundError because tests/agent_server/ has no models.py.

    Fix: evict any stale tests/agent_server registration and replace it with a
    namespace-package shim whose __path__ points to docker/base-image/agent_server.
    This matches the shim each individual unit test file already installs via
    `if "agent_server" not in sys.modules` — we just do it earlier so the guard
    fires correctly even when the tests/agent_server package was pre-loaded.
    """
    if not _AGENT_SERVER_DIR.exists():
        return

    # Evict any previously-registered agent_server (and submodules) that point
    # to tests/agent_server/ rather than docker/base-image/agent_server/.
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            existing = sys.modules[_mod]
            existing_path = getattr(existing, "__path__", None)
            if existing_path and not any(
                str(_AGENT_SERVER_DIR) in p for p in existing_path
            ):
                sys.modules.pop(_mod, None)

    # Register the real agent_server as a namespace package so that
    # `from agent_server.models import X` resolves via __path__ (the file
    # system), without executing agent_server/__init__.py (which boots FastAPI).
    if "agent_server" not in sys.modules:
        _stub = types.ModuleType("agent_server")
        _stub.__path__ = [str(_AGENT_SERVER_DIR)]  # type: ignore[attr-defined]
        _stub.__package__ = "agent_server"
        sys.modules["agent_server"] = _stub


# Evict any shadow `utils` that parent conftest already cached, then preload
# backend's utils package.
for _mod in list(sys.modules):
    if _mod == "utils" or _mod.startswith("utils."):
        sys.modules.pop(_mod, None)
_preload_backend_utils()

# Evict any tests/agent_server shadow and register the real base-image package.
_preload_real_agent_server()


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override parent's cleanup_after_test that requires api_client."""
    yield
