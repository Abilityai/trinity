"""
MCP API key management database operations.

Handles creation, validation, and revocation of MCP API keys.
Supports both user-scoped and agent-scoped keys for agent-to-agent collaboration.
"""

import secrets
import hashlib
from datetime import datetime
from typing import Optional, List, Dict

from .connection import get_db_connection
from db_models import McpApiKey, McpApiKeyCreate, McpApiKeyWithSecret
from utils.helpers import utc_now_iso


class McpKeyOperations:
    """MCP API key database operations."""

    def __init__(self, user_ops):
        """Initialize with reference to user operations for lookups."""
        self._user_ops = user_ops

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)

    @staticmethod
    def _generate_mcp_api_key() -> str:
        """Generate a new MCP API key with prefix."""
        return f"trinity_mcp_{secrets.token_urlsafe(32)}"

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        """Hash an API key for secure storage."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def _row_to_mcp_api_key(row) -> McpApiKey:
        """Convert an mcp_api_keys row to a McpApiKey model."""
        # Handle new columns with backwards compatibility
        row_keys = row.keys()
        agent_name = row["agent_name"] if "agent_name" in row_keys else None
        scope = row["scope"] if "scope" in row_keys and row["scope"] else "user"

        return McpApiKey(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            key_prefix=row["key_prefix"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None,
            usage_count=row["usage_count"],
            is_active=bool(row["is_active"]),
            user_id=row["user_id"],
            username=row["username"],
            user_email=row["email"],
            agent_name=agent_name,
            scope=scope
        )

    def create_mcp_api_key(self, username: str, key_data: McpApiKeyCreate) -> Optional[McpApiKeyWithSecret]:
        """Create a new MCP API key for a user (scope: user)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        key_id = self._generate_id()
        api_key = self._generate_mcp_api_key()
        key_hash = self._hash_api_key(api_key)
        now = utc_now_iso()

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO mcp_api_keys (id, name, description, key_prefix, key_hash, created_at, user_id, agent_name, scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'user')
            """, (
                key_id,
                key_data.name,
                key_data.description,
                api_key[:20],
                key_hash,
                now,
                user["id"]
            ))
            conn.commit()

        return McpApiKeyWithSecret(
            id=key_id,
            name=key_data.name,
            description=key_data.description,
            key_prefix=api_key[:20],
            created_at=datetime.fromisoformat(now),
            last_used_at=None,
            usage_count=0,
            is_active=True,
            user_id=user["id"],
            username=username,
            user_email=user.get("email"),
            agent_name=None,
            scope="user",
            api_key=api_key
        )

    def create_agent_mcp_api_key(self, agent_name: str, owner_username: str, description: Optional[str] = None) -> Optional[McpApiKeyWithSecret]:
        """
        Create an agent-scoped MCP API key for agent-to-agent collaboration.

        Args:
            agent_name: Name of the agent that will use this key
            owner_username: Username of the agent owner
            description: Optional description

        Returns:
            McpApiKeyWithSecret with the full API key (only returned once)
        """
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return None

        key_id = self._generate_id()
        api_key = self._generate_mcp_api_key()
        key_hash = self._hash_api_key(api_key)
        now = utc_now_iso()
        key_name = f"agent-{agent_name}-key"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO mcp_api_keys (id, name, description, key_prefix, key_hash, created_at, user_id, agent_name, scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'agent')
            """, (
                key_id,
                key_name,
                description or f"Auto-generated key for agent {agent_name}",
                api_key[:20],
                key_hash,
                now,
                user["id"],
                agent_name
            ))
            conn.commit()

        return McpApiKeyWithSecret(
            id=key_id,
            name=key_name,
            description=description or f"Auto-generated key for agent {agent_name}",
            key_prefix=api_key[:20],
            created_at=datetime.fromisoformat(now),
            last_used_at=None,
            usage_count=0,
            is_active=True,
            user_id=user["id"],
            username=owner_username,
            user_email=user.get("email"),
            agent_name=agent_name,
            scope="agent",
            api_key=api_key
        )

    def get_agent_mcp_api_key(self, agent_name: str) -> Optional[McpApiKey]:
        """Get the MCP API key for an agent (does not return the secret)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT k.*, u.username, u.email
                FROM mcp_api_keys k
                JOIN users u ON k.user_id = u.id
                WHERE k.agent_name = ? AND k.scope = 'agent' AND k.is_active = 1
                ORDER BY k.created_at DESC
                LIMIT 1
            """, (agent_name,))
            row = cursor.fetchone()
            return self._row_to_mcp_api_key(row) if row else None

    def delete_agent_mcp_api_key(self, agent_name: str) -> bool:
        """Delete all MCP API keys for an agent (called when agent is deleted)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM mcp_api_keys
                WHERE agent_name = ? AND scope = 'agent'
            """, (agent_name,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted

    def validate_mcp_api_key(
        self, api_key: str, *, track_usage: bool = True
    ) -> Optional[Dict]:
        """Validate an MCP API key and return user/agent info if valid.

        Args:
            api_key: The raw MCP API key to validate.
            track_usage: When True (default) the call bumps ``last_used_at`` /
                ``usage_count`` as before. High-frequency, low-value callers
                (the agent heartbeat — #307) pass ``False`` to validate without
                amplifying the usage counter or writing to SQLite on every beat.

        Returns:
            Dict with key info including:
            - key_id, key_name: Key identifiers
            - user_id, user_email: Owner info (username for backward compat)
            - agent_name: Agent name if scope is 'agent', else None
            - scope: 'user' or 'agent'
        """
        key_hash = self._hash_api_key(api_key)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT k.id, k.name, k.user_id, k.is_active, k.agent_name, k.scope,
                       u.username, u.email
                FROM mcp_api_keys k
                JOIN users u ON k.user_id = u.id
                WHERE k.key_hash = ?
            """, (key_hash,))
            row = cursor.fetchone()

            if not row:
                return None

            if not row["is_active"]:
                return None

            # Update usage statistics. Skipped for high-frequency, low-value
            # callers (heartbeat — #307) so a 5s beat doesn't amplify the
            # counter or write to SQLite ~12x/min/agent.
            if track_usage:
                now = utc_now_iso()
                cursor.execute("""
                    UPDATE mcp_api_keys
                    SET last_used_at = ?, usage_count = usage_count + 1
                    WHERE id = ?
                """, (now, row["id"]))
                conn.commit()

            # Include agent collaboration fields
            return {
                "key_id": row["id"],
                "key_name": row["name"],
                "user_id": row["username"],  # Return username for backward compat
                "user_email": row["email"],
                "agent_name": row["agent_name"],  # Agent name if scope is 'agent'
                "scope": row["scope"] or "user"  # 'user' or 'agent'
            }

    def get_mcp_api_key(self, key_id: str, username: str) -> Optional[McpApiKey]:
        """Get MCP API key metadata."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return None

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT k.*, u.username, u.email
                FROM mcp_api_keys k
                JOIN users u ON k.user_id = u.id
                WHERE k.id = ? AND k.user_id = ?
            """, (key_id, user["id"]))
            row = cursor.fetchone()
            return self._row_to_mcp_api_key(row) if row else None

    def list_mcp_api_keys(self, username: str) -> List[McpApiKey]:
        """List all MCP API keys for a user."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return []

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT k.*, u.username, u.email
                FROM mcp_api_keys k
                JOIN users u ON k.user_id = u.id
                WHERE k.user_id = ?
                ORDER BY k.created_at DESC
            """, (user["id"],))
            return [self._row_to_mcp_api_key(row) for row in cursor.fetchall()]

    def list_all_mcp_api_keys(self) -> List[McpApiKey]:
        """List all MCP API keys (admin only)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT k.*, u.username, u.email
                FROM mcp_api_keys k
                JOIN users u ON k.user_id = u.id
                ORDER BY k.created_at DESC
            """)
            return [self._row_to_mcp_api_key(row) for row in cursor.fetchall()]

    def revoke_mcp_api_key(self, key_id: str, username: str) -> bool:
        """Revoke (deactivate) an MCP API key."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check ownership (unless admin)
            if user["role"] != "admin":
                cursor.execute("SELECT user_id FROM mcp_api_keys WHERE id = ?", (key_id,))
                row = cursor.fetchone()
                if not row or row["user_id"] != user["id"]:
                    return False

            cursor.execute("UPDATE mcp_api_keys SET is_active = 0 WHERE id = ?", (key_id,))
            conn.commit()
            return cursor.rowcount > 0

    def delete_mcp_api_key(self, key_id: str, username: str) -> bool:
        """Permanently delete an MCP API key."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check ownership (unless admin)
            if user["role"] != "admin":
                cursor.execute("SELECT user_id FROM mcp_api_keys WHERE id = ?", (key_id,))
                row = cursor.fetchone()
                if not row or row["user_id"] != user["id"]:
                    return False

            cursor.execute("DELETE FROM mcp_api_keys WHERE id = ?", (key_id,))
            conn.commit()
            return cursor.rowcount > 0
