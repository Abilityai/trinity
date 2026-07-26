"""
Event subscription operations for agent event pub/sub (EVT-001).

Enables agents to subscribe to events from other agents and
trigger async tasks when matching events are emitted.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300) so it runs unchanged on both SQLite and PostgreSQL. Queries are
built from the ``agent_event_subscriptions`` / ``agent_events`` tables in
``db/tables.py`` (dialect-agnostic, no ``?`` placeholders) and the engine is
resolved from ``DATABASE_URL`` via ``db/engine.py``. The public API is unchanged.
"""

import json
import secrets
from typing import List, Optional

from sqlalchemy import select, insert, update, delete, and_, or_

from .engine import get_engine
from .tables import agent_event_subscriptions, agent_events
from db_models import EventSubscription, EventSubscriptionCreate, AgentEvent
from utils.helpers import utc_now_iso


class EventSubscriptionOperations:
    """Database operations for agent event subscriptions and events."""

    # =========================================================================
    # Subscription CRUD
    # =========================================================================

    def create_subscription(
        self,
        subscriber_agent: str,
        data: EventSubscriptionCreate,
        created_by: str
    ) -> EventSubscription:
        """Create a new event subscription."""
        sub_id = f"esub_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_event_subscriptions).values(
                    id=sub_id,
                    subscriber_agent=subscriber_agent,
                    source_agent=data.source_agent,
                    event_type=data.event_type,
                    target_message=data.target_message,
                    enabled=1 if data.enabled else 0,
                    created_at=now,
                    updated_at=now,
                    created_by=created_by,
                )
            )

        return EventSubscription(
            id=sub_id,
            subscriber_agent=subscriber_agent,
            source_agent=data.source_agent,
            event_type=data.event_type,
            target_message=data.target_message,
            enabled=data.enabled,
            created_at=now,
            updated_at=now,
            created_by=created_by
        )

    def get_subscription(self, subscription_id: str) -> Optional[EventSubscription]:
        """Get a subscription by ID."""
        t = agent_event_subscriptions
        stmt = select(
            t.c.id, t.c.subscriber_agent, t.c.source_agent, t.c.event_type,
            t.c.target_message, t.c.enabled, t.c.created_at, t.c.updated_at,
            t.c.created_by,
        ).where(t.c.id == subscription_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()

        if not row:
            return None
        return self._row_to_subscription(row)

    def list_subscriptions(
        self,
        subscriber_agent: Optional[str] = None,
        source_agent: Optional[str] = None,
        enabled_only: bool = False,
        limit: int = 100
    ) -> List[EventSubscription]:
        """List event subscriptions with optional filters."""
        t = agent_event_subscriptions
        stmt = select(
            t.c.id, t.c.subscriber_agent, t.c.source_agent, t.c.event_type,
            t.c.target_message, t.c.enabled, t.c.created_at, t.c.updated_at,
            t.c.created_by,
        )

        conds = []
        if subscriber_agent:
            conds.append(t.c.subscriber_agent == subscriber_agent)
        if source_agent:
            conds.append(t.c.source_agent == source_agent)
        if enabled_only:
            conds.append(t.c.enabled == 1)

        if conds:
            stmt = stmt.where(and_(*conds))

        stmt = stmt.order_by(t.c.created_at.desc()).limit(limit)

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        return [self._row_to_subscription(row) for row in rows]

    def update_subscription(
        self,
        subscription_id: str,
        event_type: Optional[str] = None,
        target_message: Optional[str] = None,
        enabled: Optional[bool] = None
    ) -> Optional[EventSubscription]:
        """Update an existing subscription."""
        values = {}

        if event_type is not None:
            values["event_type"] = event_type

        if target_message is not None:
            values["target_message"] = target_message

        if enabled is not None:
            values["enabled"] = 1 if enabled else 0

        if not values:
            return self.get_subscription(subscription_id)

        values["updated_at"] = utc_now_iso()

        t = agent_event_subscriptions
        with get_engine().begin() as conn:
            result = conn.execute(
                update(t).where(t.c.id == subscription_id).values(**values)
            )

            if result.rowcount == 0:
                return None

        return self.get_subscription(subscription_id)

    def delete_subscription(self, subscription_id: str) -> bool:
        """Delete a subscription. Returns True if deleted."""
        t = agent_event_subscriptions
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(t).where(t.c.id == subscription_id)
            )
            return result.rowcount > 0

    def delete_agent_subscriptions(self, agent_name: str) -> int:
        """Delete all subscriptions where agent is subscriber or source."""
        t = agent_event_subscriptions
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(t).where(
                    or_(
                        t.c.subscriber_agent == agent_name,
                        t.c.source_agent == agent_name,
                    )
                )
            )
            return result.rowcount

    # =========================================================================
    # Event Matching
    # =========================================================================

    def find_matching_subscriptions(
        self,
        source_agent: str,
        event_type: str
    ) -> List[EventSubscription]:
        """Find enabled subscriptions that match a source agent and event type."""
        t = agent_event_subscriptions
        stmt = select(
            t.c.id, t.c.subscriber_agent, t.c.source_agent, t.c.event_type,
            t.c.target_message, t.c.enabled, t.c.created_at, t.c.updated_at,
            t.c.created_by,
        ).where(
            and_(
                t.c.source_agent == source_agent,
                t.c.event_type == event_type,
                t.c.enabled == 1,
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        return [self._row_to_subscription(row) for row in rows]

    def has_enabled_subscriptions_for_types(self, event_types: List[str]) -> bool:
        """Cheap existence check: is ANY enabled subscription listening for one of
        ``event_types`` (across all source agents)?

        #1714: lets a bulk watchdog sweep skip all per-row completion-event work
        when nobody subscribes to ``agent.task.*`` at all — so a large sweep with
        no subscribers costs one bounded query, not one per swept row. The precise
        per-source-agent gating still happens in ``emit_task_terminal_event``.
        """
        if not event_types:
            return False
        t = agent_event_subscriptions
        stmt = (
            select(t.c.id)
            .where(and_(t.c.event_type.in_(event_types), t.c.enabled == 1))
            .limit(1)
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    # =========================================================================
    # Event Persistence
    # =========================================================================

    def create_event(
        self,
        source_agent: str,
        event_type: str,
        payload: Optional[dict] = None,
        subscriptions_triggered: int = 0
    ) -> AgentEvent:
        """Persist an emitted event."""
        event_id = f"evt_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()
        payload_json = json.dumps(payload) if payload else None

        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_events).values(
                    id=event_id,
                    source_agent=source_agent,
                    event_type=event_type,
                    payload=payload_json,
                    subscriptions_triggered=subscriptions_triggered,
                    created_at=now,
                )
            )

        return AgentEvent(
            id=event_id,
            source_agent=source_agent,
            event_type=event_type,
            payload=payload,
            subscriptions_triggered=subscriptions_triggered,
            created_at=now
        )

    def list_events(
        self,
        source_agent: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50
    ) -> List[AgentEvent]:
        """List events with optional filters."""
        t = agent_events
        stmt = select(
            t.c.id, t.c.source_agent, t.c.event_type, t.c.payload,
            t.c.subscriptions_triggered, t.c.created_at,
        )

        conds = []
        if source_agent:
            conds.append(t.c.source_agent == source_agent)
        if event_type:
            conds.append(t.c.event_type == event_type)

        if conds:
            stmt = stmt.where(and_(*conds))

        stmt = stmt.order_by(t.c.created_at.desc()).limit(limit)

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        return [self._row_to_event(row) for row in rows]

    # =========================================================================
    # Row Converters
    # =========================================================================

    def _row_to_subscription(self, row: tuple) -> EventSubscription:
        """Convert a database row to an EventSubscription model."""
        return EventSubscription(
            id=row[0],
            subscriber_agent=row[1],
            source_agent=row[2],
            event_type=row[3],
            target_message=row[4],
            enabled=bool(row[5]),
            created_at=row[6],
            updated_at=row[7],
            created_by=row[8]
        )

    def _row_to_event(self, row: tuple) -> AgentEvent:
        """Convert a database row to an AgentEvent model."""
        payload = None
        if row[3]:
            try:
                payload = json.loads(row[3])
            except json.JSONDecodeError:
                pass

        return AgentEvent(
            id=row[0],
            source_agent=row[1],
            event_type=row[2],
            payload=payload,
            subscriptions_triggered=row[4],
            created_at=row[5]
        )
