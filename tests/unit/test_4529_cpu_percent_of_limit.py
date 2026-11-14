"""#4529 — agent CPU alerts use the container's own limit, not one host core.

``check_docker_health`` used Docker's host-core formula
``(cpu_delta / system_delta) * online_cpus * 100``, so 100 meant one busy
core and the ceiling was ``online_cpus × 100``. ``MonitoringConfig`` compares
that number with ``cpu_warning_percent`` 80 and ``cpu_critical_percent`` 95.
One core of ordinary work on a 4-CPU agent was reported critical.

The recorded percent is now a share of the container's limit: ``NanoCpus``,
else CFS quota/period, else every online host core when nothing caps it.
100 means the agent is using everything it is allowed. Memory in the same
check was already a share of the container's memory limit and stays that way.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"

# Import-time stubs. lint_sys_modules.py (#762) exempts a file only when it
# declares this list and an autouse ``_restore_sys_modules`` fixture.
_STUBBED_MODULE_NAMES = [
    "database",
    "services.agent_auth",
    "services.docker_service",
    "services.docker_utils",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the stubbed names and put them back after each test.

    The loader also restores immediately: it runs at collection, before any
    fixture, and a leftover stub would leak into later files (#762).
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_monitoring_service():
    """Load monitoring_service.py by path so a polluted ``services`` package
    cannot swap the module under test (#762, same loader as #1439)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    fake_db = types.ModuleType("database")
    fake_db.db = MagicMock()
    sys.modules["database"] = fake_db
    fake_auth = types.ModuleType("services.agent_auth")
    fake_auth.agent_httpx_client = MagicMock()
    sys.modules["services.agent_auth"] = fake_auth
    sys.modules["services.docker_service"] = types.ModuleType("services.docker_service")
    sys.modules["services.docker_utils"] = types.ModuleType("services.docker_utils")
    try:
        spec = importlib.util.spec_from_file_location(
            "monitoring_service_4529_under_test",
            str(_BACKEND / "services" / "monitoring_service.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


_MS = _load_monitoring_service()


def _stats(cpu_delta, system_delta, online):
    """One stats sample. Deltas are the only numbers the formula reads."""
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu_delta + 5},
            "system_cpu_usage": system_delta + 5,
            "online_cpus": online,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 5},
            "system_cpu_usage": 5,
        },
        # 200 MiB of a 1000 MiB limit → 20% and 200 MB. Unchanged by #4529.
        "memory_stats": {"usage": 200 * 1024 * 1024, "limit": 1000 * 1024 * 1024},
    }


def _check(cores_busy, online, host_config):
    """cores_busy is host-core equivalents in the sample (1 → docker-stats 100%)."""
    container = MagicMock()
    container.status = "running"
    container.attrs = {
        "State": {"ExitCode": 0, "OOMKilled": False},
        "RestartCount": 0,
        "HostConfig": host_config,
    }
    # system_delta tracks online cores, so (cpu/system)*online*100 == cores_busy*100.
    container.stats.return_value = _stats(
        cpu_delta=int(cores_busy * 10),
        system_delta=online * 10,
        online=online,
    )
    client = MagicMock()
    client.containers.get.return_value = container
    _MS.docker_client = client
    result = _MS.check_docker_health("worker")
    client.containers.get.assert_called_once_with("agent-worker")
    return result


def _healthy_layers(docker):
    from db_models import BusinessHealthCheck, NetworkHealthCheck

    network = NetworkHealthCheck(
        agent_name="worker", reachable=True, status_code=200, error=None,
        checked_at=docker.checked_at,
    )
    business = BusinessHealthCheck(
        agent_name="worker", status="healthy", runtime_available=True,
        claude_available=True, checked_at=docker.checked_at,
    )
    return docker, network, business


@pytest.mark.unit
@pytest.mark.parametrize(
    "host_config,cores,online,expected",
    [
        # 1 core of a 4-CPU cap: docker stats says 100, the limit says 25.
        ({"NanoCpus": 4_000_000_000}, 1, 24, 25.0),
        # The whole cap is 100, which the 95 default treats as critical.
        ({"NanoCpus": 4_000_000_000}, 4, 24, 100.0),
        # 3.4 of 4 cores = 85: above the warning (80), under critical (95).
        ({"NanoCpus": 4_000_000_000}, 3.4, 24, 85.0),
        # No cap: the limit is the host, so one core of 24 is ~4%.
        ({}, 1, 24, 4.17),
        # NanoCpus unset, quota/period = 2 CPUs.
        ({"NanoCpus": 0, "CpuQuota": 200_000, "CpuPeriod": 100_000}, 1, 24, 50.0),
        # NanoCpus wins when quota names a different cap (2 vs 4).
        (
            {"NanoCpus": 4_000_000_000, "CpuQuota": 200_000, "CpuPeriod": 100_000},
            1, 24, 25.0,
        ),
        # CpuQuota -1 is Docker's "not throttled", not a limit.
        ({"NanoCpus": 0, "CpuQuota": -1, "CpuPeriod": 100_000}, 1, 24, 4.17),
        # Fractional cap: 1 core of 1.5 CPUs.
        ({"NanoCpus": 1_500_000_000}, 1, 8, 66.67),
    ],
)
def test_4529_cpu_percent_is_a_share_of_the_container_limit(
    host_config, cores, online, expected,
):
    result = _check(cores, online, host_config)
    assert result.cpu_percent == expected
    assert result.memory_percent == 20.0
    assert result.memory_mb == 200.0


@pytest.mark.unit
def test_4529_one_core_of_a_four_cpu_cap_does_not_alert():
    docker = _check(1, 24, {"NanoCpus": 4_000_000_000})
    status, issues = _MS.aggregate_health(*_healthy_layers(docker))
    assert docker.cpu_percent == 25.0
    assert status == "healthy"
    assert issues == []


@pytest.mark.unit
def test_4529_filling_the_cap_is_critical_and_the_warning_band_is_not():
    full = _check(4, 24, {"NanoCpus": 4_000_000_000})
    status, issues = _MS.aggregate_health(*_healthy_layers(full))
    assert full.cpu_percent == 100.0
    assert status == "degraded"
    assert issues == ["High CPU usage (100.0%)"]

    warm = _check(3.4, 24, {"NanoCpus": 4_000_000_000})
    status, issues = _MS.aggregate_health(*_healthy_layers(warm))
    assert warm.cpu_percent == 85.0
    assert status == "healthy"
    assert issues == ["Elevated CPU usage (85.0%)"]


@pytest.mark.unit
def test_4529_idle_sample_records_no_cpu_and_stays_healthy():
    docker = _check(0, 24, {"NanoCpus": 4_000_000_000})
    status, issues = _MS.aggregate_health(*_healthy_layers(docker))
    assert docker.cpu_percent is None
    assert status == "healthy"
    assert issues == []
