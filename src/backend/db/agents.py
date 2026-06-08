"""
Agent ownership and access control database operations.

Core ownership management stays here. All other concerns are delegated
to focused mixin classes in db/agent_settings/:
- SharingMixin: Agent sharing operations
- ResourcesMixin: Memory, CPU, timeout, parallel capacity
- SecurityMixin: Full capabilities, read-only mode
- AutonomyMixin: Autonomy mode, API key settings
- AvatarMixin: Avatar identity management
- MetadataMixin: Batch queries, rename operations
- GitPATMixin: Per-agent GitHub PAT management (#347)
"""

import sqlite3
from datetime import datetime
from typing import Optional, List, Dict

from .connection import get_db_connection
from .agent_settings import (
    SharingMixin,
    ResourcesMixin,
    SecurityMixin,
    AutonomyMixin,
    AvatarMixin,
    MetadataMixin,
    AccessPolicyMixin,
    GitPATMixin,
    FileSharingMixin,
)
from utils.helpers import utc_now_iso

# System agent name constant
SYSTEM_AGENT_NAME = "trinity-system"


class AgentOperations(
    SharingMixin,
    ResourcesMixin,
    SecurityMixin,
    AutonomyMixin,
    AvatarMixin,
    MetadataMixin,
    AccessPolicyMixin,
    GitPATMixin,
    FileSharingMixin,
):
    """Agent ownership, access control, and settings database operations.

    Core ownership methods are defined directly on this class.
    All other concerns are provided by mixin classes from db/agent_settings/.
    """

    def __init__(self, user_ops):
        """Initialize with reference to user operations for lookups."""
        self._user_ops = user_ops

    # =========================================================================
    # Agent Ownership Management
    # =========================================================================

    def register_agent_owner(self, agent_name: str, owner_username: str, is_system: bool = False) -> bool:
        """Register the owner of an agent.

        Args:
            agent_name: Name of the agent
            owner_username: Username of the owner
            is_system: True for system agents (deletion-protected)
        """
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return False

        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                # #665: explicitly pass execution_timeout_seconds = 3600.
                # SQLite stores the column's DEFAULT at column-creation
                # time and doesn't honour later DDL changes — so on
                # existing DBs the column's baked-in default is still
                # 900 even after the new schema.py landed. Passing the
                # value explicitly here keeps new-agent timeouts at
                # 60min on both fresh installs (where the schema.py
                # default already lands as 3600) and existing instances.
                cursor.execute("""
                    INSERT INTO agent_ownership (agent_name, owner_id, created_at, is_system, execution_timeout_seconds)
                    VALUES (?, ?, ?, ?, ?)
                """, (agent_name, user["id"], utc_now_iso(), 1 if is_system else 0, 3600))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Agent already registered - update is_system flag if needed
                if is_system:
                    cursor.execute("""
                        UPDATE agent_ownership SET is_system = 1 WHERE agent_name = ?
                    """, (agent_name,))
                    conn.commit()
                return False

    def is_agent_name_reserved(self, agent_name: str) -> bool:
        """True if `agent_name` is present in agent_ownership, including
        soft-deleted rows (#834).

        The unique constraint on `agent_name` doesn't distinguish live
        vs soft-deleted, so the create path needs an explicit check that
        also sees soft-deleted rows — otherwise it walks past the
        existence guard and crashes downstream on the SQL INTEGRITY
        error (and worse: leaks side effects like a created container
        before the failure).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM agent_ownership WHERE agent_name = ?",
                (agent_name,),
            )
            return cursor.fetchone() is not None

    def get_agent_owner(self, agent_name: str) -> Optional[Dict]:
        """Get the owner of an agent, including is_system flag.

        Excludes soft-deleted agents (#834): callers consume this to
        gate user-facing access; soft-deleted agents should look like
        they don't exist.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ao.id, ao.agent_name, ao.owner_id, u.username as owner_username,
                       ao.created_at, COALESCE(ao.is_system, 0) as is_system
                FROM agent_ownership ao
                JOIN users u ON ao.owner_id = u.id
                WHERE ao.agent_name = ? AND ao.deleted_at IS NULL
            """, (agent_name,))
            row = cursor.fetchone()
            if row:
                result = dict(row)
                result["is_system"] = bool(result.get("is_system", 0))
                return result
            return None

    def get_agents_by_owner(self, owner_username: str) -> List[str]:
        """Get all agent names owned by a user."""
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return []

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT agent_name FROM agent_ownership
                WHERE owner_id = ? AND deleted_at IS NULL
            """, (user["id"],))
            return [row["agent_name"] for row in cursor.fetchall()]

    def delete_agent_ownership(self, agent_name: str) -> bool:
        """Soft-delete the agent ownership row (Issue #834 Phase 1a).

        Marks `deleted_at = NOW`. Child rows (sharing, access requests,
        schedules, chat history, …) are left intact — the retention sweep
        in `cleanup_service.py` runs `cascade_delete()` to remove them
        when the soft-delete window expires (default 180 days, configurable
        via `agent_soft_delete_retention_days` in system_settings).

        Idempotent: if the row is already soft-deleted, the UPDATE is a
        no-op and we return True (the agent is in fact deleted, just not
        yet purged). Returns False only if the row doesn't exist.
        """
        from utils.helpers import utc_now_iso

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_ownership "
                "SET deleted_at = ? "
                "WHERE agent_name = ? AND deleted_at IS NULL",
                (utc_now_iso(), agent_name),
            )
            if cursor.rowcount > 0:
                conn.commit()
                return True
            # rowcount==0 — either already soft-deleted or doesn't exist
            cursor.execute(
                "SELECT 1 FROM agent_ownership WHERE agent_name = ?",
                (agent_name,),
            )
            return cursor.fetchone() is not None

    def find_soft_deleted_agents_past_retention(
        self, retention_days: int, limit: int = 5000
    ) -> List[str]:
        """List agent_names where `deleted_at` is older than `retention_days`.

        Used by the retention sweep to find rows ready for hard-purge.
        Bounded by `limit` to keep each cycle's work cap predictable
        (same pattern as #772 sweeps).
        """
        from utils.helpers import iso_cutoff

        if retention_days <= 0 or limit <= 0:
            return []

        cutoff = iso_cutoff(hours=retention_days * 24)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT agent_name FROM agent_ownership "
                "WHERE deleted_at IS NOT NULL AND deleted_at < ? "
                "LIMIT ?",
                (cutoff, limit),
            )
            return [row["agent_name"] for row in cursor.fetchall()]

    def recover_agent_ownership(self, agent_name: str) -> bool:
        """Recover a soft-deleted agent by clearing `deleted_at` (#834).

        Reverses a `delete_agent_ownership()` call within the retention
        window. Refuses to operate on:
          - a row that doesn't exist (returns False)
          - a live row (already `deleted_at IS NULL`; returns False)

        Returns True on successful recovery. Child rows survived the
        soft-delete intact, so the agent is immediately accessible
        again via the user-facing read paths. The Docker container is
        NOT recreated — that's a separate operation (operator must
        re-start the agent if they want a running container).
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_ownership SET deleted_at = NULL "
                "WHERE agent_name = ? AND deleted_at IS NOT NULL",
                (agent_name,),
            )
            if cursor.rowcount > 0:
                conn.commit()
                return True
            return False

    def list_soft_deleted_agents(self, limit: int = 200) -> List[Dict]:
        """List currently-soft-deleted agents with their `deleted_at`.

        Used by the admin recovery endpoint. Returns agent_name,
        owner_id (so admins can filter by owner), and the timestamp
        the agent was soft-deleted. The purge ETA is computed at the
        router layer using `agent_soft_delete_retention_days`.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT agent_name, owner_id, deleted_at, created_at "
                "FROM agent_ownership "
                "WHERE deleted_at IS NOT NULL "
                "ORDER BY deleted_at DESC "
                "LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def purge_agent_ownership(self, agent_name: str) -> bool:
        """Hard-delete a soft-deleted agent (#834): runs #816 cascade_delete
        on child tables then removes the agent_ownership row itself.

        Called by the retention sweep AND by ad-hoc admin tooling. Refuses
        to purge a live (non-soft-deleted) row — callers must soft-delete
        first. Returns True if a row was actually removed.
        """
        from db.agent_cleanup import cascade_delete

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT deleted_at FROM agent_ownership WHERE agent_name = ?",
                (agent_name,),
            )
            row = cursor.fetchone()
            if not row:
                return False
            if row["deleted_at"] is None:
                # Refuse to purge a live agent — explicit safety guard.
                return False

            cascade_delete(conn, agent_name)
            cursor.execute(
                "DELETE FROM agent_ownership WHERE agent_name = ?",
                (agent_name,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def can_user_access_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can access an agent (owner, shared, or admin)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Admins can access all agents
        if user["role"] == "admin":
            return True

        # Check if user is the owner
        owner = self.get_agent_owner(agent_name)
        if owner and owner["owner_username"] == username:
            return True

        # Check if agent is shared with user
        if self.is_agent_shared_with_user(agent_name, username):
            return True

        return False

    def can_user_delete_agent(self, username: str, agent_name: str) -> bool:
        """Check if a user can delete an agent (owner or admin, but NOT system agents)."""
        user = self._user_ops.get_user_by_username(username)
        if not user:
            return False

        # Check if this is a system agent - NO ONE can delete system agents
        owner = self.get_agent_owner(agent_name)
        if owner and owner.get("is_system", False):
            return False

        # Admins can delete any non-system agent
        if user["role"] == "admin":
            return True

        # Owners can delete their own non-system agents
        if owner and owner["owner_username"] == username:
            return True

        return False

    def is_system_agent(self, agent_name: str) -> bool:
        """Check if an agent is a system agent (deletion-protected)."""
        # Quick check by name
        if agent_name == SYSTEM_AGENT_NAME:
            return True
        # Check database flag
        owner = self.get_agent_owner(agent_name)
        return owner.get("is_system", False) if owner else False

    # =========================================================================
    # Voice System Prompt (VOICE-005)
    # =========================================================================

    def get_voice_system_prompt(self, agent_name: str) -> Optional[str]:
        """Get the voice system prompt for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT voice_system_prompt FROM agent_ownership "
                "WHERE agent_name = ? AND deleted_at IS NULL",
                (agent_name,),
            )
            row = cursor.fetchone()
            return row["voice_system_prompt"] if row and row["voice_system_prompt"] else None

    def set_voice_system_prompt(self, agent_name: str, prompt: Optional[str]) -> bool:
        """Set the voice system prompt for an agent."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_ownership SET voice_system_prompt = ? WHERE agent_name = ?",
                (prompt or None, agent_name),
            )
            conn.commit()
            return cursor.rowcount > 0
