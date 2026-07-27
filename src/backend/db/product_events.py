"""
Local product-event capture database operations (ent#184).

Tier-1 of the two-tier telemetry model: activation/usage events recorded **on
the operator's own instance, default-ON, with zero network egress**. Only the
onboarding-wizard step transitions are emitted here (the genuinely-new client
beacons); first-value events (first_agent_created, first_chat, ...) are derived
on read from ``audit_log``/``agent_activities`` and never re-emitted.

SQLAlchemy Core over the ``product_events`` table in ``db/tables.py`` so it runs
unchanged on SQLite and PostgreSQL. This layer holds the WRITE path + minimal
read helpers; the operator-facing funnel aggregation is an entitlement-gated
enterprise surface that reads these rows (open-core split, ent#184).
"""

import json
from typing import Dict, List, Optional

from sqlalchemy import select, insert, delete, func

from .engine import get_engine
from .tables import product_events
from utils.helpers import utc_now_iso, iso_cutoff


class ProductEventOperations:
    """Local product-event capture operations (ent#184)."""

    def record_product_event(
        self,
        installation_id: str,
        event_type: str,
        event_context: Optional[Dict] = None,
    ) -> Dict:
        """Insert one local product event. Zero egress — one local row.

        ``event_context`` is an optional small dict serialized to JSON. The
        caller (router) is responsible for allow-listing ``event_type``; this
        layer just persists.
        """
        now = utc_now_iso()
        ctx = json.dumps(event_context) if event_context else None
        stmt = insert(product_events).values(
            installation_id=installation_id,
            event_type=event_type,
            event_context=ctx,
            created_at=now,
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
        new_id = result.inserted_primary_key[0] if result.inserted_primary_key else None
        return {
            "id": new_id,
            "installation_id": installation_id,
            "event_type": event_type,
            "event_context": event_context,
            "created_at": now,
        }

    def count_product_events_by_type(self, since: Optional[str] = None) -> Dict[str, int]:
        """Counts grouped by ``event_type`` (optionally since an ISO cutoff).

        The primitive the enterprise activation-funnel view aggregates over the
        wizard-step slice. Returns ``{event_type: count}``.
        """
        stmt = select(product_events.c.event_type, func.count().label("n"))
        if since:
            stmt = stmt.where(product_events.c.created_at >= since)
        stmt = stmt.group_by(product_events.c.event_type)
        with get_engine().connect() as conn:
            return {r["event_type"]: r["n"] for r in conn.execute(stmt).mappings()}

    def list_product_events(
        self,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> List[Dict]:
        """Raw rows (oldest first) for Tier-2 backfill serialization + audit.

        Ordered by ``created_at`` ASC so a later opt-in (#12) can serialize
        history in chronological order.
        """
        stmt = select(product_events)
        conditions = []
        if event_type:
            conditions.append(product_events.c.event_type == event_type)
        if since:
            conditions.append(product_events.c.created_at >= since)
        if conditions:
            for c in conditions:
                stmt = stmt.where(c)
        stmt = (
            stmt.order_by(product_events.c.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            {
                "id": r["id"],
                "installation_id": r["installation_id"],
                "event_type": r["event_type"],
                "event_context": json.loads(r["event_context"]) if r["event_context"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def prune_product_events(self, retention_days: int, chunk_size: int = 1000) -> int:
        """Delete product events older than ``retention_days``. ``0`` disables.

        Provided for completeness; not wired into an automatic sweep in v1 (the
        table is negligible — a handful of rows per install). Chunked so a large
        table never holds the write lock for the full purge.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        while True:
            with get_engine().begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(product_events.c.id)
                        .where(product_events.c.created_at < cutoff)
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(product_events).where(product_events.c.id.in_(ids))
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break
        return total
