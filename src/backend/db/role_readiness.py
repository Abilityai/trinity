"""The agent owner's readiness stamp for a role companion (ent#527 / #663).

One row per agent: `calibrating` | `ready`, when it changed, who flipped it.
Platform-side because `template.yaml`'s `x-role.status` is agent-writable and
the rule is that only the agent OWNER flips a companion and the agent never
can. SQLAlchemy Core so it runs unchanged on SQLite and PostgreSQL.
"""
from typing import Optional

from sqlalchemy import select, delete, insert, update

from .engine import get_engine
from .tables import agent_role_readiness
from utils.helpers import utc_now_iso

READINESS_STATES = ("calibrating", "ready")


class RoleReadinessOperations:
    """Agent role-readiness stamp operations."""

    def get_role_readiness(self, agent_name: str) -> Optional[dict]:
        """The owner's stamp, or None when no owner has ever flipped this agent."""
        stmt = select(
            agent_role_readiness.c.status,
            agent_role_readiness.c.changed_at,
            agent_role_readiness.c.changed_by,
        ).where(agent_role_readiness.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None

    def set_role_readiness(self, agent_name: str, status: str, changed_by: str) -> dict:
        """Write the stamp (upsert). The caller has already decided WHO may."""
        if status not in READINESS_STATES:
            raise ValueError(f"unknown readiness state: {status!r}")
        now = utc_now_iso()
        with get_engine().begin() as conn:
            updated = conn.execute(
                update(agent_role_readiness)
                .where(agent_role_readiness.c.agent_name == agent_name)
                .values(status=status, changed_at=now, changed_by=changed_by)
            ).rowcount
            if not updated:
                conn.execute(insert(agent_role_readiness).values(
                    agent_name=agent_name, status=status, changed_at=now, changed_by=changed_by,
                ))
        return {"status": status, "changed_at": now, "changed_by": changed_by}

    def delete_role_readiness(self, agent_name: str) -> int:
        with get_engine().begin() as conn:
            return conn.execute(
                delete(agent_role_readiness).where(agent_role_readiness.c.agent_name == agent_name)
            ).rowcount
