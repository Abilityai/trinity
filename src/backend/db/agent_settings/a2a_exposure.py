"""Per-agent A2A inbound-server exposure toggle (ent#157).

Edition-agnostic OSS primitive: when ``a2a_exposed=1`` the public A2A surface
(``GET /a2a/{name}/.well-known/agent-card.json`` + the JSON-RPC task endpoint)
serves/accepts the agent. Default OFF (safe by default). OSS owns the column +
the read/enforcement (the public routes read it); the WRITE is entitlement-gated
by the enterprise A2A module (the core-primitive + enterprise-knob pattern, like
``users.suspended_at``). Modeled on ``mcp_exposure.py`` — the ``deleted_at IS
NULL`` guard is load-bearing so a soft-deleted agent can never be flipped exposed.
"""

from typing import Any, Dict, List

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

    def get_a2a_scope(self, agent_name: str) -> Dict[str, Any]:
        """trinity-enterprise#838: ``{"scope": "public"|"internal", "keyless": bool}``.

        ``public`` (today's behaviour) unless the row says ``internal`` exactly —
        an unknown value reads as public, the scope a key or a payment already
        governs, never as a widening. ``keyless`` defaults on (it only ever
        applies to an internal-scope agent and a trusted-network caller)."""
        stmt = select(
            agent_ownership.c.a2a_scope, agent_ownership.c.a2a_keyless_internal,
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return {"scope": "public", "keyless": False}
        keyless = row["a2a_keyless_internal"]
        return {
            "scope": "internal" if row["a2a_scope"] == "internal" else "public",
            "keyless": True if keyless is None else bool(keyless),
        }

    def set_a2a_scope(self, agent_name: str, *, scope: str, keyless: bool) -> bool:
        """Set the scope and the keyless switch (trinity-enterprise#838). Same
        ``deleted_at IS NULL`` guard as the exposure toggle."""
        if scope not in ("public", "internal"):
            raise ValueError(f"unknown A2A scope: {scope!r}")
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(a2a_scope=scope, a2a_keyless_internal=1 if keyless else 0)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

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
