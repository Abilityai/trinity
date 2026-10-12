"""
Tests for distributed locking.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)
import os

import time
from unittest.mock import MagicMock, patch
import pytest

from scheduler.locking import (
    DistributedLock,
    LockManager,
    LockNotAcquiredError,
    get_lock_manager
)


class TestDistributedLock:
    """Tests for DistributedLock."""

    def test_acquire_success(self, mock_redis: MagicMock):
        """Test successful lock acquisition."""
        mock_redis.set.return_value = True

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)
        acquired = lock.acquire(blocking=False)

        assert acquired is True
        mock_redis.set.assert_called_once()
        # Verify nx=True and ex=timeout were passed
        call_kwargs = mock_redis.set.call_args[1]
        assert call_kwargs["nx"] is True
        assert call_kwargs["ex"] == 60

    def test_acquire_failure(self, mock_redis: MagicMock):
        """Test failed lock acquisition (lock already held)."""
        mock_redis.set.return_value = False

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)
        acquired = lock.acquire(blocking=False)

        assert acquired is False

    def test_release(self, mock_redis: MagicMock):
        """Test lock release."""
        mock_redis.set.return_value = True
        mock_redis.eval.return_value = 1

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)
        lock.acquire(blocking=False)
        released = lock.release()

        assert released is True
        mock_redis.eval.assert_called_once()

    def test_release_not_owned(self, mock_redis: MagicMock):
        """Test release when lock not owned."""
        mock_redis.set.return_value = True
        mock_redis.eval.return_value = 0  # Lock not owned

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)
        lock.acquire(blocking=False)
        released = lock.release()

        assert released is False

    def test_context_manager_success(self, mock_redis: MagicMock):
        """Test using lock as context manager."""
        mock_redis.set.return_value = True
        mock_redis.eval.return_value = 1

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)

        executed = False
        with lock(blocking=False):
            executed = True

        assert executed is True
        mock_redis.eval.assert_called_once()  # Released

    def test_context_manager_failure(self, mock_redis: MagicMock):
        """Test context manager when lock cannot be acquired."""
        mock_redis.set.return_value = False

        lock = DistributedLock(mock_redis, "test-lock", timeout=60, auto_renewal=False)

        with pytest.raises(LockNotAcquiredError):
            with lock(blocking=False):
                pass

    def test_lock_name_prefixed(self, mock_redis: MagicMock):
        """Test that lock names are properly prefixed."""
        lock = DistributedLock(mock_redis, "my-lock", timeout=60, auto_renewal=False)
        assert lock.name == "scheduler:lock:my-lock"


class TestLockManager:
    """Tests for LockManager."""

    def test_get_schedule_lock(self, mock_lock_manager: LockManager):
        """Test getting a lock for a schedule."""
        lock = mock_lock_manager.get_schedule_lock("schedule-123")

        assert lock is not None
        assert "schedule:schedule-123" in lock.name

    def test_try_acquire_schedule_lock_success(self, mock_lock_manager: LockManager):
        """Test successful lock acquisition."""
        mock_lock_manager.redis.set.return_value = True

        lock = mock_lock_manager.try_acquire_schedule_lock("schedule-123")

        assert lock is not None

    def test_try_acquire_schedule_lock_failure(self, mock_lock_manager: LockManager):
        """Test failed lock acquisition."""
        mock_lock_manager.redis.set.return_value = False

        lock = mock_lock_manager.try_acquire_schedule_lock("schedule-123")

        assert lock is None

    def test_set_heartbeat(self, mock_lock_manager: LockManager):
        """Test setting scheduler heartbeat."""
        result = mock_lock_manager.set_heartbeat("scheduler-1", ttl=60)

        assert result is True
        mock_lock_manager.redis.setex.assert_called_once_with(
            "scheduler:heartbeat:scheduler-1",
            60,
            "alive"
        )

    def test_close(self, mock_lock_manager: LockManager):
        """Test closing the lock manager."""
        # Get reference to mock redis before close() sets _redis to None
        mock_redis = mock_lock_manager._redis

        mock_lock_manager.close()

        mock_redis.close.assert_called_once()
        assert mock_lock_manager._redis is None


class TestDuplicateExecutionPrevention:
    """Tests specifically for duplicate execution prevention."""

    def test_only_one_acquires_lock(self, mock_redis: MagicMock):
        """Test that only one caller can acquire the lock."""
        # First call succeeds, subsequent calls fail
        mock_redis.set.side_effect = [True, False, False]

        lock1 = DistributedLock(mock_redis, "schedule-1", timeout=60, auto_renewal=False)
        lock2 = DistributedLock(mock_redis, "schedule-1", timeout=60, auto_renewal=False)
        lock3 = DistributedLock(mock_redis, "schedule-1", timeout=60, auto_renewal=False)

        result1 = lock1.acquire(blocking=False)
        result2 = lock2.acquire(blocking=False)
        result3 = lock3.acquire(blocking=False)

        assert result1 is True
        assert result2 is False
        assert result3 is False

    def test_lock_released_after_execution(self, mock_redis: MagicMock):
        """Test that lock is released after execution."""
        mock_redis.set.return_value = True
        mock_redis.eval.return_value = 1

        lock = DistributedLock(mock_redis, "schedule-1", timeout=60, auto_renewal=False)

        with lock(blocking=False):
            pass  # Execution happens here

        # Lock should be released
        mock_redis.eval.assert_called()  # Release script was called
