"""Per-skill grants over an agent-permissions edge (abilityai/trinity-enterprise#662).

One row says: caller X may run library-assigned skill Y on target Z. Without a
row, a peer may invoke only Z's DECLARED skills — the rule lives in
`services/peer_skill_policy.py`; this module is rows only. Who may GRANT is the
router's decision (admin + interactive). A grant is effective only while the
edge exists: the edge still decides reach, this decides what the edge exposes.

SQLAlchemy Core, so it runs unchanged on SQLite and PostgreSQL.
"""
from typing import Dict, List, Set

from sqlalchemy import and_, delete, insert, or_, select
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_peer_skill_grants as grants, schedule_executions
from utils.helpers import utc_now_iso


class PeerSkillGrantOperations:
    """Per-skill peer grant operations."""

    def granted_peer_skills(self, caller_agent: str, target_agent: str) -> Set[str]:
        """The skill names `caller_agent` may run on `target_agent`."""
        stmt = select(grants.c.skill_name).where(and_(
            grants.c.caller_agent == caller_agent,
            grants.c.target_agent == target_agent,
        ))
        with get_engine().connect() as conn:
            return {row["skill_name"] for row in conn.execute(stmt).mappings()}

    def list_peer_skill_grants(self, caller_agent: str) -> Dict[str, Dict[str, dict]]:
        """Every grant `caller_agent` holds: ``{target: {skill: {granted_by, granted_at}}}``."""
        stmt = select(grants).where(grants.c.caller_agent == caller_agent)
        out: Dict[str, Dict[str, dict]] = {}
        with get_engine().connect() as conn:
            for row in conn.execute(stmt).mappings():
                out.setdefault(row["target_agent"], {})[row["skill_name"]] = {
                    "granted_by": row["granted_by"], "granted_at": row["granted_at"]}
        return out

    def grant_peer_skill(self, caller_agent: str, target_agent: str, skill_name: str,
                         granted_by: str) -> bool:
        """Grant. Idempotent: an existing grant keeps its original who/when.
        True if a row was created."""
        try:
            with get_engine().begin() as conn:
                conn.execute(insert(grants).values(
                    caller_agent=caller_agent, target_agent=target_agent,
                    skill_name=skill_name, granted_by=granted_by, granted_at=utc_now_iso()))
            return True
        except IntegrityError:
            return False

    def revoke_peer_skill(self, caller_agent: str, target_agent: str, skill_name: str) -> bool:
        """Revoke. True if a grant was removed."""
        with get_engine().begin() as conn:
            return conn.execute(delete(grants).where(and_(
                grants.c.caller_agent == caller_agent,
                grants.c.target_agent == target_agent,
                grants.c.skill_name == skill_name,
            ))).rowcount > 0

    def delete_agent_peer_skill_grants(self, agent_name: str) -> int:
        """Every grant naming the agent on either side (the CASCADE AgentRefs' twin)."""
        with get_engine().begin() as conn:
            return conn.execute(delete(grants).where(or_(
                grants.c.caller_agent == agent_name,
                grants.c.target_agent == agent_name,
            ))).rowcount

    def recent_peer_messages(self, caller_agent: str, target_agent: str, since: str,
                             limit: int = 500) -> List[dict]:
        """The messages `caller_agent` sent `target_agent` since `since` (ISO-Z),
        newest first — the execution ledger the upgrade report reads to tell an
        edge that USED a library skill from one that merely could."""
        stmt = (
            select(schedule_executions.c.message, schedule_executions.c.started_at)
            .where(and_(
                schedule_executions.c.agent_name == target_agent,
                schedule_executions.c.source_agent_name == caller_agent,
                schedule_executions.c.started_at >= since,
            ))
            .order_by(schedule_executions.c.started_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
