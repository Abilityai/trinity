"""
Agent autonomy mode and API key settings database operations.

Handles autonomy mode (scheduled task auto-execution) and platform API key toggle.
"""

from typing import Any, Dict

from sqlalchemy import delete, select, update, func

from ..engine import get_engine
from ..tables import (
    agent_ownership,
    agent_permissions,
    agent_reminders,
    agent_schedules,
)


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
        self, agent_name: str, *, to_user_id: int, expect: Dict[str, Any]
    ) -> bool:
        """Re-own an agent and every owner-carrying row, in one transaction
        (abilityai/trinity-enterprise#1147).

        ``expect`` is a ``{column: value}`` CAS guard on the live
        ``agent_ownership`` row (``None`` matches NULL). On a miss — the row
        is gone, deleted, or no longer matches — nothing is written and the
        call returns False; the caller decides whether to retry or fail. A
        key that is not an ``agent_ownership`` column raises ValueError.

        On a hit: ``agent_ownership`` takes the new owner, clears the spawn
        provenance (``spawned_by_agent``, ``spawned_by_key_id``,
        ``is_ephemeral``) and turns autonomy on; ``agent_schedules.owner_id``
        and ``agent_reminders.owner_id`` follow; the ``spawn:<spawner>`` edge
        in ``agent_permissions`` is removed. MCP keys are NOT touched —
        deactivating them is the caller's job.
        """
        unknown = sorted(set(expect) - set(agent_ownership.c.keys()))
        if unknown:
            raise ValueError(f"unknown agent_ownership column(s) in expect: {unknown}")

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
            result = conn.execute(
                update(agent_ownership)
                .where(guard)
                .values(
                    owner_id=to_user_id,
                    spawned_by_agent=None,
                    spawned_by_key_id=None,
                    is_ephemeral=0,
                    autonomy_enabled=1,
                )
            )
            if result.rowcount != 1:
                return False
            conn.execute(
                update(agent_schedules)
                .where(agent_schedules.c.agent_name == agent_name)
                .values(owner_id=to_user_id)
            )
            conn.execute(
                update(agent_reminders)
                .where(agent_reminders.c.agent_name == agent_name)
                .values(owner_id=to_user_id)
            )
            if spawner is not None:
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
