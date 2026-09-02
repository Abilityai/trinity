# mcp: none — browser-side product-event capture (ent#184)
"""
Local product-event capture — activation funnel, Tier-1 (ent#184).

The OSS **capture** half of the two-tier telemetry model: records
activation/usage events **on the operator's own instance, default-ON, with zero
network egress**. This endpoint writes exactly ONE local row per accepted event
and returns — it never phones home. All sharing/consent lives in Tier-2 (#12).

Only the onboarding-wizard step transitions are emitted here (the genuinely-new
client beacons); first-value events (first_agent_created, first_chat, ...) are
derived on read from ``audit_log``/``agent_activities`` by the enterprise funnel
view and are NOT accepted here.

``event_type`` is checked against a fixed allow-list so the local table can't be
spammed with arbitrary strings. The operator-facing funnel aggregation is an
entitlement-gated enterprise surface that reads these rows (open-core split).
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from database import db
from dependencies import get_current_user
from models import ProductEventCreate, User
from services.operator_intake_service import get_or_create_installation_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/product-events", tags=["product-events"])

# Fixed allow-list of client-emitted event types (ent#184). Keep in lockstep
# with stores/productTelemetry.js on the frontend. First-value events are
# DERIVED (enterprise funnel), never emitted, so they are intentionally absent.
ALLOWED_EVENT_TYPES = frozenset({
    "setup_started",
    "setup_step_intro",
    "setup_step_create",
    "setup_step_credential",
    "setup_completed",
    "setup_dismissed",
})

# Small cap on the optional context blob — this is a beacon, not a data channel.
_MAX_CONTEXT_BYTES = 2048


@router.post("")
async def record_product_event(
    body: ProductEventCreate,
    current_user: User = Depends(get_current_user),
):
    """Record one local product event (ent#184). Local-only, zero egress.

    422 on an unknown ``event_type`` (allow-list) or an over-cap ``context``.
    Any authenticated user may emit (the onboarding wizard runs for the operator
    completing first-run).
    """
    event_type = body.event_type
    if event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown event_type '{event_type}'",
        )

    context = body.context
    if context is not None:
        try:
            if len(json.dumps(context).encode("utf-8")) > _MAX_CONTEXT_BYTES:
                raise HTTPException(status_code=422, detail="context too large")
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="context must be JSON-serializable")

    installation_id = get_or_create_installation_id()
    db.record_product_event(installation_id, event_type, context)
    return {"recorded": True, "event_type": event_type}
