"""
Pytest fixtures for scheduler tests.

Provides mock databases, Redis clients, and test data.

NOTE: Path setup happens at module load time to ensure scheduler
is importable before any test collection.
"""

# Path setup MUST happen before any other imports
import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
_src_path = str(_project_root / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import os
import sqlite3
import tempfile
from datetime import datetime
from typing import Generator
from unittest.mock import MagicMock, AsyncMock

import pytest


def pytest_configure(config):
    """Ensure src is in path before test collection."""
    if _src_path not in sys.path:
        sys.path.insert(0, _src_path)


# ============================================================================
# Override parent conftest fixtures
# These prevent the scheduler tests from trying to connect to a real backend
# ============================================================================

@pytest.fixture(scope="session")
def api_config():
    """Override - scheduler tests don't need API config."""
    pytest.skip("Scheduler tests don't use Trinity API")


@pytest.fixture(scope="session")
def api_client():
    """Override - scheduler tests don't need API client."""
    pytest.skip("Scheduler tests don't use Trinity API")


@pytest.fixture(scope="session")
def unauthenticated_client():
    """Override - scheduler tests don't need unauthenticated client."""
    pytest.skip("Scheduler tests don't use Trinity API")


@pytest.fixture(scope="function")
def resource_tracker():
    """Override - scheduler tests don't need resource tracker."""
    return None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override - scheduler tests don't need cleanup."""
    yield


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture
def temp_db_path() -> Generator[str, None, None]:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    yield db_path

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def initialized_db(temp_db_path: str) -> Generator[str, None, None]:
    """Create a temporary database with schema initialized."""
    conn = sqlite3.connect(temp_db_path)
    cursor = conn.cursor()

    # Create necessary tables (MODEL-001: Added model column, RETRY-001: Added retry columns)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            timezone TEXT DEFAULT 'UTC',
            description TEXT,
            owner_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT,
            model TEXT,
            max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60
        )
    """)

    # MODEL-001: Added model_used column, RETRY-001: Added retry tracking columns
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL,
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL,
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            tool_calls TEXT,
            execution_log TEXT,
            claude_session_id TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT,
            model_used TEXT,
            skip_reason TEXT,
            attempt_number INTEGER DEFAULT 1,
            retry_of_execution_id TEXT,
            retry_scheduled_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_ownership (
            agent_name TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            autonomy_enabled INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

    yield temp_db_path


@pytest.fixture
def db(initialized_db: str):
    """Create a SchedulerDatabase instance with test database."""
    from scheduler.database import SchedulerDatabase
    return SchedulerDatabase(database_path=initialized_db)


@pytest.fixture
def db_with_data(initialized_db: str):
    """Create a SchedulerDatabase with sample test data."""
    from scheduler.database import SchedulerDatabase

    conn = sqlite3.connect(initialized_db)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Add test agent ownership with autonomy enabled
    cursor.execute("""
        INSERT INTO agent_ownership (agent_name, owner_id, autonomy_enabled, created_at)
        VALUES ('test-agent', 1, 1, ?)
    """, (now,))

    # Add test schedules
    cursor.execute("""
        INSERT INTO agent_schedules (
            id, agent_name, name, cron_expression, message, enabled,
            timezone, description, owner_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "schedule-1", "test-agent", "Morning Task",
        "0 9 * * *", "Run morning report", 1,
        "UTC", "Daily morning task", 1, now, now
    ))

    cursor.execute("""
        INSERT INTO agent_schedules (
            id, agent_name, name, cron_expression, message, enabled,
            timezone, description, owner_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "schedule-2", "test-agent", "Hourly Check",
        "0 * * * *", "Check status", 1,
        "America/New_York", "Hourly status check", 1, now, now
    ))

    cursor.execute("""
        INSERT INTO agent_schedules (
            id, agent_name, name, cron_expression, message, enabled,
            timezone, description, owner_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "schedule-3", "test-agent", "Disabled Task",
        "0 0 * * *", "Disabled task", 0,
        "UTC", "This is disabled", 1, now, now
    ))

    conn.commit()
    conn.close()

    return SchedulerDatabase(database_path=initialized_db)


# ============================================================================
# Redis/Lock Fixtures
# ============================================================================

@pytest.fixture
def mock_redis() -> MagicMock:
    """Create a mock Redis client."""
    redis_mock = MagicMock()
    redis_mock.set.return_value = True
    redis_mock.get.return_value = None
    redis_mock.delete.return_value = 1
    redis_mock.exists.return_value = 0
    redis_mock.setex.return_value = True
    redis_mock.eval.return_value = 1
    redis_mock.publish.return_value = 1
    return redis_mock


@pytest.fixture
def mock_lock_manager(mock_redis: MagicMock):
    """Create a LockManager with mock Redis."""
    from scheduler.locking import LockManager
    manager = LockManager()
    manager._redis = mock_redis
    return manager


# ============================================================================
# Model Fixtures
# ============================================================================

@pytest.fixture
def sample_schedule():
    """Create a sample Schedule for testing."""
    from scheduler.models import Schedule
    now = datetime.utcnow()
    return Schedule(
        id="test-schedule-1",
        agent_name="test-agent",
        name="Test Schedule",
        cron_expression="*/15 * * * *",
        message="Run test task",
        enabled=True,
        timezone="UTC",
        description="A test schedule",
        owner_id=1,
        created_at=now,
        updated_at=now
    )


@pytest.fixture
def sample_execution():
    """Create a sample ScheduleExecution for testing."""
    from scheduler.models import ScheduleExecution
    now = datetime.utcnow()
    return ScheduleExecution(
        id="exec-1",
        schedule_id="test-schedule-1",
        agent_name="test-agent",
        status="running",
        started_at=now,
        message="Run test task",
        triggered_by="schedule"
    )


# ============================================================================
# Config Fixtures
# ============================================================================

@pytest.fixture
def test_config(initialized_db: str):
    """Create a test configuration."""
    from scheduler.config import SchedulerConfig
    return SchedulerConfig(
        database_path=initialized_db,
        redis_url="redis://test:test@localhost:6379",
        lock_timeout=60,
        lock_auto_renewal=False,
        health_port=8099,
        log_level="DEBUG"
    )
