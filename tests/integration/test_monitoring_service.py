"""Integration tests for services/monitoring_service.py (#474).

`check_network_health()` mirrors the failure classification rule from
`agent_client._request()`: only ConnectError / ConnectTimeout count toward
the per-agent circuit breaker. Any HTTP response (200..599) records success.
Transient read/write errors do not increment the circuit.

`aggregate_health()` flags `network.status_code >= 500` as UNHEALTHY so a
wedged-but-listening agent (now `reachable=True`) doesn't silently pass as
HEALTHY.

These tests use the real Redis-backed CircuitState — they need a running
Redis (the same one the integration circuit-breaker tests use). HTTP
traffic is intercepted via `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import redis as _redis


# Modules this test stubs into sys.modules at import time so that
# monitoring_service.py can be loaded standalone (without going through
# services/__init__). The snapshot/restore fixture below keeps any
# per-test mutations from leaking between tests in this file.
#
# Recognised by tests/lint_sys_modules.py (Issue #762) as the canonical
# escape hatch for module-level sys.modules bootstrapping.
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.helpers",
    "services.agent_client",
    "database",
    "services.docker_service",
    "services.docker_utils",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules state for stubbed names, restore after each test."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ── Backend setup (matches test_circuit_breaker.py pattern) ──────────────────

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_env_password() -> str:
    """Pull REDIS_BACKEND_PASSWORD out of the repo .env."""
    env_path = _REPO / ".env"
    if not env_path.exists():
        pytest.skip(".env missing — cannot derive Redis credentials")
    for line in env_path.read_text().splitlines():
        if line.startswith("REDIS_BACKEND_PASSWORD="):
            return line.split("=", 1)[1].strip()
    pytest.skip("REDIS_BACKEND_PASSWORD not found in .env")


_PASSWORD = _load_env_password()
os.environ["REDIS_URL"] = f"redis://backend:{_PASSWORD}@localhost:6379"
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", _PASSWORD)


# Pre-load src/backend/utils/helpers.py as the `utils.helpers` submodule so
# monitoring_service's `from utils.helpers import utc_now_iso` resolves to
# the backend version rather than a missing tests/utils/helpers.py.
def _preload_backend_helpers():
    helpers_path = _BACKEND / "utils" / "helpers.py"
    if not helpers_path.exists():
        return
    if "utils.helpers" in sys.modules:
        existing_file = getattr(sys.modules["utils.helpers"], "__file__", None)
        if existing_file and str(_BACKEND) in str(existing_file):
            return
    # Ensure `utils` is a package pointing at src/backend/utils so submodule
    # lookups work.
    utils_pkg = sys.modules.get("utils")
    if utils_pkg is None or not getattr(utils_pkg, "__path__", None):
        pkg = types.ModuleType("utils")
        pkg.__path__ = [str(_BACKEND / "utils")]
        sys.modules["utils"] = pkg
    spec = importlib.util.spec_from_file_location(
        "utils.helpers", str(helpers_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["utils.helpers"] = mod
    spec.loader.exec_module(mod)


_preload_backend_helpers()


# Load the real agent_client and register it as `services.agent_client` so
# monitoring_service.check_network_health's lazy import resolves it.
_AC_SPEC = importlib.util.spec_from_file_location(
    "services.agent_client",
    str(_BACKEND / "services" / "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_AC_SPEC)
sys.modules["services.agent_client"] = agent_client
_AC_SPEC.loader.exec_module(agent_client)


# Stub `database`, `services.docker_service`, and `services.docker_utils` ONLY
# for the monitoring_service module load below, then restore the real
# sys.modules entries. These are imported lazily inside health-check functions
# and are never exercised by the tests, so the stubs are needed only to satisfy
# imports during exec_module. Leaving them installed permanently leaks a bare
# `services.docker_utils` (no `container_get_archive`) into later test files
# such as test_whatsapp_adapter, breaking their imports with
# "cannot import name 'container_get_archive' ... (unknown location)" (#211).
# `services.agent_client` (loaded above) is the REAL module and IS used at test
# runtime by check_network_health's lazy import, so it deliberately stays.
_CONFINED_KEYS = ("database", "services.docker_service", "services.docker_utils")
_saved_mods = {k: sys.modules.get(k) for k in _CONFINED_KEYS}

_fake_database = types.ModuleType("database")
_fake_database.db = MagicMock()
sys.modules["database"] = _fake_database

_fake_docker_service = types.ModuleType("services.docker_service")
_fake_docker_service.docker_client = None
_fake_docker_service.get_agent_container = MagicMock(return_value=None)
_fake_docker_service.list_all_agents_fast = MagicMock(return_value=[])
sys.modules["services.docker_service"] = _fake_docker_service

_fake_docker_utils = types.ModuleType("services.docker_utils")
sys.modules["services.docker_utils"] = _fake_docker_utils


# Load monitoring_service via spec_from_file_location (avoid services/__init__).
_MS_SPEC = importlib.util.spec_from_file_location(
    "monitoring_service_under_test",
    str(_BACKEND / "services" / "monitoring_service.py"),
)
monitoring_service = importlib.util.module_from_spec(_MS_SPEC)
_MS_SPEC.loader.exec_module(monitoring_service)

# monitoring_service now holds its own bound references (db, lazy importers);
# restore the real sys.modules entries so the fakes don't leak into later
# test files. agent_client is intentionally NOT restored (real + needed).
for _k, _v in _saved_mods.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


pytestmark = pytest.mark.integration


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def redis_client():
    client = _redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    except Exception as e:
        pytest.skip(f"Redis unavailable: {e}")
    yield client
    client.close()


@pytest.fixture
def agent_name(redis_client):
    """Unique per-test agent name with auto-cleanup."""
    name = f"mon-test-{uuid.uuid4().hex[:10]}"
    yield name
    redis_client.delete(
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}",
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}",
    )


@pytest.fixture(autouse=True)
def _reset_agent_client_redis():
    agent_client._reset_circuit_redis_client()
    yield
    agent_client._reset_circuit_redis_client()


def _patch_httpx(monkeypatch, handler):
    """Make every `httpx.AsyncClient(...)` inside monitoring_service route
    through a MockTransport that runs `handler(request)`. The handler may
    return an httpx.Response or raise an exception.
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(monitoring_service.httpx, "AsyncClient", factory)


# ── check_network_health classification ──────────────────────────────────────

class TestCheckNetworkHealthClassification:
    """#474 — check_network_health applies the same classification rule as
    AgentClient._request: only ConnectError/ConnectTimeout trip the circuit;
    any HTTP response records success (5xx is handled downstream by
    aggregate_health)."""

    def _read_failures(self, name: str) -> int:
        return agent_client.CircuitState(name).failure_count

    def _read_state(self, name: str) -> str:
        return agent_client.CircuitState(name).state

    def test_200_records_success_returns_reachable(
        self, agent_name, monkeypatch
    ):
        """Case 1: 200 OK → reachable=True, status_code=200, record_success.

        Pre-seed the circuit with 2 failures; a 200 response must clear them.
        """
        cs = agent_client.CircuitState(agent_name)
        cs.record_failure()
        cs.record_failure()
        assert self._read_failures(agent_name) == 2

        _patch_httpx(
            monkeypatch, lambda _req: httpx.Response(200, json={"ok": True})
        )

        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is True
        assert result.status_code == 200
        assert self._read_failures(agent_name) == 0
        assert self._read_state(agent_name) == "closed"

    def test_503_records_success_returns_reachable(
        self, agent_name, monkeypatch
    ):
        """Case 2: 503 from /health → reachable=True, status_code=503,
        record_success. The 5xx is surfaced via aggregate_health, NOT via
        an open circuit. This is the symmetric-with-_request rule (#474).
        """
        cs = agent_client.CircuitState(agent_name)
        cs.record_failure()
        cs.record_failure()
        assert self._read_failures(agent_name) == 2

        _patch_httpx(
            monkeypatch, lambda _req: httpx.Response(503, text="wedged")
        )

        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is True
        assert result.status_code == 503
        # Counter must clear — agent demonstrated reachability.
        assert self._read_failures(agent_name) == 0
        assert self._read_state(agent_name) == "closed"

    def test_connect_error_records_failure(self, agent_name, monkeypatch):
        """Case 3: ConnectError → reachable=False, +1 circuit failure."""

        def handler(_req):
            raise httpx.ConnectError("refused")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        assert result.status_code is None
        assert self._read_failures(agent_name) == 1

    def test_connect_timeout_records_failure(self, agent_name, monkeypatch):
        """Case 4: ConnectTimeout → reachable=False, +1 circuit failure.

        ConnectTimeout is a TimeoutException subclass — must match
        CIRCUIT_FAILURE_EXCEPTIONS first.
        """

        def handler(_req):
            raise httpx.ConnectTimeout("handshake timed out")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        assert result.status_code is None
        assert self._read_failures(agent_name) == 1

    def test_read_timeout_records_failure(self, agent_name, monkeypatch):
        """Case 5: ReadTimeout on /health → reachable=False, +1 circuit failure.

        /health is supposed to be a small, fast endpoint. A read timeout
        here is a liveness signal (event-loop wedged / GIL stuck), so the
        /health-specific TimeoutException handler in monitoring_service
        DOES record_failure() even though the same exception is circuit-
        neutral in AgentClient._request().
        """

        def handler(_req):
            raise httpx.ReadTimeout("slow")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        assert self._read_failures(agent_name) == 1

    def test_read_error_records_failure(self, agent_name, monkeypatch):
        """Case 6: ReadError on /health → reachable=False, +1 circuit failure.

        Partial-write/mid-read drop on /health is a liveness signal
        (agent crashed mid-write — OOM, segfault, event-loop wedge), so
        the /health-specific handler in monitoring_service does
        record_failure().
        """

        def handler(_req):
            raise httpx.ReadError("mid-read socket loss")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        assert self._read_failures(agent_name) == 1

    def test_pool_timeout_records_failure(self, agent_name, monkeypatch):
        """Case 7: PoolTimeout on /health → reachable=False, +1 circuit failure.

        PoolTimeout subclasses httpx.TimeoutException, so it falls into the
        /health-specific timeout handler that treats any timeout as a
        liveness signal and records_failure().
        """

        def handler(_req):
            raise httpx.PoolTimeout("pool exhausted")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        assert self._read_failures(agent_name) == 1

    def test_unrelated_exception_does_not_record(self, agent_name, monkeypatch):
        """Case 8: unrelated RuntimeError → log+swallow, 0 circuit delta.

        Unknown errors are almost always our bug, not agent unhealth; we
        should not penalise the circuit for them.
        """

        def handler(_req):
            raise RuntimeError("our bug")

        _patch_httpx(monkeypatch, handler)
        result = asyncio.run(monitoring_service.check_network_health(agent_name))
        assert result.reachable is False
        # Error message captured but no circuit increment.
        assert "RuntimeError" in (result.error or "")
        assert self._read_failures(agent_name) == 0
        assert self._read_state(agent_name) == "closed"

    def test_cancelled_error_propagates(self, agent_name, monkeypatch):
        """Case 9: asyncio.CancelledError during probe must propagate
        (don't swallow during shutdown).

        `asyncio.run()` wraps CancelledError in its task cancellation flow,
        so we check that *some* exception was raised — and crucially, that
        no NetworkHealthCheck was returned (which would mean we silently
        swallowed the cancellation).
        """

        def handler(_req):
            raise asyncio.CancelledError()

        _patch_httpx(monkeypatch, handler)

        # The cancellation must propagate, not be swallowed.
        result = None
        with pytest.raises(BaseException):
            result = asyncio.run(
                monitoring_service.check_network_health(agent_name)
            )
        assert result is None, (
            "CancelledError swallowed — function returned a result"
        )
        # No circuit increment — the cancellation isn't an agent-health signal.
        assert self._read_failures(agent_name) == 0


# ── aggregate_health 5xx classification ──────────────────────────────────────

class TestAggregateHealthFiveHundred:
    """#474 — aggregate_health() must flag network.status_code >= 500 as
    UNHEALTHY. Without this, the new 'any HTTP response = reachable' rule
    would let a wedged-but-listening agent silently pass as HEALTHY."""

    def _make_inputs(self, status_code, reachable=True, error=None):
        from db_models import (
            DockerHealthCheck,
            NetworkHealthCheck,
            BusinessHealthCheck,
        )

        now = "2026-05-11T00:00:00Z"
        docker = DockerHealthCheck(
            agent_name="x",
            container_status="running",
            checked_at=now,
        )
        network = NetworkHealthCheck(
            agent_name="x",
            reachable=reachable,
            status_code=status_code,
            error=error,
            checked_at=now,
        )
        business = BusinessHealthCheck(
            agent_name="x",
            status="healthy",
            runtime_available=True,
            claude_available=True,
            checked_at=now,
        )
        return docker, network, business

    def test_200_status_routes_healthy(self):
        """Status 200, docker+business OK → HEALTHY."""
        from db_models import AgentHealthStatus

        docker, network, business = self._make_inputs(status_code=200)
        status, issues = monitoring_service.aggregate_health(
            docker, network, business
        )
        assert status == AgentHealthStatus.HEALTHY
        assert issues == []

    def test_500_status_routes_unhealthy(self):
        """Status 500 → UNHEALTHY with descriptive issue.

        The new rule: 'any HTTP response' makes reachable=True, but a 5xx
        means the HTTP layer is broken. aggregate_health flags it.
        """
        from db_models import AgentHealthStatus

        docker, network, business = self._make_inputs(status_code=500)
        status, issues = monitoring_service.aggregate_health(
            docker, network, business
        )
        assert status == AgentHealthStatus.UNHEALTHY
        assert any("/health returned 500" in i for i in issues)

    def test_503_status_routes_unhealthy(self):
        """Status 503 → UNHEALTHY with descriptive issue."""
        from db_models import AgentHealthStatus

        docker, network, business = self._make_inputs(status_code=503)
        status, issues = monitoring_service.aggregate_health(
            docker, network, business
        )
        assert status == AgentHealthStatus.UNHEALTHY
        assert any("/health returned 503" in i for i in issues)

    def test_unreachable_still_unhealthy(self):
        """Regression guard: status_code=None + reachable=False → UNHEALTHY.

        The existing path (Network unreachable) must keep working.
        """
        from db_models import AgentHealthStatus

        docker, network, business = self._make_inputs(
            status_code=None, reachable=False, error="ConnectError: refused"
        )
        status, issues = monitoring_service.aggregate_health(
            docker, network, business
        )
        assert status == AgentHealthStatus.UNHEALTHY
        assert any("unreachable" in i.lower() for i in issues)
