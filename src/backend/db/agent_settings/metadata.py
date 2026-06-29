"""
Agent metadata batch queries and rename operations.

Handles N+1 query optimization and cross-table agent rename.
"""

from typing import List, Dict

from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError

from ..engine import get_engine
from ..tables import (
    agent_ownership,
    agent_sharing,
    agent_schedules,
    schedule_executions,
    chat_sessions,
    chat_messages,
    agent_activities,
    agent_permissions,
    agent_shared_folder_config,
    agent_git_config,
    agent_skills,
    agent_tags,
    agent_public_links,
    mcp_api_keys,
    agent_health_checks,
    agent_dashboard_values,
    monitoring_alert_cooldowns,
    agent_shared_files,
    agent_reports,
)


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
        if is_admin:
            # Admin sees all live agents. Soft-deleted agents (#834)
            # are excluded — admins recover via the dedicated admin
            # endpoint, not via the user-facing accessible list.
            stmt = select(agent_ownership.c.agent_name).where(
                agent_ownership.c.deleted_at.is_(None)
            )
            with get_engine().connect() as conn:
                return [row["agent_name"] for row in conn.execute(stmt).mappings()]

        # Get owned + shared agents. agent_sharing rows for a
        # soft-deleted agent are filtered out via the join to
        # agent_ownership; without it, a shared-with user would see
        # the deleted agent's name in their accessible list.
        stmt = text("""
            SELECT DISTINCT agent_name FROM (
                SELECT ao.agent_name FROM agent_ownership ao
                JOIN users u ON ao.owner_id = u.id
                WHERE LOWER(u.email) = LOWER(:user_email) AND ao.deleted_at IS NULL
                UNION
                SELECT s.agent_name FROM agent_sharing s
                JOIN agent_ownership ao2 ON ao2.agent_name = s.agent_name
                WHERE LOWER(s.shared_with_email) = LOWER(:user_email) AND ao2.deleted_at IS NULL
            ) AS accessible
        """)
        with get_engine().connect() as conn:
            return [
                row["agent_name"]
                for row in conn.execute(stmt, {"user_email": user_email}).mappings()
            ]

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
        - agent_shared_files
        - agent_reports

        Args:
            old_name: Current agent name
            new_name: New agent name (must be unique)

        Returns:
            True if rename succeeded, False if failed
        """
        with get_engine().begin() as conn:
            try:
                # Check if old agent exists AND is live (not soft-deleted).
                # Renaming a soft-deleted agent is meaningless; the row
                # is on its way out.
                exists = conn.execute(
                    select(1).where(
                        agent_ownership.c.agent_name == old_name,
                        agent_ownership.c.deleted_at.is_(None),
                    )
                ).first()
                if not exists:
                    return False

                # Check if new name is already taken. Intentionally does
                # NOT filter `deleted_at IS NULL` — soft-deleted rows
                # still reserve the name during the retention window
                # (#834 acceptance criterion: "Agent name remains
                # reserved for the retention period").
                taken = conn.execute(
                    select(1).where(agent_ownership.c.agent_name == new_name)
                ).first()
                if taken:
                    return False

                # Update all tables in order
                # Primary table
                conn.execute(
                    update(agent_ownership)
                    .where(agent_ownership.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Sharing
                conn.execute(
                    update(agent_sharing)
                    .where(agent_sharing.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Schedules
                conn.execute(
                    update(agent_schedules)
                    .where(agent_schedules.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Executions
                conn.execute(
                    update(schedule_executions)
                    .where(schedule_executions.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Chat sessions
                conn.execute(
                    update(chat_sessions)
                    .where(chat_sessions.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Chat messages
                conn.execute(
                    update(chat_messages)
                    .where(chat_messages.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Activities
                conn.execute(
                    update(agent_activities)
                    .where(agent_activities.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Permissions (both source and target)
                conn.execute(
                    update(agent_permissions)
                    .where(agent_permissions.c.source_agent == old_name)
                    .values(source_agent=new_name)
                )
                conn.execute(
                    update(agent_permissions)
                    .where(agent_permissions.c.target_agent == old_name)
                    .values(target_agent=new_name)
                )

                # Shared folder config
                conn.execute(
                    update(agent_shared_folder_config)
                    .where(agent_shared_folder_config.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Git config
                conn.execute(
                    update(agent_git_config)
                    .where(agent_git_config.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Skills
                conn.execute(
                    update(agent_skills)
                    .where(agent_skills.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Tags
                conn.execute(
                    update(agent_tags)
                    .where(agent_tags.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Public links
                conn.execute(
                    update(agent_public_links)
                    .where(agent_public_links.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # MCP API keys
                conn.execute(
                    update(mcp_api_keys)
                    .where(mcp_api_keys.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Health checks
                conn.execute(
                    update(agent_health_checks)
                    .where(agent_health_checks.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Dashboard values
                conn.execute(
                    update(agent_dashboard_values)
                    .where(agent_dashboard_values.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Monitoring cooldowns
                conn.execute(
                    update(monitoring_alert_cooldowns)
                    .where(monitoring_alert_cooldowns.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Shared files (outbound — amazing-file-outbound).
                # FK has ON UPDATE CASCADE as belt-and-suspenders; this keeps
                # the explicit cascade list complete for visibility.
                conn.execute(
                    update(agent_shared_files)
                    .where(agent_shared_files.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                # Entitled-module agent-scoped tables (ent#46) registered via
                # db.agent_cleanup.register_agent_owned_table — e.g.
                # enterprise_connectors. Table/column come from code (not user
                # input); values are bound. Absent tables are skipped.
                from db.agent_cleanup import EXTRA_AGENT_REFS, _table_exists
                for table, column in EXTRA_AGENT_REFS:
                    if not _table_exists(conn, table):
                        continue
                    conn.execute(
                        text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                        {"new": new_name, "old": old_name},
                    )
                # Reports (#918) — re-key so the renamed agent keeps its report
                # history and the old name doesn't leave orphaned rows a reused
                # name could inherit (cross-tenant disclosure).
                conn.execute(
                    update(agent_reports)
                    .where(agent_reports.c.agent_name == old_name)
                    .values(agent_name=new_name)
                )

                return True

            except IntegrityError:
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
        # Single query that joins all needed tables. Kept as text() — the
        # multi-LEFT-JOIN with COALESCE/CASE is materially clearer than the
        # Core equivalent and is portable (no sqlite-only constructs).
        stmt = text("""
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
                AND LOWER(s.shared_with_email) = LOWER(:user_email)
            WHERE ao.deleted_at IS NULL
        """)

        with get_engine().connect() as conn:
            rows = conn.execute(stmt, {"user_email": user_email or ""}).mappings()

            result = {}
            for row in rows:
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
