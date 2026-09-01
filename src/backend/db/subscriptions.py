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
from datetime import datetime
from typing import Optional, List, Dict, Any, Sequence, Set, Tuple

from sqlalchemy import select, insert, update, delete, func, and_

from .engine import get_engine
from .tables import (
    subscription_credentials,
    subscription_headroom_history,
    subscription_rate_limit_events,
    agent_ownership,
    chat_messages,
    schedule_executions,
    users,
)
from db_models import SubscriptionCredential, SubscriptionUsage, SubscriptionUsageWindow, SubscriptionWithAgents
from utils.helpers import iso_cutoff, utc_now_iso


def _headroom_history_prune_predicate(cutoff: str):
    """The ONE predicate the ent#433 headroom-history retention sweep deletes by.

    Shared verbatim by `count_headroom_history_candidates` (which feeds the
    #1644 blast-radius guard) and `prune_headroom_history` (which does the
    deleting). Kept as a module-level function precisely so the two cannot
    drift: a guard that counts a different row set than the prune removes is a
    guard over nothing (see `retention_guard.evaluate`).
    """
    return subscription_headroom_history.c.fetched_at < cutoff


def _rate_limit_event_prune_predicate(cutoff: str):
    """The ONE predicate the ent#433 failure-event retention sweep deletes by.

    Same count/prune-parity contract as the headroom predicate above.
    """
    return subscription_rate_limit_events.c.occurred_at < cutoff


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

            # ent#433: cascade the headroom history explicitly. The DDL's
            # ON DELETE CASCADE is decorative — `PRAGMA foreign_keys` is off
            # platform-wide and the PG DDL path strips FK clauses — so without
            # this the rows would linger for the full retention window with no
            # subscription to belong to. In-transaction rather than a
            # best-effort call at the router (where `clear_snapshot` lives), so
            # a second caller of this accessor cannot miss it.
            #
            # Known, accepted residual: `get_headroom(wait=False)` spawns a
            # background probe that can still be in flight here and land its
            # INSERT after this commits. That orphan is reaped by the retention
            # sweep. It is NOT worth a pre-INSERT existence check, which would
            # add a read to every probe to close a window measured in seconds.
            conn.execute(
                delete(subscription_headroom_history).where(
                    subscription_headroom_history.c.subscription_id == subscription_id
                )
            )

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
        error_message: str = "",
        failure_kind: str = "rate_limit",
    ) -> int:
        """
        Record a subscription-failure event for an (agent, subscription) pair.

        #471: ``failure_kind`` ("rate_limit" | "auth") is now PERSISTED — the
        caller has carried it since #441/#792 but the table conflated the two,
        and every observability consumer reads this stream as "429 pressure".

        Returns the count of consecutive events for this pair
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
                    failure_kind=failure_kind,
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
        """Has this subscription been RATE-LIMITED (429) in the last 2 hours?

        #2352: this predicate answers the DISPLAY question — "is this
        subscription being throttled right now" — and is therefore scoped to
        ``failure_kind = 'rate_limit'``. It used to count every row in the
        window regardless of kind, so an auth failure (401/403 — a dead,
        expired, or `.env`-shadowed token) set ``rate_limited_now`` and every
        surface reported a credential problem as quota exhaustion, pointing the
        operator at the wrong remedy. That is the conflation #471's
        ``failure_kind`` column exists to end; the display layer already honours
        it (``rateLimitEventCount`` reads the ``rate_limit`` kind only) — the
        predicate was the layer that had not caught up.

        ``failure_kind IS NULL`` (pre-#471 rows) is deliberately EXCLUDED:
        unknown is not promoted to "429", the same rule the frontend applies by
        never folding ``unknown`` into ``rate_limit``. It is close to
        unreachable in practice — the writer has always passed a kind, the
        table is swept at 24h, and this window is 2h — but the direction of the
        choice matters: a false "rate-limited" sends the operator to wait out a
        window that was never full.

        NOT the predicate for "should auto-switch avoid this subscription" —
        that one wants ANY recent failure and lives in
        ``has_recent_subscription_failures()``. Keep them apart: collapsing
        them back into one is exactly how this bug happened.
        """
        return self._failure_event_count(subscription_id, kinds=("rate_limit",)) > 0

    def has_recent_subscription_failures(
        self, subscription_id: str, hours: int = 2
    ) -> bool:
        """Has this subscription failed for ANY reason in the window?

        #2352: the kind-BLIND predicate — deliberately the pre-split behaviour
        of ``is_subscription_rate_limited()``, preserved verbatim under a name
        that says what it means. Auto-switch and new-agent assignment use it to
        skip a subscription that recently failed, and they must keep counting
        auth failures: a subscription with a dead token is the *last* place to
        move an agent to, and #444's ping-pong is what happens when candidate
        filtering forgets a recent failure.

        ``failure_kind IS NULL`` COUNTS here — an unclassified failure is still
        a failure, and this predicate's job is caution, not attribution.
        """
        return self._failure_event_count(subscription_id, hours=hours) > 0

    def _failure_event_count(
        self,
        subscription_id: str,
        *,
        hours: int = 2,
        kinds: Optional[Tuple[str, ...]] = None,
    ) -> int:
        """Rows in ``subscription_rate_limit_events`` for one subscription.

        ``kinds=None`` counts every kind (incl. NULL); a tuple restricts to
        those kinds and thereby EXCLUDES NULL, since ``IN`` never matches NULL
        in SQL — the exclusion documented on the two callers above is a
        property of this one comparison, not a second rule applied elsewhere.
        """
        stmt = (
            select(func.count().label("cnt"))
            .select_from(subscription_rate_limit_events)
            .where(
                and_(
                    subscription_rate_limit_events.c.subscription_id == subscription_id,
                    *self._failure_event_window(hours=hours, kinds=kinds),
                )
            )
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    @staticmethod
    def _failure_event_window(*, hours: int, kinds: Optional[Tuple[str, ...]] = None):
        """The window + kind half of the failure-event predicate, WITHOUT the
        subscription-id term.

        Extracted so the single-id count above and the batched id-set below are
        the same question asked of the same rows — one scoped to a row, one to
        a set. Copying the two conditions into the batch instead would give the
        display predicate two definitions of "recently rate-limited", which is
        precisely the drift #2352 spent a fix untangling.
        """
        conditions = [
            # #476: iso_cutoff, not datetime('now', ...) — the format has to
            # match how occurred_at was written.
            subscription_rate_limit_events.c.occurred_at > iso_cutoff(hours),
        ]
        if kinds is not None:
            conditions.append(subscription_rate_limit_events.c.failure_kind.in_(kinds))
        return conditions

    def rate_limited_subscription_ids(
        self, subscription_ids: Sequence[str]
    ) -> Set[str]:
        """The subset of ``subscription_ids`` that ``is_subscription_rate_limited``
        would answer True for — in ONE query (#2443).

        The per-id predicate is a synchronous SQLAlchemy call, and both of its
        hot callers ask it once per subscription: the #447 recovery sweep (every
        300s, now chunk-concurrent) and the dashboard pressure batch (every 60s).
        Asking N times off the event loop is still N round-trips; asking once is
        one, and it is the shape ``get_failure_event_counts_by_subscription``
        already uses next door.

        Empty input short-circuits without touching the DB — an ``IN ()`` is a
        wasted round-trip whose answer is known.
        """
        ids = [s for s in dict.fromkeys(subscription_ids) if s]
        if not ids:
            return set()
        stmt = (
            select(subscription_rate_limit_events.c.subscription_id)
            .where(
                and_(
                    subscription_rate_limit_events.c.subscription_id.in_(ids),
                    *self._failure_event_window(hours=2, kinds=("rate_limit",)),
                )
            )
            .distinct()
        )
        with get_engine().connect() as conn:
            return {row[0] for row in conn.execute(stmt)}

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

    def cleanup_old_rate_limit_events(
        self, retention_days: int = 30, chunk_size: int = 1000
    ) -> int:
        """Prune rate-limit events past ``retention_days`` (ent#433; was 24h).

        This table is the platform's only durable record of REAL AGENT WORK
        hitting a provider rate limit — timestamped and attributed to the
        agent that caused it. It used to be swept at a hardcoded 24 hours with
        no operator-visible window, no #1644 blast-radius guard, and no entry
        on `GET /api/settings/retention`, while every sibling table had all
        three. ent#433 gives it a real window.

        Widening the default from 1 day to 30 is the #1638-safe direction — no
        install loses data — and cannot change any existing answer, because
        every consumer already filters by time itself (`hours=24` at the
        pressure call site, a 2h predicate for `rate_limited_now`).

        Chunked like the other retention prunes so a first sweep on a busy
        install doesn't hold the write lock for the whole purge. `0` disables.
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
                        select(subscription_rate_limit_events.c.id)
                        .where(_rate_limit_event_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                total += conn.execute(
                    delete(subscription_rate_limit_events).where(
                        subscription_rate_limit_events.c.id.in_(ids)
                    )
                ).rowcount
            if len(ids) < chunk_size:
                break
        return total

    def get_failure_event_counts(
        self, subscription_id: str, hours: int = 24
    ) -> Dict[str, Any]:
        """#471 — failure events on record for one subscription within a window.

        Returns ``{"total": int, "by_kind": {kind: int}}``. A NULL
        ``failure_kind`` (pre-#471 row) buckets under ``"unknown"``. The table
        is bounded by the 24h sweep, so this is a cheap grouped read.
        """
        stmt = (
            select(
                subscription_rate_limit_events.c.failure_kind,
                func.count().label("cnt"),
            )
            .where(
                and_(
                    subscription_rate_limit_events.c.subscription_id == subscription_id,
                    subscription_rate_limit_events.c.occurred_at > iso_cutoff(hours),
                )
            )
            .group_by(subscription_rate_limit_events.c.failure_kind)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()
        by_kind: Dict[str, int] = {}
        for kind, cnt in rows:
            by_kind[kind or "unknown"] = int(cnt)
        return {"total": sum(by_kind.values()), "by_kind": by_kind}

    def get_failure_event_counts_by_subscription(
        self, hours: int = 24
    ) -> Dict[str, Dict[str, Any]]:
        """#471 — one grouped read, per subscription, within the window.

        Returns ``{subscription_id: {"total": int, "by_kind": {kind: int}}}`` —
        the SAME shape as the single-subscription ``get_failure_event_counts``,
        so the two cannot be read differently. #2352 widened it from a bare
        total: the fleet pressure endpoint has to tell a 429 from an auth
        failure, and a total structurally cannot. A NULL ``failure_kind``
        (pre-#471 row) buckets under ``"unknown"`` and is never folded into
        another kind.

        One query, still — the extra dimension is a second GROUP BY column, not
        a second read. Bounded by the 24h sweep.
        """
        stmt = (
            select(
                subscription_rate_limit_events.c.subscription_id,
                subscription_rate_limit_events.c.failure_kind,
                func.count().label("cnt"),
            )
            .where(subscription_rate_limit_events.c.occurred_at > iso_cutoff(hours))
            .group_by(
                subscription_rate_limit_events.c.subscription_id,
                subscription_rate_limit_events.c.failure_kind,
            )
        )
        out: Dict[str, Dict[str, Any]] = {}
        with get_engine().connect() as conn:
            for sid, kind, cnt in conn.execute(stmt).all():
                entry = out.setdefault(sid, {"total": 0, "by_kind": {}})
                entry["by_kind"][kind or "unknown"] = int(cnt)
                entry["total"] += int(cnt)
        return out

    # =========================================================================
    # Subscription headroom history (ent#433)
    # =========================================================================

    def insert_headroom_history(
        self, subscription_id: str, snapshot: Dict[str, Any]
    ) -> None:
        """Persist ONE #471 probe result (ent#433).

        Called from `subscription_headroom_service._record_history`, which runs
        this **off the event loop** via `asyncio.to_thread` and swallows any
        raise — history is enrichment and must never affect probe availability.

        Every non-skipped probe outcome lands here, failures included, because a
        three-day dead token must not be byte-identical to nobody-watching.
        `utilization_pct` stays NULL wherever the provider did not report one —
        notably on a 429, which reports a window status with no figure.
        """
        def _win(key: str) -> Dict[str, Any]:
            w = snapshot.get(key) or {}
            return w if isinstance(w, dict) else {}

        five, seven = _win("five_hour"), _win("seven_day")
        stmt = insert(subscription_headroom_history).values(
            subscription_id=subscription_id,
            # Defensive: `_probe` always stamps this, but the column is NOT NULL
            # and a missing value must not turn enrichment into an exception.
            fetched_at=snapshot.get("fetched_at") or utc_now_iso(),
            status=snapshot.get("status") or "ok",
            five_hour_utilization_pct=five.get("utilization_pct"),
            five_hour_resets_at=five.get("resets_at"),
            five_hour_status=five.get("status"),
            seven_day_utilization_pct=seven.get("utilization_pct"),
            seven_day_resets_at=seven.get("resets_at"),
            seven_day_status=seven.get("status"),
            representative_claim=snapshot.get("representative_claim"),
            overage_status=snapshot.get("overage_status"),
            unified_status=snapshot.get("unified_status"),
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_headroom_history(
        self, subscription_id: str, *, hours: int, bucket: str
    ) -> List[Dict[str, Any]]:
        """Bucketed headroom series — the LAST sample in each bucket (ent#433).

        `last`, never `MAX`, for three independent reasons (see requirements
        §20.5b): probes are demand-driven so a max is biased by how often anyone
        looked; the 5h and 7d windows peak at different instants so a two-column
        max has no single owning row; and a 429 carries a window status with a
        NULL utilization, which a max would drop exactly when it matters most.

        Bucketing is `substr` on the ISO-Z string — dialect-agnostic on both
        backends, no date functions, no timezone conversion, and it slices the
        same UTC string the row was written with (Invariant #16). No
        `replace(...,' ','T')` wrapper here: unlike `schedule_executions`, this
        column is only ever written by `utc_now_iso()`, so the space-separated
        legacy form that wrapper exists for cannot occur.

        Selection uses ROW_NUMBER() rather than a bare non-aggregated column
        beside an aggregate — the latter is a SQLite-only extension that raises
        GroupingError on PostgreSQL.

        Only buckets that actually hold a sample are returned; gaps are gaps.
        The caller pairs each row's real `fetched_at` with its logical
        `bucket_start` so a consumer can tell a gap from sample jitter.
        """
        width = 13 if bucket == "hour" else 10  # YYYY-MM-DDTHH | YYYY-MM-DD
        key = func.substr(subscription_headroom_history.c.fetched_at, 1, width)
        ranked = (
            select(
                key.label("bucket"),
                subscription_headroom_history.c.fetched_at,
                subscription_headroom_history.c.status,
                subscription_headroom_history.c.five_hour_utilization_pct,
                subscription_headroom_history.c.five_hour_resets_at,
                subscription_headroom_history.c.five_hour_status,
                subscription_headroom_history.c.seven_day_utilization_pct,
                subscription_headroom_history.c.seven_day_resets_at,
                subscription_headroom_history.c.seven_day_status,
                subscription_headroom_history.c.representative_claim,
                subscription_headroom_history.c.overage_status,
                subscription_headroom_history.c.unified_status,
                func.row_number()
                .over(
                    partition_by=key,
                    order_by=subscription_headroom_history.c.fetched_at.desc(),
                )
                .label("rn"),
                func.count()
                .over(partition_by=key)
                .label("samples"),
            )
            .where(
                and_(
                    subscription_headroom_history.c.subscription_id == subscription_id,
                    subscription_headroom_history.c.fetched_at > iso_cutoff(hours),
                )
            )
            .subquery()
        )
        stmt = (
            select(ranked)
            .where(ranked.c.rn == 1)
            .order_by(ranked.c.bucket.asc())
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                # Logical bucket key, normalised to a full ISO-Z instant. This
                # is what makes gap detection decidable client-side: a real
                # timestamp alone cannot distinguish sample jitter from a
                # missing bucket.
                "bucket_start": (
                    f"{r['bucket']}:00:00Z" if bucket == "hour"
                    else f"{r['bucket']}T00:00:00Z"
                ),
                "fetched_at": r["fetched_at"],
                "status": r["status"],
                "samples": int(r["samples"]),
                "five_hour": {
                    "utilization_pct": r["five_hour_utilization_pct"],
                    "resets_at": r["five_hour_resets_at"],
                    "status": r["five_hour_status"],
                },
                "seven_day": {
                    "utilization_pct": r["seven_day_utilization_pct"],
                    "resets_at": r["seven_day_resets_at"],
                    "status": r["seven_day_status"],
                },
                "representative_claim": r["representative_claim"],
                "overage_status": r["overage_status"],
                "unified_status": r["unified_status"],
            })
        return out

    def count_headroom_history_candidates(
        self, retention_days: int, limit: int
    ) -> int:
        """Bounded candidate count for the #1644 blast-radius guard (ent#433).

        Shares `_headroom_history_prune_predicate` with the prune BY
        CONSTRUCTION — a guard that counts a different row set than the one
        about to be deleted protects nothing.
        """
        if retention_days <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        sub = (
            select(subscription_headroom_history.c.id)
            .where(_headroom_history_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(conn.execute(select(func.count()).select_from(sub)).scalar_one())

    def prune_headroom_history(
        self, retention_days: int = 30, chunk_size: int = 1000
    ) -> int:
        """Delete headroom history older than ``retention_days`` (ent#433).

        Chunked DELETE (mirrors `prune_agent_reports`) so a large table doesn't
        hold the write lock for the full purge. `0` disables the sweep.
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
                        select(subscription_headroom_history.c.id)
                        .where(_headroom_history_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                total += conn.execute(
                    delete(subscription_headroom_history).where(
                        subscription_headroom_history.c.id.in_(ids)
                    )
                ).rowcount
            if len(ids) < chunk_size:
                break
        return total

    def count_rate_limit_event_candidates(
        self, retention_days: int, limit: int
    ) -> int:
        """Bounded candidate count for the failure-event sweep guard (ent#433).

        Shares `_rate_limit_event_prune_predicate` with the prune, same
        constraint as the headroom counterpart above.
        """
        if retention_days <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        sub = (
            select(subscription_rate_limit_events.c.id)
            .where(_rate_limit_event_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(conn.execute(select(func.count()).select_from(sub)).scalar_one())

    def get_agent_subscription_map(
        self, agent_names: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """#471 — batch: each (live) agent's subscription binding + API-key flag.

        Pure-DB (never Docker), one query. ``agent_names=None`` = the whole
        fleet (admin); otherwise scoped to the given names. The auth-mode enum
        derivation from these fields lives in
        ``subscription_service.derive_auth_mode`` — reuse it, never re-derive.
        """
        stmt = (
            select(
                agent_ownership.c.agent_name,
                agent_ownership.c.subscription_id,
                agent_ownership.c.use_platform_api_key,
                subscription_credentials.c.name.label("subscription_name"),
            )
            .select_from(
                agent_ownership.outerjoin(
                    subscription_credentials,
                    agent_ownership.c.subscription_id == subscription_credentials.c.id,
                )
            )
            .where(agent_ownership.c.deleted_at.is_(None))
        )
        if agent_names is not None:
            names = list(agent_names)
            if not names:
                return {}
            stmt = stmt.where(agent_ownership.c.agent_name.in_(names))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {
            r["agent_name"]: {
                "subscription_id": r["subscription_id"],
                "subscription_name": r["subscription_name"],
                "use_platform_api_key": bool(r["use_platform_api_key"]),
            }
            for r in rows
        }

    def get_subscription_usage_breakdown(self, subscription_id: str):
        """#471 Tier 2 — per-agent consumption for both windows, ranked by cost.

        GROUP BY agent_name over chat_messages + schedule_executions (the same
        two sources SUB-004 aggregates), merged per agent, ordered by
        ``cost_usd`` desc — cost is model-weighted by construction, so it is
        the honest "who burns the quota" ranking on a mixed-model subscription.
        Uses idx_chat_messages_subscription / idx_executions_subscription.
        """
        from db_models import SubscriptionUsageBreakdown, SubscriptionUsageBreakdownRow

        def _window_rows(conn, cutoff: str):
            # Same dedup rule as get_subscription_usage's _query_window (#471):
            # executions = sole source for cost/context/turns; chat side
            # contributes output_tokens only (a /chat or persisted-/task turn
            # writes cost into BOTH tables — summing both double-counts it).
            chat_rows = conn.execute(
                select(
                    chat_messages.c.agent_name,
                    func.coalesce(func.sum(chat_messages.c.output_tokens), 0).label("output_tokens"),
                )
                .where(
                    and_(
                        chat_messages.c.subscription_id == subscription_id,
                        chat_messages.c.role == "assistant",
                        chat_messages.c.timestamp >= cutoff,
                    )
                )
                .group_by(chat_messages.c.agent_name)
            ).mappings().all()
            exec_rows = conn.execute(
                select(
                    schedule_executions.c.agent_name,
                    func.coalesce(func.sum(schedule_executions.c.context_used), 0).label("input_tokens"),
                    func.coalesce(func.sum(schedule_executions.c.cost), 0.0).label("cost_usd"),
                    func.count().label("exec_count"),
                )
                .where(
                    and_(
                        schedule_executions.c.subscription_id == subscription_id,
                        schedule_executions.c.started_at >= cutoff,
                        schedule_executions.c.status.notin_(["running", "pending"]),
                    )
                )
                .group_by(schedule_executions.c.agent_name)
            ).mappings().all()

            merged: Dict[str, SubscriptionUsageBreakdownRow] = {}
            for r in exec_rows:
                merged[r["agent_name"]] = SubscriptionUsageBreakdownRow(
                    agent_name=r["agent_name"],
                    input_tokens=int(r["input_tokens"] or 0),
                    cost_usd=float(r["cost_usd"] or 0.0),
                    message_count=int(r["exec_count"] or 0),
                )
            for r in chat_rows:
                row = merged.setdefault(
                    r["agent_name"],
                    SubscriptionUsageBreakdownRow(agent_name=r["agent_name"]),
                )
                row.output_tokens += int(r["output_tokens"] or 0)
            return sorted(merged.values(), key=lambda r: r.cost_usd, reverse=True)

        with get_engine().connect() as conn:
            return SubscriptionUsageBreakdown(
                subscription_id=subscription_id,
                window_5h=_window_rows(conn, iso_cutoff(5)),
                window_7d=_window_rows(conn, iso_cutoff(168)),
            )

    def _list_unfailed_subscriptions(self, exclude_id: Optional[str] = None) -> List[SubscriptionCredential]:
        """Subscriptions that have NOT failed in the 2h window, in load-balance
        order (`agent_count ASC, name ASC`), optionally excluding one id.

        The shared body of the two candidate listings below. Kind-BLIND by
        design (#2352): the display predicate `is_subscription_rate_limited`
        was narrowed to real 429s so a dead token stops reading as quota
        exhaustion; candidate selection must NOT inherit that narrowing, or
        agents get moved onto subscriptions the platform just watched fail to
        authenticate. See #444 for what a forgetful candidate filter does.
        """
        agent_count = self._agent_count_subquery()
        stmt = (
            select(*self._subscription_select_columns(), agent_count)
            .select_from(
                subscription_credentials.join(
                    users, subscription_credentials.c.owner_id == users.c.id
                )
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(subscription_credentials.c.id != exclude_id)
        # The name tiebreak makes this order deterministic (SQLite leaves ties
        # unspecified) — it is what the #2409 ranker degrades to when no
        # headroom reading is usable, so "today's order" has to BE an order.
        stmt = stmt.order_by(agent_count.asc(), subscription_credentials.c.name.asc())
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        subs = [self._row_to_subscription(row) for row in rows]
        return [sub for sub in subs if not self.has_recent_subscription_failures(sub.id)]

    def list_assignable_subscriptions(self) -> List[SubscriptionCredential]:
        """The new-agent auto-assign candidate list (#74): every subscription
        that has NOT failed recently, load-balance order.

        FILTER ONLY. Which candidate is best — and whether its token still
        decrypts (#340) — is decided by
        `services.subscription_service.select_subscription_for_new_agent`
        over the cached provider headroom (#2409); this layer holds no Redis
        edge (Invariant #1).
        """
        return self._list_unfailed_subscriptions()

    def list_viable_alternative_subscriptions(
        self, current_subscription_id: str
    ) -> List[SubscriptionCredential]:
        """The auto-switch candidate list (SUB-003): every subscription except
        the current one that has NOT failed recently, load-balance order.

        FILTER ONLY, never a ranking — it used to be both (first survivor
        wins), which is how an agent got moved onto a subscription at 99% of
        its weekly window, and how an UNUSED dead-token subscription (no
        agents ⇒ no failure rows) sorted first. Which survivor is best is
        decided by `services.subscription_auto_switch.
        select_best_alternative_subscription` over the cached provider
        headroom (#2409); this layer holds no Redis edge (Invariant #1). An
        empty list means what `None` used to: no viable alternative.
        """
        return self._list_unfailed_subscriptions(exclude_id=current_subscription_id)

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
        # #471 boil-lake: iso_cutoff() per Invariant #16 — the naive
        # datetime.utcnow().isoformat() cutoffs compared a Z-less string
        # against Z-suffixed stored timestamps (sub-second edge only, but
        # the wrong idiom to copy).
        cutoff_5h = iso_cutoff(5)
        cutoff_7d = iso_cutoff(168)

        with get_engine().connect() as conn:

            def _query_window(cutoff: str) -> SubscriptionUsageWindow:
                # #471 dedup: a modern turn writes BOTH a schedule_executions
                # row AND (for /chat + persisted /task) a cost-bearing
                # chat_messages row — verified live: one sync-chat turn counted
                # its cost twice under the old chat+exec SUM. Executions are
                # the canonical run record (status-as-projection #1082), so
                # they are the SOLE source for cost / context-est. / turn
                # count; chat_messages contributes ONLY output_tokens, which
                # executions do not carry. (Pre-#1483 chat rows without a
                # paired execution fall outside any rolling window by now.)
                chat_row = conn.execute(
                    select(
                        func.coalesce(func.sum(chat_messages.c.output_tokens), 0).label("output_tokens"),
                    ).where(
                        and_(
                            chat_messages.c.subscription_id == subscription_id,
                            chat_messages.c.role == "assistant",
                            chat_messages.c.timestamp >= cutoff,
                        )
                    )
                ).mappings().first()

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
                    input_tokens=int(exec_row["input_tokens"] or 0),
                    output_tokens=int(chat_row["output_tokens"] or 0),
                    cost_usd=float(exec_row["cost_usd"] or 0.0),
                    message_count=int(exec_row["exec_count"] or 0),
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

        # #471: failure-event counters + the DB half of `rate_limited_now`
        # (the 2h predicate SUB-003's own machinery uses). The headroom
        # service may OR-in a fresh provider verdict and attach `headroom`/
        # `source` — see subscription_headroom_service.decorate_usage, the
        # ONE place the final derivation lives.
        counts = self.get_failure_event_counts(subscription_id, hours=24)
        return SubscriptionUsage(
            subscription_id=subscription_id,
            window_5h=window_5h,
            window_7d=window_7d,
            agents=agents,
            failure_events_24h=counts["total"],
            failure_events_by_kind=counts["by_kind"],
            rate_limited_now=self.is_subscription_rate_limited(subscription_id),
        )
