"""Per-agent display label (ent#181).

A human-readable name an owner can change freely, while the agent's slug
(``agent_name``) never moves. The slug is the identity — routes, container and
volume names + their immutable Docker labels, MCP keys, A2A cards, Redis
keyspaces and every ``agent_name`` column key on it — so this column is
presentation only: it is rendered, never resolved.

That separation is the point. The §1.3 slug rename (RENAME-001) has to stop the
container, rewrite ~20 tables, clear every per-agent Redis keyspace, and *still*
leaves the agent's volumes under the old base, because Docker can rename neither
a volume nor its ``trinity.agent-name`` label — the root of the
#1664/#1665/#1667/#1669/#1671 family. Changing a label touches one column.

NULL means "use the slug": every existing agent renders exactly as before, no
backfill, and clearing a label reverts to the slug rather than blanking a name.
"""

from typing import Dict, List, Optional

from sqlalchemy import and_, select, update

from ..engine import get_engine
from ..tables import agent_ownership


class DisplayLabelMixin:
    """Mixin for the per-agent display label (ent#181)."""

    def get_display_label(self, agent_name: str) -> Optional[str]:
        """The agent's label, or None when it has none (render the slug).

        None is deliberately not coerced to ``agent_name`` here: callers that
        need the rendered name use :meth:`get_display_name`, while the API needs
        to tell "no label set" from "label happens to equal the slug" so the UI
        can show an empty field rather than a pre-filled one.
        """
        stmt = select(agent_ownership.c.display_label).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return row["display_label"] if row else None

    def get_display_name(self, agent_name: str) -> str:
        """What a human should see for this agent: label, else the slug."""
        return self.get_display_label(agent_name) or agent_name

    def get_display_labels_for_agents(self, agent_names: List[str]) -> Dict[str, str]:
        """``{agent_name: label}`` for the agents that HAVE one.

        Batch, because the agent list renders every agent on the dashboard: a
        per-agent read here would be an N+1 on the hottest endpoint. Mirrors
        ``get_tags_for_agents``, which the same list already uses. Agents
        without a label are simply absent from the map — the caller falls back
        to the slug, so an empty map means "everything renders as it does today".
        """
        if not agent_names:
            return {}
        stmt = select(
            agent_ownership.c.agent_name, agent_ownership.c.display_label
        ).where(
            and_(
                agent_ownership.c.agent_name.in_(agent_names),
                agent_ownership.c.display_label.isnot(None),
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            return {
                r["agent_name"]: r["display_label"]
                for r in conn.execute(stmt).mappings()
            }

    def set_display_label(self, agent_name: str, label: Optional[str]) -> bool:
        """Set (or clear, with None) the agent's label. True if a row changed.

        Guards ``deleted_at IS NULL`` like the other settings setters — a
        soft-deleted agent is on its way out and must not be edited.

        A blank/whitespace-only label is stored as NULL rather than as an empty
        string: an empty label would render as a nameless agent everywhere, and
        "clear it" is the only sane reading of submitting an empty box.
        """
        if label is not None:
            label = label.strip() or None
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_ownership)
                .where(
                    and_(
                        agent_ownership.c.agent_name == agent_name,
                        agent_ownership.c.deleted_at.is_(None),
                    )
                )
                .values(display_label=label)
            )
            return (result.rowcount or 0) > 0
