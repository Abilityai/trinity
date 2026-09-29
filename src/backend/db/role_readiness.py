"""The agent owner's readiness stamp for a role companion (ent#527 / #663).

One row per agent: `calibrating` | `ready`, when it changed, who flipped it.
Platform-side because `template.yaml`'s `x-role.status` is agent-writable and
the rule is that only the agent OWNER flips a companion and the agent never
can. SQLAlchemy Core so it runs unchanged on SQLite and PostgreSQL.
"""
from typing import Dict, Iterable, Optional

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

    def get_role_readiness_for_agents(self, agent_names: Iterable[str]) -> Dict[str, dict]:
        """The stamps of many agents in ONE query, for the agents list and the fleet
        grid (ent#527 rider, ruling 2026-09-24).

        `{name: {status, changed_at, source}}` for stamped agents only — an agent
        with no stamp is absent, never a guessed `calibrating` (whether it is a
        companion at all is in its template.yaml, which a list must not read).
        WHO flipped it is deliberately left out: the list is visible to every
        viewer of the agent, the role card (owner-scoped) keeps the person.
        `source` is `rollout` for the ent#689 seed, `owner` otherwise.
        """
        names = list(dict.fromkeys(n for n in agent_names if n))
        if not names:
            return {}
        stmt = select(
            agent_role_readiness.c.agent_name,
            agent_role_readiness.c.status,
            agent_role_readiness.c.changed_at,
            agent_role_readiness.c.changed_by,
        ).where(agent_role_readiness.c.agent_name.in_(names))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {
            r["agent_name"]: {
                "status": r["status"],
                "changed_at": r["changed_at"],
                "source": "rollout" if str(r["changed_by"] or "").startswith("rollout:") else "owner",
            }
            for r in rows
            if r["status"] in READINESS_STATES
        }

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
