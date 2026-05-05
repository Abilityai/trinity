"""
Scheduler service configuration.

All configuration is loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


def _require_redis_url() -> str:
    """Read REDIS_URL and fail fast if it lacks credentials (Issue #589)."""
    url = os.getenv("REDIS_URL", "")
    parsed = urlparse(url) if url else None
    if not url or not parsed or not parsed.username or not parsed.password:
        raise RuntimeError(
            "REDIS_URL must include credentials (redis://user:password@host:port). "
            "Generate passwords with: openssl rand -hex 24. "
            "See docs/migrations/REDIS_AUTH.md for details."
        )
    return url


@dataclass
class SchedulerConfig:
    """Configuration for the scheduler service."""

    # Database
    database_path: str = field(default_factory=lambda: os.getenv(
        "DATABASE_PATH", "/data/trinity.db"
    ))

    # Redis — must embed credentials; no fallback default (Issue #589)
    redis_url: str = field(default_factory=_require_redis_url)

    # Lock settings
    lock_timeout: int = field(default_factory=lambda: int(os.getenv(
        "LOCK_TIMEOUT", "600"
    )))  # 10 minutes default
    lock_auto_renewal: bool = field(default_factory=lambda: os.getenv(
        "LOCK_AUTO_RENEWAL", "true"
    ).lower() == "true")

    # Health server
    health_port: int = field(default_factory=lambda: int(os.getenv(
        "HEALTH_PORT", "8001"
    )))
    health_host: str = field(default_factory=lambda: os.getenv(
        "HEALTH_HOST", "0.0.0.0"
    ))

    # Scheduler settings
    default_timezone: str = field(default_factory=lambda: os.getenv(
        "DEFAULT_TIMEZONE", "UTC"
    ))
    schedule_reload_interval: int = field(default_factory=lambda: int(os.getenv(
        "SCHEDULE_RELOAD_INTERVAL", "60"
    )))  # seconds

    # Agent communication
    agent_timeout: float = field(default_factory=lambda: float(os.getenv(
        "AGENT_TIMEOUT", "900"
    )))  # 15 minutes

    # Polling interval for async task completion (SCHED-ASYNC-001)
    poll_interval: int = field(default_factory=lambda: int(os.getenv(
        "POLL_INTERVAL", "10"
    )))  # seconds between DB polls while waiting for task completion

    # Grace seconds added to task timeout for slot acquisition + finalize.
    # Extracted from a literal so tests can override via config patching (#415).
    poll_deadline_buffer: float = field(default_factory=lambda: float(os.getenv(
        "POLL_DEADLINE_BUFFER", "60"
    )))

    # Misfire grace time — how long after a missed trigger APScheduler will
    # still execute the job.  Default 30s is far too low for weekly cron jobs
    # whose container may restart.  3600s (1 hour) gives ample runway.
    misfire_grace_time: int = field(default_factory=lambda: int(os.getenv(
        "MISFIRE_GRACE_TIME", "3600"
    )))  # seconds (Issue #145)

    # Backend API (for process executions)
    backend_url: str = field(default_factory=lambda: os.getenv(
        "BACKEND_URL", "http://backend:8000"
    ))

    # Internal API shared secret (C-003) - must match backend's INTERNAL_API_SECRET or SECRET_KEY
    internal_api_secret: str = field(default_factory=lambda: os.getenv(
        "INTERNAL_API_SECRET", ""
    ))

    # Logging
    log_level: str = field(default_factory=lambda: os.getenv(
        "LOG_LEVEL", "INFO"
    ))

    # Event publishing (for WebSocket compatibility)
    publish_events: bool = field(default_factory=lambda: os.getenv(
        "PUBLISH_EVENTS", "true"
    ).lower() == "true")

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        """Create config from environment variables."""
        return cls()


# Global config instance
config = SchedulerConfig.from_env()
