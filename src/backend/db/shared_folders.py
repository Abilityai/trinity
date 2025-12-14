"""
Shared folder database operations.

Phase 9.11: Agent Shared Folders - enables agents to expose and consume
shared folders for file-based collaboration.
"""

import sqlite3
from datetime import datetime
from typing import Optional, List

from .connection import get_db_connection
from db_models import SharedFolderConfig


class SharedFolderOperations:
    """Shared folder database operations."""

    def __init__(self, permission_ops):
        """Initialize with reference to permission operations."""
        self._permission_ops = permission_ops

    @staticmethod
    def _row_to_config(row) -> SharedFolderConfig:
        """Convert a database row to SharedFolderConfig model."""
        return SharedFolderConfig(
            agent_name=row["agent_name"],
            expose_enabled=bool(row["expose_enabled"]),
            consume_enabled=bool(row["consume_enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"])
        )

    # =========================================================================
    # Shared Folder Config CRUD Operations
    # =========================================================================

    def get_shared_folder_config(self, agent_name: str) -> Optional[SharedFolderConfig]:
        """
        Get shared folder configuration for an agent.

        Returns None if no configuration exists (defaults apply).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name, expose_enabled, consume_enabled, created_at, updated_at
                FROM agent_shared_folder_config
                WHERE agent_name = ?
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                return self._row_to_config(row)
            return None

    def upsert_shared_folder_config(
        self,
        agent_name: str,
        expose_enabled: Optional[bool] = None,
        consume_enabled: Optional[bool] = None
    ) -> SharedFolderConfig:
        """
        Create or update shared folder configuration for an agent.

        Only updates the fields that are provided (not None).
        Returns the updated configuration.
        """
        now = datetime.utcnow().isoformat()

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Check if config exists
            cursor.execute("""
                SELECT agent_name, expose_enabled, consume_enabled, created_at, updated_at
                FROM agent_shared_folder_config
                WHERE agent_name = ?
            """, (agent_name,))
            existing = cursor.fetchone()

            if existing:
                # Update existing config
                new_expose = expose_enabled if expose_enabled is not None else bool(existing["expose_enabled"])
                new_consume = consume_enabled if consume_enabled is not None else bool(existing["consume_enabled"])

                cursor.execute("""
                    UPDATE agent_shared_folder_config
                    SET expose_enabled = ?, consume_enabled = ?, updated_at = ?
                    WHERE agent_name = ?
                """, (new_expose, new_consume, now, agent_name))
                conn.commit()

                return SharedFolderConfig(
                    agent_name=agent_name,
                    expose_enabled=new_expose,
                    consume_enabled=new_consume,
                    created_at=datetime.fromisoformat(existing["created_at"]),
                    updated_at=datetime.fromisoformat(now)
                )
            else:
                # Create new config with defaults
                new_expose = expose_enabled if expose_enabled is not None else False
                new_consume = consume_enabled if consume_enabled is not None else False

                cursor.execute("""
                    INSERT INTO agent_shared_folder_config
                    (agent_name, expose_enabled, consume_enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (agent_name, new_expose, new_consume, now, now))
                conn.commit()

                return SharedFolderConfig(
                    agent_name=agent_name,
                    expose_enabled=new_expose,
                    consume_enabled=new_consume,
                    created_at=datetime.fromisoformat(now),
                    updated_at=datetime.fromisoformat(now)
                )

    def delete_shared_folder_config(self, agent_name: str) -> bool:
        """
        Delete shared folder configuration when an agent is deleted.

        Returns True if a config was deleted.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM agent_shared_folder_config
                WHERE agent_name = ?
            """, (agent_name,))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Shared Folder Discovery Operations
    # =========================================================================

    def get_agents_exposing_folders(self) -> List[str]:
        """
        Get list of all agent names that have expose_enabled=True.

        Returns list of agent names.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM agent_shared_folder_config
                WHERE expose_enabled = 1
                ORDER BY agent_name
            """)
            return [row["agent_name"] for row in cursor.fetchall()]

    def get_available_shared_folders(self, requesting_agent: str) -> List[str]:
        """
        Get list of agents whose shared folders can be mounted by the requesting agent.

        This returns agents that:
        1. Have expose_enabled=True
        2. Are different from the requesting agent
        3. The requesting agent has permission to communicate with

        Returns list of agent names.
        """
        # Get all agents exposing folders
        exposing_agents = self.get_agents_exposing_folders()

        # Filter by permission
        available = []
        for agent in exposing_agents:
            if agent != requesting_agent:
                # Check if requesting agent has permission to access this agent
                if self._permission_ops.is_permitted(requesting_agent, agent):
                    available.append(agent)

        return available

    def get_consuming_agents(self, source_agent: str) -> List[str]:
        """
        Get list of agents that have consume_enabled and permission to mount
        from the source agent.

        This is useful for understanding which agents would mount a folder
        when it's exposed.

        Returns list of agent names.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM agent_shared_folder_config
                WHERE consume_enabled = 1 AND agent_name != ?
                ORDER BY agent_name
            """, (source_agent,))
            consuming_agents = [row["agent_name"] for row in cursor.fetchall()]

        # Filter by permission (target agent must have permission to call source)
        available = []
        for agent in consuming_agents:
            if self._permission_ops.is_permitted(agent, source_agent):
                available.append(agent)

        return available

    # =========================================================================
    # Docker Volume Name Helpers
    # =========================================================================

    @staticmethod
    def get_shared_volume_name(agent_name: str) -> str:
        """
        Get the Docker volume name for an agent's shared folder.

        Format: agent-{name}-shared
        """
        return f"agent-{agent_name}-shared"

    @staticmethod
    def get_shared_mount_path(source_agent: str) -> str:
        """
        Get the mount path for a shared folder inside the consuming agent.

        Format: /home/developer/shared-in/{source_agent}
        """
        return f"/home/developer/shared-in/{source_agent}"
