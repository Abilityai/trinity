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

from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, insert, update, delete, func
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_ownership, users
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
    McpExposureMixin,
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
    McpExposureMixin,
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

    def register_agent_owner(
        self,
        agent_name: str,
        owner_username: str,
        is_system: bool = False,
        require_email: bool = False,
    ) -> bool:
        """Register the owner of an agent.

        Args:
            agent_name: Name of the agent
            owner_username: Username of the owner
            is_system: True for system agents (deletion-protected)
            require_email: #1129 — initial value for the per-agent
                ``require_email`` access-policy flag, seeded from the
                fleet-wide default at creation. Defaults False so internal
                callers (e.g. the system agent) are unaffected; user agent
                creation passes the platform default.
        """
        user = self._user_ops.get_user_by_username(owner_username)
        if not user:
            return False

        try:
            # #665: explicitly pass execution_timeout_seconds = 3600.
            # SQLite stores the column's DEFAULT at column-creation
            # time and doesn't honour later DDL changes — so on
            # existing DBs the column's baked-in default is still
            # 900 even after the new schema.py landed. Passing the
            # value explicitly here keeps new-agent timeouts at
            # 60min on both fresh installs (where the schema.py
            # default already lands as 3600) and existing instances.
            # #1129: same reasoning for require_email — pass it
            # explicitly so the secure-by-default seed lands on existing
            # DBs whose baked-in column default is 0.
            with get_engine().begin() as conn:
                conn.execute(
                    insert(agent_ownership).values(
                        agent_name=agent_name,
                        owner_id=user["id"],
                        created_at=utc_now_iso(),
                        is_system=1 if is_system else 0,
                        execution_timeout_seconds=3600,
                        require_email=1 if require_email else 0,
                    )
                )
            return True
        except IntegrityError:
            # Agent already registered - update is_system flag if needed
            if is_system:
                with get_engine().begin() as conn:
                    conn.execute(
                        update(agent_ownership)
                        .where(agent_ownership.c.agent_name == agent_name)
                        .values(is_system=1)
                    )
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
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_ownership.c.agent_name).where(
                    agent_ownership.c.agent_name == agent_name
                )
            ).first()
            return row is not None

    def get_agent_owner(self, agent_name: str) -> Optional[Dict]:
        """Get the owner of an agent, including is_system flag.

        Excludes soft-deleted agents (#834): callers consume this to
        gate user-facing access; soft-deleted agents should look like
        they don't exist.
        """
        stmt = (
            select(
                agent_ownership.c.id,
                agent_ownership.c.agent_name,
                agent_ownership.c.owner_id,
                users.c.username.label("owner_username"),
                agent_ownership.c.created_at,
                func.coalesce(agent_ownership.c.is_system, 0).label("is_system"),
            )
            .select_from(
                agent_ownership.join(users, agent_ownership.c.owner_id == users.c.id)
            )
            .where(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
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

        stmt = select(agent_ownership.c.agent_name).where(
            agent_ownership.c.owner_id == user["id"],
            agent_ownership.c.deleted_at.is_(None),
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

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

        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
                .values(deleted_at=utc_now_iso())
            )
            if result.rowcount > 0:
                return True
            # rowcount==0 — either already soft-deleted or doesn't exist
            row = conn.execute(
                select(agent_ownership.c.agent_name).where(
                    agent_ownership.c.agent_name == agent_name
                )
            ).first()
            return row is not None

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
        stmt = (
            select(agent_ownership.c.agent_name)
            .where(
                agent_ownership.c.deleted_at.is_not(None),
                agent_ownership.c.deleted_at < cutoff,
            )
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

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
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_not(None),
                )
                .values(deleted_at=None)
            )
            return result.rowcount > 0

    def list_soft_deleted_agents(self, limit: int = 200) -> List[Dict]:
        """List currently-soft-deleted agents with their `deleted_at`.

        Used by the admin recovery endpoint. Returns agent_name,
        owner_id (so admins can filter by owner), and the timestamp
        the agent was soft-deleted. The purge ETA is computed at the
        router layer using `agent_soft_delete_retention_days`.
        """
        stmt = (
            select(
                agent_ownership.c.agent_name,
                agent_ownership.c.owner_id,
                agent_ownership.c.deleted_at,
                agent_ownership.c.created_at,
            )
            .where(agent_ownership.c.deleted_at.is_not(None))
            .order_by(agent_ownership.c.deleted_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def purge_agent_ownership(self, agent_name: str) -> bool:
        """Hard-delete a soft-deleted agent (#834): runs #816 cascade_delete
        on child tables then removes the agent_ownership row itself.

        Called by the retention sweep AND by ad-hoc admin tooling. Refuses
        to purge a live (non-soft-deleted) row — callers must soft-delete
        first. Returns True if a row was actually removed.
        """
        from db.agent_cleanup import cascade_delete

        with get_engine().begin() as conn:
            row = conn.execute(
                select(agent_ownership.c.deleted_at).where(
                    agent_ownership.c.agent_name == agent_name
                )
            ).mappings().first()
            if not row:
                return False
            if row["deleted_at"] is None:
                # Refuse to purge a live agent — explicit safety guard.
                return False

            # cascade_delete() runs SQLAlchemy Core deletes; hand it this
            # Connection so its deletes run inside this same transaction (#300).
            cascade_delete(conn, agent_name)
            result = conn.execute(
                delete(agent_ownership).where(
                    agent_ownership.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0

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
        stmt = select(agent_ownership.c.voice_system_prompt).where(
            agent_ownership.c.agent_name == agent_name,
            agent_ownership.c.deleted_at.is_(None),
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return row["voice_system_prompt"] if row and row["voice_system_prompt"] else None

    def set_voice_system_prompt(self, agent_name: str, prompt: Optional[str]) -> bool:
        """Set the voice system prompt for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(voice_system_prompt=prompt or None)
            )
            return result.rowcount > 0

    # =========================================================================
    # Voice Name (#28) — persisted per-agent Gemini Live voice
    # =========================================================================

    def get_voice_name(self, agent_name: str) -> str:
        """Get the persisted Gemini voice for an agent.

        Falls back to DEFAULT_VOICE_NAME ('Kore') when unset, and ALSO when the
        persisted value is not in the current GEMINI_VOICE_NAMES set (a voice
        removed after it was saved) — defense-in-depth so the call path never
        hands Gemini an unusable voice (#28, reviewer M1).
        """
        from config import DEFAULT_VOICE_NAME, GEMINI_VOICE_NAMES

        stmt = select(agent_ownership.c.voice_name).where(
            agent_ownership.c.agent_name == agent_name,
            agent_ownership.c.deleted_at.is_(None),
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        value = row["voice_name"] if row else None
        return value if value in GEMINI_VOICE_NAMES else DEFAULT_VOICE_NAME

    def set_voice_name(self, agent_name: str, voice_name: Optional[str]) -> bool:
        """Set the persisted Gemini voice for an agent (None clears it → default)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(voice_name=voice_name or None)
            )
            return result.rowcount > 0

    def get_public_channel_model(self, agent_name: str) -> Optional[str]:
        """Per-agent model override for public-facing channels (#894).

        Returns the persisted model id, or ``None`` when unset OR when the
        persisted value is not a currently-valid public-channel model (a model
        removed after it was saved). ``None`` ⇒ the caller inherits the platform
        default — same defense-in-depth posture as ``get_voice_name`` (#28).
        """
        from services.settings_service import is_valid_public_channel_model

        stmt = select(agent_ownership.c.public_channel_model).where(
            agent_ownership.c.agent_name == agent_name,
            agent_ownership.c.deleted_at.is_(None),
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        value = row["public_channel_model"] if row else None
        return value if (value and is_valid_public_channel_model(value)) else None

    def set_public_channel_model(self, agent_name: str, model: Optional[str]) -> bool:
        """Set/clear the per-agent public-channel model override (None clears it)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(public_channel_model=model or None)
            )
            return result.rowcount > 0
