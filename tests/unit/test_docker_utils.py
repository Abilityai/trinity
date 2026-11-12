"""
Unit tests for async Docker utilities.

Tests the ThreadPoolExecutor-based async wrappers for blocking Docker SDK calls.
Uses mocking to test in isolation without requiring Docker daemon.

Module: src/backend/services/docker_utils.py
Issue: https://github.com/abilityai/trinity/issues/42
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch
import sys
from pathlib import Path

# Add backend path for imports (relative to this file)
_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / 'src' / 'backend')
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def get_docker_utils():
    """Import docker_utils with mocked dependencies to avoid import chain."""
    # Mock the docker_client import at module level
    mock_docker_client = Mock()

    # Pre-populate sys.modules with mocks to avoid import chain
    # The chain is: docker_utils -> docker_service -> models -> utils.helpers
    with patch.dict('sys.modules', {
        'services.docker_service': Mock(docker_client=mock_docker_client)
    }):
        # Force reimport
        if 'services.docker_utils' in sys.modules:
            del sys.modules['services.docker_utils']

        # Now import - it will use our mocked docker_client
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "docker_utils",
            f"{backend_path}/services/docker_utils.py"
        )
        docker_utils = importlib.util.module_from_spec(spec)

        spec.loader.exec_module(docker_utils)

        # #4406: docker_utils reaches the client through docker_service.get_docker_client().
        docker_utils.docker_service = Mock(get_docker_client=Mock(return_value=mock_docker_client))

        return docker_utils, mock_docker_client


@pytest.mark.unit
class TestContainerOperations:
    """Test async container operation wrappers."""

    @pytest.mark.asyncio
    async def test_container_stop_calls_docker_sdk(self):
        """container_stop() executes container.stop() in thread pool."""
        docker_utils, _ = get_docker_utils()

        mock_container = Mock()
        mock_container.stop = Mock()

        await docker_utils.container_stop(mock_container, timeout=15)

        # Verify stop was called with correct timeout
        mock_container.stop.assert_called_once_with(timeout=15)

    @pytest.mark.asyncio
    async def test_container_start_calls_docker_sdk(self):
        """container_start() executes container.start() in thread pool."""
        docker_utils, _ = get_docker_utils()

        mock_container = Mock()
        mock_container.start = Mock()

        await docker_utils.container_start(mock_container)

        mock_container.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_container_reload_calls_docker_sdk(self):
        """container_reload() executes container.reload() in thread pool."""
        docker_utils, _ = get_docker_utils()

        mock_container = Mock()
        mock_container.reload = Mock()

        await docker_utils.container_reload(mock_container)

        mock_container.reload.assert_called_once()

    @pytest.mark.asyncio
    async def test_container_remove_calls_docker_sdk(self):
        """container_remove() executes container.remove() in thread pool."""
        docker_utils, _ = get_docker_utils()

        mock_container = Mock()
        mock_container.remove = Mock()

        await docker_utils.container_remove(mock_container, force=True)

        mock_container.remove.assert_called_once_with(force=True)

    @pytest.mark.asyncio
    async def test_container_stats_returns_dict(self):
        """container_stats() returns stats dictionary from Docker SDK."""
        docker_utils, _ = get_docker_utils()

        expected_stats = {
            "cpu_stats": {"cpu_usage": {"total_usage": 1000}},
            "memory_stats": {"usage": 500000}
        }

        mock_container = Mock()
        mock_container.stats = Mock(return_value=expected_stats)

        result = await docker_utils.container_stats(mock_container, stream=False)

        assert result == expected_stats
        mock_container.stats.assert_called_once_with(stream=False)


@pytest.mark.unit
class TestVolumeOperations:
    """Test async volume operation wrappers."""

    @pytest.mark.asyncio
    async def test_volume_get_calls_docker_sdk(self):
        """volume_get() retrieves volume via docker_client.volumes.get()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_volume = Mock()
        mock_docker_client.volumes.get = Mock(return_value=mock_volume)

        result = await docker_utils.volume_get("test-volume")

        assert result == mock_volume
        mock_docker_client.volumes.get.assert_called_once_with("test-volume")

    @pytest.mark.asyncio
    async def test_volume_create_calls_docker_sdk(self):
        """volume_create() creates volume via docker_client.volumes.create()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_volume = Mock()
        mock_docker_client.volumes.create = Mock(return_value=mock_volume)

        result = await docker_utils.volume_create("test-volume", labels={"app": "trinity"})

        assert result == mock_volume
        mock_docker_client.volumes.create.assert_called_once_with(
            name="test-volume",
            labels={"app": "trinity"}
        )

    @pytest.mark.asyncio
    async def test_volume_remove_calls_docker_sdk(self):
        """volume_remove() removes volume via volume.remove()."""
        docker_utils, _ = get_docker_utils()

        mock_volume = Mock()
        mock_volume.remove = Mock()

        await docker_utils.volume_remove(mock_volume)

        mock_volume.remove.assert_called_once()


@pytest.mark.unit
class TestContainerCreation:
    """Test async container creation wrapper."""

    @pytest.mark.asyncio
    async def test_containers_run_calls_docker_sdk(self):
        """containers_run() creates container via docker_client.containers.run()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_container = Mock()
        mock_docker_client.containers.run = Mock(return_value=mock_container)

        result = await docker_utils.containers_run(
            "trinity-agent-base:latest",
            name="agent-test",
            detach=True,
            network="trinity-agent-network"
        )

        assert result == mock_container
        mock_docker_client.containers.run.assert_called_once_with(
            "trinity-agent-base:latest",
            command=None,
            name="agent-test",
            detach=True,
            network="trinity-agent-network"
        )


@pytest.mark.unit
class TestExecOperations:
    """Test async exec operation wrappers."""

    @pytest.mark.asyncio
    async def test_container_exec_run_calls_docker_sdk(self):
        """container_exec_run() executes command via container.exec_run()."""
        docker_utils, _ = get_docker_utils()

        mock_result = Mock()
        mock_result.exit_code = 0
        mock_result.output = b"command output"

        mock_container = Mock()
        mock_container.exec_run = Mock(return_value=mock_result)

        result = await docker_utils.container_exec_run(
            mock_container,
            "echo hello",
            user="developer"
        )

        assert result.exit_code == 0
        mock_container.exec_run.assert_called_once()
        call_kwargs = mock_container.exec_run.call_args[1]
        assert call_kwargs['user'] == 'developer'

    @pytest.mark.asyncio
    async def test_api_exec_create_calls_docker_api(self):
        """api_exec_create() creates exec instance via docker_client.api.exec_create()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_exec_instance = {"Id": "exec123"}
        mock_docker_client.api.exec_create = Mock(return_value=mock_exec_instance)

        result = await docker_utils.api_exec_create(
            "container123",
            ["claude", "--dangerously-skip-permissions"],
            stdin=True,
            tty=True,
            user="developer"
        )

        assert result["Id"] == "exec123"
        mock_docker_client.api.exec_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_exec_start_calls_docker_api(self):
        """api_exec_start() starts exec instance via docker_client.api.exec_start()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_socket = Mock()
        mock_docker_client.api.exec_start = Mock(return_value=mock_socket)

        result = await docker_utils.api_exec_start("exec123", socket=True, tty=True)

        assert result == mock_socket
        mock_docker_client.api.exec_start.assert_called_once_with(
            "exec123", socket=True, tty=True
        )


@pytest.mark.unit
class TestErrorHandling:
    """Test error propagation from Docker SDK."""

    @pytest.mark.asyncio
    async def test_container_stop_propagates_docker_errors(self):
        """Docker SDK errors propagate through async wrapper."""
        docker_utils, _ = get_docker_utils()

        mock_container = Mock()
        mock_container.stop = Mock(side_effect=Exception("Container not found"))

        with pytest.raises(Exception) as excinfo:
            await docker_utils.container_stop(mock_container)

        assert "Container not found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_volume_get_raises_not_found(self):
        """volume_get() raises exception for missing volumes."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_docker_client.volumes.get = Mock(
            side_effect=Exception("Volume not found")
        )

        with pytest.raises(Exception) as excinfo:
            await docker_utils.volume_get("nonexistent-volume")

        assert "Volume not found" in str(excinfo.value)


@pytest.mark.unit
class TestThreadPoolExecutor:
    """Test ThreadPoolExecutor configuration."""

    def test_executor_has_limited_workers(self):
        """Executor is limited to 4 workers to avoid overwhelming Docker daemon."""
        docker_utils, _ = get_docker_utils()

        # ThreadPoolExecutor._max_workers is the configured max
        assert docker_utils._docker_executor._max_workers == 4

    def test_executor_has_descriptive_thread_prefix(self):
        """Executor threads have 'docker-' prefix for debugging."""
        docker_utils, _ = get_docker_utils()

        assert docker_utils._docker_executor._thread_name_prefix == "docker-"


@pytest.mark.unit
class TestContainerGet:
    """Test container_get wrapper."""

    @pytest.mark.asyncio
    async def test_container_get_calls_docker_sdk(self):
        """container_get() retrieves container via docker_client.containers.get()."""
        docker_utils, mock_docker_client = get_docker_utils()

        mock_container = Mock()
        mock_docker_client.containers.get = Mock(return_value=mock_container)

        result = await docker_utils.container_get("agent-test")

        assert result == mock_container
        mock_docker_client.containers.get.assert_called_once_with("agent-test")
