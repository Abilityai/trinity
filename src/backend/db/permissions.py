"""
Agent-to-agent permissions database operations.

Phase 9.10: Centralized permission system controlling which agents
can communicate with other agents.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``agent_permissions``
table in ``db/tables.py`` (dialect-agnostic expressions, no ``?`` placeholders),
and the engine is resolved via ``db/engine.py``. The public API of
``PermissionOperations`` is unchanged.
"""

from datetime import datetime
from typing import Optional, List

from sqlalchemy import select, insert, delete, and_
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_permissions
from db_models import AgentPermission
from utils.helpers import utc_now_iso


class PermissionOperations:
    """Agent-to-agent permission database operations."""

    def __init__(self, user_ops, agent_ops):
        """Initialize with references to user and agent operations."""
        self._user_ops = user_ops
        self._agent_ops = agent_ops

    @staticmethod
    def _row_to_permission(row) -> AgentPermission:
        """Convert a permission row to an AgentPermission model."""
        return AgentPermission(
            id=row["id"],
            source_agent=row["source_agent"],
            target_agent=row["target_agent"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"]
        )

    # =========================================================================
    # Permission CRUD Operations
    # =========================================================================

    def get_permitted_agents(self, source_agent: str) -> List[str]:
        """
        Get list of agent names that source_agent is permitted to call.

        Returns list of target agent names.
        """
        stmt = (
            select(agent_permissions.c.target_agent)
            .where(agent_permissions.c.source_agent == source_agent)
            .order_by(agent_permissions.c.target_agent)
        )
        with get_engine().connect() as conn:
            return [row["target_agent"] for row in conn.execute(stmt).mappings()]

    def get_all_permission_edges(self, accessible_agents: Optional[set] = None) -> List[dict]:
        """
        Get all permission edges for graph visualization (bulk endpoint).

        Args:
            accessible_agents: Optional set of agent names to filter by.
                              If provided, only returns edges where BOTH
                              source and target are in the set.

        Returns list of {"source": str, "target": str} dicts.
        """
        stmt = select(
            agent_permissions.c.source_agent,
            agent_permissions.c.target_agent,
        )

        if accessible_agents:
            # Filter at SQL level to avoid info leakage
            agent_list = list(accessible_agents)
            stmt = stmt.where(
                and_(
                    agent_permissions.c.source_agent.in_(agent_list),
                    agent_permissions.c.target_agent.in_(agent_list),
                )
            )

        stmt = stmt.order_by(
            agent_permissions.c.source_agent,
            agent_permissions.c.target_agent,
        )

        with get_engine().connect() as conn:
            return [
                {"source": row["source_agent"], "target": row["target_agent"]}
                for row in conn.execute(stmt).mappings()
            ]

    def get_permission_details(self, source_agent: str) -> List[AgentPermission]:
        """
        Get full permission details for an agent.

        Returns list of AgentPermission objects.
        """
        stmt = (
            select(
                agent_permissions.c.id,
                agent_permissions.c.source_agent,
                agent_permissions.c.target_agent,
                agent_permissions.c.created_at,
                agent_permissions.c.created_by,
            )
            .where(agent_permissions.c.source_agent == source_agent)
            .order_by(agent_permissions.c.target_agent)
        )
        with get_engine().connect() as conn:
            return [self._row_to_permission(row) for row in conn.execute(stmt).mappings()]

    def is_permitted(self, source_agent: str, target_agent: str) -> bool:
        """
        Check if source_agent is permitted to call target_agent.

        Returns True if permission exists.
        """
        stmt = select(agent_permissions.c.id).where(
            and_(
                agent_permissions.c.source_agent == source_agent,
                agent_permissions.c.target_agent == target_agent,
            )
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def add_permission(self, source_agent: str, target_agent: str, created_by: str) -> Optional[AgentPermission]:
        """
        Add permission for source_agent to call target_agent.

        Returns the created permission or None if it already exists.
        """
        now = utc_now_iso()

        try:
            with get_engine().begin() as conn:
                result = conn.execute(
                    insert(agent_permissions).values(
                        source_agent=source_agent,
                        target_agent=target_agent,
                        created_at=now,
                        created_by=created_by,
                    )
                )
                new_id = result.inserted_primary_key[0]
        except IntegrityError:
            # Permission already exists
            return None

        return AgentPermission(
            id=new_id,
            source_agent=source_agent,
            target_agent=target_agent,
            created_at=datetime.fromisoformat(now),
            created_by=created_by
        )

    def remove_permission(self, source_agent: str, target_agent: str) -> bool:
        """
        Remove permission for source_agent to call target_agent.

        Returns True if a permission was removed.
        """
        stmt = delete(agent_permissions).where(
            and_(
                agent_permissions.c.source_agent == source_agent,
                agent_permissions.c.target_agent == target_agent,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def set_permissions(self, source_agent: str, target_agents: List[str], created_by: str) -> int:
        """
        Set permissions for source_agent (full replacement).

        Removes all existing permissions and adds new ones.
        Returns the number of permissions set.
        """
        now = utc_now_iso()

        with get_engine().begin() as conn:
            # Remove all existing permissions for this source
            conn.execute(
                delete(agent_permissions).where(
                    agent_permissions.c.source_agent == source_agent
                )
            )

            # Add new permissions
            for target in target_agents:
                if target != source_agent:  # Can't permit self
                    sp = conn.begin_nested()
                    try:
                        conn.execute(
                            insert(agent_permissions).values(
                                source_agent=source_agent,
                                target_agent=target,
                                created_at=now,
                                created_by=created_by,
                            )
                        )
                        sp.commit()
                    except IntegrityError:
                        sp.rollback()  # Skip duplicates

            return len(target_agents)

    def delete_agent_permissions(self, agent_name: str) -> int:
        """
        Delete all permissions involving an agent (when agent is deleted).

        Removes permissions where agent is source OR target.
        Returns total number of permissions deleted.
        """
        with get_engine().begin() as conn:
            # Delete where agent is source
            source_result = conn.execute(
                delete(agent_permissions).where(
                    agent_permissions.c.source_agent == agent_name
                )
            )
            source_count = source_result.rowcount

            # Delete where agent is target
            target_result = conn.execute(
                delete(agent_permissions).where(
                    agent_permissions.c.target_agent == agent_name
                )
            )
            target_count = target_result.rowcount

            return source_count + target_count

    def grant_default_permissions(self, agent_name: str, owner_username: str) -> int:
        """
        Grant default permissions for a new agent.

        RESTRICTIVE DEFAULT (2026-02-19): New agents start with NO permissions.
        Owners must explicitly grant permissions via the Permissions tab.

        This is a no-op function that returns 0 permissions granted.
        The method is kept for API compatibility and potential future use.

        Previously (Option B): Granted bidirectional permissions with all
        same-owner agents automatically. Changed to restrictive default for
        better security - agents should explicitly opt-in to collaboration.

        Returns number of permissions created (always 0 with restrictive default).
        """
        # Restrictive default: no automatic permissions
        # Owners must explicitly configure permissions in the UI
        return 0
