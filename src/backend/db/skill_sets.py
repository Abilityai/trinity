"""Skill-set assignment rows (trinity-enterprise#530). SQL only.

A set assigned to an agent is one ``agent_skill_sets`` row; its members are
materialised as ``agent_skills`` rows with ``individual = 0`` (a member that was
also assigned on its own keeps ``individual = 1``). Every mutation here ends in
``_apply`` — one transaction that re-reads the agent's rows and assigned sets and
re-plans them with the pure rule in ``services.skill_sets.plan_member_rows`` —
so two set operations on one agent cannot leave a shared member orphaned or
missing. On PostgreSQL the agent's ownership row is locked FOR UPDATE for the
duration; on SQLite a no-op write takes the RESERVED lock up front
(``lock_agent_rows``), so the transaction's reads cannot go stale.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import and_, delete, false, insert, select, update
from sqlalchemy.exc import IntegrityError

from .engine import get_engine, is_sqlite, make_insert
from .tables import agent_ownership, agent_skill_sets, agent_skills
from utils.helpers import utc_now_iso

Resolved = Dict[str, Optional[List[str]]]


def lock_agent_rows(conn, agent_name: str) -> None:
    """Serialise every writer of one agent's skill rows for this transaction.

    PostgreSQL: the agent's ownership row FOR UPDATE. SQLite: a no-op write
    that makes pysqlite open the transaction and take the RESERVED lock NOW, so
    the reads that follow cannot go stale under a concurrent writer (a deferred
    transaction would only lock at its first real write — review finding 9).
    """
    if is_sqlite():
        conn.execute(update(agent_skill_sets).where(false()).values(set_name=agent_skill_sets.c.set_name))
    else:
        conn.execute(select(agent_ownership.c.agent_name)
                     .where(agent_ownership.c.agent_name == agent_name)
                     .with_for_update())


def agent_held_sets(conn, agent_name: str) -> Dict[str, Optional[str]]:
    """``set → source_id`` the agent holds, read on ``conn``."""
    return {r[0]: r[1] for r in conn.execute(
        select(agent_skill_sets.c.set_name, agent_skill_sets.c.source_id)
        .where(agent_skill_sets.c.agent_name == agent_name))}


class SkillSetsOperations:
    # ------------------------------------------------------------------ reads

    def list_agent_sets(self, agent_name: str) -> List[dict]:
        stmt = (select(agent_skill_sets)
                .where(agent_skill_sets.c.agent_name == agent_name)
                .order_by(agent_skill_sets.c.set_name))
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def agent_set_names(self, agent_name: str) -> List[str]:
        return [r["set_name"] for r in self.list_agent_sets(agent_name)]

    def skill_rows(self, agent_name: str, conn=None) -> Dict[str, bool]:
        """skill → individual? (a NULL individual reads as individual)."""
        stmt = select(agent_skills.c.skill_name, agent_skills.c.individual).where(
            agent_skills.c.agent_name == agent_name)
        if conn is not None:
            return {r[0]: r[1] is None or bool(r[1]) for r in conn.execute(stmt)}
        with get_engine().connect() as c:
            return {r[0]: r[1] is None or bool(r[1]) for r in c.execute(stmt)}

    # --------------------------------------------------------------- writes

    def _lock(self, conn, agent_name: str) -> None:
        lock_agent_rows(conn, agent_name)

    def _apply(self, conn, agent_name: str, resolved: Resolved,
               assigned_by: str, assigned_by_agent: Optional[str]) -> Tuple[List[str], List[str]]:
        from services.skill_sets import plan_member_rows

        rows = self.skill_rows(agent_name, conn)
        assigned = [r[0] for r in conn.execute(
            select(agent_skill_sets.c.set_name).where(agent_skill_sets.c.agent_name == agent_name))]
        add, remove = plan_member_rows(rows, assigned, resolved)
        now = utc_now_iso()
        added = []
        for name in sorted(add):
            # A plain insert in a savepoint, not ON CONFLICT: `add` already
            # excludes every row this transaction read, and not every install's
            # agent_skills carries the UNIQUE the conflict target would need.
            try:
                with conn.begin_nested():
                    conn.execute(insert(agent_skills).values(
                        agent_name=agent_name, skill_name=name, assigned_by=assigned_by,
                        assigned_at=now, assigned_by_agent=assigned_by_agent, individual=0,
                    ))
                added.append(name)
            except IntegrityError:
                pass   # a concurrent individual assign landed first — it stands
        if remove:
            conn.execute(delete(agent_skills).where(
                agent_skills.c.agent_name == agent_name,
                agent_skills.c.skill_name.in_(sorted(remove)),
                agent_skills.c.individual == 0,
            ))
        return added, sorted(remove)

    def assign_set(self, agent_name: str, set_name: str, source_id: Optional[str], assigned_by: str,
                   assigned_by_agent: Optional[str], resolved: Resolved) -> Tuple[bool, List[str], List[str]]:
        """(created?, added members, removed members). Idempotent."""
        with get_engine().begin() as conn:
            self._lock(conn, agent_name)
            created = conn.execute(
                make_insert(agent_skill_sets).values(
                    agent_name=agent_name, set_name=set_name, source_id=source_id,
                    assigned_by=assigned_by, assigned_by_agent=assigned_by_agent, assigned_at=utc_now_iso(),
                ).on_conflict_do_nothing(index_elements=[agent_skill_sets.c.agent_name, agent_skill_sets.c.set_name])
            ).rowcount > 0
            if not created:
                # Re-assigning re-points a drifted set at the source that owns it now.
                conn.execute(update(agent_skill_sets).where(
                    agent_skill_sets.c.agent_name == agent_name,
                    agent_skill_sets.c.set_name == set_name).values(source_id=source_id))
            add, remove = self._apply(conn, agent_name, resolved, assigned_by, assigned_by_agent)
        return created, add, remove

    def unassign_set(self, agent_name: str, set_name: str, resolved: Resolved,
                     assigned_by: str) -> Tuple[bool, List[str], List[str]]:
        """(was assigned?, added, removed)."""
        with get_engine().begin() as conn:
            self._lock(conn, agent_name)
            existed = conn.execute(delete(agent_skill_sets).where(
                agent_skill_sets.c.agent_name == agent_name,
                agent_skill_sets.c.set_name == set_name)).rowcount > 0
            add, remove = self._apply(conn, agent_name, resolved, assigned_by, None)
        return existed, add, remove

    def reconcile(self, agent_name: str, resolved: Resolved) -> Tuple[List[str], List[str]]:
        """Bring set-derived rows in line with the current catalogs (fail-closed)."""
        with get_engine().begin() as conn:
            self._lock(conn, agent_name)
            return self._apply(conn, agent_name, resolved, "system", None)

    def replace_sets(self, agent_name: str, set_names: List[str], source_ids: Dict[str, Optional[str]],
                     assigned_by: str, assigned_by_agent: Optional[str],
                     resolved: Resolved) -> Tuple[List[str], List[str]]:
        """Bulk replace of the agent's assigned sets (the PUT's `sets` field)."""
        with get_engine().begin() as conn:
            self._lock(conn, agent_name)
            current = {r[0] for r in conn.execute(
                select(agent_skill_sets.c.set_name).where(agent_skill_sets.c.agent_name == agent_name))}
            wanted = set(set_names)
            if current - wanted:
                conn.execute(delete(agent_skill_sets).where(
                    agent_skill_sets.c.agent_name == agent_name,
                    agent_skill_sets.c.set_name.in_(sorted(current - wanted))))
            now = utc_now_iso()
            for name in sorted(wanted - current):
                conn.execute(make_insert(agent_skill_sets).values(
                    agent_name=agent_name, set_name=name, source_id=source_ids.get(name),
                    assigned_by=assigned_by, assigned_by_agent=assigned_by_agent, assigned_at=now,
                ).on_conflict_do_nothing(index_elements=[agent_skill_sets.c.agent_name, agent_skill_sets.c.set_name]))
            return self._apply(conn, agent_name, resolved, assigned_by, assigned_by_agent)

    def set_individual(self, agent_name: str, skill_name: str, individual: bool) -> bool:
        with get_engine().begin() as conn:
            return conn.execute(update(agent_skills).where(and_(
                agent_skills.c.agent_name == agent_name,
                agent_skills.c.skill_name == skill_name,
            )).values(individual=1 if individual else 0)).rowcount > 0
