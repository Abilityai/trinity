"""
Agent skills database operations.

Manages skill assignments to agents. Skills themselves are stored in
a GitHub repository; this module only tracks which skills are assigned
to which agents.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged
on both SQLite and PostgreSQL. Queries are built from the ``agent_skills``
table handle in ``db/tables.py``; the engine is resolved via ``db/engine.py``.
"""

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import select, insert, delete, or_
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_skills, agent_ownership, users
from db_models import AgentSkill
from utils.helpers import utc_now_iso


class SkillsOperations:
    """Agent skills database operations."""

    @staticmethod
    def _row_to_skill(row) -> AgentSkill:
        """Convert a database row to an AgentSkill model."""
        return AgentSkill(
            id=row["id"],
            agent_name=row["agent_name"],
            skill_name=row["skill_name"],
            assigned_by=row["assigned_by"],
            assigned_at=datetime.fromisoformat(row["assigned_at"]),
            # ent#237: None on rows written before multi-source.
            source_id=row["source_id"],
        )

    # =========================================================================
    # Skill Assignment Operations
    # =========================================================================

    def get_agent_skills(self, agent_name: str) -> List[AgentSkill]:
        """
        Get all skills assigned to an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            List of AgentSkill objects
        """
        stmt = (
            select(
                agent_skills.c.id,
                agent_skills.c.agent_name,
                agent_skills.c.skill_name,
                agent_skills.c.assigned_by,
                agent_skills.c.assigned_at,
                agent_skills.c.source_id,
            )
            .where(agent_skills.c.agent_name == agent_name)
            .order_by(agent_skills.c.skill_name)
        )
        with get_engine().connect() as conn:
            return [self._row_to_skill(row) for row in conn.execute(stmt).mappings()]

    def get_agent_skill_names(self, agent_name: str) -> List[str]:
        """
        Get skill names assigned to an agent.

        Args:
            agent_name: Name of the agent

        Returns:
            List of skill names
        """
        stmt = (
            select(agent_skills.c.skill_name)
            .where(agent_skills.c.agent_name == agent_name)
            .order_by(agent_skills.c.skill_name)
        )
        with get_engine().connect() as conn:
            return [row["skill_name"] for row in conn.execute(stmt).mappings()]

    def assign_skill(
        self,
        agent_name: str,
        skill_name: str,
        assigned_by: str,
        source_id: Optional[str] = None,
    ) -> Optional[AgentSkill]:
        """
        Assign a skill to an agent.

        Args:
            agent_name: Name of the agent
            skill_name: Name of the skill
            assigned_by: Username of who is assigning
            source_id: ent#237 — which skill source the name resolved to at
                assignment time. Recorded, not keyed: the UNIQUE stays
                (agent_name, skill_name) because the agent-side identity is the
                bare directory `.claude/skills/<name>/` and two sources' copies
                cannot coexist there.

        Returns:
            AgentSkill object if created, None if already exists
        """
        now = utc_now_iso()

        stmt = insert(agent_skills).values(
            agent_name=agent_name,
            skill_name=skill_name,
            assigned_by=assigned_by,
            assigned_at=now,
            source_id=source_id,
        )
        try:
            with get_engine().begin() as conn:
                result = conn.execute(stmt)
                new_id = result.inserted_primary_key[0]

            return AgentSkill(
                id=new_id,
                agent_name=agent_name,
                skill_name=skill_name,
                assigned_by=assigned_by,
                assigned_at=datetime.fromisoformat(now),
                source_id=source_id,
            )
        except IntegrityError:
            # Skill already assigned
            return None

    def unassign_skill(self, agent_name: str, skill_name: str) -> bool:
        """
        Remove a skill assignment from an agent.

        Args:
            agent_name: Name of the agent
            skill_name: Name of the skill to remove

        Returns:
            True if a skill was removed
        """
        stmt = delete(agent_skills).where(
            agent_skills.c.agent_name == agent_name,
            agent_skills.c.skill_name == skill_name,
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def set_agent_skills(
        self,
        agent_name: str,
        skill_names: List[str],
        assigned_by: str,
        source_ids: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Set skills for an agent (full replacement).

        Removes all existing skills and assigns the new list.

        Args:
            agent_name: Name of the agent
            skill_names: List of skill names to assign
            assigned_by: Username of who is assigning
            source_ids: ent#237 — optional {skill_name: source_id} map recording
                which source each name resolved to. Missing entries store NULL
                rather than guessing, so an unrecorded origin is visibly unknown
                instead of falsely attributed.

        Returns:
            Number of skills assigned
        """
        now = utc_now_iso()
        source_ids = source_ids or {}

        with get_engine().begin() as conn:
            # Remove all existing skills for this agent
            conn.execute(
                delete(agent_skills).where(agent_skills.c.agent_name == agent_name)
            )

            # Add new skills
            for skill_name in skill_names:
                try:
                    with conn.begin_nested():
                        conn.execute(
                            insert(agent_skills).values(
                                agent_name=agent_name,
                                skill_name=skill_name,
                                assigned_by=assigned_by,
                                assigned_at=now,
                                source_id=source_ids.get(skill_name),
                            )
                        )
                except IntegrityError:
                    pass  # Skip duplicates

            return len(skill_names)

    def delete_agent_skills(self, agent_name: str) -> int:
        """
        Delete all skill assignments for an agent (cleanup on agent delete).

        Args:
            agent_name: Name of the agent

        Returns:
            Number of skills deleted
        """
        stmt = delete(agent_skills).where(agent_skills.c.agent_name == agent_name)
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount

    def is_skill_assigned(self, agent_name: str, skill_name: str) -> bool:
        """
        Check if a skill is assigned to an agent.

        Args:
            agent_name: Name of the agent
            skill_name: Name of the skill

        Returns:
            True if the skill is assigned
        """
        stmt = select(agent_skills.c.id).where(
            agent_skills.c.agent_name == agent_name,
            agent_skills.c.skill_name == skill_name,
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def get_agents_with_skill(self, skill_name: str) -> List[str]:
        """
        Get all agents that have a specific skill assigned.

        Args:
            skill_name: Name of the skill

        Returns:
            List of agent names
        """
        stmt = (
            select(agent_skills.c.agent_name)
            .where(agent_skills.c.skill_name == skill_name)
            .order_by(agent_skills.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [row["agent_name"] for row in conn.execute(stmt).mappings()]

    def get_assignable_agents(self, owner_username: Optional[str]) -> List[Dict]:
        """Agents the caller may ASSIGN a skill to (ent#386).

        Deliberately adjacent to ``get_all_skill_assignments`` and filtered
        identically (live rows only, ghosts excluded), because the two lists are
        read together by one screen: the holder chips come from that query and
        the assign dropdown from this one. If the exclusions drift apart, the
        dropdown offers an agent the holder list can never show — you assign a
        skill and nothing appears.

        ``owner_username=None`` means admin ⇒ no ownership filter, mirroring the
        ``visible is None`` convention the assignments route already uses. That
        is NOT the same as an empty result for a user who owns nothing, and
        collapsing the two would hand a non-admin the whole fleet.

        The predicate is the WRITE gate's, not the read's: the skill write
        routes take ``get_owned_agent_by_name`` (owner-or-admin), while the
        holder list is owned ∪ shared. A shared agent therefore appears as a
        holder but never as an assign target — correct, and the reason this
        cannot reuse the visibility helper.

        Ephemeral agents are excluded for the same reason the holder list
        excludes them: a ghost is hard-discarded at budget, so an assignment to
        one is a row that stops meaning anything within minutes.
        """
        stmt = (
            select(
                agent_ownership.c.agent_name,
                agent_ownership.c.display_label,
            )
            .select_from(
                agent_ownership.join(
                    users, agent_ownership.c.owner_id == users.c.id
                )
            )
            .where(agent_ownership.c.deleted_at.is_(None))
            .where(
                or_(
                    agent_ownership.c.is_ephemeral.is_(None),
                    agent_ownership.c.is_ephemeral == 0,
                )
            )
            .order_by(agent_ownership.c.agent_name)
        )
        if owner_username is not None:
            stmt = stmt.where(users.c.username == owner_username)

        with get_engine().connect() as conn:
            return [
                {
                    "agent_name": row["agent_name"],
                    "display_label": row["display_label"],
                }
                for row in conn.execute(stmt).mappings()
            ]

    def get_all_skill_assignments(self) -> List[Dict[str, Optional[str]]]:
        """Every (skill, agent) assignment in one statement (ent#384).

        Backs `GET /api/skills/assignments`, the fleet-wide "which agents hold
        this skill" read behind the Library's Skills tab. Deliberately ONE
        query rather than N calls to `get_agents_with_skill` — the Skills tab
        renders one block per library skill, and the per-block shape is the
        N+1 the ent#260 List view deleted rather than migrated.

        Three filters, each load-bearing rather than hygiene:

        * **INNER JOIN `agent_ownership`** — an `agent_skills` row whose
          ownership row is gone is a cascade orphan (canary L-03's territory),
          not a holder. Dropping it here is fail-closed.
        * **`deleted_at IS NULL`** — #834 preserves a soft-deleted agent's
          child rows for up to 180 days. The caller's access filter hides them
          from non-admins incidentally (the container is removed at delete),
          but admins read this unfiltered, so without the predicate a deleted
          agent renders as a current holder. Same class as ent#335, where a
          collector copied the schedule's own `deleted_at` filter but not the
          owning agent's and flagged 6,220 preserved rows.
        * **ephemeral excluded** — a ghost is hard-discarded at budget, so its
          chip would link to a page that 404s within minutes, and a fan-out
          burst would inflate every count. Heartbeat and fleet health exclude
          ghosts for the same reason. `is_ephemeral` is nullable on rows
          written before trinity-enterprise#69, so NULL must read as "not a
          ghost" — a bare `== 0` silently drops every pre-#69 agent.

        Returns rows as plain dicts (`skill_name`, `agent_name`,
        `display_label`) — no model, because the router's `response_model`
        owns the wire shape and the access filter runs between the two.
        `display_label` rides along so the caller needs no second query and no
        cross-store join for the ent#181 human-facing name (NULL ⇒ render the
        slug).
        """
        stmt = (
            select(
                agent_skills.c.skill_name,
                agent_skills.c.agent_name,
                agent_ownership.c.display_label,
            )
            .select_from(
                agent_skills.join(
                    agent_ownership,
                    agent_skills.c.agent_name == agent_ownership.c.agent_name,
                )
            )
            .where(agent_ownership.c.deleted_at.is_(None))
            .where(
                or_(
                    agent_ownership.c.is_ephemeral.is_(None),
                    agent_ownership.c.is_ephemeral == 0,
                )
            )
            .order_by(agent_skills.c.skill_name, agent_skills.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [
                {
                    "skill_name": row["skill_name"],
                    "agent_name": row["agent_name"],
                    "display_label": row["display_label"],
                }
                for row in conn.execute(stmt).mappings()
            ]
