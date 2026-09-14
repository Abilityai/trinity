"""
Shared pytest fixtures for Trinity API tests.

⚠️  THIS SUITE MUTATES THE TARGET INSTANCE. The API fixtures CREATE and DELETE
real agents on whatever ``TRINITY_API_URL`` points at. Point it at a local dev
instance — never staging/production. Every agent the suite creates is named
with the ``pytest-ephemeral-`` prefix and is torn down by name; the suite never
deletes an agent it did not create. The optional leftover-sweep (for crashed
runs) is OFF by default and, even when enabled, only removes
``pytest-ephemeral-`` agents on a localhost target (#1558).

OPTIMIZATION NOTES (2025-12-09):
- Module-scoped agent fixture: Creates ONE agent per test FILE (not per test)
- Session-scoped shared agent: Single agent for tests that can share
- Tiered test execution: smoke < core < full

Configuration:
- TRINITY_API_URL: Backend URL (default: http://localhost:8000)
- TRINITY_TEST_USERNAME: Test user username (default: admin)
- TRINITY_TEST_PASSWORD: Test user password (default: password)
- TRINITY_MCP_API_KEY: MCP API key for authenticated tests
- TEST_AGENT_NAME: Pre-existing agent for agent-server tests
- TRINITY_TEST_CLEANUP_SWEEP: opt-in (1/true) — sweep leftover
  ``pytest-ephemeral-`` agents from prior crashed runs at session start.
  Refuses unless TRINITY_API_URL is localhost. Default OFF (#1558).
"""

# Skip test files that require backend context (can't be run from test suite)
collect_ignore: list[str] = []  # test_archive_security relocated to tests/unit/ (#1895)

# WHERE TESTS LIVE (#1895): the per-PR CI unit job collects ONLY tests/unit/, so a
# self-contained test belongs there, not here. The live-in-root set (files that
# genuinely need a running backend) is enforced by tests/lint_root_test_placement.py
# + its ratcheted baseline — do NOT hand-maintain a list here. The two intentional
# basename twins that STAY in root, test_1083_callback_endpoint.py (a
# pytest.mark.smoke live layer) and test_websocket_auth.py (a real wss:// handshake),
# are deliberate: their mocked/direct logic twins live under tests/unit/ and the two
# layers legitimately coexist across the two collected islands (do not dedupe).

# ---------------------------------------------------------------------------
# Issue #589: backend config now raises at import-time if REDIS_URL lacks
# credentials. Set a dummy creds-bearing URL BEFORE any backend module is
# imported (the preload calls below trigger transitive `import config` for
# many tests). Real Redis tests under tests/security/ override these via
# their own conftest from .env.
# ---------------------------------------------------------------------------
import os as _os_589
# #1775: whether the CALLER supplied a Redis target, captured BEFORE the
# sentinels below are installed. This is the only place that can know: once the
# setdefault runs, `"REDIS_URL" in os.environ` is unconditionally true, so a
# child conftest cannot tell "the harness told us where Redis is" from "this
# file defaulted it to a dummy". tests/integration/conftest.py reads this to
# decide whether an unreachable Redis is a FAILURE (harness declared a target)
# or a legitimate SKIP (we had to derive the target ourselves).
CALLER_SUPPLIED_REDIS_URL = "REDIS_URL" in _os_589.environ
ROOT_REDIS_URL_SENTINEL = "redis://test:test@redis:6379"
ROOT_REDIS_PASSWORD_SENTINEL = "test"
_os_589.environ.setdefault("REDIS_URL", ROOT_REDIS_URL_SENTINEL)
_os_589.environ.setdefault("REDIS_PASSWORD", ROOT_REDIS_PASSWORD_SENTINEL)
_os_589.environ.setdefault("REDIS_BACKEND_PASSWORD", ROOT_REDIS_PASSWORD_SENTINEL)

# ---------------------------------------------------------------------------
# Issue #754 (C-003): Load SECRET_KEY and INTERNAL_API_SECRET from the
# project .env file so internal API tests can authenticate against the
# running backend without requiring the caller to export these manually.
# Uses setdefault so explicitly-provided env vars always win.
# dotenv_values handles duplicate keys by returning the last value.
# ---------------------------------------------------------------------------
import pathlib as _pathlib754
_dot_env_754 = _pathlib754.Path(__file__).resolve().parent.parent / ".env"
if _dot_env_754.exists():
    try:
        from dotenv import dotenv_values as _dotenv_values_754
        _env_vals_754 = _dotenv_values_754(_dot_env_754)
        # INTERNAL_API_SECRET / SECRET_KEY / AGENT_AUTH_SECRET have no earlier
        # default — setdefault is fine (the caller's explicit export still wins).
        # AGENT_AUTH_SECRET (#1159) is auto-generated into .env by start.sh; the
        # backend's derive_agent_token() raises without it, so tests that mint
        # per-agent auth tokens (circuit breaker, monitoring, watchdog, PAT
        # propagation) need it loaded here too.
        for _k754 in ("INTERNAL_API_SECRET", "SECRET_KEY", "AGENT_AUTH_SECRET"):
            _v754 = _env_vals_754.get(_k754)
            if _v754:
                _os_589.environ.setdefault(_k754, _v754)
        # REDIS_BACKEND_PASSWORD was setdefault'd to "test" at line 30 for
        # backend-config import safety; that placeholder must be OVERRIDDEN
        # when the real .env value is available, otherwise tests/security/
        # ACL tests pass garbage to redis-cli. Don't clobber an explicit
        # caller export (which would have set the var before this module ran).
        _rbp754 = _env_vals_754.get("REDIS_BACKEND_PASSWORD")
        if _rbp754 and _os_589.environ.get("REDIS_BACKEND_PASSWORD") in (None, "test"):
            _os_589.environ["REDIS_BACKEND_PASSWORD"] = _rbp754
        # TRINITY_TEST_PASSWORD aliases ADMIN_PASSWORD so the api_client
        # default ("password") doesn't trip the per-account rate limiter
        # (5 fails / 900s at routers/auth.py:35-46).
        if "TRINITY_TEST_PASSWORD" not in _os_589.environ:
            _admin_pw = _env_vals_754.get("ADMIN_PASSWORD")
            if _admin_pw:
                _os_589.environ["TRINITY_TEST_PASSWORD"] = _admin_pw
    except ImportError:
        pass  # python-dotenv not installed; env vars must be set manually

# ---------------------------------------------------------------------------
# Pre-load src/backend/models as the canonical `models` in sys.modules
# BEFORE any test file is collected.
#
# Problem: test_inter_agent_timeout_unit.py does
#   sys.modules.setdefault("models", _fake_models)
# at module-import time (before fixtures run) so that it can import
# routers/fan_out.py without a full backend environment. In a combined run,
# `test_inter_agent_timeout_unit.py` is collected alphabetically before
# `test_self_execute.py`, `test_validation.py`, and `test_watchdog_unit.py`
# (which do `from models import TaskExecutionStatus` etc.). Because `models`
# is not in sys.modules yet, the fake stub gets installed permanently and
# the real backend models never loads → ImportError for missing symbols.
#
# Fix: pre-register utils.helpers (backend) and models.py here before any
# test file runs its top-level code. setdefault in that unit test then
# becomes a benign no-op.
#
# NOTE (#2080): this constraint is HISTORY. The test helpers used to live in
# `tests/utils`, which sits first on the pythonpath and therefore shadowed
# `src/backend/utils` — so `utils` meant the test package, `utils.helpers` had
# to be force-installed as a submodule of it, and every module added to the
# backend package (most recently `safe_yaml.py`) became unimportable, taking
# ~1,000 tests with it. The helpers are now `tests/testkit`, so `utils`
# unambiguously means `src/backend/utils` and no shadow-repair is needed.
# The preload below is kept for a DIFFERENT reason: several root-level tests
# stub `utils.helpers` into sys.modules at import time, and this pins the real
# module as the baseline the per-test restore returns to.
#
# Also pre-register `routers` as a proper namespace package pointing to
# src/backend/routers/. test_inter_agent_timeout_unit.py has:
#   if "routers" not in sys.modules:
#       sys.modules["routers"] = types.ModuleType("routers")  # plain module!
# at module-import time. If we pre-register with __path__, the guard fires
# and the plain-module stub is never installed. Otherwise, test_ip_rate_limit_fix.py
# (collected later) finds `routers` already registered as a plain module
# (no __path__) and `import routers.public` fails with "routers is not a package".
# ---------------------------------------------------------------------------
import importlib.util
import sys
import types
from pathlib import Path as _Path

_TESTS_DIR = _Path(__file__).resolve().parent
_PROJECT_ROOT = _TESTS_DIR.parent
_BACKEND = _PROJECT_ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)


def _preload_backend_helpers_submodule():
    """Pin src/backend/utils/helpers.py as the canonical `utils.helpers`.

    Since #2080 this is a POLLUTION defence, not a shadow repair: the test
    helpers moved to `tests/testkit`, so `utils` already resolves to the
    backend package. What this still buys is a known-good baseline for the
    #762 per-test restore, because several root-level tests replace
    `utils.helpers` with a stub at module-import time.
    """
    existing = sys.modules.get("utils.helpers")
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and _BACKEND_STR in str(existing_file):
            return  # already correct

    helpers_path = _BACKEND / "utils" / "helpers.py"
    if not helpers_path.exists():
        return

    spec = importlib.util.spec_from_file_location(
        "utils.helpers", str(helpers_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["utils.helpers"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _preload_backend_models():
    """Load src/backend/models.py as the canonical `models` module."""
    existing = sys.modules.get("models")
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and _BACKEND_STR in str(existing_file):
            if hasattr(existing, "ActivityType"):
                return  # already complete and correct
        # Evict the wrong / partial entry.
        del sys.modules["models"]

    models_path = _BACKEND / "models.py"
    if not models_path.exists():
        return

    # utils.helpers must be resolvable before models.py is exec'd.
    _preload_backend_helpers_submodule()

    # Backend path needed for any other transitive imports inside models.py.
    if _BACKEND_STR not in sys.path:
        sys.path.insert(0, _BACKEND_STR)

    spec = importlib.util.spec_from_file_location("models", str(models_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["models"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert hasattr(module, "ActivityType"), (
        "models.py loaded but ActivityType is missing — check for import errors"
    )


def _preload_backend_routers_namespace():
    """Pre-register src/backend/routers/ as the `routers` namespace package.

    Historically test_inter_agent_timeout_unit.py installed a PLAIN `routers`
    module stub at collection (`sys.modules["routers"] = types.ModuleType(...)`,
    no __path__), which broke a later `import routers.public`
    ("routers is not a package"). Since #1895 relocated that file to tests/unit/
    (and migrated this preload into tests/unit/conftest.py), the root is left with
    NO plain-stub installer, and `src/backend/routers/__init__.py` resolves as a
    real package on sys.path anyway — so this preload is now harmless insurance
    for the root island, not a hard dependency. Kept as a belt.
    """
    routers_dir = _BACKEND / "routers"
    if not routers_dir.exists():
        return

    existing = sys.modules.get("routers")
    if existing is not None:
        # Upgrade to a package if it's currently a plain module.
        if not getattr(existing, "__path__", None):
            existing.__path__ = [str(routers_dir)]  # type: ignore[attr-defined]
            existing.__package__ = "routers"
        return

    pkg = types.ModuleType("routers")
    pkg.__path__ = [str(routers_dir)]  # type: ignore[attr-defined]
    pkg.__package__ = "routers"
    sys.modules["routers"] = pkg


_preload_backend_models()
_preload_backend_routers_namespace()

# Pre-load services.agent_client so CircuitState is in sys.modules before
# test_voice_tools.py installs an incomplete stub (#762 followup).
try:
    import services.agent_client  # noqa: F401
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Issue #762: cross-file sys.modules pollution baseline + autouse restore.
#
# Several root-level test files (e.g. test_validation.py, test_watchdog_unit.py,
# test_self_execute.py, test_inter_agent_timeout_unit.py) stub `utils.helpers`,
# `database`, `models`, `services.*` etc. into sys.modules at module-collection
# time (top-level `sys.modules[k] = stub`). When `test_audit_log_unit.py` runs
# its fixtures later, `from db.audit import ...` triggers
#   db/__init__.py → db/schedules.py → from utils.helpers import iso_cutoff
# and fails because the cached `utils.helpers` is the stub, not the real
# backend module.
#
# Fix: capture each invariant module's object ONCE at conftest import (after
# the preloads above have run), and restore it after every test. This is
# function-scoped because the bug is intra-session pollution; per-test restore
# is the right granularity.
#
# Codex review pin: the snapshot MUST be captured at conftest import time, not
# at fixture setup. A fixture-setup snapshot would record an already-polluted
# value and treat it as truth. This block runs immediately after
# _preload_backend_models() / _preload_backend_routers_namespace() so the
# baseline is the pristine post-preload state.
# ---------------------------------------------------------------------------
_SYS_MODULES_INVARIANT_KEYS = (
    # Backend canonical modules pre-loaded above.
    "utils.helpers",
    "models",
    # `db.*` subtree: stale entries break `from db.audit import ...` etc.
    "db",
    "db.audit",
    "db.connection",
    "db.schedules",
    "db.users",
    "db.agents",
    "db.activities",
    "db.chat",
    "db.mcp_keys",
    "db.permissions",
    "db.shared_folders",
    "db.settings",
    # `database` is mocked by test_validation.py at line 43.
    "database",
    # Services: stubbed by test_validation.py (task_execution_service),
    # test_telegram_webhook_backfill.py (platform_audit_service,
    # settings_service), test_voice_tools.py (agent_client), etc.
    "services",
    "services.agent_client",
    "services.docker_service",
    "services.platform_audit_service",
    "services.settings_service",
    "services.task_execution_service",
    "services.validation_service",
    # #736: the outbound A2A target resolver is FAIL-CLOSED, and a stubbed
    # `sys.modules` entry silently inverts that INSIDE the suite that proves it
    # closed — a `MagicMock` module's `resolve_endpoint()` returns a truthy mock
    # whose `.url` is also a mock, i.e. a resolved endpoint out of thin air. The
    # module's own `isinstance(ResolvedEndpoint)` check is the real defence;
    # this baseline pin is the belt, so a cross-file stub cannot leave a later
    # test asserting fail-closed against a mock that fails open.
    "services.a2a_outbound",
    # Dependencies / adapters: stubbed by various adapter unit tests.
    "dependencies",
    "adapters",
    "adapters.transports",
    # Utility shadows.
    "utils.credential_sanitizer",
)

_SYS_MODULES_BASELINE = {
    k: sys.modules.get(k) for k in _SYS_MODULES_INVARIANT_KEYS
}
# #1481: db.schedules is now a package — baseline any loaded db.schedules.<slice>
# children too (prefix match, so a future sub-split needs no edit) so cross-file
# pollution of a submodule is restored to its canonical object, not just the parent.
_SYS_MODULES_BASELINE.update(
    {k: sys.modules[k] for k in list(sys.modules) if k.startswith("db.schedules.")}
)


def _restore_invariant_sys_modules() -> None:
    """Restore invariant keys whose baseline value was a real module object.
    Keys that had no baseline (None) are left untouched — they may be
    deliberate stubs installed by individual test files for their own use
    (e.g. test_validation.py stubs `services.task_execution_service`),
    and evicting them would break those tests."""
    for k, baseline in _SYS_MODULES_BASELINE.items():
        if baseline is not None:
            sys.modules[k] = baseline


# ---------------------------------------------------------------------------
# End of early-init block
# ---------------------------------------------------------------------------

import os
import pytest
import uuid
import time
from typing import Callable, Generator


@pytest.fixture(autouse=True)
def _restore_sys_modules_baseline_762():
    """Autouse: restore the pristine post-preload sys.modules baseline both
    before AND after every test. Defends against cross-file pollution from
    test files that install stubs into sys.modules at module-collection time
    (Issue #762) — restore-before catches collection-time pollution that
    landed before this test ran; restore-after catches pollution from inside
    the test itself."""
    _restore_invariant_sys_modules()
    yield
    _restore_invariant_sys_modules()

from testkit.api_client import TrinityApiClient, ApiConfig
from testkit.cleanup import (
    ResourceTracker,
    cleanup_test_agent,
    register_created_agent,
    session_created_agents,
    select_sweepable_agents,
    is_local_target,
    EPHEMERAL_AGENT_PREFIX,
)


def pytest_configure(config):
    """Configure custom markers.

    Also clean up leftover test agents from prior runs to avoid quota exhaustion.
    """
    config.addinivalue_line("markers", "smoke: mark test as smoke test (fast, no agent)")
    config.addinivalue_line("markers", "slow: mark test as slow running (chat execution)")
    config.addinivalue_line("markers", "requires_agent: test requires a running agent")
    config.addinivalue_line("markers", "unit: unit tests that don't need backend")
    config.addinivalue_line("markers", "integration: tests requiring a running Docker stack (#589)")


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--cleanup-only",
        action="store_true",
        default=False,
        help="Only run cleanup of test resources, no tests",
    )
    parser.addoption(
        "--skip-cleanup",
        action="store_true",
        default=False,
        help="Skip cleanup of test resources after tests",
    )
    parser.addoption(
        "--fast",
        action="store_true",
        default=False,
        help="Run only fast tests (no agent creation)",
    )


@pytest.fixture(scope="session")
def api_config() -> ApiConfig:
    """Load API configuration from environment."""
    return ApiConfig.from_env()


@pytest.fixture(scope="session")
def api_client(api_config: ApiConfig) -> Generator[TrinityApiClient, None, None]:
    """Create authenticated API client for the test session.

    #1558: the previous version deleted EVERY agent named ``test-*`` on the
    target instance at session start — silent, unprompted data loss that
    destroyed real user agents. The leftover-sweep is now:
      - OFF by default (opt-in via ``TRINITY_TEST_CLEANUP_SWEEP``),
      - localhost-only (refuses staging/prod), and
      - scoped to the suite's own ``pytest-ephemeral-`` prefix — never the
        broad ``test-`` a human might use.
    Agents this session creates are registered and reclaimed by name at the end
    (belt-and-suspenders over each fixture's own teardown).
    """
    import sys

    client = TrinityApiClient(api_config)
    try:
        client.authenticate()

        # Opt-in leftover-sweep for crashed prior runs. select_sweepable_agents
        # is fail-closed: [] unless enabled AND localhost, and only ever returns
        # provably suite-owned names.
        sweep_enabled = os.getenv("TRINITY_TEST_CLEANUP_SWEEP", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if sweep_enabled and not is_local_target(api_config.base_url):
            print(
                f"\n[conftest] TRINITY_TEST_CLEANUP_SWEEP set but target "
                f"'{api_config.base_url}' is not localhost — refusing to sweep (#1558)",
                file=sys.stderr,
            )
        try:
            response = client.get("/api/agents")
            if response.status_code == 200:
                names = [a["name"] for a in response.json()]
                leftover = select_sweepable_agents(
                    names, api_config.base_url, enabled=sweep_enabled
                )
                for name in leftover:
                    cleanup_test_agent(client, name, require_suite_owned=True)
                if leftover:
                    print(
                        f"\n[conftest] Swept {len(leftover)} leftover "
                        f"'{EPHEMERAL_AGENT_PREFIX}' agent(s)",
                        file=sys.stderr,
                    )
        except Exception:
            pass  # Best-effort cleanup

        yield client
    finally:
        # Reclaim any agent this session created that a fixture teardown missed
        # (e.g. a crashed fixture). Only touches registered, suite-owned names.
        for name in session_created_agents():
            try:
                cleanup_test_agent(client, name, require_suite_owned=True)
            except Exception:
                pass
        client.close()


@pytest.fixture(scope="session")
def unauthenticated_client(api_config: ApiConfig) -> Generator[TrinityApiClient, None, None]:
    """Create unauthenticated API client."""
    client = TrinityApiClient(api_config)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="function")
def resource_tracker() -> ResourceTracker:
    """Track created resources for cleanup."""
    return ResourceTracker()


@pytest.fixture(scope="function")
def ws_ticket(api_client: TrinityApiClient) -> Callable[[], str]:
    """Mint a fresh single-use WebSocket ticket (#550).

    Returned callable allows a single test to mint multiple tickets
    (e.g. replay tests that need a second fresh ticket).
    """
    def _mint() -> str:
        resp = api_client.post("/api/ws/ticket")
        assert resp.status_code == 200, resp.text
        return resp.json()["ticket"]
    return _mint


@pytest.fixture(scope="function")
def test_agent_name() -> str:
    """Generate unique test agent name.

    #1558: uses the dedicated ``pytest-ephemeral-`` prefix (never the broad
    ``test-``) and registers the name so it's reclaimed at session end even if a
    test forgets its own teardown. Registering an unused name is harmless — the
    reclaim is 404-tolerant.
    """
    name = f"{EPHEMERAL_AGENT_PREFIX}agent-{uuid.uuid4().hex[:8]}"
    register_created_agent(name)
    return name


@pytest.fixture(scope="function")
def test_credential_name() -> str:
    """Generate unique test credential name."""
    return f"TEST_API_CRED_{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture(scope="function")
def test_mcp_key_name() -> str:
    """Generate unique test MCP key name."""
    return f"test-api-key-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="function")
def test_schedule_name() -> str:
    """Generate unique test schedule name."""
    return f"test-api-schedule-{uuid.uuid4().hex[:8]}"


# =============================================================================
# MODULE-SCOPED AGENT FIXTURE (OPTIMIZED)
# Creates ONE agent per test module instead of per test function
# =============================================================================

@pytest.fixture(scope="module")
def module_agent_name(request) -> str:
    """Generate unique agent name for the module."""
    # Use module name to create predictable but unique name (#1558: ephemeral prefix)
    module_name = request.module.__name__.replace("test_", "").replace("_", "-")[:20]
    name = f"{EPHEMERAL_AGENT_PREFIX}{module_name}-{uuid.uuid4().hex[:6]}"
    register_created_agent(name)
    return name


@pytest.fixture(scope="module")
def created_agent(
    api_client: TrinityApiClient,
    module_agent_name: str,
    request
) -> Generator[dict, None, None]:
    """Create a test agent for the entire module.

    OPTIMIZED: scope="module" means ONE agent per test file.
    All tests in the same file share this agent.
    """
    agent_name = module_agent_name

    # Create the agent
    response = api_client.post(
        "/api/agents",
        json={"name": agent_name},
    )

    if response.status_code not in [200, 201]:
        pytest.skip(f"Failed to create test agent: {response.text}")

    agent = response.json()

    # Wait for agent to be ready (optimized wait - check status instead of fixed sleep)
    max_wait = 45
    start = time.time()
    agent_data = None
    while time.time() - start < max_wait:
        check = api_client.get(f"/api/agents/{agent_name}")
        if check.status_code == 200:
            agent_data = check.json()
            if agent_data.get("status") == "running":
                # Brief wait for agent server to fully initialize
                time.sleep(2)
                break
        time.sleep(1)

    if not agent_data or agent_data.get("status") != "running":
        cleanup_test_agent(api_client, agent_name)
        pytest.skip(f"Agent {agent_name} did not start within {max_wait}s")

    yield agent_data

    # Cleanup after ALL tests in module complete
    if not request.config.getoption("--skip-cleanup"):
        cleanup_test_agent(api_client, agent_name)


@pytest.fixture(scope="module")
def stopped_agent(
    api_client: TrinityApiClient,
    request
) -> Generator[dict, None, None]:
    """Create a stopped test agent for the module.

    Creates agent, waits for it to start, then stops it.
    OPTIMIZED: scope="module" - one stopped agent per test file.
    """
    agent_name = f"{EPHEMERAL_AGENT_PREFIX}stopped-{uuid.uuid4().hex[:6]}"
    register_created_agent(agent_name)

    # Create the agent
    response = api_client.post(
        "/api/agents",
        json={"name": agent_name},
    )

    if response.status_code not in [200, 201]:
        pytest.skip(f"Failed to create test agent: {response.text}")

    # Wait for agent to start
    time.sleep(8)

    # Stop the agent
    api_client.post(f"/api/agents/{agent_name}/stop")
    time.sleep(2)

    # Get final state
    check = api_client.get(f"/api/agents/{agent_name}")
    if check.status_code == 200:
        yield check.json()
    else:
        pytest.skip("Failed to get agent state")

    # Cleanup
    if not request.config.getoption("--skip-cleanup"):
        cleanup_test_agent(api_client, agent_name)


# =============================================================================
# SESSION-SCOPED SHARED AGENT (MAXIMUM OPTIMIZATION)
# Single agent shared across ALL tests that don't modify agent state
# =============================================================================

@pytest.fixture(scope="session")
def shared_agent(api_client: TrinityApiClient, request) -> Generator[dict, None, None]:
    """Session-scoped shared agent for read-only tests.

    Use this for tests that:
    - Only READ data (logs, info, files)
    - Don't modify agent state
    - Don't need isolation

    DO NOT use for tests that:
    - Modify agent settings
    - Test agent creation/deletion
    - Need a clean agent state
    """
    agent_name = f"{EPHEMERAL_AGENT_PREFIX}shared-{uuid.uuid4().hex[:6]}"
    register_created_agent(agent_name)

    # Create agent
    response = api_client.post(
        "/api/agents",
        json={"name": agent_name},
    )

    if response.status_code not in [200, 201]:
        pytest.skip(f"Failed to create shared test agent: {response.text}")

    # Wait for agent to be ready
    max_wait = 45
    start = time.time()
    agent_data = None
    while time.time() - start < max_wait:
        check = api_client.get(f"/api/agents/{agent_name}")
        if check.status_code == 200:
            agent_data = check.json()
            if agent_data.get("status") == "running":
                time.sleep(3)
                break
        time.sleep(1)

    if not agent_data or agent_data.get("status") != "running":
        cleanup_test_agent(api_client, agent_name)
        pytest.skip(f"Shared agent did not start within {max_wait}s")

    yield agent_data

    # Cleanup after entire test session
    if not request.config.getoption("--skip-cleanup"):
        cleanup_test_agent(api_client, agent_name)


# =============================================================================
# LEGACY FUNCTION-SCOPED FIXTURE (for tests that need isolation)
# =============================================================================

@pytest.fixture(scope="function")
def isolated_agent(
    api_client: TrinityApiClient,
    test_agent_name: str,
    resource_tracker,
    request
) -> Generator[dict, None, None]:
    """Create an ISOLATED test agent (cleaned up after each test).

    Use this ONLY for tests that:
    - Modify agent state destructively
    - Test agent deletion
    - Need guaranteed clean state

    For most tests, use `created_agent` (module-scoped) instead.
    """
    # Create the agent
    response = api_client.post(
        "/api/agents",
        json={"name": test_agent_name},
    )

    if response.status_code not in [200, 201]:
        pytest.skip(f"Failed to create test agent: {response.text}")

    agent = response.json()
    resource_tracker.track_agent(test_agent_name)

    # Wait for agent to be ready
    max_wait = 30
    start = time.time()
    while time.time() - start < max_wait:
        check = api_client.get(f"/api/agents/{test_agent_name}")
        if check.status_code == 200:
            agent_data = check.json()
            if agent_data.get("status") == "running":
                time.sleep(2)
                yield agent_data
                break
        time.sleep(1)
    else:
        cleanup_test_agent(api_client, test_agent_name)
        pytest.skip(f"Agent {test_agent_name} did not start within {max_wait}s")

    # Cleanup
    if not request.config.getoption("--skip-cleanup"):
        cleanup_test_agent(api_client, test_agent_name)


@pytest.fixture(scope="session")
def pre_existing_agent(api_config: ApiConfig) -> str:
    """Get pre-existing agent name from environment.

    Used for agent-server direct tests that need a running agent.
    """
    agent_name = api_config.test_agent_name
    if not agent_name:
        pytest.skip("TEST_AGENT_NAME environment variable not set")
    return agent_name


def pytest_collection_modifyitems(config, items):
    """Modify test collection based on markers and options."""
    # If cleanup-only mode, skip all tests
    if config.getoption("--cleanup-only"):
        skip_all = pytest.mark.skip(reason="cleanup-only mode")
        for item in items:
            item.add_marker(skip_all)

    # If fast mode, skip tests that require agents
    if config.getoption("--fast"):
        skip_agent = pytest.mark.skip(reason="--fast mode: skipping agent tests")
        for item in items:
            if "requires_agent" in [m.name for m in item.iter_markers()]:
                item.add_marker(skip_agent)
            # Also skip tests that use created_agent fixture
            if "created_agent" in item.fixturenames or "stopped_agent" in item.fixturenames:
                item.add_marker(skip_agent)


@pytest.fixture(autouse=True)
def cleanup_after_test(api_client: TrinityApiClient, resource_tracker: ResourceTracker, request):
    """Automatically clean up tracked resources after each test."""
    yield
    if not request.config.getoption("--skip-cleanup"):
        resource_tracker.cleanup(api_client)


# --- #2140: module stubs that cannot outlive the load they exist for ---------
import contextlib as _contextlib


@_contextlib.contextmanager
def stubbed_modules(mapping):
    """Install `mapping` into `sys.modules` for a block, then restore and verify.

    Three unit files hand-load `services/agent_service/lifecycle.py`, which needs
    the REAL `services.agent_service*` names present while it executes — line 27
    is an absolute `from services.agent_service.helpers import validate_base_image`,
    so the relative slots those files register are not enough. All three used to
    install them PERMANENTLY.

    That is not a tidiness point. `services/compatibility/spec.py`:

        def applies_to_runtime(check, runtime):
            if not check.claude_only:
                return True
            from services.agent_service.helpers import is_claude_runtime

    resolves that import LAZILY, so it picks up whatever is in `sys.modules` at
    call time. Every test running afterwards silently lost `claude_only`
    filtering and `test_codex_runtime_omits_claude_only_checks` failed —
    presenting as a real #1187 multi-runtime regression rather than as pollution
    from another file. `pytest-randomly` made it a latent flake, not a constant.

    Shared rather than inlined BECAUSE it was inlined: the first fix repaired
    one copy and left two others producing the identical symptom.

    The exit assertion uses IDENTITY, not `isinstance(..., Mock)`: the package
    stub is a real `types.ModuleType` from `module_from_spec`, so a type check
    waves through a re-leak of `services.agent_service` itself — the leak
    `test_1310_auth_consolidation.py` documents as aborting its own collection.
    A leak therefore fails at IMPORT, in the file that caused it, naming it.
    """
    snapshot = {name: sys.modules.get(name) for name in mapping}
    sys.modules.update(mapping)
    try:
        yield
    finally:
        for name, original in snapshot.items():
            if original is not None:
                sys.modules[name] = original
            else:
                sys.modules.pop(name, None)
        leaked = [name for name, stub in mapping.items() if sys.modules.get(name) is stub]
        assert not leaked, (
            f"module stubs outlived the load that needed them: {leaked}. "
            "They are resolved lazily by services/compatibility/spec.py, so this "
            "breaks claude_only filtering in whatever test runs next — see #2140."
        )


def ensure_schema_tables(*names: str) -> None:
    """Create the named `db/schema.py` tables + their indexes on the engine.

    Idempotent. Replaces the deleted `client_portal/schema.py` and
    `shared_sessions/schema.py` shims (#2493): a test that needs tables present
    without running the full migration chain applies the canonical DDL directly
    (Invariant #3 — the DDL's single home is `db/schema.py`). Index statements
    are matched by table name rather than a hand-kept list, so an index added
    to the schema cannot be silently missed here.
    """
    from sqlalchemy import text

    from db.engine import get_engine
    from db.schema import INDEXES, TABLES

    markers = tuple(f"{name}(" for name in names)
    with get_engine().begin() as conn:
        for name in names:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if any(marker in stmt for marker in markers):
                conn.execute(text(stmt))
