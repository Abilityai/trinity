"""Per-agent retrieval-memory opt-out (trinity-enterprise#1022).

The platform switch ``trinity_memory_enabled`` (default OFF) gates retrieval
memory for every agent; this column lets an owner keep one agent out once the
platform is on. Opt-OUT rather than opt-in on purpose: the platform switch is
the deliberate act, and an agent should not need a second one to take part.

Modeled on ``operator_resume.py`` — including its ``deleted_at IS NULL`` guard,
so a soft-deleted agent can never be flipped.
"""

from sqlalchemy import and_, func, select, update

from ..engine import get_engine
from ..tables import agent_ownership


class MemoryOptOutMixin:
    """Mixin for the per-agent retrieval-memory opt-out (trinity-enterprise#1022)."""

    def get_memory_opt_out(self, agent_name: str) -> bool:
        """Whether this agent is kept out of retrieval memory. Default: False.

        A missing row, a soft-deleted agent, or a NULL column (rows from before
        the migration) all read as False — the agent follows the platform switch.
        """
        stmt = select(
            func.coalesce(agent_ownership.c.memory_opt_out, 0).label("memory_opt_out")
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row["memory_opt_out"]) if row else False

    def set_memory_opt_out(self, agent_name: str, opt_out: bool) -> bool:
        """Keep one agent out of (or back into) retrieval memory.

        Returns True if a live row was updated (False for unknown or
        soft-deleted agents, so the caller can 404 rather than report success).
        """
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(memory_opt_out=1 if opt_out else 0)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0
