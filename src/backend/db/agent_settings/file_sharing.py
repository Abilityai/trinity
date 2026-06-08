"""
Agent file-sharing toggle (amazing-file-outbound, FILES-001).

Per-agent opt-in flag that controls whether the agent gets a Docker volume
mounted at /home/developer/public/. When enabled, the agent can call the
`share_file` MCP tool to mint a public download URL for files it writes there.

This mixin only manages the toggle + volume-name convention. Actual volume
creation happens in services/agent_service/crud.py mirroring the
shared-folders pattern.
"""

from db.connection import get_db_connection


class FileSharingMixin:
    """Mixin for the per-agent file-sharing opt-in toggle."""

    # =========================================================================
    # Toggle state
    # =========================================================================

    def get_file_sharing_enabled(self, agent_name: str) -> bool:
        """Whether the agent has file sharing enabled. Default: False."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(file_sharing_enabled, 0) AS file_sharing_enabled
                FROM agent_ownership WHERE agent_name = ? AND deleted_at IS NULL
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
            return bool(row["file_sharing_enabled"]) if row else False

    def set_file_sharing_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Flip the toggle. Returns True if a row was updated."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE agent_ownership SET file_sharing_enabled = ?
                WHERE agent_name = ?
                """,
                (1 if enabled else 0, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Volume / mount conventions
    # =========================================================================

    @staticmethod
    def get_public_volume_name(agent_name: str) -> str:
        """Docker volume name for the agent's publish dir."""
        return f"agent-{agent_name}-public"

    @staticmethod
    def get_public_mount_path() -> str:
        """Where the volume is mounted inside the agent container."""
        return "/home/developer/public"
