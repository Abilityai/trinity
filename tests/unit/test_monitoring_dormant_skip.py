"""#631 — perform_health_check must not run network/business probes or write
DB rows when the agent's circuit breaker is dormant.

This was the SQLite-contention source: two uvicorn workers each polling a
known-dead agent every 30s and each emitting 4 health_check rows per cycle.
With Redis-backed dormant state, perform_health_check must short-circuit
into a synthetic AgentHealthDetail without touching db or httpx.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# Backend / config plumbing — match the pattern in other unit tests that load
# backend modules directly via importlib without dragging in services/__init__.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def monitoring_service(monkeypatch):
    """Load src/backend/services/monitoring_service.py with stubs for its
    expensive imports (docker, database)."""
    # Stub docker top-level import.
    fake_docker = _stub_module("docker")
    fake_docker.from_env = MagicMock(side_effect=Exception("stubbed"))
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    # Stub the database facade — we'll spy on it via a MagicMock proxy.
    fake_db_module = _stub_module("database")
    fake_db_module.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    # Stub services.docker_service / docker_utils referenced inside health checks.
    fake_docker_service = _stub_module("services.docker_service")
    fake_docker_service.docker_client = None
    fake_docker_service.get_agent_container = MagicMock(return_value=None)
    fake_docker_service.list_all_agents_fast = MagicMock(return_value=[])
    monkeypatch.setitem(sys.modules, "services.docker_service", fake_docker_service)

    fake_docker_utils = _stub_module("services.docker_utils")
    fake_docker_utils.container_exec_run = AsyncMock()
    monkeypatch.setitem(sys.modules, "services.docker_utils", fake_docker_utils)

    # Stub agent_client so the lazy import inside _is_circuit_dormant resolves
    # without instantiating the real Redis-backed CircuitState.
    fake_agent_client = _stub_module("services.agent_client")

    class _StubCircuitState:
        def __init__(self, agent_name):
            self.agent_name = agent_name
            self.state = _StubCircuitState._state_for(agent_name)

        @staticmethod
        def _state_for(name):
            return _StubCircuitState._state_overrides.get(name, "closed")

        _state_overrides = {}

    fake_agent_client.CircuitState = _StubCircuitState
    monkeypatch.setitem(sys.modules, "services.agent_client", fake_agent_client)

    spec = importlib.util.spec_from_file_location(
        "monitoring_service_under_test",
        str(_BACKEND / "services" / "monitoring_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["monitoring_service_under_test"] = module
    spec.loader.exec_module(module)
    return module, fake_agent_client.CircuitState, fake_db_module.db


pytestmark = pytest.mark.unit


# ── _is_circuit_dormant helper ───────────────────────────────────────────────

class TestIsCircuitDormant:

    def test_dormant_returns_true(self, monitoring_service):
        mod, StubState, _ = monitoring_service
        StubState._state_overrides = {"agent-x": "dormant"}
        assert mod._is_circuit_dormant("agent-x") is True

    def test_open_returns_false(self, monitoring_service):
        mod, StubState, _ = monitoring_service
        StubState._state_overrides = {"agent-x": "open"}
        assert mod._is_circuit_dormant("agent-x") is False

    def test_closed_returns_false(self, monitoring_service):
        mod, _, _ = monitoring_service
        assert mod._is_circuit_dormant("agent-never-touched") is False


# ── perform_health_check short-circuit ───────────────────────────────────────

class TestPerformHealthCheckDormantSkip:

    def test_dormant_returns_synthetic_detail_without_db_writes(
        self, monitoring_service
    ):
        mod, StubState, fake_db = monitoring_service
        StubState._state_overrides = {"sleepy": "dormant"}

        import asyncio
        result = asyncio.run(mod.perform_health_check("sleepy", store_results=True))

        assert result.agent_name == "sleepy"
        assert result.aggregate_status == "unhealthy"
        assert result.docker is None
        assert result.network is None
        assert result.business is None
        assert any("dormant" in iss.lower() for iss in result.issues)

        # No DB writes — the entire flood source is gated.
        assert fake_db.create_health_check.call_count == 0
        # No history reads either (we returned before the alert path).
        assert fake_db.get_agent_health_history.call_count == 0

    def test_non_dormant_still_writes_when_store_results(
        self, monitoring_service, monkeypatch
    ):
        """Sanity check: closed-state agents follow the normal write path.

        Stub the network/business probes so we don't hit real httpx; just
        verify that db.create_health_check is reached.
        """
        mod, StubState, fake_db = monitoring_service
        StubState._state_overrides = {}  # closed for all

        # Avoid running the real probes — they'd try real httpx connections.
        from db_models import (
            AgentHealthStatus,
            DockerHealthCheck,
            NetworkHealthCheck,
            BusinessHealthCheck,
        )
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        async def fake_network(name, _timeout):
            return NetworkHealthCheck(
                agent_name=name, reachable=True, latency_ms=5.0, checked_at=now_iso
            )

        async def fake_business(name, _timeout):
            return BusinessHealthCheck(
                agent_name=name, status="healthy", checked_at=now_iso
            )

        def fake_docker(name):
            return DockerHealthCheck(
                agent_name=name, container_status="running", checked_at=now_iso
            )

        monkeypatch.setattr(mod, "check_network_health", fake_network)
        monkeypatch.setattr(mod, "check_business_health", fake_business)
        monkeypatch.setattr(mod, "check_docker_health", fake_docker)

        # Avoid alert-service path (depends on monitoring_alerts module).
        monkeypatch.setattr(
            mod,
            "aggregate_health",
            lambda *_a, **_kw: (AgentHealthStatus.HEALTHY, []),
        )
        # Stub history calls so we don't error on missing data.
        fake_db.get_agent_health_history = MagicMock(return_value=[])
        fake_db.calculate_uptime_percent = MagicMock(return_value=99.0)
        fake_db.calculate_avg_latency = MagicMock(return_value=10.0)

        # Provide the alert service stub.
        fake_alerts = types.ModuleType("services.monitoring_alerts")

        class _AlertSvc:
            evaluate_and_alert = AsyncMock()
            alert_container_stopped = AsyncMock()
            alert_high_restart_count = AsyncMock()
            alert_resource_critical = AsyncMock()

        fake_alerts.get_alert_service = lambda: _AlertSvc()
        monkeypatch.setitem(sys.modules, "services.monitoring_alerts", fake_alerts)

        import asyncio
        asyncio.run(mod.perform_health_check("alive-agent", store_results=True))

        # Closed-state agent: full DB write path runs (4 inserts: docker,
        # network, business, aggregate).
        assert fake_db.create_health_check.call_count == 4
