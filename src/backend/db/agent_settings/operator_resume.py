"""Per-agent respond→resume opt-in (ent#329).

An operator's answer to a parked operator-queue item is written back to the
agent's queue file within ~5s, but it is only *processed* at the agent's next
turn. An agent with no schedule and no heartbeat has no next turn, so an
approved action silently never executes until somebody re-triggers the agent by
hand. This flag lets the owner say "an answer should wake this agent".

Default OFF, and the switch is per-AGENT rather than per-request:

* a dispatch on respond spends money, so it must never be unconditional — a
  respond-storm would otherwise fan out one execution per answer;
* an agent-declared ``resume: true`` on the item would let the agent decide that
  answering costs the answerer money, which is exactly what ent#430 AC #3 rules
  out for Workspace clients.

Edition-agnostic OSS primitive (#995 split): OSS owns the column, the dispatch
and its enforcement; the entitled Workspace surface only renders the ask.

Modeled on the circuit-breaker getter/setter in ``resources.py`` — including its
``deleted_at IS NULL`` guard, so a soft-deleted agent can never be flipped on.

ent#1102 adds the wider sibling ``wake_on_ending``, which covers both this
answer-wake and the end of a run the agent delegated. It is **tri-state**
(NULL / 0 / 1) where this one is boolean, because its instance default is ON:
an ``INTEGER DEFAULT 0`` cannot tell "nobody ever touched this" from "the owner
said off", so flipping the default would silently discard the second. Nothing
reads either column directly — ``services/wake_policy.py`` is the only reader,
and it treats ``operator_resume_enabled = 1`` as an explicit opt-in made before
the tri-state existed.
"""

from typing import Optional

from sqlalchemy import and_, func, select, update

from ..engine import get_engine
from ..tables import agent_ownership


class OperatorResumeMixin:
    """Mixin for the per-agent respond→resume opt-in toggle (ent#329)."""

    def get_operator_resume_enabled(self, agent_name: str) -> bool:
        """Whether an operator answer should re-trigger this agent. Default: False.

        Fail-safe by construction: a missing row, a soft-deleted agent, or a NULL
        column (every row that existed before the migration) all read as False,
        so the dispatch stays off for everyone who did not ask for it.
        """
        stmt = select(
            func.coalesce(
                agent_ownership.c.operator_resume_enabled, 0
            ).label("operator_resume_enabled")
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return bool(row["operator_resume_enabled"]) if row else False

    def set_operator_resume_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable/disable respond→resume for one agent.

        Returns True if a live row was updated (False for unknown or
        soft-deleted agents, so the caller can 404 rather than report success).
        """
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(operator_resume_enabled=1 if enabled else 0)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    # -- ent#1102: the wider, tri-state wake switch ------------------------

    def get_wake_on_ending(self, agent_name: str) -> Optional[bool]:
        """This agent's wake override, or ``None`` for "follow the instance default".

        Three states, and the third is the point: ``None`` is returned both for a
        NULL column (every row that predates the migration) and for an unknown or
        soft-deleted agent, because neither has expressed an opinion. An explicit
        0 is a real answer and comes back as ``False``. No COALESCE here — one
        would collapse exactly the distinction this column exists to keep.
        """
        stmt = select(agent_ownership.c.wake_on_ending).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row or row["wake_on_ending"] is None:
            return None
        return bool(row["wake_on_ending"])

    def set_wake_on_ending(self, agent_name: str, enabled: Optional[bool]) -> bool:
        """Set (or clear, with ``None``) one agent's wake override.

        Returns True if a live row was updated (False for unknown or soft-deleted
        agents, so the caller can 404 rather than report success).
        """
        value = None if enabled is None else (1 if enabled else 0)
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(wake_on_ending=value)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0
