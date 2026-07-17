"""
Unit tests for async SSH service.

Tests the async methods for SSH credential management that use
Docker exec operations via the async docker_utils wrappers.

Module: src/backend/services/ssh_service.py
Issue: https://github.com/abilityai/trinity/issues/42
Security: https://github.com/abilityai/trinity/issues/175
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, AsyncMock
import sys
import json
import importlib.util
from pathlib import Path

# Add backend path for imports (relative to this file)
_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / 'src' / 'backend')
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

# #1615: password SSH auth (and the stdlib `crypt` import that broke on Python
# 3.13) was removed — no `crypt` mock is needed any more.


def get_ssh_service():
    """Import SshService with mocked dependencies."""
    # Create mock modules to break the import chain
    mock_redis = Mock()
    mock_redis_client = Mock()
    mock_redis.from_url = Mock(return_value=mock_redis_client)

    mock_container = Mock()
    mock_get_agent_container = Mock(return_value=mock_container)
    mock_container_exec_run = AsyncMock()

    # Create mock for docker_utils
    mock_docker_utils = Mock()
    mock_docker_utils.container_exec_run = mock_container_exec_run

    # Pre-populate sys.modules with mocks
    with patch.dict('sys.modules', {
        'redis': mock_redis,
        'services.docker_service': Mock(get_agent_container=mock_get_agent_container),
        'services.docker_utils': mock_docker_utils,
    }):
        # Force reimport
        if 'services.ssh_service' in sys.modules:
            del sys.modules['services.ssh_service']

        # Load module directly
        spec = importlib.util.spec_from_file_location(
            "ssh_service",
            f"{backend_path}/services/ssh_service.py"
        )
        ssh_service = importlib.util.module_from_spec(spec)

        # Inject mocks
        ssh_service.redis = mock_redis
        ssh_service.get_agent_container = mock_get_agent_container
        ssh_service.container_exec_run = mock_container_exec_run

        spec.loader.exec_module(ssh_service)

        return ssh_service, {
            'redis': mock_redis,
            'redis_client': mock_redis_client,
            'get_agent_container': mock_get_agent_container,
            'container_exec_run': mock_container_exec_run,
            'container': mock_container,
        }


@pytest.mark.unit
class TestPasswordAuthRemoved:
    """#1615: password SSH auth is gone — the broken helpers must not exist."""

    def test_password_helpers_are_removed(self):
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()
        assert not hasattr(service, "generate_password")
        assert not hasattr(service, "set_container_password")

    def test_module_does_not_import_crypt(self):
        """The stdlib `crypt` import (removed in Python 3.13) is gone."""
        import pathlib
        src = pathlib.Path(backend_path, "services", "ssh_service.py").read_text()
        assert "import crypt" not in src


@pytest.mark.unit
class TestAsyncSshKeyInjection:
    """Test async SSH key injection into containers."""

    @pytest.mark.asyncio
    async def test_inject_ssh_key_uses_async_exec(self):
        """inject_ssh_key() runs one atomic exec, passing the key via env (#1616).

        The whole operation (mkdir/chmod/append-if-absent/chmod) is a single
        ``sh -c`` script; the public key flows in through the exec ``environment``
        and is NEVER interpolated into the command string.
        """
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mock_exec_result = Mock()
        mock_exec_result.exit_code = 0
        mock_exec_result.output = b""

        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = mock_exec_result

        key = "ssh-ed25519 AAAA... trinity-ephemeral-test"
        result = await service.inject_ssh_key("test-agent", key)

        assert result is True
        # One consolidated exec (was 3 separate calls pre-#1616).
        assert mocks['container_exec_run'].call_count == 1
        call = mocks['container_exec_run'].call_args
        # cmd is a LIST (bypasses docker-py's shlex.split — the injection layer).
        cmd = call.args[1] if len(call.args) > 1 else call.kwargs["cmd"]
        assert isinstance(cmd, list)
        assert cmd[:2] == ["sh", "-c"]
        # The key travels via environment, never baked into the script text.
        assert call.kwargs["environment"]["TRINITY_SSH_KEY"] == key
        assert key not in cmd[2]

    @pytest.mark.asyncio
    async def test_inject_ssh_key_returns_false_on_container_not_found(self):
        """inject_ssh_key() returns False when container doesn't exist."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mocks['get_agent_container'].return_value = None

        result = await service.inject_ssh_key("nonexistent-agent", "ssh-ed25519 AAAA...")

        assert result is False

    @pytest.mark.asyncio
    async def test_inject_ssh_key_returns_false_on_exec_failure(self):
        """inject_ssh_key() returns False when the exec script fails."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mocks['get_agent_container'].return_value = mock_container

        # The single consolidated exec fails (e.g. permission denied on chmod).
        mock_failure = Mock(exit_code=1, output=b"Permission denied")
        mocks['container_exec_run'].return_value = mock_failure

        result = await service.inject_ssh_key("test-agent", "ssh-ed25519 AAAA...")

        assert result is False

    @pytest.mark.asyncio
    async def test_inject_ssh_key_skip_if_present_toggles_grep_guard(self):
        """skip_if_present controls whether the script guards with grep (#1616)."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = Mock(exit_code=0, output=b"")

        # Default (True): append-if-absent — the script grep-guards.
        await service.inject_ssh_key("a", "ssh-ed25519 AAAA... c1")
        script_default = mocks['container_exec_run'].call_args.args[1][2]
        assert "grep -qxF" in script_default

        # Explicit False: always append — no grep guard.
        await service.inject_ssh_key("a", "ssh-ed25519 AAAA... c1", skip_if_present=False)
        script_always = mocks['container_exec_run'].call_args.args[1][2]
        assert "grep -qxF" not in script_always


@pytest.mark.unit
class TestAsyncPasswordManagement:
    """Legacy password-credential CLEANUP stays (clear_container_password) so any
    pre-#1615 password creds are still torn down — setting passwords is gone."""

    @pytest.mark.asyncio
    async def test_clear_container_password_uses_async_exec(self):
        """clear_container_password() uses async container_exec_run()."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mock_exec_result = Mock()
        mock_exec_result.exit_code = 0
        mock_exec_result.output = b""

        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = mock_exec_result

        result = await service.clear_container_password("test-agent")

        assert result is True
        mocks['container_exec_run'].assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_container_password_returns_true_when_container_missing(self):
        """clear_container_password() returns True when container doesn't exist (cleanup ok)."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mocks['get_agent_container'].return_value = None

        result = await service.clear_container_password("deleted-agent")

        # Should return True because container deletion means credential cleanup succeeded
        assert result is True


@pytest.mark.unit
class TestAsyncKeyRemoval:
    """Test async SSH key removal from containers."""

    @pytest.mark.asyncio
    async def test_remove_ssh_key_uses_async_exec(self):
        """remove_ssh_key() uses async container_exec_run()."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mock_exec_result = Mock()
        mock_exec_result.exit_code = 0
        mock_exec_result.output = b""

        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = mock_exec_result

        result = await service.remove_ssh_key(
            "test-agent",
            "trinity-ephemeral-test-agent-1234567890"
        )

        assert result is True
        mocks['container_exec_run'].assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_ssh_key_returns_true_when_container_missing(self):
        """remove_ssh_key() returns True when container doesn't exist (cleanup ok)."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mocks['get_agent_container'].return_value = None

        result = await service.remove_ssh_key("deleted-agent", "key-comment")

        assert result is True


@pytest.mark.unit
class TestAsyncCleanupOperations:
    """Test async credential cleanup operations."""

    @pytest.mark.asyncio
    async def test_cleanup_agent_credentials_removes_keys_and_passwords(self):
        """cleanup_agent_credentials() removes all credentials for an agent."""
        ssh_service, mocks = get_ssh_service()

        # Configure Redis mock. #1616: ssh_service iterates via scan_iter (KEYS is
        # blocked for the `-@dangerous` backend ACL user), so mock scan_iter.
        mocks['redis_client'].scan_iter.return_value = [
            "ssh_access:test-agent:key1",
            "ssh_access:test-agent:pwd1"
        ]
        mocks['redis_client'].get.side_effect = [
            json.dumps({"auth_type": "key", "credential_id": "key1"}),
            json.dumps({"auth_type": "password", "credential_id": "pwd1"})
        ]

        service = ssh_service.SshService()

        mock_container = Mock()
        mock_exec_result = Mock()
        mock_exec_result.exit_code = 0
        mock_exec_result.output = b""

        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = mock_exec_result

        count = await service.cleanup_agent_credentials("test-agent")

        assert count == 2
        # Should delete Redis keys
        assert mocks['redis_client'].delete.call_count == 2

    @pytest.mark.asyncio
    async def test_revoke_key_removes_from_container_and_redis(self):
        """revoke_key() removes key from container and Redis."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        mock_container = Mock()
        mock_exec_result = Mock()
        mock_exec_result.exit_code = 0
        mock_exec_result.output = b""

        mocks['get_agent_container'].return_value = mock_container
        mocks['container_exec_run'].return_value = mock_exec_result

        result = await service.revoke_key("test-agent", "key-comment")

        assert result is True
        mocks['redis_client'].delete.assert_called_once_with(
            "ssh_access:test-agent:key-comment"
        )


@pytest.mark.unit
class TestRedisMetadataStorage:
    """Test Redis metadata storage (synchronous)."""

    def test_store_credential_metadata_sets_redis_key_with_ttl(self):
        """store_credential_metadata() stores JSON with TTL in Redis."""
        ssh_service, mocks = get_ssh_service()
        service = ssh_service.SshService()

        service.store_credential_metadata(
            agent_name="test-agent",
            credential_id="key-123",
            auth_type="key",
            created_by="admin@example.com",
            ttl_hours=4.0,
            public_key="ssh-ed25519 AAAA..."
        )

        mocks['redis_client'].setex.assert_called_once()
        call_args = mocks['redis_client'].setex.call_args

        # Verify key format
        assert call_args[0][0] == "ssh_access:test-agent:key-123"

        # Verify TTL (4 hours = 14400 seconds)
        assert call_args[0][1] == 14400

        # Verify JSON content
        stored_data = json.loads(call_args[0][2])
        assert stored_data["agent_name"] == "test-agent"
        assert stored_data["auth_type"] == "key"
        assert stored_data["public_key"] == "ssh-ed25519 AAAA..."

    def test_list_active_keys_returns_all_credentials(self):
        """list_active_keys() returns all active credentials from Redis."""
        ssh_service, mocks = get_ssh_service()

        # #1616: scan_iter, not keys (KEYS blocked for the backend ACL user).
        mocks['redis_client'].scan_iter.return_value = [
            "ssh_access:agent1:key1",
            "ssh_access:agent2:key2"
        ]
        mocks['redis_client'].get.side_effect = [
            json.dumps({"agent_name": "agent1", "auth_type": "key"}),
            json.dumps({"agent_name": "agent2", "auth_type": "password"})
        ]

        service = ssh_service.SshService()
        keys = service.list_active_keys()

        assert len(keys) == 2
        assert keys[0]["agent_name"] == "agent1"
        assert keys[1]["agent_name"] == "agent2"
