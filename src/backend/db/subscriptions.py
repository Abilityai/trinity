"""
Subscription credentials database operations (SUB-002).

Manages Claude Max/Pro subscription tokens for agents.
Tokens are generated via `claude setup-token` (~1 year lifetime) and injected
as `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers.
Subscriptions are registered once and can be assigned to multiple agents.
Tokens are encrypted using the same AES-256-GCM system as other credentials.
"""

import uuid
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from .connection import get_db_connection
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
        # Convert row to dict for safe access (sqlite3.Row doesn't have .get())
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

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check if subscription with this name already exists
            cursor.execute(
                "SELECT id FROM subscription_credentials WHERE name = ?",
                (name,)
            )
            existing = cursor.fetchone()

            if existing:
                # Update existing subscription
                subscription_id = existing["id"]
                cursor.execute("""
                    UPDATE subscription_credentials
                    SET encrypted_credentials = ?,
                        subscription_type = ?,
                        rate_limit_tier = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (encrypted, subscription_type, rate_limit_tier, now, subscription_id))
            else:
                # Create new subscription
                subscription_id = str(uuid.uuid4())
                cursor.execute("""
                    INSERT INTO subscription_credentials
                    (id, name, encrypted_credentials, subscription_type, rate_limit_tier, owner_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (subscription_id, name, encrypted, subscription_type, rate_limit_tier, owner_id, now, now))

            conn.commit()

            # Return the subscription (without agent count for now)
            cursor.execute("""
                SELECT s.*, u.email as owner_email
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                WHERE s.id = ?
            """, (subscription_id,))
            row = cursor.fetchone()

            return self._row_to_subscription(row)

    def get_subscription(self, subscription_id: str) -> Optional[SubscriptionCredential]:
        """
        Get a subscription by ID.

        Args:
            subscription_id: The subscription UUID

        Returns:
            SubscriptionCredential or None if not found
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.email as owner_email,
                    (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                WHERE s.id = ?
            """, (subscription_id,))
            row = cursor.fetchone()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.email as owner_email,
                    (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                WHERE s.name = ?
            """, (name,))
            row = cursor.fetchone()

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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, encrypted_credentials FROM subscription_credentials WHERE id = ?",
                (subscription_id,)
            )
            row = cursor.fetchone()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()

            if owner_id:
                cursor.execute("""
                    SELECT s.*, u.email as owner_email,
                        (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                    FROM subscription_credentials s
                    JOIN users u ON s.owner_id = u.id
                    WHERE s.owner_id = ?
                    ORDER BY s.name
                """, (owner_id,))
            else:
                cursor.execute("""
                    SELECT s.*, u.email as owner_email,
                        (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                    FROM subscription_credentials s
                    JOIN users u ON s.owner_id = u.id
                    ORDER BY s.name
                """)

            return [self._row_to_subscription(row) for row in cursor.fetchall()]

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
        with get_db_connection() as conn:
            cursor = conn.cursor()

            for sub in subscriptions:
                cursor.execute(
                    "SELECT agent_name FROM agent_ownership WHERE subscription_id = ? AND deleted_at IS NULL",
                    (sub.id,)
                )
                agents = [row["agent_name"] for row in cursor.fetchall()]

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
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # First clear all agent assignments
            cursor.execute(
                "UPDATE agent_ownership SET subscription_id = NULL WHERE subscription_id = ?",
                (subscription_id,)
            )
            cleared_count = cursor.rowcount

            # Then delete the subscription
            cursor.execute(
                "DELETE FROM subscription_credentials WHERE id = ?",
                (subscription_id,)
            )
            deleted = cursor.rowcount > 0

            conn.commit()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Verify subscription exists
            cursor.execute(
                "SELECT id FROM subscription_credentials WHERE id = ?",
                (subscription_id,)
            )
            if not cursor.fetchone():
                raise ValueError(f"Subscription {subscription_id} not found")

            # Update agent ownership
            cursor.execute("""
                UPDATE agent_ownership
                SET subscription_id = ?
                WHERE agent_name = ?
            """, (subscription_id, agent_name))

            if cursor.rowcount == 0:
                raise ValueError(f"Agent {agent_name} not found in ownership table")

            conn.commit()
            return True

    def clear_agent_subscription(self, agent_name: str) -> bool:
        """
        Clear subscription assignment from an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            True if cleared (even if was already null)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership
                SET subscription_id = NULL
                WHERE agent_name = ?
            """, (agent_name,))
            conn.commit()
            return True

    def get_agent_subscription(self, agent_name: str) -> Optional[SubscriptionCredential]:
        """
        Get the subscription assigned to an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            SubscriptionCredential or None if no subscription assigned
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.email as owner_email,
                    (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                JOIN agent_ownership ao ON ao.subscription_id = s.id
                WHERE ao.agent_name = ? AND ao.deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()

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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT agent_name FROM agent_ownership WHERE subscription_id = ? AND deleted_at IS NULL",
                (subscription_id,)
            )
            return [row["agent_name"] for row in cursor.fetchall()]

    def get_agent_subscription_id(self, agent_name: str) -> Optional[str]:
        """
        Get the subscription ID assigned to an agent (lightweight check).

        Args:
            agent_name: Name of the agent

        Returns:
            Subscription ID or None
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT subscription_id FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL",
                (agent_name,)
            )
            row = cursor.fetchone()
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

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO subscription_rate_limit_events
                (id, agent_name, subscription_id, error_message, occurred_at)
                VALUES (?, ?, ?, ?, ?)
            """, (event_id, agent_name, subscription_id, error_message, now))
            conn.commit()

            # Count consecutive events in last 2 hours (#476: iso_cutoff,
            # not datetime('now', ...), so the format matches occurred_at)
            cursor.execute("""
                SELECT COUNT(*) as cnt
                FROM subscription_rate_limit_events
                WHERE agent_name = ? AND subscription_id = ?
                  AND occurred_at > ?
            """, (agent_name, subscription_id, iso_cutoff(2)))
            return cursor.fetchone()["cnt"]

    def is_subscription_rate_limited(self, subscription_id: str) -> bool:
        """Check if a subscription has been rate-limited in the last 2 hours."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as cnt
                FROM subscription_rate_limit_events
                WHERE subscription_id = ?
                  AND occurred_at > ?
            """, (subscription_id, iso_cutoff(2)))
            return cursor.fetchone()["cnt"] > 0

    def clear_rate_limit_events(self, agent_name: str, subscription_id: str) -> None:
        """Clear rate-limit events for an (agent, subscription) pair after successful switch."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM subscription_rate_limit_events
                WHERE agent_name = ? AND subscription_id = ?
            """, (agent_name, subscription_id))
            conn.commit()

    def cleanup_old_rate_limit_events(self) -> int:
        """Remove rate-limit tracking records older than 24 hours. Returns count deleted."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM subscription_rate_limit_events
                WHERE occurred_at < ?
            """, (iso_cutoff(24),))
            count = cursor.rowcount
            conn.commit()
            return count

    def get_least_used_subscription(self) -> Optional[SubscriptionCredential]:
        """
        Get subscription with fewest assigned agents (round-robin).

        Tie-break: alphabetical by name.
        Skips subscriptions that are currently rate-limited or have invalid tokens.
        Used for auto-assignment on agent creation (#74).

        Returns:
            SubscriptionCredential with fewest agents, or None if no viable subscription
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.*, u.email as owner_email,
                    (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                ORDER BY agent_count ASC, s.name ASC
            """)
            for row in cursor.fetchall():
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
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Get all subscriptions except current, ordered by agent count (ascending)
            cursor.execute("""
                SELECT s.*, u.email as owner_email,
                    (SELECT COUNT(*) FROM agent_ownership WHERE subscription_id = s.id AND deleted_at IS NULL) as agent_count
                FROM subscription_credentials s
                JOIN users u ON s.owner_id = u.id
                WHERE s.id != ?
                ORDER BY agent_count ASC
            """, (current_subscription_id,))

            for row in cursor.fetchall():
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

        with get_db_connection() as conn:
            cursor = conn.cursor()

            def _query_window(cutoff: str) -> SubscriptionUsageWindow:
                # Chat messages: input tokens stored in context_used, output in output_tokens
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(context_used), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(cost), 0.0) AS cost_usd,
                        COUNT(*) AS message_count
                    FROM chat_messages
                    WHERE subscription_id = ?
                      AND role = 'assistant'
                      AND timestamp >= ?
                """, (subscription_id, cutoff))
                chat_row = cursor.fetchone()

                # Schedule executions: input tokens in context_used (no separate output_tokens)
                cursor.execute("""
                    SELECT
                        COALESCE(SUM(context_used), 0) AS input_tokens,
                        COALESCE(SUM(cost), 0.0) AS cost_usd,
                        COUNT(*) AS exec_count
                    FROM schedule_executions
                    WHERE subscription_id = ?
                      AND started_at >= ?
                      AND status NOT IN ('running', 'pending')
                """, (subscription_id, cutoff))
                exec_row = cursor.fetchone()

                return SubscriptionUsageWindow(
                    input_tokens=int(chat_row["input_tokens"] or 0) + int(exec_row["input_tokens"] or 0),
                    output_tokens=int(chat_row["output_tokens"] or 0),
                    cost_usd=float(chat_row["cost_usd"] or 0.0) + float(exec_row["cost_usd"] or 0.0),
                    message_count=int(chat_row["message_count"] or 0) + int(exec_row["exec_count"] or 0),
                )

            window_5h = _query_window(cutoff_5h)
            window_7d = _query_window(cutoff_7d)

            # Currently-assigned agents (live assignment, not historical)
            cursor.execute(
                "SELECT agent_name FROM agent_ownership WHERE subscription_id = ? AND deleted_at IS NULL",
                (subscription_id,)
            )
            agents = [row["agent_name"] for row in cursor.fetchall()]

        return SubscriptionUsage(
            subscription_id=subscription_id,
            window_5h=window_5h,
            window_7d=window_7d,
            agents=agents,
        )
