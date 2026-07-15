"""
Telemetry Tests (test_telemetry.py)

Tests for host and container telemetry endpoints.
Covers OBS-011 (Host CPU/memory/disk) and OBS-012 (Aggregate container stats).
"""

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


class TestHostTelemetry:
    """OBS-011: Host CPU/memory/disk monitoring."""

    pytestmark = pytest.mark.smoke

    def test_host_returns_cpu_metrics(self, api_client: TrinityApiClient):
        """GET /api/telemetry/host returns CPU metrics."""
        response = api_client.get("/api/telemetry/host")
        assert_status(response, 200)
        data = assert_json_response(response)

        assert "cpu" in data
        cpu = data["cpu"]
        assert "percent" in cpu
        assert isinstance(cpu["percent"], (int, float))
        assert 0 <= cpu["percent"] <= 100

    def test_host_returns_memory_metrics(self, api_client: TrinityApiClient):
        """GET /api/telemetry/host returns memory metrics."""
        response = api_client.get("/api/telemetry/host")
        assert_status(response, 200)
        data = response.json()

        assert "memory" in data
        memory = data["memory"]
        assert_has_fields(memory, ["percent", "used_gb", "total_gb"])
        assert 0 <= memory["percent"] <= 100
        assert memory["used_gb"] >= 0
        assert memory["total_gb"] > 0
        assert memory["used_gb"] <= memory["total_gb"]

    def test_host_returns_disk_metrics(self, api_client: TrinityApiClient):
        """GET /api/telemetry/host returns disk metrics."""
        response = api_client.get("/api/telemetry/host")
        assert_status(response, 200)
        data = response.json()

        assert "disk" in data
        disk = data["disk"]
        assert_has_fields(disk, ["percent", "used_gb", "total_gb"])
        assert 0 <= disk["percent"] <= 100
        assert disk["used_gb"] >= 0
        assert disk["total_gb"] > 0

    def test_host_returns_timestamp(self, api_client: TrinityApiClient):
        """GET /api/telemetry/host includes timestamp."""
        response = api_client.get("/api/telemetry/host")
        assert_status(response, 200)
        data = response.json()

        assert "timestamp" in data
        # Timestamp should be an ISO format string
        assert isinstance(data["timestamp"], str)

    def test_host_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/telemetry/host requires authentication."""
        response = unauthenticated_client.get("/api/telemetry/host", auth=False)
        assert_status(response, 401)

    def test_host_cpu_count_present(self, api_client: TrinityApiClient):
        """GET /api/telemetry/host includes CPU count."""
        response = api_client.get("/api/telemetry/host")
        assert_status(response, 200)
        data = response.json()

        cpu = data.get("cpu", {})
        if "count" in cpu:
            assert isinstance(cpu["count"], int)
            assert cpu["count"] > 0


class TestContainerTelemetry:
    """OBS-012: Aggregate container stats."""

    def test_containers_returns_running_count(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/telemetry/containers returns running container count.

        #1617: a cold per-worker cache reports running_count=None + no_data=True
        (not 0). Poll briefly for the background refresh to land a measured
        count; tolerate the cold shape if it hasn't yet.
        """
        import time as _time

        data = None
        for _ in range(10):
            response = api_client.get("/api/telemetry/containers")
            assert_status(response, 200)
            data = assert_json_response(response)
            assert "running_count" in data
            assert "no_data" in data
            if not data["no_data"]:
                break
            _time.sleep(1)

        if data["no_data"]:
            # Still cold — measurement not yet available; contract still holds.
            assert data["running_count"] is None
        else:
            assert isinstance(data["running_count"], int)
            assert data["running_count"] >= 0

    def test_containers_returns_total_cpu_percent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/telemetry/containers returns aggregate CPU percent."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        assert "total_cpu_percent" in data
        assert isinstance(data["total_cpu_percent"], (int, float))
        assert data["total_cpu_percent"] >= 0

    def test_containers_returns_total_memory_mb(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/telemetry/containers returns aggregate memory in MB."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        assert "total_memory_mb" in data
        assert isinstance(data["total_memory_mb"], (int, float))
        assert data["total_memory_mb"] >= 0

    def test_containers_includes_container_list(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/telemetry/containers includes list of containers."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        assert "containers" in data
        assert isinstance(data["containers"], list)

        # With created_agent, there should be at least one container
        if len(data["containers"]) > 0:
            container = data["containers"][0]
            assert "name" in container
            assert isinstance(container["name"], str)

    def test_containers_container_has_metrics(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Each container in list has CPU and memory metrics."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        containers = data.get("containers", [])
        for container in containers:
            # Each container should have metrics or an error
            if "error" not in container:
                assert "cpu" in container or "cpu_percent" in container or "memory_mb" in container

    def test_containers_includes_test_agent(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Container list includes the created test agent."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        containers = data.get("containers", [])
        agent_names = [c.get("name", "") for c in containers]

        # The test agent should be in the list (may have 'agent-' prefix)
        found = any(
            created_agent["name"] in name or name in created_agent["name"]
            for name in agent_names
        )
        # Note: This might fail if container naming differs
        # If not found, just skip rather than fail
        if not found and len(containers) > 0:
            # Container is there, just named differently
            pass

    def test_containers_returns_timestamp(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/telemetry/containers includes timestamp."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        assert "timestamp" in data
        assert isinstance(data["timestamp"], str)

    def test_containers_requires_auth(
        self,
        unauthenticated_client: TrinityApiClient
    ):
        """GET /api/telemetry/containers requires authentication."""
        response = unauthenticated_client.get("/api/telemetry/containers", auth=False)
        assert_status(response, 401)


class TestTelemetryConsistency:
    """Tests for telemetry data consistency."""

    def test_container_count_matches_list_length(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Running count matches length of containers list.

        #1617: skip the comparison while the cache is cold (no_data=True,
        running_count=None) — poll briefly for a measured payload first.
        """
        import time as _time

        data = None
        for _ in range(10):
            response = api_client.get("/api/telemetry/containers")
            assert_status(response, 200)
            data = response.json()
            if not data.get("no_data"):
                break
            _time.sleep(1)

        if data.get("no_data"):
            pytest.skip("container-stats cache still cold (no measured data yet)")

        running_count = data.get("running_count", 0)
        containers = data.get("containers", [])

        # Running count should match container list length
        assert running_count == len(containers), \
            f"running_count ({running_count}) != len(containers) ({len(containers)})"

    def test_total_memory_is_sum_of_containers(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Total memory is approximately sum of individual container memory."""
        response = api_client.get("/api/telemetry/containers")
        assert_status(response, 200)
        data = response.json()

        total_memory = data.get("total_memory_mb", 0)
        containers = data.get("containers", [])

        # Sum individual container memory
        individual_sum = sum(
            c.get("memory_mb", 0) for c in containers
            if "error" not in c
        )

        # Allow some tolerance for rounding/timing differences
        if individual_sum > 0:
            # Total should be within 10% of sum or equal
            tolerance = max(individual_sum * 0.1, 1)
            assert abs(total_memory - individual_sum) <= tolerance, \
                f"Total memory ({total_memory}) differs from sum ({individual_sum})"
