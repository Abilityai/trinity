"""Per-agent A2A inbound-server exposure toggle (ent#157).

Edition-agnostic OSS primitive: when ``a2a_exposed=1`` the public A2A surface
(``GET /a2a/{name}/.well-known/agent-card.json`` + the JSON-RPC task endpoint)
serves/accepts the agent. Default OFF (safe by default). OSS owns the column +
the read/enforcement (the public routes read it); the WRITE is entitlement-gated
by the enterprise A2A module (the core-primitive + enterprise-knob pattern, like
``users.suspended_at``). Modeled on ``mcp_exposure.py`` — the ``deleted_at IS
NULL`` guard is load-bearing so a soft-deleted agent can never be flipped exposed.
"""

from typing import Dict, List

from sqlalchemy import and_, func, select, update

from ..engine import get_engine
from ..tables import agent_ownership


class A2AExposureMixin:
    """Mixin for the per-agent A2A-exposure opt-in toggle (ent#157)."""

    def get_a2a_exposed(self, agent_name: str) -> bool:
        """Whether the agent is exposed over the A2A inbound server. Default: False."""
        stmt = select(
            func.coalesce(agent_ownership.c.a2a_exposed, 0).label("a2a_exposed")
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row["a2a_exposed"]) if row else False

    def set_a2a_exposed(self, agent_name: str, enabled: bool) -> bool:
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
            .values(a2a_exposed=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def get_a2a_exposed_agents(self) -> List[Dict[str, str]]:
        """All live agents with ``a2a_exposed=1`` (``[{"agent_name": ...}]``)."""
        stmt = select(agent_ownership.c.agent_name).where(
            and_(
                func.coalesce(agent_ownership.c.a2a_exposed, 0) == 1,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            return [
                {"agent_name": row["agent_name"]}
                for row in conn.execute(stmt).mappings()
            ]
