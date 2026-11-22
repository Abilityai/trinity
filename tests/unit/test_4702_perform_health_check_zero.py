"""perform_health_check reports a measured 0% uptime / 0 ms latency (#4702).

The 24h figures were built with a truthiness test, so a measured `0.0` (an
agent unreachable for the whole window, or a 0 ms probe) came back as `None`,
which consumers read as "never measured". Sibling of #4685 (router builder).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _stub_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def monitoring_service(monkeypatch):
    fake_docker = _stub_module("docker")
    fake_docker.from_env = MagicMock(side_effect=Exception("stubbed"))
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    fake_db_module = _stub_module("database")
    fake_db_module.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    monkeypatch.setitem(
        sys.modules,
        "services.docker_service",
        _stub_module(
            "services.docker_service",
            docker_client=None,
            get_agent_container=MagicMock(return_value=None),
            list_all_agents_fast=MagicMock(return_value=[]),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "services.docker_utils",
        _stub_module("services.docker_utils", container_exec_run=AsyncMock()),
    )

    class _ClosedCircuit:
        def __init__(self, agent_name):
            self.state = "closed"

    monkeypatch.setitem(
        sys.modules,
        "services.agent_client",
        _stub_module("services.agent_client", CircuitState=_ClosedCircuit),
    )

    spec = importlib.util.spec_from_file_location(
        "monitoring_service_under_test_4702",
        str(_BACKEND / "services" / "monitoring_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "monitoring_service_under_test_4702", module)
    spec.loader.exec_module(module)

    from db_models import (
        AgentHealthStatus,
        BusinessHealthCheck,
        DockerHealthCheck,
        NetworkHealthCheck,
    )

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    async def fake_network(name, _timeout):
        return NetworkHealthCheck(
            agent_name=name, reachable=False, latency_ms=0.0, checked_at=now
        )

    async def fake_business(name, _timeout):
        return BusinessHealthCheck(agent_name=name, status="unknown", checked_at=now)

    def fake_docker_check(name):
        return DockerHealthCheck(
            agent_name=name, container_status="running", checked_at=now
        )

    monkeypatch.setattr(module, "check_network_health", fake_network)
    monkeypatch.setattr(module, "check_business_health", fake_business)
    monkeypatch.setattr(module, "check_docker_health", fake_docker_check)
    monkeypatch.setattr(
        module, "aggregate_health", lambda *_a, **_k: (AgentHealthStatus.UNHEALTHY, [])
    )
    return module, fake_db_module.db


def _check(module):
    return asyncio.run(module.perform_health_check("agent-x", store_results=False))


def test_measured_zero_uptime_and_latency_are_reported_as_zero(monitoring_service):
    mod, fake_db = monitoring_service
    fake_db.calculate_uptime_percent.return_value = 0.0
    fake_db.calculate_avg_latency.return_value = 0.0

    result = _check(mod)

    assert result.uptime_percent_24h == 0.0
    assert result.uptime_percent_24h is not None
    assert result.avg_latency_24h_ms == 0.0
    assert result.avg_latency_24h_ms is not None


def test_no_measurement_still_reads_none(monitoring_service):
    mod, fake_db = monitoring_service
    fake_db.calculate_uptime_percent.return_value = None
    fake_db.calculate_avg_latency.return_value = None

    result = _check(mod)

    assert result.uptime_percent_24h is None
    assert result.avg_latency_24h_ms is None
