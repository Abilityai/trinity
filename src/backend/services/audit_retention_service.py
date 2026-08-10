"""
Audit Log Retention Service.

Daily APScheduler job that prunes ``audit_log`` rows past the retention
window. Closes the gap left after #20 / Phase 4 of
``docs/requirements/AUDIT_TRAIL_ARCHITECTURE.md``: the append-only
contract is enforced by SQLite triggers, but nothing was deleting old
entries.

Configuration (env vars):

- ``AUDIT_LOG_RETENTION_DAYS`` (default ``365``) — minimum age before a
  row is eligible for deletion. Floored at 365 because the
  ``audit_log_no_delete`` trigger refuses younger rows.
- ``AUDIT_RETENTION_ENABLED`` (default ``true``) — set to ``false`` to
  disable the daily prune.
- ``AUDIT_RETENTION_HOUR`` (default ``4``) — UTC hour to run. Defaults
  to one hour after log archival to spread the nightly DB writes.

Hash chain note: pruning DELETEs entries, which breaks the SHA-256
``previous_hash``/``entry_hash`` chain across the cutoff. Verification
via ``POST /api/audit-log/verify`` should be scoped to ranges *within*
the retention window. This is documented behavior — pruning ages out
unverifiable history rather than maintaining a chain over deleted rows.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import db
from services.platform_audit_service import platform_audit_service

logger = logging.getLogger(__name__)

# Trigger floor — see ``audit_log_no_delete`` in db/schema.py
_RETENTION_FLOOR_DAYS = 365

AUDIT_LOG_RETENTION_DAYS = max(
    int(os.getenv("AUDIT_LOG_RETENTION_DAYS", str(_RETENTION_FLOOR_DAYS))),
    _RETENTION_FLOOR_DAYS,
)
AUDIT_RETENTION_ENABLED = os.getenv("AUDIT_RETENTION_ENABLED", "true").lower() == "true"
AUDIT_RETENTION_HOUR = int(os.getenv("AUDIT_RETENTION_HOUR", "4"))


class AuditRetentionService:
    """Daily prune of expired audit_log rows."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        if not AUDIT_RETENTION_ENABLED:
            logger.info("Audit retention disabled (AUDIT_RETENTION_ENABLED=false)")
            return

        self.scheduler.add_job(
            self.prune,
            CronTrigger(hour=AUDIT_RETENTION_HOUR, minute=15),
            id="audit_log_retention",
            name="Daily audit_log retention prune",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        logger.info(
            "Audit retention scheduler started: daily at %02d:15 UTC (retention=%dd)",
            AUDIT_RETENTION_HOUR,
            AUDIT_LOG_RETENTION_DAYS,
        )

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Audit retention scheduler stopped")

    async def prune(self) -> Dict[str, Any]:
        """Run a single prune cycle. Returns summary for tests/manual triggers."""
        retention_days = AUDIT_LOG_RETENTION_DAYS
        try:
            removed = db.prune_audit_log(retention_days)
        except Exception as exc:
            logger.exception("audit_log prune failed: %s", exc)
            return {"removed": 0, "retention_days": retention_days, "error": str(exc)}

        # `hash_chain_enabled`, not the old private attribute (#2015): the flag
        # moved to `system_settings`, and a `getattr(..., False)` default would
        # have degraded silently to "never warn" the moment it did.
        if platform_audit_service.hash_chain_enabled and removed:
            logger.warning(
                "audit_log prune removed %d rows while hash chain is enabled — "
                "verification ranges spanning the cutoff will fail by design",
                removed,
            )

        logger.info(
            "audit_log prune complete: removed=%d retention_days=%d",
            removed,
            retention_days,
        )
        return {"removed": removed, "retention_days": retention_days}


audit_retention_service = AuditRetentionService()
