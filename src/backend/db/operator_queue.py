"""
Operator queue database operations (OPS-001).

Persists operator queue items synced from agent JSON files.
Supports listing, filtering, responding, and statistics.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300): runs unchanged on both SQLite and PostgreSQL. Queries are built
from the ``operator_queue`` table in ``db/tables.py`` (dialect-agnostic
expressions, no ``?`` placeholders, no ``datetime('now')``/``julianday`` —
time math is done in Python). The public API of ``OperatorQueueOperations`` is
unchanged.
"""

import json
from typing import Optional, List, Dict, Set
from datetime import datetime

from sqlalchemy import select, update, func, and_, case

from .engine import get_engine, make_insert
from .tables import operator_queue
from utils.helpers import utc_now_iso, iso_cutoff


class OperatorQueueOperations:
    """Database operations for the operator queue."""

    @staticmethod
    def _row_to_item(row) -> Dict:
        """Convert a database row (RowMapping) to a queue item dict."""
        return {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "type": row["type"],
            "status": row["status"],
            "priority": row["priority"],
            "title": row["title"],
            "question": row["question"],
            "options": json.loads(row["options"]) if row["options"] else None,
            "context": json.loads(row["context"]) if row["context"] else None,
            "execution_id": row["execution_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "response": row["response"],
            "response_text": row["response_text"],
            "responded_by_id": row["responded_by_id"],
            "responded_by_email": row["responded_by_email"],
            "responded_at": row["responded_at"],
            "acknowledged_at": row["acknowledged_at"],
            "cleared_at": row["cleared_at"],  # #1017
        }

    # Columns selected for a full queue-item record, in the canonical order.
    _SELECT_COLS = (
        operator_queue.c.id,
        operator_queue.c.agent_name,
        operator_queue.c.type,
        operator_queue.c.status,
        operator_queue.c.priority,
        operator_queue.c.title,
        operator_queue.c.question,
        operator_queue.c.options,
        operator_queue.c.context,
        operator_queue.c.execution_id,
        operator_queue.c.created_at,
        operator_queue.c.expires_at,
        operator_queue.c.response,
        operator_queue.c.response_text,
        operator_queue.c.responded_by_id,
        operator_queue.c.responded_by_email,
        operator_queue.c.responded_at,
        operator_queue.c.acknowledged_at,
        operator_queue.c.cleared_at,  # #1017 — Clear All hide flag
    )

    def create_item(self, agent_name: str, item: Dict) -> str:
        """Create a queue item from agent JSON data.

        Args:
            agent_name: The agent that created this item
            item: Queue item data from agent's operator-queue.json

        Returns:
            The item ID
        """
        item_id = item["id"]
        options_json = json.dumps(item.get("options")) if item.get("options") else None
        context_json = json.dumps(item.get("context")) if item.get("context") else None

        # Agents author operator-queue.json free-form, so the sync boundary must
        # be defensive (#1426): a required field missing from one item used to
        # raise KeyError here, and because the item stayed `pending` in the agent
        # file the 5s sync loop retried and error-logged it forever — the request
        # never reached the Operating Room. Default the hard-indexed fields
        # (mirrors how type/status/priority are already defaulted) so the item is
        # created once and the loop stops (the next cycle sees it via exists()).
        # `created_at` defaults to now (ingest time) per the issue's preferred fix.
        stmt = make_insert(operator_queue).values(
            id=item_id,
            agent_name=agent_name,
            type=item.get("type", "question"),
            status=item.get("status", "pending"),
            priority=item.get("priority", "medium"),
            title=item.get("title") or "Agent request",
            question=item.get("question") or item.get("title") or "(no details provided)",
            options=options_json,
            context=context_json,
            execution_id=item.get("context", {}).get("execution_id") if item.get("context") else None,
            created_at=item.get("created_at") or utc_now_iso(),
            expires_at=item.get("expires_at"),
        ).on_conflict_do_nothing(index_elements=["id"])

        with get_engine().begin() as conn:
            conn.execute(stmt)
        return item_id

    def get_item(self, item_id: str) -> Optional[Dict]:
        """Get a single queue item by ID."""
        stmt = select(*self._SELECT_COLS).where(operator_queue.c.id == item_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None
        return self._row_to_item(row)

    def list_items(
        self,
        status: Optional[str] = None,
        type: Optional[str] = None,
        priority: Optional[str] = None,
        agent_name: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        accessible_agent_names: Optional[Set[str]] = None,
        include_cleared: bool = False,
    ) -> List[Dict]:
        """List queue items with optional filters.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items whose agent_name is in the set are returned. Empty set
        short-circuits to [] (user has no accessible agents).

        include_cleared: rows hidden by Clear All (#1017) are excluded by
        default. Only listing honors this — get_item and the sync-service
        accessors never filter on cleared_at.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return []

        conds = []
        if not include_cleared:
            conds.append(operator_queue.c.cleared_at.is_(None))  # #1017

        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))
        if status:
            conds.append(operator_queue.c.status == status)
        if type:
            conds.append(operator_queue.c.type == type)
        if priority:
            conds.append(operator_queue.c.priority == priority)
        if agent_name:
            conds.append(operator_queue.c.agent_name == agent_name)
        if since:
            conds.append(operator_queue.c.created_at >= since)

        # Sort: pending items by priority then age, others by created_at desc
        status_order = case(
            (operator_queue.c.status == "pending", 0),
            else_=1,
        )
        priority_order = case(
            (operator_queue.c.priority == "critical", 0),
            (operator_queue.c.priority == "high", 1),
            (operator_queue.c.priority == "medium", 2),
            (operator_queue.c.priority == "low", 3),
            else_=4,
        )

        stmt = select(*self._SELECT_COLS)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = (
            stmt.order_by(
                status_order,
                priority_order,
                operator_queue.c.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_item(row) for row in rows]

    def respond_to_item(
        self,
        item_id: str,
        response: str,
        response_text: Optional[str],
        responded_by_id: str,
        responded_by_email: str,
    ) -> Optional[Dict]:
        """Record an operator response to a queue item.

        Returns the updated item or None if not found.
        """
        now = utc_now_iso()

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "pending",
                    )
                )
                .values(
                    status="responded",
                    response=response,
                    response_text=response_text,
                    responded_by_id=responded_by_id,
                    responded_by_email=responded_by_email,
                    responded_at=now,
                )
            )

            if result.rowcount == 0:
                # Check if item exists at all
                row = conn.execute(
                    select(operator_queue.c.id, operator_queue.c.status).where(
                        operator_queue.c.id == item_id
                    )
                ).mappings().first()
                if not row:
                    return None
                # Item exists but not pending — lost a race (e.g. bulk-cancel
                # landed between the router's status check and this UPDATE).
                # Mark the conflict so the router can 409 instead of returning
                # a 200 for a response that was never recorded (#1017).
                item = self.get_item(item_id)
                item["_status_conflict"] = True
                return item

        return self.get_item(item_id)

    def cancel_item(self, item_id: str) -> Optional[Dict]:
        """Cancel a pending queue item."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "pending",
                    )
                )
                .values(status="cancelled")
            )

            if result.rowcount == 0:
                exists = conn.execute(
                    select(operator_queue.c.id).where(operator_queue.c.id == item_id)
                ).first()
                if not exists:
                    return None

        return self.get_item(item_id)

    def bulk_cancel_items(
        self,
        ids: List[str],
        accessible_agent_names: Optional[Set[str]] = None,
    ) -> int:
        """Cancel the listed items that are still pending (#1017).

        Only items in `ids` are touched — the caller sends the ids it actually
        showed the operator, so a sync-loop race can never cancel items the
        operator never saw. Non-pending and inaccessible ids are skipped.

        accessible_agent_names: None = no filter (admin); empty set = no-op
        (a zero-agent user must not be able to touch anything); non-empty =
        SQL-side IN filter.

        Returns the number of items actually cancelled.
        """
        if not ids:
            return 0
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return 0

        conds = [
            operator_queue.c.status == "pending",
            operator_queue.c.id.in_(list(ids)),
        ]
        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue).where(and_(*conds)).values(status="cancelled")
            )
            return result.rowcount

    def clear_resolved_items(
        self,
        agent_name: Optional[str] = None,
        accessible_agent_names: Optional[Set[str]] = None,
    ) -> int:
        """Hide terminal queue items — Clear All on the Resolved tab (#1017).

        Sets cleared_at on acknowledged/cancelled/expired rows; list_items
        excludes them by default. 'responded' rows are intentionally kept
        visible: the sync service still has to deliver the operator's answer
        to the agent file. A hide flag — NOT a DELETE — because the 5s sync
        loop re-creates any DB-missing item whose agent-file entry still says
        'pending' (always true for expired items, and for cancelled items
        whose flip hasn't been written back yet); deleting those rows would
        resurrect them. Actual row deletion is the retention sweep's job
        (#1142).

        Same tri-state accessible_agent_names contract as bulk_cancel_items.
        Returns the number of rows hidden.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return 0

        now = utc_now_iso()
        conds = [
            operator_queue.c.status.in_(("acknowledged", "cancelled", "expired")),
            operator_queue.c.cleared_at.is_(None),
        ]
        if accessible_agent_names is not None:
            conds.append(operator_queue.c.agent_name.in_(sorted(accessible_agent_names)))
        if agent_name:
            conds.append(operator_queue.c.agent_name == agent_name)

        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue).where(and_(*conds)).values(cleared_at=now)
            )
            return result.rowcount

    def mark_acknowledged(self, item_id: str) -> bool:
        """Mark an item as acknowledged by the agent."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.id == item_id,
                        operator_queue.c.status == "responded",
                    )
                )
                .values(status="acknowledged", acknowledged_at=now)
            )
            return result.rowcount > 0

    def mark_expired(self) -> int:
        """Mark pending items past their expires_at as expired.

        Returns number of items expired.
        """
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(operator_queue)
                .where(
                    and_(
                        operator_queue.c.status == "pending",
                        operator_queue.c.expires_at.isnot(None),
                        operator_queue.c.expires_at < now,
                    )
                )
                .values(status="expired")
            )
            return result.rowcount

    def get_stats(self, accessible_agent_names: Optional[Set[str]] = None) -> Dict:
        """Get queue statistics.

        accessible_agent_names: if None, no access filter (admin). If a set,
        only items for accessible agents are counted. Empty set returns zeros.
        """
        if accessible_agent_names is not None and len(accessible_agent_names) == 0:
            return {
                "by_status": {},
                "by_type": {},
                "by_priority": {},
                "by_agent": {},
                "pending_count": 0,
                "avg_response_seconds": None,
                "responded_today": 0,
            }

        # Access filter applied to every aggregate query.
        access_cond = None
        if accessible_agent_names is not None:
            access_cond = operator_queue.c.agent_name.in_(sorted(accessible_agent_names))

        def _with_access(*conds):
            all_conds = list(conds)
            if access_cond is not None:
                all_conds.append(access_cond)
            return all_conds

        with get_engine().connect() as conn:
            # Counts by status
            status_stmt = select(
                operator_queue.c.status, func.count()
            ).group_by(operator_queue.c.status)
            access_only = _with_access()
            if access_only:
                status_stmt = status_stmt.where(and_(*access_only))
            by_status = {row[0]: row[1] for row in conn.execute(status_stmt).all()}

            # Counts by type (pending only)
            type_stmt = (
                select(operator_queue.c.type, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.type)
            )
            by_type = {row[0]: row[1] for row in conn.execute(type_stmt).all()}

            # Counts by priority (pending only)
            priority_stmt = (
                select(operator_queue.c.priority, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.priority)
            )
            by_priority = {row[0]: row[1] for row in conn.execute(priority_stmt).all()}

            # Counts by agent (pending only)
            agent_stmt = (
                select(operator_queue.c.agent_name, func.count())
                .where(and_(*_with_access(operator_queue.c.status == "pending")))
                .group_by(operator_queue.c.agent_name)
            )
            by_agent = {row[0]: row[1] for row in conn.execute(agent_stmt).all()}

            # Average response time (for responded items). Computed in Python
            # from the ISO-Z timestamp strings — julianday() is SQLite-only.
            resp_conds = [operator_queue.c.responded_at.isnot(None)]
            avg_stmt = select(
                operator_queue.c.created_at, operator_queue.c.responded_at
            ).where(and_(*_with_access(*resp_conds)))
            deltas = []
            for created_at, responded_at in conn.execute(avg_stmt).all():
                if not created_at or not responded_at:
                    continue
                try:
                    c = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    r = datetime.fromisoformat(responded_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                deltas.append((r - c).total_seconds())
            avg_response_seconds = round(sum(deltas) / len(deltas), 1) if deltas else None

            # Items responded today
            today = datetime.utcnow().strftime("%Y-%m-%d")
            today_conds = [
                operator_queue.c.responded_at.isnot(None),
                operator_queue.c.responded_at >= today,
            ]
            today_stmt = select(func.count()).where(and_(*_with_access(*today_conds)))
            responded_today = conn.execute(today_stmt).scalar() or 0

        return {
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_agent": by_agent,
            "pending_count": by_status.get("pending", 0),
            "avg_response_seconds": avg_response_seconds,
            "responded_today": responded_today,
        }

    def get_pending_item_ids(self) -> List[str]:
        """Get IDs of all pending items (for sync service to check)."""
        stmt = select(operator_queue.c.id).where(operator_queue.c.status == "pending")
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).all()]

    def get_responded_items_for_agent(self, agent_name: str) -> List[Dict]:
        """Get responded (not yet acknowledged) items for a specific agent.

        Used by sync service to write responses back to agent files.
        """
        stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status == "responded",
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

    def get_terminal_items_for_agent(self, agent_name: str, since_hours: int = 168) -> List[Dict]:
        """Get recently cancelled/expired items for a specific agent (#1017).

        Used by the sync service to flip still-'pending' entries in the
        agent's queue file to their terminal status so the agent stops
        waiting (and so a stale 'pending' file entry can't resurrect the
        item if its row is ever purged). Deliberately NOT filtered on
        cleared_at — hidden items still need their flip delivered. Bounded
        by created_at (there is no per-status timestamp) so the per-agent
        5s sync query stays cheap.
        """
        cutoff = iso_cutoff(since_hours)
        stmt = select(*self._SELECT_COLS).where(
            and_(
                operator_queue.c.agent_name == agent_name,
                operator_queue.c.status.in_(("cancelled", "expired")),
                operator_queue.c.created_at >= cutoff,
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_item(row) for row in rows]

    def item_exists(self, item_id: str) -> bool:
        """Check if an item exists in the database."""
        stmt = select(operator_queue.c.id).where(operator_queue.c.id == item_id)
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None
