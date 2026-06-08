"""
Agent security settings database operations.

Handles full capabilities (container security), read-only mode, and
guardrails configuration (GUARD-001).
"""

import json
from typing import Optional

from db.connection import get_db_connection


class SecurityMixin:
    """Mixin for agent security settings (full capabilities, read-only mode)."""

    # =========================================================================
    # Full Capabilities (Container Security)
    # =========================================================================

    def get_full_capabilities(self, agent_name: str) -> bool:
        """
        Get full_capabilities setting for an agent.
        When True, container runs with Docker default capabilities (apt-get works).
        When False (default), container runs with restricted capabilities (secure).

        Args:
            agent_name: Name of the agent

        Returns:
            True if full capabilities enabled, False otherwise
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT full_capabilities FROM agent_ownership
                WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return bool(row[0])
            return False

    def set_full_capabilities(self, agent_name: str, enabled: bool) -> bool:
        """
        Set full_capabilities setting for an agent.
        Requires container restart to take effect.

        Args:
            agent_name: Name of the agent
            enabled: True for Docker default capabilities, False for restricted

        Returns:
            True if update succeeded, False otherwise
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE agent_ownership SET full_capabilities = ?
                WHERE agent_name = ?
            """, (1 if enabled else 0, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Read-Only Mode
    # =========================================================================

    def get_read_only_mode(self, agent_name: str) -> dict:
        """
        Get read-only mode status and configuration for an agent.

        Returns:
            dict with 'enabled' (bool) and 'config' (dict or None)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(read_only_mode, 0) as read_only_mode, read_only_config
                FROM agent_ownership
                WHERE agent_name = ? AND deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                import json
                config = None
                if row["read_only_config"]:
                    try:
                        config = json.loads(row["read_only_config"])
                    except json.JSONDecodeError:
                        config = None
                return {
                    "enabled": bool(row["read_only_mode"]),
                    "config": config
                }
            return {"enabled": False, "config": None}

    def set_read_only_mode(self, agent_name: str, enabled: bool, config: Optional[dict] = None) -> bool:
        """
        Set read-only mode status and configuration for an agent.

        Args:
            agent_name: Name of the agent
            enabled: True to enable read-only mode
            config: Optional dict with 'blocked_patterns' and 'allowed_patterns' lists

        Returns:
            True if update succeeded
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            config_json = json.dumps(config) if config else None
            cursor.execute("""
                UPDATE agent_ownership SET read_only_mode = ?, read_only_config = ?
                WHERE agent_name = ?
            """, (1 if enabled else 0, config_json, agent_name))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Guardrails (GUARD-001)
    # =========================================================================

    def get_guardrails_config(self, agent_name: str) -> dict:
        """Return per-agent guardrails overrides, or {} if none set."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT guardrails_config FROM agent_ownership "
                "WHERE agent_name = ? AND deleted_at IS NULL",
                (agent_name,),
            )
            row = cursor.fetchone()
            if row and row["guardrails_config"]:
                try:
                    return json.loads(row["guardrails_config"])
                except json.JSONDecodeError:
                    return {}
            return {}

    def set_guardrails_config(self, agent_name: str, config: Optional[dict]) -> bool:
        """Store per-agent guardrails overrides as a JSON blob. None clears."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            payload = json.dumps(config) if config else None
            cursor.execute(
                "UPDATE agent_ownership SET guardrails_config = ? WHERE agent_name = ?",
                (payload, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
