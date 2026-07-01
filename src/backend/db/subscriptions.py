"""
Subscription credentials database operations (SUB-002).

Manages Claude Max/Pro subscription tokens for agents.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers.
Subscriptions are registered once and can be assigned to multiple agents.
Tokens are encrypted using the same AES-256-GCM system as other credentials.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. The public API of ``SubscriptionOperations`` and
every return shape is preserved exactly.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select, insert, update, delete, func, and_

from .engine import get_engine
from .tables import (
    subscription_credentials,
    subscription_rate_limit_events,
    agent_ownership,
    chat_messages,
    schedule_executions,
    users,
)
from db_models import SubscriptionCredential, SubscriptionUsage, SubscriptionUsageWindow, SubscriptionWithAgents
from utils.helpers import iso_cutoff, utc_now_iso


class SubscriptionOperations:
    """Database operations for subscription credential management."""

    def __init__(self, encryption_service=None):
        """
        Initialize with optional encryption service.

        Args:
            encryption_service: CredentialEncryptionService instance for encrypting/decrypting
        """
        self._encryption_service = encryption_service

    def _get_encryption_service(self):
        """Get or create the encryption service."""
        if self._encryption_service is None:
            from services.credential_encryption import get_credential_encryption_service
            self._encryption_service = get_credential_encryption_service()
        return self._encryption_service

    @staticmethod
    def _row_to_subscription(row, include_agents: bool = False) -> SubscriptionCredential:
        """Convert a database row to a SubscriptionCredential model."""
        # Convert row to dict for safe access (RowMapping doesn't have .get())
        row_dict = dict(row) if row else {}
        data = {
            "id": row_dict["id"],
            "name": row_dict["name"],
            "subscription_type": row_dict.get("subscription_type"),
            "rate_limit_tier": row_dict.get("rate_limit_tier"),
            "owner_id": row_dict["owner_id"],
            "owner_email": row_dict.get("owner_email"),
            "created_at": datetime.fromisoformat(row_dict["created_at"]),
            "updated_at": datetime.fromisoformat(row_dict["updated_at"]),
            "agent_count": row_dict.get("agent_count", 0),
        }

        if include_agents:
            data["agents"] = row_dict.get("agents", [])
            return SubscriptionWithAgents(**data)

        return SubscriptionCredential(**data)

    # Columns selected for a subscription row joined with its owner. Mirrors the
    # prior `SELECT s.*, u.email as owner_email` projection (only the columns the
    # model converter actually reads — encrypted_credentials is intentionally
    # omitted, as it was never read by _row_to_subscription).
    @staticmethod
    def _subscription_select_columns():
        return [
            subscription_credentials.c.id,
            subscription_credentials.c.name,
            subscription_credentials.c.subscription_type,
            subscription_credentials.c.rate_limit_tier,
            subscription_credentials.c.owner_id,
            subscription_credentials.c.created_at,
            subscription_credentials.c.updated_at,
            users.c.email.label("owner_email"),
        ]

    @staticmethod
    def _agent_count_subquery():
        """Correlated agent-count subquery: live (non-deleted) agents per subscription.

        ``agent_ownership`` is aliased (#1199) so the subquery's FROM table is
        always distinct from any ``agent_ownership`` in the enclosing query.
        Without the alias, callers that *also* join ``agent_ownership`` in their
        outer FROM (``get_agent_subscription``) make SQLAlchemy auto-correlate
        ``agent_ownership`` out of the subquery, leaving it with no FROM clause —
        a compile-time ``InvalidRequestError``. With the alias, auto-correlation
        only removes ``subscription_credentials`` (the intended correlation), so
        the helper is safe in every caller.
        """
        ao = agent_ownership.alias("ao_count")
        return (
            select(func.count())
            .select_from(ao)
            .where(
                and_(
                    ao.c.subscription_id == subscription_credentials.c.id,
                    ao.c.deleted_at.is_(None),
                )
            )
            .scalar_subquery()
            .label("agent_count")
        )

    # =========================================================================
    # CRUD Operations
    # =========================================================================

    def create_subscription(
        self,
        name: str,
        token: str,
        owner_id: int,
        subscription_type: Optional[str] = None,
        rate_limit_tier: Optional[str] = None,
    ) -> SubscriptionCredential:
        """
        Create or update a subscription credential.

        Performs upsert by name - if a subscription with the same name exists,
        it will be updated with the new token.

        Args:
            name: Unique name for the subscription (e.g., "eugene-max")
            token: Long-lived token from `claude setup-token` (sk-ant-oat01-...)
            owner_id: User ID of the subscription owner
            subscription_type: Type like "max" or "pro"
            rate_limit_tier: Rate limit tier if known

        Returns:
            The created/updated SubscriptionCredential
        """
        # Encrypt the token
        encryption_service = self._get_encryption_service()
        encrypted = encryption_service.encrypt({"token": token})

        now = utc_now_iso()

        with get_engine().begin() as conn:
            # Check if subscription with this name already exists
            existing = conn.execute(
                select(subscription_credentials.c.id).where(
                    subscription_credentials.c.name == name
                )
            ).mappings().first()

            if existing:
                # Update existing subscription
                subscription_id = existing["id"]
                conn.execute(
                    update(subscription_credentials)
                    .where(subscription_credentials.c.id == subscription_id)
                    .values(
                        encrypted_credentials=encrypted,
                        subscription_type=subscription_type,
                        rate_limit_tier=rate_limit_tier,
                        updated_at=now,
                    )
                )
            else:
                # Create new subscription
                subscription_id = str(uuid.uuid4())
                conn.execute(
                    insert(subscription_credentials).values(
                        id=subscription_id,
                        name=name,
                        encrypted_credentials=encrypted,
                        subscription_type=subscription_type,
                        rate_limit_tier=rate_limit_tier,
                        owner_id=owner_id,
                        created_at=now,
                        updated_at=now,
                    )
                )

            # Return the subscription (without agent count for now)
            row = conn.execute(
                select(*self._subscription_select_columns())
                .select_from(
                    subscription_credentials.join(
                        users, subscription_credentials.c.owner_id == users.c.id
                    )
                )
                .where(subscription_credentials.c.id == subscription_id)
            ).mappings().first()

            return self._row_to_subscription(row)

    def get_subscription(self, subscription_id: str) -> Optional[SubscriptionCredential]:
        """
        Get a subscription by ID.

        Args:
            subscription_id: The subscription UUID

        Returns:
            SubscriptionCredential or None if not found
        """
        stmt = (
            select(*self._subscription_select_columns(), self._agent_count_subquery())
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
            .where(subscription_credentials.c.id == subscription_id)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if row:
            return self._row_to_subscription(row)
        return None

    def get_subscription_by_name(self, name: str) -> Optional[SubscriptionCredential]:
        """
        Get a subscription by name.

        Args:
            name: The subscription name

        Returns:
            SubscriptionCredential or None if not found
        """
        stmt = (
            select(*self._subscription_select_columns(), self._agent_count_subquery())
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
            .where(subscription_credentials.c.name == name)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if row:
            return self._row_to_subscription(row)
        return None

    def get_subscription_token(self, subscription_id: str) -> Optional[str]:
        """
        Get the decrypted token for a subscription.

        INTERNAL USE ONLY - tokens should not be exposed via API.

        Args:
            subscription_id: The subscription UUID

        Returns:
            Decrypted token string or None (including for legacy format subscriptions)
        """
        import logging
        _logger = logging.getLogger(__name__)

        stmt = select(
            subscription_credentials.c.name,
            subscription_credentials.c.encrypted_credentials,
        ).where(subscription_credentials.c.id == subscription_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if not row:
            return None

        # Decrypt credentials
        encryption_service = self._get_encryption_service()
        decrypted = encryption_service.decrypt(row["encrypted_credentials"])

        # SUB-002 format: {"token": "sk-ant-oat01-..."}
        token = decrypted.get("token")
        if token:
            return token

        # Legacy SUB-001 format: {".credentials.json": "..."} — return None with warning
        if ".credentials.json" in decrypted:
            _logger.warning(
                f"Subscription '{row['name']}' ({subscription_id}) uses legacy "
                f".credentials.json format. Re-register with `claude setup-token`."
            )
            return None

        _logger.warning(f"Subscription '{row['name']}' ({subscription_id}) has unknown credential format")
        return None

    def list_subscriptions(self, owner_id: Optional[int] = None) -> List[SubscriptionCredential]:
        """
        List all subscriptions, optionally filtered by owner.

        Args:
            owner_id: Optional user ID to filter by

        Returns:
            List of SubscriptionCredential objects
        """
        stmt = (
            select(*self._subscription_select_columns(), self._agent_count_subquery())
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
            .order_by(subscription_credentials.c.name)
        )
        if owner_id:
            stmt = stmt.where(subscription_credentials.c.owner_id == owner_id)

        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        return [self._row_to_subscription(row) for row in rows]

    def has_any_subscription(self) -> bool:
        """Cheap existence check — does ANY subscription exist?

        Used by the hot ``/api/settings/feature-flags`` path (onboarding's
        ``claude_auth_configured``) to avoid materializing + decrypting every
        subscription row just to test presence.
        """
        stmt = select(func.count()).select_from(subscription_credentials)
        with get_engine().connect() as conn:
            return (conn.execute(stmt).scalar() or 0) > 0

    def list_subscriptions_with_agents(self, owner_id: Optional[int] = None) -> List[SubscriptionWithAgents]:
        """
        List subscriptions with their assigned agents.

        Args:
            owner_id: Optional user ID to filter by

        Returns:
            List of SubscriptionWithAgents objects
        """
        subscriptions = self.list_subscriptions(owner_id)

        result = []
        with get_engine().connect() as conn:
            for sub in subscriptions:
                rows = conn.execute(
                    select(agent_ownership.c.agent_name).where(
                        and_(
                            agent_ownership.c.subscription_id == sub.id,
                            agent_ownership.c.deleted_at.is_(None),
                        )
                    )
                ).mappings().all()
                agents = [row["agent_name"] for row in rows]

                result.append(SubscriptionWithAgents(
                    id=sub.id,
                    name=sub.name,
                    subscription_type=sub.subscription_type,
                    rate_limit_tier=sub.rate_limit_tier,
                    owner_id=sub.owner_id,
                    owner_email=sub.owner_email,
                    created_at=sub.created_at,
                    updated_at=sub.updated_at,
                    agent_count=len(agents),
                    agents=agents,
                ))

        return result

    def delete_subscription(self, subscription_id: str) -> bool:
        """
        Delete a subscription and cascade clear agent assignments.

        Args:
            subscription_id: The subscription UUID to delete

        Returns:
            True if deleted, False if not found
        """
        with get_engine().begin() as conn:
            # First clear all agent assignments
            cleared_count = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.subscription_id == subscription_id)
                .values(subscription_id=None)
            ).rowcount

            # Then delete the subscription
            deleted = conn.execute(
                delete(subscription_credentials).where(
                    subscription_credentials.c.id == subscription_id
                )
            ).rowcount > 0

        if deleted and cleared_count > 0:
            import logging
            logging.getLogger(__name__).info(
                f"Deleted subscription {subscription_id}, cleared {cleared_count} agent assignments"
            )

        return deleted

    # =========================================================================
    # Agent Assignment Operations
    # =========================================================================

    def assign_subscription_to_agent(
        self,
        agent_name: str,
        subscription_id: str
    ) -> bool:
        """
        Assign a subscription to an agent.

        Args:
            agent_name: Name of the agent
            subscription_id: ID of the subscription to assign

        Returns:
            True if successful
        """
        with get_engine().begin() as conn:
            # Verify subscription exists
            existing = conn.execute(
                select(subscription_credentials.c.id).where(
                    subscription_credentials.c.id == subscription_id
                )
            ).mappings().first()
            if not existing:
                raise ValueError(f"Subscription {subscription_id} not found")

            # Update agent ownership
            result = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(subscription_id=subscription_id)
            )

            if result.rowcount == 0:
                raise ValueError(f"Agent {agent_name} not found in ownership table")

            return True

    def clear_agent_subscription(self, agent_name: str) -> bool:
        """
        Clear subscription assignment from an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            True if cleared (even if was already null)
        """
        with get_engine().begin() as conn:
            conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(subscription_id=None)
            )
            return True

    def get_agent_subscription(self, agent_name: str) -> Optional[SubscriptionCredential]:
        """
        Get the subscription assigned to an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            SubscriptionCredential or None if no subscription assigned
        """
        stmt = (
            select(*self._subscription_select_columns(), self._agent_count_subquery())
            .select_from(
                subscription_credentials
                .join(users, subscription_credentials.c.owner_id == users.c.id)
                .join(
                    agent_ownership,
                    agent_ownership.c.subscription_id == subscription_credentials.c.id,
                )
            )
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()

        if row:
            return self._row_to_subscription(row)
        return None

    def get_agents_by_subscription(self, subscription_id: str) -> List[str]:
        """
        Get all agents using a specific subscription.

        Args:
            subscription_id: The subscription UUID

        Returns:
            List of agent names
        """
        stmt = select(agent_ownership.c.agent_name).where(
            and_(
                agent_ownership.c.subscription_id == subscription_id,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [row["agent_name"] for row in rows]

    def get_agent_subscription_id(self, agent_name: str) -> Optional[str]:
        """
        Get the subscription ID assigned to an agent (lightweight check).

        Args:
            agent_name: Name of the agent

        Returns:
            Subscription ID or None
        """
        stmt = select(agent_ownership.c.subscription_id).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return row["subscription_id"] if row else None

    # =========================================================================
    # Rate-Limit Tracking (SUB-003: Auto-Switch)
    # =========================================================================

    def record_rate_limit_event(
        self,
        agent_name: str,
        subscription_id: str,
        error_message: str = ""
    ) -> int:
        """
        Record a rate-limit event for an (agent, subscription) pair.

        Returns the count of consecutive rate-limit events for this pair
        (events within the last 2 hours, no successful execution in between).
        """
        now = utc_now_iso()
        event_id = str(uuid.uuid4())

        with get_engine().begin() as conn:
            conn.execute(
                insert(subscription_rate_limit_events).values(
                    id=event_id,
                    agent_name=agent_name,
                    subscription_id=subscription_id,
                    error_message=error_message,
                    occurred_at=now,
                )
            )

            # Count consecutive events in last 2 hours (#476: iso_cutoff,
            # not datetime('now', ...), so the format matches occurred_at)
            cnt = conn.execute(
                select(func.count().label("cnt"))
                .select_from(subscription_rate_limit_events)
                .where(
                    and_(
                        subscription_rate_limit_events.c.agent_name == agent_name,
                        subscription_rate_limit_events.c.subscription_id == subscription_id,
                        subscription_rate_limit_events.c.occurred_at > iso_cutoff(2),
                    )
                )
            ).scalar_one()
            return cnt

    def is_subscription_rate_limited(self, subscription_id: str) -> bool:
        """Check if a subscription has been rate-limited in the last 2 hours."""
        stmt = (
            select(func.count().label("cnt"))
            .select_from(subscription_rate_limit_events)
            .where(
                and_(
                    subscription_rate_limit_events.c.subscription_id == subscription_id,
                    subscription_rate_limit_events.c.occurred_at > iso_cutoff(2),
                )
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).scalar_one() > 0

    def clear_rate_limit_events(self, agent_name: str, subscription_id: str) -> None:
        """Clear rate-limit events for an (agent, subscription) pair after successful switch."""
        stmt = delete(subscription_rate_limit_events).where(
            and_(
                subscription_rate_limit_events.c.agent_name == agent_name,
                subscription_rate_limit_events.c.subscription_id == subscription_id,
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def cleanup_old_rate_limit_events(self) -> int:
        """Remove rate-limit tracking records older than 24 hours. Returns count deleted."""
        stmt = delete(subscription_rate_limit_events).where(
            subscription_rate_limit_events.c.occurred_at < iso_cutoff(24)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount

    def get_least_used_subscription(self) -> Optional[SubscriptionCredential]:
        """
        Get subscription with fewest assigned agents (round-robin).

        Tie-break: alphabetical by name.
        Skips subscriptions that are currently rate-limited or have invalid tokens.
        Used for auto-assignment on agent creation (#74).

        Returns:
            SubscriptionCredential with fewest agents, or None if no viable subscription
        """
        agent_count = self._agent_count_subquery()
        stmt = (
            select(*self._subscription_select_columns(), agent_count)
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
            .order_by(agent_count.asc(), subscription_credentials.c.name.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        for row in rows:
            sub = self._row_to_subscription(row)
            # Skip rate-limited subscriptions
            if self.is_subscription_rate_limited(sub.id):
                continue
            # Skip subscriptions with invalid/legacy tokens (#340)
            token = self.get_subscription_token(sub.id)
            if not token:
                continue
            return sub
        return None

    def select_best_alternative_subscription(
        self,
        current_subscription_id: str
    ) -> Optional[SubscriptionCredential]:
        """
        Select the best alternative subscription for auto-switch.

        Strategy:
        - Exclude the current subscription
        - Skip subscriptions rate-limited in the last 2 hours
        - Prefer subscriptions with fewer assigned agents (load-balance)

        Returns:
            Best alternative SubscriptionCredential, or None if no viable option
        """
        agent_count = self._agent_count_subquery()
        # Get all subscriptions except current, ordered by agent count (ascending)
        stmt = (
            select(*self._subscription_select_columns(), agent_count)
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
            .where(subscription_credentials.c.id != current_subscription_id)
            .order_by(agent_count.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        for row in rows:
            sub = self._row_to_subscription(row)
            # Skip if rate-limited in the last 2 hours
            if not self.is_subscription_rate_limited(sub.id):
                return sub

        return None

    # =========================================================================
    # Usage Tracking (SUB-004: Per-subscription usage windows)
    # =========================================================================

    def get_subscription_usage(self, subscription_id: str) -> SubscriptionUsage:
        """
        Return rolling usage totals for a subscription across two time windows:
        - window_5h: last 5 hours
        - window_7d: last 7 days (168 hours)

        Aggregates chat_messages and schedule_executions by subscription_id.

        Args:
            subscription_id: The subscription UUID to query

        Returns:
            SubscriptionUsage with two windows and list of currently-assigned agents
        """
        now = datetime.utcnow()
        cutoff_5h = (now - timedelta(hours=5)).isoformat()
        cutoff_7d = (now - timedelta(hours=168)).isoformat()

        with get_engine().connect() as conn:

            def _query_window(cutoff: str) -> SubscriptionUsageWindow:
                # Chat messages: input tokens stored in context_used, output in output_tokens
                chat_row = conn.execute(
                    select(
                        func.coalesce(func.sum(chat_messages.c.context_used), 0).label("input_tokens"),
                        func.coalesce(func.sum(chat_messages.c.output_tokens), 0).label("output_tokens"),
                        func.coalesce(func.sum(chat_messages.c.cost), 0.0).label("cost_usd"),
                        func.count().label("message_count"),
                    ).where(
                        and_(
                            chat_messages.c.subscription_id == subscription_id,
                            chat_messages.c.role == "assistant",
                            chat_messages.c.timestamp >= cutoff,
                        )
                    )
                ).mappings().first()

                # Schedule executions: input tokens in context_used (no separate output_tokens)
                exec_row = conn.execute(
                    select(
                        func.coalesce(func.sum(schedule_executions.c.context_used), 0).label("input_tokens"),
                        func.coalesce(func.sum(schedule_executions.c.cost), 0.0).label("cost_usd"),
                        func.count().label("exec_count"),
                    ).where(
                        and_(
                            schedule_executions.c.subscription_id == subscription_id,
                            schedule_executions.c.started_at >= cutoff,
                            schedule_executions.c.status.notin_(["running", "pending"]),
                        )
                    )
                ).mappings().first()

                return SubscriptionUsageWindow(
                    input_tokens=int(chat_row["input_tokens"] or 0) + int(exec_row["input_tokens"] or 0),
                    output_tokens=int(chat_row["output_tokens"] or 0),
                    cost_usd=float(chat_row["cost_usd"] or 0.0) + float(exec_row["cost_usd"] or 0.0),
                    message_count=int(chat_row["message_count"] or 0) + int(exec_row["exec_count"] or 0),
                )

            window_5h = _query_window(cutoff_5h)
            window_7d = _query_window(cutoff_7d)

            # Currently-assigned agents (live assignment, not historical)
            agent_rows = conn.execute(
                select(agent_ownership.c.agent_name).where(
                    and_(
                        agent_ownership.c.subscription_id == subscription_id,
                        agent_ownership.c.deleted_at.is_(None),
                    )
                )
            ).mappings().all()
            agents = [row["agent_name"] for row in agent_rows]

        return SubscriptionUsage(
            subscription_id=subscription_id,
            window_5h=window_5h,
            window_7d=window_7d,
            agents=agents,
        )
