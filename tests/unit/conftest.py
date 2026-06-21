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
# #1159: every backend→agent HTTP call now derives a per-agent auth token, and
# the derivation is fail-closed (raises on an empty master). Give unit tests a
# deterministic master so any test exercising agent_client / helpers / the
# residual inline callers gets a token instead of a RuntimeError.
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)

# database.py instantiates `db = DatabaseManager()` at import, which calls
# init_database() and tries to mkdir(/data). On the host that path is
# read-only, so any unit test importing backend services would fail unless
# an earlier test happens to set TRINITY_DB_PATH first. Pin a tmp path
# here so every unit test's import path is self-sufficient. Tests that
# need a real fixture DB still override via monkeypatch.setenv.
import tempfile as _tempfile  # noqa: E402

# Per-process tmp path so two concurrent `pytest unit/` invocations on the
# same dev machine don't race on the same SQLite file. The DB is initialized
# eagerly at conftest load (via the `import database` preload below), so the
# race window without this PID suffix would be measurable. CI runs one
# process per matrix job and is unaffected either way.
os.environ.setdefault(
    "TRINITY_DB_PATH",
    str(Path(_tempfile.gettempdir()) / f"trinity-unit-tests-{os.getpid()}.db"),
)

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

# Pre-load services.agent_client so CircuitState is in sys.modules before
# test_fleet_status_resilience.py / test_voice_tools.py install partial stubs
# at module-collection time. The autouse restore below then replaces any such
# stub with the real module before each test runs (#762 followup).
try:
    import services.agent_client  # noqa: F401
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Issue #762 followup: cross-file sys.modules pollution for unit tier.
#
# test_fleet_status_resilience.py (module-collection scope) does
#   sys.modules.setdefault("services.agent_client", types.SimpleNamespace(...))
# with a stub missing CircuitState. test_voice_tools.py also installs partial
# stubs (its per-test fixture only protects its own class). Both leak across
# files and break `from services.agent_client import CircuitState` in
# task_execution_service.py, cascading into test_file_upload.py and
# test_session_persistence_flag.py.
#
# Mirror the parent conftest's baseline+autouse mechanism (tests/conftest.py:
# 186-281) for the unit tier, which uses its own rootdir (norecursedirs = ..).
# ---------------------------------------------------------------------------
_SYS_MODULES_INVARIANT_KEYS = (
    "services",
    "services.agent_client",
)

_SYS_MODULES_BASELINE = {
    k: sys.modules.get(k) for k in _SYS_MODULES_INVARIANT_KEYS
}


def _restore_invariant_sys_modules() -> None:
    """Restore invariant keys whose baseline value was a real module object.
    Keys that had no baseline (None) are left untouched — they may be
    deliberate stubs installed by individual test files for their own use."""
    for k, baseline in _SYS_MODULES_BASELINE.items():
        if baseline is not None:
            sys.modules[k] = baseline


# ---------------------------------------------------------------------------
# Cross-test sys.modules baseline-restore (PR #797 follow-up).
#
# Unit tests run with `tests/unit/` as the pytest rootdir because
# `tests/unit/pytest.ini` exists with `norecursedirs = ..`. As a result,
# `tests/conftest.py`'s autouse `_restore_sys_modules_baseline_762`
# fixture is NEVER loaded for the unit suite — the unit suite was the
# blind spot. Without an autouse restore here, any collection-time
# `sys.modules[name] = stub` in one test file leaks for the rest of the
# pytest-randomly session, producing order-dependent failures (e.g. PR
# #797's three `test_voice_auth` regressions, traced to `database` and
# `config` stubs from other files reaching the voice handler).
#
# Mirrors `tests/conftest.py`'s pattern with two improvements:
#  1. Explicit baseline `_VALUES` rather than `_INVARIANT_KEYS` — captures
#     the real module objects so restore is unambiguous.
#  2. Snapshot-and-pop: keys present at baseline are restored to their
#     baseline value; keys absent from baseline but matching a documented
#     `_POP_PREFIXES` are removed. Parent's "skip if baseline is None"
#     rule leaves new stubs in place; we close that hole.
# ---------------------------------------------------------------------------

# Force-load `config` and `database` so they are part of the baseline.
# Test files that stub these (test_voice_tools, test_config_fail_fast,
# test_cleanup_unreachable_orphan) need a real-module baseline value to
# restore against between tests. Both are safe to import here: REDIS_URL
# and TRINITY_DB_PATH are pinned above (lines 19-34) before any backend
# code runs.
import config as _real_config  # noqa: E402,F401
import database as _real_database  # noqa: E402,F401

_SYS_MODULES_BASELINE_KEYS = frozenset(sys.modules)
_SYS_MODULES_BASELINE_VALUES = {
    k: sys.modules[k]
    for k in (
        "utils",
        "utils.helpers",
        "models",
        "database",
        "config",
        # NOTE: `agent_server` is intentionally NOT in this list. Several
        # unit tests (test_agent_server_auto_sync.py, test_git_status_dual_ahead_behind.py)
        # force-reload `agent_server` at module collection with importlib,
        # producing a different module object than the conftest's preload
        # stub. Restoring our stub between tests then misaligns with
        # references those test files captured at collection time. The
        # per-file preload guards (`if "agent_server" not in sys.modules`)
        # handle the cross-file case adequately.
    )
    if k in sys.modules
}

# Prefix policy for popping non-baseline keys after each test. Narrow on
# purpose — aggressive popping (e.g. asyncio/httpx submodules loaded
# transitively) churns the cache and slows the suite without buying
# correctness. Widen this list when a specific cross-file leak is
# observed.
_POP_PREFIXES: tuple[str, ...] = (
    "docker",
    "services.docker_service",
    "services.template_service",
    "services.gemini_voice",
    "services.platform_audit_service",
    "services.task_execution_service",
    "services.cleanup_service",
    "dependencies",
    "passlib",
)


def _restore_unit_sys_modules() -> None:
    for _k, _v in _SYS_MODULES_BASELINE_VALUES.items():
        if _v is not None:
            sys.modules[_k] = _v
    for _k in list(sys.modules):
        if _k in _SYS_MODULES_BASELINE_KEYS:
            continue
        if any(_k == p or _k.startswith(p + ".") for p in _POP_PREFIXES):
            sys.modules.pop(_k, None)


@pytest.fixture(autouse=True)
def _restore_sys_modules_baseline_unit():
    """Restore the pristine post-preload sys.modules baseline before AND
    after every test. Defends against cross-file pollution from test
    files that install stubs at module-collection time. Mirror of the
    parent #762 fixture, which never runs for the unit suite."""
    _restore_unit_sys_modules()
    yield
    _restore_unit_sys_modules()


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override parent's cleanup_after_test that requires api_client.

    Also restores the post-preload sys.modules baseline before AND after every
    test to defend against cross-file pollution (#762 followup)."""
    _restore_invariant_sys_modules()
    yield
    _restore_invariant_sys_modules()
