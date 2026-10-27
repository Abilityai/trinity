"""The operator-queue service's collaborators, bound once (#3785, O2).

Every module of the package reads them as `deps.X`, and none of them is bound
on the package itself. One binding means one patch reaches every reader, in
every module a code path crosses, and a stale
`monkeypatch.setattr(oqs, "db", …)` raises `AttributeError` instead of
patching a copy nobody reads (Invariant #1(a), the PR #2487 rule). Patch
`oqs.deps.X`.

This module reads no package name, so `__init__` imports it before anything
else of its own.
"""
# ruff: noqa: F401 — a binding module: every import here IS the binding the package reads.

import logging

from database import db
from db import operator_queue as _queue_db
from dependencies import principal_reach
from redis_breaker_util import get_breaker_redis
from services import ask_service
from services.agent_client import AgentClient

logger = logging.getLogger("services.operator_queue_service")


async def _audit_sync(action: str, agent_name: str, item_id: str, details: dict) -> None:
    """Accountability transitions only (ingested · diverged · reconciled ·
    written_back · undeliverable) — ids and enums, never agent text; best-effort."""
    try:
        from services.platform_audit_service import platform_audit_service, AuditEventType
        await platform_audit_service.log(
            event_type=AuditEventType.OPERATOR_QUEUE,
            event_action=action,
            source="system",
            target_type="operator_queue",
            target_id=str(item_id),
            details={"agent_name": agent_name, **details},
        )
    except Exception as e:  # noqa: BLE001 — audit never breaks the sync
        logger.debug("[OperatorQueue] audit %s for %s skipped: %s", action, item_id, e)
