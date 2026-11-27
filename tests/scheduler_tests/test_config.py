"""
Tests for scheduler configuration.
"""

import os
import pytest
from unittest.mock import patch

from scheduler.config import SchedulerConfig


_TEST_REDIS_URL = "redis://test:testpassword@localhost:6379"


class TestSchedulerConfig:
    """Tests for SchedulerConfig."""

    def test_default_values(self):
        """Test that default config values are set correctly."""
        with patch.dict(os.environ, {"REDIS_URL": _TEST_REDIS_URL}, clear=True):
            config = SchedulerConfig()

            assert config.database_path == "/data/trinity.db"
            assert config.redis_url == _TEST_REDIS_URL
            assert config.lock_timeout == 600
            assert config.lock_auto_renewal is True
            assert config.health_port == 8001
            assert config.default_timezone == "UTC"
            # #1022: dispatch + pre-check deadlines lifted from literals to config.
            assert config.dispatch_timeout == 30.0
            assert config.pre_check_timeout == 70.0

    def test_env_override(self):
        """Test that environment variables override defaults."""
        env = {
            "DATABASE_PATH": "/custom/path.db",
            "REDIS_URL": "redis://test:testpassword@custom:6380",
            "LOCK_TIMEOUT": "120",
            "LOCK_AUTO_RENEWAL": "false",
            "HEALTH_PORT": "9000",
            "LOG_LEVEL": "DEBUG"
        }
        with patch.dict(os.environ, env, clear=True):
            config = SchedulerConfig.from_env()

            assert config.database_path == "/custom/path.db"
            assert config.redis_url == "redis://test:testpassword@custom:6380"
            assert config.lock_timeout == 120
            assert config.lock_auto_renewal is False
            assert config.health_port == 9000
            assert config.log_level == "DEBUG"

    def test_agent_timeout(self):
        """Test agent timeout configuration."""
        with patch.dict(os.environ, {"REDIS_URL": _TEST_REDIS_URL, "AGENT_TIMEOUT": "1800"}, clear=True):
            config = SchedulerConfig()
            assert config.agent_timeout == 1800.0

    def test_dispatch_timeout_override(self):
        """#1022: DISPATCH_TIMEOUT overrides the dispatch deadline (float)."""
        with patch.dict(os.environ, {"REDIS_URL": _TEST_REDIS_URL, "DISPATCH_TIMEOUT": "12.5"}, clear=True):
            config = SchedulerConfig()
            assert config.dispatch_timeout == 12.5

    def test_pre_check_timeout_override(self):
        """#1022: PRE_CHECK_TIMEOUT overrides the pre-check deadline (float)."""
        with patch.dict(os.environ, {"REDIS_URL": _TEST_REDIS_URL, "PRE_CHECK_TIMEOUT": "90"}, clear=True):
            config = SchedulerConfig()
            assert config.pre_check_timeout == 90.0
