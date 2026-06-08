"""#675 — the periodic fleet health check must cover EVERY agent, not
just `status == "running"` ones.

Regression guard for the root cause: `_run_check_cycle` filtered to
running agents, so stopped/exited/crashed agents were never
re-checked and their `agent_health_checks` rows went stale for
months in production. A down agent is exactly the case monitoring
exists to surface.

Also asserts the #675 structured per-pass log line is emitted so a
stalled / partial fleet check is observable next time.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


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


class _FakeAgent:
    """Stand-in for the AgentStatus dataclass list_all_agents_fast() returns."""

    def __init__(self, name: str, status: str):
        self.name = name
        self.status = status


@pytest.fixture
def mon(monkeypatch):
    """Load monitoring_service.py with its heavy imports stubbed."""
    fake_docker = _stub_module("docker")
    fake_docker.from_env = MagicMock(side_effect=Exception("stubbed"))
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    fake_db_module = _stub_module("database")
    fake_db_module.db = MagicMock()
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    fake_docker_service = _stub_module("services.docker_service")
    fake_docker_service.docker_client = None
    fake_docker_service.get_agent_container = MagicMock(return_value=None)
    # Default: a realistic mixed fleet — 1 running, 2 stopped. The #675
    # bug dropped the two stopped ones every cycle.
    fake_docker_service.list_all_agents_fast = MagicMock(return_value=[
        _FakeAgent("competitor-analyzer", "running"),
        _FakeAgent("dd-legal", "stopped"),
        _FakeAgent("dd-market", "stopped"),
    ])
    monkeypatch.setitem(sys.modules, "services.docker_service", fake_docker_service)

    fake_docker_utils = _stub_module("services.docker_utils")
    fake_docker_utils.container_exec_run = AsyncMock()
    monkeypatch.setitem(sys.modules, "services.docker_utils", fake_docker_utils)

    fake_agent_client = _stub_module("services.agent_client")

    class _StubCircuitState:
        _state_overrides: dict = {}

        def __init__(self, agent_name):
            self.agent_name = agent_name
            self.state = self._state_overrides.get(agent_name, "closed")

    fake_agent_client.CircuitState = _StubCircuitState
    monkeypatch.setitem(sys.modules, "services.agent_client", fake_agent_client)

    spec = importlib.util.spec_from_file_location(
        "monitoring_service_675",
        str(_BACKEND / "services" / "monitoring_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "monitoring_service_675", module)
    spec.loader.exec_module(module)
    return module, fake_docker_service


# ---------------------------------------------------------------------------
# Root-cause regression: every agent checked, not just running
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_checks_running_and_stopped_agents(mon):
    module, _ = mon

    captured = {}

    async def _fake_fleet_check(names, config, store_results=True):
        captured["names"] = list(names)
        captured["store_results"] = store_results
        # Return a minimal FleetHealthStatus-shaped object.
        summary = module.FleetHealthSummary(total_agents=len(names))
        summary.healthy = 1
        summary.unhealthy = 2
        return module.FleetHealthStatus(
            enabled=True, last_check_at="now", summary=summary, agents=[]
        )

    module.perform_fleet_health_check = _fake_fleet_check

    svc = module.MonitoringService()
    await svc._run_check_cycle()

    # The whole fleet — including the two stopped agents that the #675
    # bug silently dropped every cycle.
    assert set(captured["names"]) == {
        "competitor-analyzer", "dd-legal", "dd-market"
    }, (
        "fleet check must include stopped agents (#675); "
        f"got {captured['names']}"
    )
    assert captured["store_results"] is True


@pytest.mark.asyncio
async def test_cycle_no_op_when_zero_agents(mon):
    module, fake_docker_service = mon
    fake_docker_service.list_all_agents_fast = MagicMock(return_value=[])

    called = {"n": 0}

    async def _fake_fleet_check(*a, **k):
        called["n"] += 1

    module.perform_fleet_health_check = _fake_fleet_check
    svc = module.MonitoringService()
    await svc._run_check_cycle()

    assert called["n"] == 0, "no agents → no fleet check call"


@pytest.mark.asyncio
async def test_cycle_all_stopped_fleet_still_checked(mon):
    """The exact #675 production shape: nothing running. Pre-fix this
    returned early (running_agents empty) and refreshed nothing."""
    module, fake_docker_service = mon
    fake_docker_service.list_all_agents_fast = MagicMock(return_value=[
        _FakeAgent("dd-legal", "stopped"),
        _FakeAgent("dd-market", "stopped"),
    ])

    captured = {}

    async def _fake_fleet_check(names, config, store_results=True):
        captured["names"] = list(names)
        summary = module.FleetHealthSummary(total_agents=len(names))
        return module.FleetHealthStatus(
            enabled=True, last_check_at="now", summary=summary, agents=[]
        )

    module.perform_fleet_health_check = _fake_fleet_check
    svc = module.MonitoringService()
    await svc._run_check_cycle()

    assert set(captured["names"]) == {"dd-legal", "dd-market"}, (
        "an all-stopped fleet must still be health-checked (#675); "
        "this is the exact months-stale production scenario"
    )


# ---------------------------------------------------------------------------
# #675 ask: structured per-pass log line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pass_summary_log_emitted(mon, caplog):
    module, _ = mon

    async def _fake_fleet_check(names, config, store_results=True):
        summary = module.FleetHealthSummary(total_agents=len(names))
        summary.healthy = 1
        summary.unhealthy = 2
        return module.FleetHealthStatus(
            enabled=True, last_check_at="now", summary=summary, agents=[]
        )

    module.perform_fleet_health_check = _fake_fleet_check
    svc = module.MonitoringService()

    with caplog.at_level(logging.INFO, logger=module.logger.name):
        await svc._run_check_cycle()

    line = next(
        (r.getMessage() for r in caplog.records
         if "fleet_health_check pass complete" in r.getMessage()),
        None,
    )
    assert line is not None, (
        f"expected a structured pass-summary log; "
        f"got {[r.getMessage() for r in caplog.records]}"
    )
    # 1 running + 2 stopped, with the stubbed status breakdown.
    assert "agents=3 (running=1 stopped=2)" in line
    assert "healthy=1" in line and "unhealthy=2" in line
    assert "duration_ms=" in line
