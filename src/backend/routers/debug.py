"""
Debug / observability endpoints.

Admin-only. Cheap to expose — exposes in-memory counters only, no DB queries.
Primary use case: #306 Redis Streams event bus soak dashboard (gates defined in
``docs/archive/plans/ORCHESTRATION_RELIABILITY_2026-04.md``).

Endpoints:
    GET /api/debug/event-bus-stats — publisher/consumer counters + cumulative
        watchdog orphan/auto-terminate totals since last process restart.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from dependencies import require_admin
from models import User
from services.cleanup_service import cleanup_service
from services.event_bus import event_bus, stream_dispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/event-bus-stats")
async def event_bus_stats(_: User = Depends(require_admin)) -> Dict[str, Any]:
    """Return in-memory counters for the #306 soak dashboard.

    All counters are monotonic and reset on backend restart — compare deltas
    across snapshots, not absolute values.
    """
    return {
        "publisher": event_bus.stats(),
        "dispatcher": stream_dispatcher.stats(),
        "watchdog": {
            "cumulative_orphaned": cleanup_service.cumulative_orphaned,
            "cumulative_auto_terminated": cleanup_service.cumulative_auto_terminated,
            "last_run_at": cleanup_service.last_run_at,
        },
    }
