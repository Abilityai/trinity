"""
Agent autonomy mode and API key settings database operations.

Handles autonomy mode (scheduled task auto-execution) and platform API key toggle.
"""

from typing import Dict

from db.connection import get_db_connection


class AutonomyMixin:
    """Mixin for agent autonomy mode and API key settings."""

    # =========================================================================
    # Agent API Key Settings
    # =========================================================================

    def get_use_platform_api_key(self, agent_name: str) -> bool:
        """Check if agent should use platform API key (default: True)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(use_platform_api_key, 1) as use_platform_api_key
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return bool(row["use_platform_api_key"])
            return True  # Default to using platform key

    def set_use_platform_api_key(self, agent_name: str, use_platform_key: bool) -> bool:
        """Set whether agent should use platform API key."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET use_platform_api_key = ?
                WHERE agent_name = ?
            """, (1 if use_platform_key else 0, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Autonomy Mode
    # =========================================================================

    def get_autonomy_enabled(self, agent_name: str) -> bool:
        """Check if autonomy mode is enabled for agent (scheduled tasks run automatically)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(autonomy_enabled, 0) as autonomy_enabled
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return bool(row["autonomy_enabled"])
            return False  # Default to disabled

    def set_autonomy_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Set whether autonomy mode is enabled for agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET autonomy_enabled = ?
                WHERE agent_name = ?
            """, (1 if enabled else 0, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    def get_all_agents_autonomy_status(self) -> Dict[str, bool]:
        """Get autonomy status for all agents (for dashboard display)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name, COALESCE(autonomy_enabled, 0) as autonomy_enabled
                FROM agent_ownership
                WHERE deleted_at IS NULL
            """)
            return {row["agent_name"]: bool(row["autonomy_enabled"]) for row in cursor.fetchall()}
