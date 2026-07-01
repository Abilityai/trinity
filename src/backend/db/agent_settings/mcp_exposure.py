"""Per-agent MCP exposure toggle (#846).

Per-agent opt-in flag that controls whether the Trinity MCP server dynamically
registers a dedicated ``chat_with_<slug>`` tool for the agent. When enabled, the
MCP server (which polls the backend) adds a tool functionally identical to
``chat_with_agent`` with the agent name pre-filled, and FastMCP pushes
``notifications/tools/list_changed`` to connected clients.

The flag publishes a surface only — execution always runs the same
``checkAgentAccess`` gate, so ownership/sharing is never bypassed.

Modeled on the circuit-breaker getter/setter in ``resources.py`` (NOT
``file_sharing.py`` — its setter omits the ``deleted_at IS NULL`` guard; we must
not flip a soft-deleted agent into exposed state).
"""

from typing import List, Dict

from sqlalchemy import select, update, func, and_

from ..engine import get_engine
from ..tables import agent_ownership


class McpExposureMixin:
    """Mixin for the per-agent MCP-exposure opt-in toggle (#846)."""

    def get_mcp_exposed(self, agent_name: str) -> bool:
        """Whether the agent is exposed as a dedicated MCP tool. Default: False."""
        stmt = select(
            func.coalesce(agent_ownership.c.mcp_exposed, 0).label("mcp_exposed")
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row["mcp_exposed"]) if row else False

    def set_mcp_exposed(self, agent_name: str, enabled: bool) -> bool:
        """Flip the toggle. Guards ``deleted_at IS NULL`` so a soft-deleted agent
        can never be flipped into exposed state. Returns True if a row updated."""
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(mcp_exposed=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_mcp_exposed_agents(self) -> List[Dict[str, str]]:
        """All live agents with ``mcp_exposed=1``.

        Returns ``[{"agent_name": ...}, ...]`` (DB-only; the dedicated-tool
        ``tool_name``/``description`` are computed at the service/router layer
        from the shared slug helper + cheap Docker metadata).
        """
        stmt = select(agent_ownership.c.agent_name).where(
            and_(
                func.coalesce(agent_ownership.c.mcp_exposed, 0) == 1,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            return [
                {"agent_name": row["agent_name"]}
                for row in conn.execute(stmt).mappings()
            ]
