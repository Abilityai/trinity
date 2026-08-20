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

from sqlalchemy import select, insert, update, delete, func, and_, or_
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
    DisplayLabelMixin,
    McpExposureMixin,
    OperatorResumeMixin,
    A2AExposureMixin,
    TtsMixin,
    EphemeralMixin,
)
from utils.helpers import utc_now_iso

# System agent name constant
SYSTEM_AGENT_NAME = "trinity-system"


def _soft_deleted_agents_predicate(cutoff: str):
    """WHERE clause for the #834 Phase 1a soft-deleted agent hard-purge.

    #1644: shared between `find_soft_deleted_agents_past_retention` (what the
    sweep purges) and `count_soft_deleted_agents_past_retention` (what the guard
    counts) so the two can never diverge. This purge chains into
    `remove_agent_volumes` (#1581) — the count MUST describe exactly the set that
    is about to become unrecoverable.
    """
    return and_(
        agent_ownership.c.deleted_at.is_not(None),
        agent_ownership.c.deleted_at < cutoff,
    )


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
    DisplayLabelMixin,
    McpExposureMixin,
    OperatorResumeMixin,
    A2AExposureMixin,
    TtsMixin,
    EphemeralMixin,
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
        is_ephemeral: bool = False,
        ephemeral_max_executions: Optional[int] = None,
        ephemeral_expires_at: Optional[str] = None,
        spawned_by_agent: Optional[str] = None,
        spawned_by_key_id: Optional[str] = None,
        max_parallel_tasks: Optional[int] = None,
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
            is_ephemeral / ephemeral_max_executions / ephemeral_expires_at:
                trinity-enterprise#69 ghost-agent budget. ``ephemeral_expires_at``
                must be non-NULL whenever ``is_ephemeral`` is set (no immortal
                ghost — the creation path always stamps it).
            spawned_by_agent / spawned_by_key_id: Part 2 spawn provenance,
                written for ANY agent-spawned creation (durable or ephemeral).
            max_parallel_tasks: optional explicit concurrency cap; ghosts pass 1
                (overshoot bound). None keeps the column default.
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
            values = dict(
                agent_name=agent_name,
                owner_id=user["id"],
                created_at=utc_now_iso(),
                is_system=1 if is_system else 0,
                execution_timeout_seconds=3600,
                require_email=1 if require_email else 0,
            )
            if is_ephemeral:
                values.update(
                    is_ephemeral=1,
                    ephemeral_max_executions=ephemeral_max_executions,
                    ephemeral_expires_at=ephemeral_expires_at,
                )
            if spawned_by_agent:
                values.update(
                    spawned_by_agent=spawned_by_agent,
                    spawned_by_key_id=spawned_by_key_id,
                )
            if max_parallel_tasks is not None:
                values["max_parallel_tasks"] = max_parallel_tasks
            with get_engine().begin() as conn:
                conn.execute(insert(agent_ownership).values(**values))
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

    def is_volume_base_reserved(
        self, volume_base: str, exclude_agent: Optional[str] = None
    ) -> bool:
        """True if any ownership row (live OR soft-deleted) owns the Docker
        data volumes named ``agent-{volume_base}-{workspace|public|shared}``
        (#1664).

        The volume-identity counterpart of :meth:`is_agent_name_reserved`, and
        the ONLY safe orphan predicate for the #1581 volume sweep. A volume
        describes itself by name and by its immutable ``trinity.agent-name``
        label, but Docker can rename neither — so after an agent rename BOTH
        carry the pre-rename name while the volume is still the live agent's
        home. Asking "is this *name* an agent?" therefore answers the wrong
        question and marks live data an orphan; asking "does any agent own
        volumes under this base?" answers the right one.

        A row owns volumes under BOTH of its identities — the check is a
        UNION, not a switch:

        - ``agent_name``: NULL ``volume_base_name`` means "same as agent_name"
          (every agent that was never renamed, so no backfill is needed), AND
          a renamed agent still creates *new* volumes under its CURRENT name —
          `get_public_volume_name` / `get_shared_volume_name` are called with
          the live name, so enabling file-sharing after a rename produces
          `agent-{new}-public`. A renamed agent therefore legitimately owns
          volumes under two bases, and the public volume is unmounted whenever
          file-sharing is off — matching only the pin would mark that live
          agent's shared files an orphan and delete them.
        - ``volume_base_name``: the pre-rename base its workspace kept.

        Union-of-both is also strictly safer than the pre-#1664 predicate it
        replaces (`is_agent_name_reserved` ≡ the first branch alone), so this
        can only protect more than before, never less. A purged row matches
        neither, so a genuine orphan is still reclaimable.

        ``exclude_agent`` ignores one row — pass the agent that is ASKING, so
        the question becomes "does anyone ELSE claim this base?" (#1671). The
        rename gate needs it: an agent renamed ``B``→``A`` keeps pin ``B``, so
        renaming it back to ``B`` must be allowed (it already owns that base,
        and the result has exactly one claimant). Without the exclusion that
        legitimate rename-back is refused, and told its own volumes belong to
        someone else.
        """
        if not volume_base:
            return False
        where = [
            or_(
                agent_ownership.c.agent_name == volume_base,
                agent_ownership.c.volume_base_name == volume_base,
            )
        ]
        if exclude_agent:
            where.append(agent_ownership.c.agent_name != exclude_agent)
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_ownership.c.agent_name).where(*where)
            ).first()
            return row is not None

    def get_volume_base_name(self, agent_name: str) -> Optional[str]:
        """The base name of ``agent_name``'s Docker data volumes (#1664).

        Returns the pinned ``volume_base_name`` when set (the agent was
        renamed and kept its volumes), else ``agent_name``. Returns None only
        when the agent has no ownership row at all.
        """
        with get_engine().connect() as conn:
            row = conn.execute(
                select(
                    agent_ownership.c.agent_name,
                    agent_ownership.c.volume_base_name,
                ).where(agent_ownership.c.agent_name == agent_name)
            ).first()
            if row is None:
                return None
            return row.volume_base_name or row.agent_name

    def set_volume_base_name(self, agent_name: str, volume_base: str) -> bool:
        """Pin ``agent_name``'s volume identity, but only if not already pinned
        (#1664). Returns True when a row was written.

        Guarded on ``volume_base_name IS NULL`` so it can never overwrite an
        earlier rename's pin with a later one — the volume base is frozen at
        the FIRST rename and every subsequent rename must keep pointing at the
        same volumes. Used by the boot-time heal for agents renamed before this
        column existed; the rename path pins inline (see
        ``MetadataMixin.rename_agent``).
        """
        if not agent_name or not volume_base:
            return False
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.volume_base_name.is_(None),
                )
                .values(volume_base_name=volume_base)
            )
            return (result.rowcount or 0) > 0

    def is_agent_live(self, agent_name: str) -> bool:
        """True if `agent_name` has a live (non-soft-deleted) ownership row.

        Deliberately checks ONLY `agent_ownership` with `deleted_at IS NULL`
        — no `users` join — so it matches the webhook token-lookup predicate
        (`get_schedule_by_webhook_token`, db/schedules.py) *exactly*. This is
        the schedule/webhook creation gate (#1445): a schedule may only be
        created on an agent the trigger lookup would actually serve.

        Do NOT "fix" this to reuse `get_agent_owner` — that INNER-JOINs
        `users`, so it returns None for a live agent whose owner-user row is
        missing (FKs are disabled platform-wide). That would be a false
        negative: the gate would 404 an agent the webhook lookup happily
        resolves, re-opening the mismatch this predicate closes.
        """
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_ownership.c.agent_name).where(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
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

    def count_non_system_agents(self) -> int:
        """Count live (non-soft-deleted), non-system agents.

        The Cornelius first-run seeder (ent#107) uses this as its
        "genuinely-fresh install" signal: on an established fleet (any
        non-system agent already present) the default-Cornelius seed is
        skipped, so upgrading an existing install never spawns a surprise
        container. The system agent (`is_system=1`) is always present and
        must not count.
        """
        stmt = select(func.count()).select_from(agent_ownership).where(
            agent_ownership.c.deleted_at.is_(None),
            func.coalesce(agent_ownership.c.is_system, 0) == 0,
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    @staticmethod
    def _deactivate_agent_keys_in_txn(conn, agent_name: str, *, active: bool) -> int:
        """Flip per-agent key activity on an EXISTING connection (#1745).

        Runs inside the caller's transaction so the credential state can never
        diverge from the ownership state — a key left active after a committed
        delete is the whole bug.
        """
        from .tables import mcp_api_keys

        return conn.execute(
            update(mcp_api_keys)
            .where(
                mcp_api_keys.c.agent_name == agent_name,
                mcp_api_keys.c.scope.in_(("agent", "connector")),
                mcp_api_keys.c.is_active == (0 if active else 1),
            )
            .values(is_active=1 if active else 0)
        ).rowcount or 0

    def deactivate_agent_mcp_keys(self, agent_name: str) -> int:
        """Deactivate this agent's agent- and connector-scoped MCP keys (#1811).

        Standalone-transaction wrapper over `_deactivate_agent_keys_in_txn`, for
        callers outside a delete/recover transaction — specifically the recovery
        recreate, which mints a fresh key on every invocation (the old key's
        plaintext is unrecoverable) and previously left every superseded row
        active. Returns the number of rows flipped.
        """
        with get_engine().begin() as conn:
            return self._deactivate_agent_keys_in_txn(conn, agent_name, active=False)

    def reconcile_spawn_key_id(self, agent_name: str, current_key_id: str) -> int:
        """Re-point this parent's children at its CURRENT agent-key id (#1854).

        ``enforce_agent_spawn_scope`` 403s unless
        ``get_agent_mcp_api_key(parent).id == child.spawned_by_key_id``, so
        rotating a parent's key silently severs parenthood for every child it
        spawned — unrecoverably, because the old id is gone.

        Keyed on ``!= :current_id``, deliberately NOT ``= :old_id``:

        * ``get_agent_mcp_api_key`` is ``ORDER BY created_at DESC LIMIT 1``, so
          the moment the new key commits it returns the NEW id — re-reading it
          to build an ``= :old_id`` predicate makes the UPDATE a no-op.
        * #1811 recorded 50 accumulated active rows on one instance, so children
          can be stranded on any of several superseded ids; only the ``!=`` form
          converges them.

        Idempotent and convergent: it repairs children stranded by an earlier
        crashed rotation, and serves the rollback direction too (after the
        superseded rows are deleted, the surviving key is newest-active again).

        Security: the predicate is scoped to ``spawned_by_agent = :agent`` —
        rows whose provenance ALREADY names this parent, written server-side at
        creation. It cannot grant parenthood over an agent this parent never
        spawned, and it does not widen the gate's matching rule (relaxing
        ``enforce_agent_spawn_scope`` to accept "any active key" was rejected:
        that edits shared auth code on behalf of one caller's transient state).

        Returns the number of children re-pointed.
        """
        if not current_key_id:
            return 0
        stmt = (
            update(agent_ownership)
            .where(
                agent_ownership.c.spawned_by_agent == agent_name,
                agent_ownership.c.spawned_by_key_id.isnot(None),
                agent_ownership.c.spawned_by_key_id != current_key_id,
            )
            .values(spawned_by_key_id=current_key_id)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount or 0

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
                # #1745: revoke the agent's credentials in the SAME transaction.
                # `mcp_api_keys` is CASCADE in AGENT_REFS precisely because "an
                # orphaned key must not survive its agent", but that cascade only
                # runs at the hard purge — i.e. after the whole soft-delete
                # window (default 180 days), during which the key kept
                # authenticating, enumerating the fleet, and able to mint a fresh
                # key of its own. Deactivate (not delete) so recovery can restore
                # them; `validate_mcp_api_key` requires is_active=1, so this
                # closes both the REST and MCP paths.
                self._deactivate_agent_keys_in_txn(conn, agent_name, active=False)
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
            .where(_soft_deleted_agents_predicate(cutoff))
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

    def count_soft_deleted_agents_past_retention(
        self, retention_days: int, limit: int
    ) -> int:
        """#1644: how many agents `_sweep_soft_deleted_agents` would hard-purge.

        Every candidate here is one agent whose Docker data volumes are destroyed
        (#1581) — this count is not "rows", it is "unrecoverable losses". The guard
        floors this sweep at 0 for that reason: any purge at all is worth one ack.
        """
        if retention_days <= 0 or limit <= 0:
            return 0
        from utils.helpers import iso_cutoff

        cutoff = iso_cutoff(hours=retention_days * 24)
        inner = (
            select(agent_ownership.c.agent_name)
            .where(_soft_deleted_agents_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(inner)).scalar() or 0
            )

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
            if result.rowcount > 0:
                # #1745: recovery is the inverse of delete — restore the same
                # credentials so a recovered agent keeps working without
                # re-issuing keys into a running container.
                self._deactivate_agent_keys_in_txn(conn, agent_name, active=True)
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

    # =========================================================================
    # Public/Channel System Prompt (#1205)
    # Custom instructions injected into public-facing conversations only
    # (public links, Slack/Telegram/WhatsApp channels, x402 paid chat).
    # Text-surface counterpart of voice_system_prompt.
    # =========================================================================

    def get_public_channel_system_prompt(self, agent_name: str) -> Optional[str]:
        """Get the public/channel system prompt for an agent (#1205)."""
        stmt = select(agent_ownership.c.public_channel_system_prompt).where(
            agent_ownership.c.agent_name == agent_name,
            agent_ownership.c.deleted_at.is_(None),
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return (
                row["public_channel_system_prompt"]
                if row and row["public_channel_system_prompt"]
                else None
            )

    def set_public_channel_system_prompt(
        self, agent_name: str, prompt: Optional[str]
    ) -> bool:
        """Set the public/channel system prompt for an agent (#1205).

        Empty/whitespace-only clears the value (strict no-op surface).
        """
        cleaned = prompt.strip() if prompt else None
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(agent_ownership.c.agent_name == agent_name)
                .values(public_channel_system_prompt=cleaned or None)
            )
            return result.rowcount > 0
