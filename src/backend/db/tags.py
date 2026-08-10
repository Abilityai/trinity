"""
Tag operations for agent organization (ORG-001).

Tags enable lightweight grouping of agents into logical systems.
Agents can have multiple tags, enabling multi-system membership.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the ``agent_tags`` table in
``db/tables.py``; the engine is resolved via ``db/engine.py``. Public API is
unchanged.
"""

from typing import List

from sqlalchemy import select, insert, delete, func, update

from .engine import get_engine, make_insert
from .tables import agent_tags
from db_models import AgentTagList, TagWithCount
from utils.helpers import utc_now_iso


# ---------------------------------------------------------------------------
# Org-overlay reserved namespaces (trinity-enterprise#305)
#
# The Grid view's org overlay stores departments and reporting lines as tags:
#   dept-<name>          department membership (on the agent itself)
#   reports-to-<agent>   "this agent reports to <agent>" (on the REPORT row)
#
# These prefixes are reserved against agent-principal writes at the router
# (mirrors the #1578 reserved event namespace: a prompt-injected agent must
# not redraw the org chart operators act on). Keep in sync with the frontend
# mirror in src/frontend/src/utils/gridOrg.js.
# ---------------------------------------------------------------------------

ORG_DEPT_PREFIX = "dept-"
ORG_REPORTS_PREFIX = "reports-to-"
ORG_TAG_PREFIXES = (ORG_DEPT_PREFIX, ORG_REPORTS_PREFIX)


def rename_reports_to_refs(conn, old_name: str, new_name: str) -> int:
    """
    Rewrite ``reports-to-<old_name>`` tag VALUES fleet-wide to point at
    ``new_name``. Runs on the caller's connection so it is atomic with the
    rename transaction (db/agent_settings/metadata.py:rename_agent).

    The `(agent_name, tag)` primary key makes a blind UPDATE abort whenever a
    row already holds ``reports-to-<new_name>`` (stale tag from a purged
    predecessor, hand-set duplicate) — and that IntegrityError would fail the
    WHOLE rename. So: delete the colliding rows first, then update.

    Returns the number of refs rewritten.
    """
    old_tag = ORG_REPORTS_PREFIX + old_name
    new_tag = ORG_REPORTS_PREFIX + new_name
    holders = select(agent_tags.c.agent_name).where(agent_tags.c.tag == old_tag)
    conn.execute(
        delete(agent_tags).where(
            agent_tags.c.tag == new_tag,
            agent_tags.c.agent_name.in_(holders),
        )
    )
    result = conn.execute(
        update(agent_tags).where(agent_tags.c.tag == old_tag).values(tag=new_tag)
    )
    return result.rowcount or 0


def delete_reports_to_refs(conn, agent_name: str) -> int:
    """
    Delete dangling ``reports-to-<agent_name>`` tag VALUES fleet-wide.

    Called from the hard-purge cascade (db/agent_cleanup.py:cascade_delete) —
    the row-level AGENT_REFS cascade removes the agent's OWN tag rows, but
    refs to it live on OTHER agents' rows and would otherwise silently
    re-attach if the name is ever reused after the retention window.
    Soft-delete keeps refs (render skips missing agents; recovery restores
    the lines intact).

    Returns the number of refs deleted.
    """
    result = conn.execute(
        delete(agent_tags).where(agent_tags.c.tag == ORG_REPORTS_PREFIX + agent_name)
    )
    return result.rowcount or 0


class TagOperations:
    """Database operations for agent tags."""

    def get_agent_tags(self, agent_name: str) -> List[str]:
        """Get all tags for an agent, sorted alphabetically."""
        stmt = (
            select(agent_tags.c.tag)
            .where(agent_tags.c.agent_name == agent_name)
            .order_by(agent_tags.c.tag)
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).all()]

    def set_agent_tags(self, agent_name: str, tags: List[str]) -> List[str]:
        """
        Replace all tags for an agent.

        Args:
            agent_name: The agent name
            tags: List of tags to set (replaces existing)

        Returns:
            The normalized, sorted list of tags
        """
        # Normalize tags: lowercase, strip whitespace, remove duplicates
        normalized = sorted(set(t.lower().strip() for t in tags if t.strip()))

        with get_engine().begin() as conn:
            # Delete existing tags
            conn.execute(
                delete(agent_tags).where(agent_tags.c.agent_name == agent_name)
            )

            # Insert new tags
            now = utc_now_iso()
            for tag in normalized:
                conn.execute(
                    insert(agent_tags).values(
                        agent_name=agent_name, tag=tag, created_at=now
                    )
                )

        return normalized

    def add_tag(self, agent_name: str, tag: str) -> List[str]:
        """
        Add a single tag to an agent.

        Args:
            agent_name: The agent name
            tag: Tag to add

        Returns:
            Updated list of all tags for the agent
        """
        normalized_tag = tag.lower().strip()
        if not normalized_tag:
            return self.get_agent_tags(agent_name)

        now = utc_now_iso()

        # Use ON CONFLICT DO NOTHING to handle duplicates (composite PK)
        stmt = make_insert(agent_tags).values(
            agent_name=agent_name, tag=normalized_tag, created_at=now
        ).on_conflict_do_nothing(index_elements=["agent_name", "tag"])
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_agent_tags(agent_name)

    def remove_tag(self, agent_name: str, tag: str) -> List[str]:
        """
        Remove a single tag from an agent.

        Args:
            agent_name: The agent name
            tag: Tag to remove

        Returns:
            Updated list of all tags for the agent
        """
        normalized_tag = tag.lower().strip()

        stmt = delete(agent_tags).where(
            agent_tags.c.agent_name == agent_name,
            agent_tags.c.tag == normalized_tag,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return self.get_agent_tags(agent_name)

    def list_all_tags(self) -> List[TagWithCount]:
        """
        List all unique tags with agent counts.

        Returns:
            List of tags with their usage counts, sorted by count descending
        """
        count_col = func.count().label("count")
        stmt = (
            select(agent_tags.c.tag, count_col)
            .group_by(agent_tags.c.tag)
            .order_by(count_col.desc(), agent_tags.c.tag.asc())
        )
        with get_engine().connect() as conn:
            return [
                TagWithCount(tag=row[0], count=row[1])
                for row in conn.execute(stmt).all()
            ]

    def get_agents_by_tag(self, tag: str) -> List[str]:
        """
        Get all agent names that have a specific tag.

        Args:
            tag: Tag to search for

        Returns:
            List of agent names with this tag
        """
        normalized_tag = tag.lower().strip()

        stmt = (
            select(agent_tags.c.agent_name)
            .where(agent_tags.c.tag == normalized_tag)
            .order_by(agent_tags.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).all()]

    def get_agents_by_tags(self, tags: List[str]) -> List[str]:
        """
        Get all agent names that have ANY of the specified tags (OR logic).

        Args:
            tags: List of tags to search for

        Returns:
            List of unique agent names with any of these tags
        """
        if not tags:
            return []

        normalized_tags = [t.lower().strip() for t in tags if t.strip()]
        if not normalized_tags:
            return []

        stmt = (
            select(agent_tags.c.agent_name)
            .where(agent_tags.c.tag.in_(normalized_tags))
            .distinct()
            .order_by(agent_tags.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt).all()]

    def delete_agent_tags(self, agent_name: str) -> None:
        """
        Delete all tags for an agent (called when agent is deleted).

        Args:
            agent_name: The agent name
        """
        stmt = delete(agent_tags).where(agent_tags.c.agent_name == agent_name)
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_tags_for_agents(self, agent_names: List[str]) -> dict:
        """
        Batch get tags for multiple agents.

        Args:
            agent_names: List of agent names

        Returns:
            Dict mapping agent_name -> list of tags
        """
        if not agent_names:
            return {}

        stmt = (
            select(agent_tags.c.agent_name, agent_tags.c.tag)
            .where(agent_tags.c.agent_name.in_(agent_names))
            .order_by(agent_tags.c.agent_name, agent_tags.c.tag)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        result = {name: [] for name in agent_names}
        for row in rows:
            result[row[0]].append(row[1])

        return result
