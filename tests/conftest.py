"""
Shared pytest fixtures for Trinity API tests.

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
"""

# Skip test files that require backend context (can't be run from test suite)
collect_ignore = ["test_archive_security.py"]

# ---------------------------------------------------------------------------
# Issue #589: backend config now raises at import-time if REDIS_URL lacks
# credentials. Set a dummy creds-bearing URL BEFORE any backend module is
# imported (the preload calls below trigger transitive `import config` for
# many tests). Real Redis tests under tests/security/ override these via
# their own conftest from .env.
# ---------------------------------------------------------------------------
import os as _os_589
_os_589.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
_os_589.environ.setdefault("REDIS_PASSWORD", "test")
_os_589.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

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
# Constraint: this conftest also does `from utils.api_client import ...`
# which needs tests/utils/api_client.py (NOT src/backend/utils/). We must
# NOT replace the `utils` package entry — only install `utils.helpers` as
# a submodule while leaving `utils` itself pointing to tests/utils/.
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
    """Pre-register src/backend/utils/helpers.py as `utils.helpers`.

    Does NOT change `sys.modules["utils"]` — conftest.py needs that to
    point to tests/utils/ (for api_client, cleanup, etc.). We only
    install the helpers submodule so that `from utils.helpers import X`
    inside models.py resolves to the backend version.
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

    test_inter_agent_timeout_unit.py runs at collection time and does:
        if "routers" not in sys.modules:
            sys.modules["routers"] = types.ModuleType("routers")  # plain module!
    A plain module has no __path__, so `import routers.public` later in
    test_ip_rate_limit_fix.py fails with "routers is not a package".

    Pre-registering a proper namespace package here causes the
    `if "routers" not in sys.modules:` guard to fire and prevents the
    plain-module stub from being installed.
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

# ---------------------------------------------------------------------------
# End of early-init block
# ---------------------------------------------------------------------------

import os
import pytest
import uuid
import time
from typing import Generator

from utils.api_client import TrinityApiClient, ApiConfig
from utils.cleanup import ResourceTracker, cleanup_test_agent


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

    Also cleans up leftover test agents from prior runs to avoid quota exhaustion.
    """
    client = TrinityApiClient(api_config)
    try:
        client.authenticate()

        # Clean up leftover test agents from prior runs
        try:
            response = client.get("/api/agents")
            if response.status_code == 200:
                agents = response.json()
                leftover = [a["name"] for a in agents if a["name"].startswith("test-")]
                for name in leftover:
                    cleanup_test_agent(client, name)
                if leftover:
                    import sys
                    print(f"\n[conftest] Cleaned up {len(leftover)} leftover test agents", file=sys.stderr)
        except Exception:
            pass  # Best-effort cleanup

        yield client
    finally:
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
def test_agent_name() -> str:
    """Generate unique test agent name."""
    return f"test-api-agent-{uuid.uuid4().hex[:8]}"


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
    # Use module name to create predictable but unique name
    module_name = request.module.__name__.replace("test_", "").replace("_", "-")[:20]
    return f"test-{module_name}-{uuid.uuid4().hex[:6]}"


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
    agent_name = f"test-stopped-{uuid.uuid4().hex[:6]}"

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
    agent_name = f"test-shared-session-{uuid.uuid4().hex[:6]}"

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
