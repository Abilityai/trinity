"""
Agent metadata batch queries and rename operations.

Handles N+1 query optimization and cross-table agent rename.
"""

import sqlite3
from typing import List, Dict

from db.connection import get_db_connection


class MetadataMixin:
    """Mixin for batch metadata queries and agent rename operations."""

    # =========================================================================
    # Batch Metadata Query (N+1 Fix)
    # =========================================================================

    def get_accessible_agent_names(self, user_email: str, is_admin: bool = False) -> List[str]:
        """
        Get list of agent names the user can access.

        Used by /ws/events endpoint to filter events to user's accessible agents.

        Args:
            user_email: User's email address
            is_admin: True if user is admin (sees all agents)

        Returns:
            List of agent names the user can access (owned + shared, or all if admin)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            if is_admin:
                # Admin sees all live agents. Soft-deleted agents (#834)
                # are excluded — admins recover via the dedicated admin
                # endpoint, not via the user-facing accessible list.
                cursor.execute(
                    "SELECT agent_name FROM agent_ownership WHERE deleted_at IS NULL"
                )
                return [row["agent_name"] for row in cursor.fetchall()]

            # Get owned + shared agents. agent_sharing rows for a
            # soft-deleted agent are filtered out via the join to
            # agent_ownership; without it, a shared-with user would see
            # the deleted agent's name in their accessible list.
            cursor.execute("""
                SELECT DISTINCT agent_name FROM (
                    SELECT ao.agent_name FROM agent_ownership ao
                    JOIN users u ON ao.owner_id = u.id
                    WHERE LOWER(u.email) = LOWER(?) AND ao.deleted_at IS NULL
                    UNION
                    SELECT s.agent_name FROM agent_sharing s
                    JOIN agent_ownership ao2 ON ao2.agent_name = s.agent_name
                    WHERE LOWER(s.shared_with_email) = LOWER(?) AND ao2.deleted_at IS NULL
                )
            """, (user_email, user_email))
            return [row["agent_name"] for row in cursor.fetchall()]

    # =========================================================================
    # Agent Rename (RENAME-001)
    # =========================================================================

    def rename_agent(self, old_name: str, new_name: str) -> bool:
        """
        Rename an agent by updating all database references.

        This updates agent_name in all tables that reference it:
        - agent_ownership (primary)
        - agent_sharing
        - agent_schedules
        - schedule_executions
        - chat_sessions
        - chat_messages
        - agent_activities
        - agent_permissions (source and target)
        - agent_shared_folder_config
        - agent_git_config
        - agent_skills
        - agent_tags
        - agent_public_links
        - mcp_api_keys
        - agent_health_checks
        - agent_dashboard_values
        - monitoring_alert_cooldowns

        Args:
            old_name: Current agent name
            new_name: New agent name (must be unique)

        Returns:
            True if rename succeeded, False if failed
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                # Check if old agent exists AND is live (not soft-deleted).
                # Renaming a soft-deleted agent is meaningless; the row
                # is on its way out.
                cursor.execute(
                    "SELECT 1 FROM agent_ownership "
                    "WHERE agent_name = ? AND deleted_at IS NULL",
                    (old_name,),
                )
                if not cursor.fetchone():
                    return False

                # Check if new name is already taken. Intentionally does
                # NOT filter `deleted_at IS NULL` — soft-deleted rows
                # still reserve the name during the retention window
                # (#834 acceptance criterion: "Agent name remains
                # reserved for the retention period").
                cursor.execute("SELECT 1 FROM agent_ownership WHERE agent_name = ?", (new_name,))
                if cursor.fetchone():
                    return False

                # Update all tables in order
                # Primary table
                cursor.execute(
                    "UPDATE agent_ownership SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Sharing
                cursor.execute(
                    "UPDATE agent_sharing SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Schedules
                cursor.execute(
                    "UPDATE agent_schedules SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Executions
                cursor.execute(
                    "UPDATE schedule_executions SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Chat sessions
                cursor.execute(
                    "UPDATE chat_sessions SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Chat messages
                cursor.execute(
                    "UPDATE chat_messages SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Activities
                cursor.execute(
                    "UPDATE agent_activities SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Permissions (both source and target)
                cursor.execute(
                    "UPDATE agent_permissions SET source_agent = ? WHERE source_agent = ?",
                    (new_name, old_name)
                )
                cursor.execute(
                    "UPDATE agent_permissions SET target_agent = ? WHERE target_agent = ?",
                    (new_name, old_name)
                )

                # Shared folder config
                cursor.execute(
                    "UPDATE agent_shared_folder_config SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Git config
                cursor.execute(
                    "UPDATE agent_git_config SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Skills
                cursor.execute(
                    "UPDATE agent_skills SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Tags
                cursor.execute(
                    "UPDATE agent_tags SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Public links
                cursor.execute(
                    "UPDATE agent_public_links SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # MCP API keys
                cursor.execute(
                    "UPDATE mcp_api_keys SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Health checks
                cursor.execute(
                    "UPDATE agent_health_checks SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Dashboard values
                cursor.execute(
                    "UPDATE agent_dashboard_values SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Monitoring cooldowns
                cursor.execute(
                    "UPDATE monitoring_alert_cooldowns SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                # Shared files (outbound — amazing-file-outbound).
                # FK has ON UPDATE CASCADE as belt-and-suspenders; this keeps
                # the explicit cascade list complete for visibility.
                cursor.execute(
                    "UPDATE agent_shared_files SET agent_name = ? WHERE agent_name = ?",
                    (new_name, old_name)
                )

                conn.commit()
                return True

            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def can_user_rename_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can rename an agent (only owner or admin, NOT system agents)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Check if this is a system agent - NO ONE can rename system agents
        owner = self.get_agent_owner(agent_name)
        if owner and owner.get("is_system", False):
            return False

        # Admins can rename any non-system agent
        if user["role"] == "admin":
            return True

        # Only owners can rename their agents
        if owner and owner["owner_username"] == username:
            return True

        return False

    def get_all_agent_metadata(self, user_email: str = None) -> Dict[str, Dict]:
        """
        Fetch all agent metadata in a SINGLE query.

        This eliminates the N+1 query problem by joining all related tables
        and returning a dict keyed by agent_name.

        Args:
            user_email: Current user's email for checking share access

        Returns:
            Dict mapping agent_name to metadata dict containing:
            - owner_id, owner_username, owner_email
            - is_system, autonomy_enabled, use_platform_api_key
            - memory_limit, cpu_limit
            - github_repo, github_branch
            - is_shared_with_user (bool)
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Single query that joins all needed tables
            cursor.execute("""
                SELECT
                    ao.agent_name,
                    ao.owner_id,
                    u.username as owner_username,
                    u.email as owner_email,
                    COALESCE(ao.is_system, 0) as is_system,
                    COALESCE(ao.autonomy_enabled, 0) as autonomy_enabled,
                    COALESCE(ao.read_only_mode, 0) as read_only_enabled,
                    COALESCE(ao.use_platform_api_key, 1) as use_platform_api_key,
                    ao.memory_limit,
                    ao.cpu_limit,
                    ao.avatar_updated_at,
                    gc.github_repo,
                    gc.working_branch as github_branch,
                    CASE
                        WHEN s.id IS NOT NULL THEN 1
                        ELSE 0
                    END as is_shared_with_user
                FROM agent_ownership ao
                LEFT JOIN users u ON ao.owner_id = u.id
                LEFT JOIN agent_git_config gc ON gc.agent_name = ao.agent_name
                LEFT JOIN agent_sharing s ON s.agent_name = ao.agent_name
                    AND LOWER(s.shared_with_email) = LOWER(?)
                WHERE ao.deleted_at IS NULL  -- #834: exclude soft-deleted agents
            """, (user_email or '',))

            result = {}
            for row in cursor.fetchall():
                result[row["agent_name"]] = {
                    "owner_id": row["owner_id"],
                    "owner_username": row["owner_username"],
                    "owner_email": row["owner_email"],
                    "is_system": bool(row["is_system"]),
                    "autonomy_enabled": bool(row["autonomy_enabled"]),
                    "read_only_enabled": bool(row["read_only_enabled"]),
                    "use_platform_api_key": bool(row["use_platform_api_key"]),
                    "memory_limit": row["memory_limit"],
                    "cpu_limit": row["cpu_limit"],
                    "github_repo": row["github_repo"],
                    "github_branch": row["github_branch"],
                    "is_shared_with_user": bool(row["is_shared_with_user"]),
                    "avatar_updated_at": row["avatar_updated_at"],
                }

            return result
