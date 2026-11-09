"""
Agent autonomy mode and API key settings database operations.

Handles autonomy mode (scheduled task auto-execution) and platform API key toggle.
"""

from typing import Any, Dict, Optional

from sqlalchemy import delete, select, update, func

from ..engine import get_engine
from ..tables import (
    agent_ownership,
    agent_permissions,
    agent_reminders,
    agent_schedules,
)

# Every column that names an agent's owner, per agent — the write-set of
# `transfer_agent_ownership`. A new owner-carrying column on a table keyed by
# `agent_name` joins this tuple, or tests/unit/test_ent1147_transfer_agent_ownership.py
# fails (the transfer would otherwise leave it pointing at the previous owner).
TRANSFER_OWNER_COLUMNS = (
    (agent_ownership, "owner_id"),
    (agent_schedules, "owner_id"),
    (agent_reminders, "owner_id"),
)

_SPAWN_COLUMNS = ("spawned_by_agent", "spawned_by_key_id")


class AutonomyMixin:
    """Mixin for agent autonomy mode and API key settings."""

    # =========================================================================
    # Agent API Key Settings
    # =========================================================================

    def get_use_platform_api_key(self, agent_name: str) -> bool:
        """Check if agent should use platform API key (default: True)."""
        stmt = select(
            func.coalesce(agent_ownership.c.use_platform_api_key, 1).label(
                "use_platform_api_key"
            )
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return bool(row["use_platform_api_key"])
        return True  # Default to using platform key

    def set_use_platform_api_key(self, agent_name: str, use_platform_key: bool) -> bool:
        """Set whether agent should use platform API key."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(use_platform_api_key=1 if use_platform_key else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    # =========================================================================
    # Autonomy Mode
    # =========================================================================

    def get_autonomy_enabled(self, agent_name: str) -> bool:
        """Check if autonomy mode is enabled for agent (scheduled tasks run automatically)."""
        stmt = select(
            func.coalesce(agent_ownership.c.autonomy_enabled, 0).label(
                "autonomy_enabled"
            )
        ).where(
            (agent_ownership.c.agent_name == agent_name)
            & (agent_ownership.c.deleted_at.is_(None))
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row:
            return bool(row["autonomy_enabled"])
        return False  # Default to disabled

    def set_autonomy_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Set whether autonomy mode is enabled for agent."""
        stmt = (
            update(agent_ownership)
            .where(agent_ownership.c.agent_name == agent_name)
            .values(autonomy_enabled=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def transfer_agent_ownership(
        self,
        agent_name: str,
        *,
        to_user_id: int,
        expect: Dict[str, Any],
        clear_spawn_provenance: bool,
        clear_ephemeral: bool,
        enable_autonomy: bool,
        restore_spawn_provenance: Optional[Dict[str, Optional[str]]] = None,
    ) -> bool:
        """Re-own an agent and every owner-carrying row, in one transaction
        (abilityai/trinity-enterprise#1147).

        ``expect`` is a ``{column: value}`` CAS guard on the live
        ``agent_ownership`` row (``None`` matches NULL). On a miss — the row
        is gone, deleted, or no longer matches — nothing is written and the
        call returns False; the caller decides whether to retry or fail. A
        key that is not an ``agent_ownership`` column raises ValueError.

        On a hit, every column in ``TRANSFER_OWNER_COLUMNS`` takes
        ``to_user_id``: the ownership row's ``owner_id`` and the agent's
        ``agent_schedules`` / ``agent_reminders`` rows. Nothing else about the
        agent changes unless the caller asks for it by name — every option
        below is required, so no call changes agent state by default:

        * ``clear_spawn_provenance`` — null ``spawned_by_agent`` and
          ``spawned_by_key_id``, and remove the ``spawn:<spawner>`` edge in
          ``agent_permissions``;
        * ``clear_ephemeral`` — set ``is_ephemeral`` to 0;
        * ``enable_autonomy`` — set ``autonomy_enabled`` to 1 (False leaves it
          as it is; it never turns autonomy off);
        * ``restore_spawn_provenance`` — ``{"spawned_by_agent": ...,
          "spawned_by_key_id": ...}`` written back onto the row (an inverse
          transfer); exclusive with ``clear_spawn_provenance``.

        MCP keys are NOT touched — deactivating them is the caller's job.
        """
        unknown = sorted(set(expect) - set(agent_ownership.c.keys()))
        if unknown:
            raise ValueError(f"unknown agent_ownership column(s) in expect: {unknown}")
        if restore_spawn_provenance is not None:
            if clear_spawn_provenance:
                raise ValueError(
                    "clear_spawn_provenance and restore_spawn_provenance are exclusive"
                )
            extra = sorted(set(restore_spawn_provenance) - set(_SPAWN_COLUMNS))
            if extra:
                raise ValueError(f"not a spawn provenance column: {extra}")

        values: Dict[str, Any] = {
            column: to_user_id
            for table, column in TRANSFER_OWNER_COLUMNS
            if table is agent_ownership
        }
        if clear_spawn_provenance:
            values.update({column: None for column in _SPAWN_COLUMNS})
        if restore_spawn_provenance is not None:
            values.update(restore_spawn_provenance)
        if clear_ephemeral:
            values["is_ephemeral"] = 0
        if enable_autonomy:
            values["autonomy_enabled"] = 1

        live = (agent_ownership.c.agent_name == agent_name) & (
            agent_ownership.c.deleted_at.is_(None)
        )
        with get_engine().begin() as conn:
            spawner = conn.execute(
                select(agent_ownership.c.spawned_by_agent).where(live)
            ).scalar()
            guard = live & agent_ownership.c.spawned_by_agent.is_not_distinct_from(spawner)
            for column, value in expect.items():
                guard = guard & agent_ownership.c[column].is_not_distinct_from(value)
            result = conn.execute(update(agent_ownership).where(guard).values(**values))
            if result.rowcount != 1:
                return False
            for table, column in TRANSFER_OWNER_COLUMNS:
                if table is agent_ownership:
                    continue
                conn.execute(
                    update(table)
                    .where(table.c.agent_name == agent_name)
                    .values({column: to_user_id})
                )
            if clear_spawn_provenance and spawner is not None:
                conn.execute(
                    delete(agent_permissions).where(
                        (agent_permissions.c.source_agent == spawner)
                        & (agent_permissions.c.target_agent == agent_name)
                        & (agent_permissions.c.created_by == f"spawn:{spawner}")
                    )
                )
        return True

    def get_all_agents_autonomy_status(self) -> Dict[str, bool]:
        """Get autonomy status for all agents (for dashboard display)."""
        stmt = select(
            agent_ownership.c.agent_name,
            func.coalesce(agent_ownership.c.autonomy_enabled, 0).label(
                "autonomy_enabled"
            ),
        ).where(agent_ownership.c.deleted_at.is_(None))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {row["agent_name"]: bool(row["autonomy_enabled"]) for row in rows}
