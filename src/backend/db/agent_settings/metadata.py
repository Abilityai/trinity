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
    agent_permissions,
    agent_git_config,
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

        Re-keys `agent_name` in `agent_ownership` (here) and in EVERY table
        registered in `db.agent_cleanup.AGENT_REFS` (via `cascade_rename`),
        plus any entitled-module table registered at runtime through
        `register_agent_owned_table`.

        Deliberately NOT a hand-maintained list of tables (#1819): the previous
        docstring enumerated 19 and the code matched it, while the registry had
        grown to 41 — so a rename silently stranded 23 tables' worth of the
        agent's data, including its Session-tab history. The list is derived
        now; adding a table to `AGENT_REFS` is all a new feature has to do.

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

                # #1671: the name being free does not mean its VOLUME BASE is.
                # #1664 made one row claim one base and gated creation on it;
                # rename is the only other producer of `volume_base_name`, so
                # leaving it ungated lets an ordinary swap mint the collision
                # the create gate refuses: rename `x` -> `x-old` (row keeps pin
                # `x`), then rename `y` -> `x`. Two rows would then claim base
                # `x`, and because `get_public_volume_name` names off the LIVE
                # name (deliberate — see is_volume_base_reserved), the new `x`
                # get-then-creates onto the old agent's `agent-x-public`: the
                # #1667 silent-adopt disclosure through the ungated path. It
                # also strands the volumes forever — with two claimants the
                # purge guard skips BOTH bases and the orphan sweep never
                # reclaims them.
                #
                # Only ANOTHER row's pin blocks: `agent_name == new_name` is the
                # `taken` check above, and this row's own pin must not block a
                # rename-back (an agent renamed `B`->`A` keeps pin `B`; renaming
                # it to `B` is legitimate and leaves a single claimant).
                #
                # Enforced HERE, inside the rename transaction, not only at the
                # router: it closes the check-then-write gap and covers any
                # future caller of rename_agent (the #1445 router+chokepoint
                # pattern).
                base_claimed = conn.execute(
                    select(1).where(
                        agent_ownership.c.volume_base_name == new_name,
                        agent_ownership.c.agent_name != old_name,
                    )
                ).first()
                if base_claimed:
                    return False

                # Update all tables in order
                # Primary table.
                #
                # #1664: pin the volume identity in the SAME statement that
                # moves the name. Rename keeps the agent's Docker data volumes
                # (`agent-{old}-{workspace|public|shared}`) — Docker can rename
                # neither a volume nor its immutable `trinity.agent-name`
                # label — so from here on the row is the only record of which
                # volumes are this agent's. Without it the #1581 orphan sweep
                # reads the stale volume name/label, finds no agent by that
                # name, and force-removes the LIVE agent's home volume the
                # moment a container recreate leaves it briefly unattached
                # (#1664).
                #
                # COALESCE, not a plain SET: on a second rename the base must
                # stay pinned to the FIRST rename's name — that is where the
                # volumes actually live. Atomic with the rename by
                # construction (same transaction), so a crash can never leave
                # a renamed row with an unpinned base.
                existing_base = conn.execute(
                    select(agent_ownership.c.volume_base_name).where(
                        agent_ownership.c.agent_name == old_name
                    )
                ).scalar()
                conn.execute(
                    update(agent_ownership)
                    .where(agent_ownership.c.agent_name == old_name)
                    .values(
                        agent_name=new_name,
                        volume_base_name=existing_base or old_name,
                    )
                )

                # Every other agent-keyed table, from the ONE registry
                # (#1819). This used to be ~19 hand-written `update()` blocks,
                # and the list had silently fallen 23 tables behind
                # `AGENT_REFS`: a rename stranded the agent's Session-tab
                # history (the reported symptom), and also its reminders,
                # loops, notifications, operator-queue items, sync state,
                # compatibility results, per-user memory, and its Telegram /
                # WhatsApp / VoIP / Slack channel bindings.
                #
                # `cascade_rename` was written for exactly this and had ZERO
                # callers — the delete path consumed the registry while rename
                # kept its own copy, so every table added since only ever
                # joined one of them. Two hand-maintained lists for one
                # question is the defect; deriving both from `AGENT_REFS` is
                # the fix, and `test_1819_rename_cascade_parity.py` fails CI if
                # they diverge again.
                #
                # Safe by construction: every table the old sequence touched is
                # already in the registry (verified — the hand list was a strict
                # subset), `cascade_rename` skips absent tables, and it handles
                # the multi-column refs (`agent_permissions.source_agent` /
                # `.target_agent`, the event-subscription pair) the hand list
                # spelled out twice.
                from db.agent_cleanup import cascade_rename

                cascade_rename(conn, old_name, new_name)

                # Entitled-module agent-scoped tables (ent#46) registered via
                # db.agent_cleanup.register_agent_owned_table. Kept separate
                # from the registry pass: these are runtime-registered by the
                # private submodule, so they are not in `AGENT_REFS` at import
                # time. Table/column come from code (not user input); values
                # are bound. Absent tables are skipped.
                from db.agent_cleanup import EXTRA_AGENT_REFS, _table_exists
                for table, column in EXTRA_AGENT_REFS:
                    if not _table_exists(conn, table):
                        continue
                    conn.execute(
                        text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
                        {"new": new_name, "old": old_name},
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
                COALESCE(ao.mcp_exposed, 0) as mcp_exposed,
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
                    "mcp_exposed": bool(row["mcp_exposed"]),
                    "memory_limit": row["memory_limit"],
                    "cpu_limit": row["cpu_limit"],
                    "github_repo": row["github_repo"],
                    "github_branch": row["github_branch"],
                    "is_shared_with_user": bool(row["is_shared_with_user"]),
                    "avatar_updated_at": row["avatar_updated_at"],
                }

            return result
