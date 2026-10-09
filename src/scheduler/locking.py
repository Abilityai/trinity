"""
Distributed locking for the scheduler service.

Uses Redis locks to prevent duplicate schedule executions
across multiple scheduler instances (or workers).
"""

import logging
import threading
import time
from contextlib import contextmanager
from typing import Optional

import redis

from .config import config

logger = logging.getLogger(__name__)


class DistributedLock:
    """
    A distributed lock using Redis.

    Supports:
    - Non-blocking acquire
    - Automatic expiration
    - Optional auto-renewal for long-running tasks
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        name: str,
        timeout: int = None,
        auto_renewal: bool = None
    ):
        """
        Initialize a distributed lock.

        Args:
            redis_client: Redis client instance
            name: Lock name (will be prefixed with 'scheduler:lock:')
            timeout: Lock timeout in seconds (default from config)
            auto_renewal: Whether to auto-renew the lock (default from config)
        """
        self.redis = redis_client
        self.name = f"scheduler:lock:{name}"
        self.timeout = timeout or config.lock_timeout
        self.auto_renewal = auto_renewal if auto_renewal is not None else config.lock_auto_renewal
        self._renewal_thread: Optional[threading.Thread] = None
        self._stop_renewal = threading.Event()
        self._lock_token: Optional[str] = None

    def acquire(self, blocking: bool = False, blocking_timeout: float = None) -> bool:
        """
        Attempt to acquire the lock.

        Args:
            blocking: If True, wait for lock to become available
            blocking_timeout: Max time to wait if blocking

        Returns:
            True if lock was acquired, False otherwise
        """
        import secrets
        self._lock_token = secrets.token_hex(16)

        if blocking:
            end_time = time.time() + (blocking_timeout or self.timeout)
            while time.time() < end_time:
                if self._try_acquire():
                    self._start_renewal()
                    return True
                time.sleep(0.1)
            return False
        else:
            if self._try_acquire():
                self._start_renewal()
                return True
            return False

    def _try_acquire(self) -> bool:
        """Try to acquire the lock once."""
        return self.redis.set(
            self.name,
            self._lock_token,
            nx=True,
            ex=self.timeout
        )

    def release(self) -> bool:
        """
        Release the lock.

        Only releases if we own the lock (based on token).

        Returns:
            True if lock was released, False if we didn't own it
        """
        self._stop_renewal.set()
        if self._renewal_thread:
            self._renewal_thread.join(timeout=1)
            self._renewal_thread = None

        # Use Lua script for atomic check-and-delete
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = self.redis.eval(script, 1, self.name, self._lock_token)
        return bool(result)

    def _start_renewal(self):
        """Start the auto-renewal thread if enabled."""
        if not self.auto_renewal:
            return

        self._stop_renewal.clear()
        self._renewal_thread = threading.Thread(
            target=self._renewal_loop,
            daemon=True
        )
        self._renewal_thread.start()

    def _renewal_loop(self):
        """Background thread that renews the lock."""
        # Renew at 50% of timeout to be safe
        renewal_interval = self.timeout / 2

        while not self._stop_renewal.wait(renewal_interval):
            try:
                # Only extend if we still own the lock
                script = """
                if redis.call("get", KEYS[1]) == ARGV[1] then
                    return redis.call("expire", KEYS[1], ARGV[2])
                else
                    return 0
                end
                """
                result = self.redis.eval(
                    script, 1, self.name, self._lock_token, self.timeout
                )
                if not result:
                    logger.warning(f"Lock {self.name} renewal failed - lock lost")
                    break
                else:
                    logger.debug(f"Lock {self.name} renewed for {self.timeout}s")
            except Exception as e:
                logger.error(f"Error renewing lock {self.name}: {e}")
                break

    @contextmanager
    def __call__(self, blocking: bool = False):
        """Context manager interface for the lock."""
        acquired = self.acquire(blocking=blocking)
        if not acquired:
            raise LockNotAcquiredError(f"Could not acquire lock: {self.name}")
        try:
            yield
        finally:
            self.release()


class LockNotAcquiredError(Exception):
    """Raised when a lock cannot be acquired."""
    pass


class LockManager:
    """
    Manages distributed locks for the scheduler.

    Provides a high-level interface for acquiring execution locks.
    """

    def __init__(self, redis_url: str = None):
        """
        Initialize the lock manager.

        Args:
            redis_url: Redis connection URL (default from config)
        """
        self.redis_url = redis_url or config.redis_url
        self._redis: Optional[redis.Redis] = None

    @property
    def redis(self) -> redis.Redis:
        """Get or create Redis connection."""
        if self._redis is None:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    def get_schedule_lock(
        self,
        schedule_id: str,
        timeout: int = None,
        auto_renewal: bool = None
    ) -> DistributedLock:
        """
        Get a lock for a specific schedule execution.

        Args:
            schedule_id: The schedule ID to lock
            timeout: Lock timeout in seconds
            auto_renewal: Whether to auto-renew

        Returns:
            DistributedLock instance
        """
        return DistributedLock(
            redis_client=self.redis,
            name=f"schedule:{schedule_id}",
            timeout=timeout,
            auto_renewal=auto_renewal
        )

    def try_acquire_schedule_lock(self, schedule_id: str) -> Optional[DistributedLock]:
        """
        Try to acquire a lock for a schedule, returning the lock if successful.

        This is a convenience method for the common pattern:
        1. Try to acquire lock
        2. If successful, return lock for caller to manage
        3. If failed, return None

        Args:
            schedule_id: The schedule ID to lock

        Returns:
            DistributedLock if acquired, None otherwise
        """
        lock = self.get_schedule_lock(schedule_id)
        if lock.acquire(blocking=False):
            return lock
        return None

    def set_heartbeat(self, instance_id: str, ttl: int = 60) -> bool:
        """
        Set a heartbeat for this scheduler instance.

        Args:
            instance_id: Unique identifier for this scheduler
            ttl: Time-to-live in seconds

        Returns:
            True if successful
        """
        return self.redis.setex(
            f"scheduler:heartbeat:{instance_id}",
            ttl,
            "alive"
        )

    def close(self):
        """Close the Redis connection."""
        if self._redis:
            self._redis.close()
            self._redis = None


# Global lock manager instance
_lock_manager: Optional[LockManager] = None


def get_lock_manager() -> LockManager:
    """Get or create the global lock manager."""
    global _lock_manager
    if _lock_manager is None:
        _lock_manager = LockManager()
    return _lock_manager
