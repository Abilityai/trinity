"""
Agent avatar identity database operations.

Handles avatar identity prompts, defaults, and custom avatar management.
"""

from typing import Optional, Dict

from db.connection import get_db_connection


class AvatarMixin:
    """Mixin for agent avatar identity operations."""

    def set_avatar_identity(self, agent_name: str, prompt: str, updated_at: str) -> bool:
        """Set avatar identity prompt and updated timestamp for an agent.
        Also clears is_default_avatar flag (custom avatar overrides default).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership
                SET avatar_identity_prompt = ?, avatar_updated_at = ?, is_default_avatar = 0
                WHERE agent_name = ?
            """, (prompt, updated_at, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def get_avatar_identity(self, agent_name: str) -> Optional[Dict]:
        """Get avatar identity prompt and metadata for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT avatar_identity_prompt, avatar_updated_at
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row and row["avatar_identity_prompt"]:
                return {
                    "identity_prompt": row["avatar_identity_prompt"],
                    "updated_at": row["avatar_updated_at"],
                }
            return None

    def clear_avatar_identity(self, agent_name: str) -> bool:
        """Clear avatar identity prompt and timestamp for an agent.
        Also clears is_default_avatar flag.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership
                SET avatar_identity_prompt = NULL, avatar_updated_at = NULL, is_default_avatar = 0
                WHERE agent_name = ?
            """, (agent_name,))
            conn.commit()
            return cursor.rowcount > 0

    def get_agents_without_custom_avatar(self) -> list:
        """Return agents that need a default avatar (no avatar or existing default).

        Returns list of dicts with agent_name for agents where:
        - avatar_updated_at IS NULL (no avatar at all), OR
        - is_default_avatar = 1 (has a default that can be overwritten)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name
                FROM agent_ownership
                WHERE (avatar_updated_at IS NULL OR is_default_avatar = 1)
                  AND deleted_at IS NULL
            """)
            return [{"agent_name": row["agent_name"]} for row in cursor.fetchall()]

    def set_default_avatar(self, agent_name: str, identity_prompt: str, updated_at: str) -> bool:
        """Set avatar as a default (auto-generated) avatar."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership
                SET avatar_identity_prompt = ?, avatar_updated_at = ?, is_default_avatar = 1
                WHERE agent_name = ?
            """, (identity_prompt, updated_at, agent_name))
            conn.commit()
            return cursor.rowcount > 0
