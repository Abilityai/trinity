"""Per-agent respond→resume opt-in (ent#329).

An operator's answer to a parked operator-queue item is written back to the
agent's queue file within ~5s, but it is only *processed* at the agent's next
turn. An agent with no schedule and no heartbeat has no next turn, so an
approved action silently never executes until somebody re-triggers the agent by
hand. This flag lets the owner say "an answer should wake this agent".

Default ON since abilityai/trinity-enterprise#1108 (the operator's ruling after
the 2026-11-05 film: a person who approved in her Inbox had to write "go
ahead" in the chat before anything moved). The column is tri-state: NULL is
the platform default (on), 0 the owner's explicit OFF, 1 the owner's explicit
ON — so an owner's choice is still told apart from "never said", and the
switch stays per-AGENT rather than per-request:

* a dispatch on respond spends money, so the OWNER keeps the switch — a
  respond-storm still fans out one execution per answer, and the owner can
  turn that off for an agent that must not spend on answers;
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
reads either column directly — ``services/wake_policy.py`` is the only reader.
It reads this column RAW (``get_operator_resume_override``), because since
ent#1108 it is tri-state too: 1 is an owner's explicit ON made before the
wider switch existed, 0 is an owner's explicit OFF (the ent#1108 migration
moved every untouched ``DEFAULT 0`` to NULL, so a 0 that survives IS a
decision), and NULL is no opinion. The Settings PUT writes both columns in
step, so the two can only disagree on rows that predate ent#1102.
"""

from typing import Optional, Union

from sqlalchemy import and_, func, select, update

from ..engine import get_engine
from ..tables import agent_ownership


class _NoLiveRow:
    """What ``get_wake_on_ending`` returns for an agent with no live ownership row.

    Distinct from ``None`` (a live row whose column is NULL — "no opinion",
    follow the instance default) because the two must resolve differently: a
    NULL row is owned by somebody who has not spoken, a missing or soft-deleted
    row is owned by nobody, and a wake for an agent nobody owns is spend on
    nothing (the ent#1108 getter's rule). Falsy, so a reader that forgot the
    distinction reads it as "off", never as "on".
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NO_LIVE_ROW"


NO_LIVE_ROW = _NoLiveRow()


class OperatorResumeMixin:
    """Mixin for the per-agent respond→resume opt-in toggle (ent#329)."""

    def get_operator_resume_enabled(self, agent_name: str) -> bool:
        """Whether an operator answer should re-trigger this agent. Default: True.

        NULL — the owner never said — reads as the platform default, ON
        (abilityai/trinity-enterprise#1108); 0 is the owner's explicit OFF. A
        missing row or a soft-deleted agent still reads False: a wake for an
        agent nobody owns is spend on nothing.
        """
        stmt = select(
            func.coalesce(
                agent_ownership.c.operator_resume_enabled, 1
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

    def get_operator_resume_override(self, agent_name: str) -> Optional[bool]:
        """The ent#329 column RAW: ``None`` for NULL (no opinion, ent#1108), an
        unknown or a soft-deleted agent; ``False`` for the owner's explicit 0;
        ``True`` for 1. No COALESCE — this is the read ``wake_policy`` makes,
        and the platform default is its business, not this column's.
        """
        stmt = select(agent_ownership.c.operator_resume_enabled).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row or row["operator_resume_enabled"] is None:
            return None
        return bool(row["operator_resume_enabled"])

    def set_operator_resume_enabled(self, agent_name: str, enabled: Optional[bool]) -> bool:
        """Enable/disable respond→resume for one agent (``None`` clears it to
        NULL — the ent#1108 "follow the platform default" state).

        Returns True if a live row was updated (False for unknown or
        soft-deleted agents, so the caller can 404 rather than report success).
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
            .values(operator_resume_enabled=value)
        )
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    # -- ent#1102: the wider, tri-state wake switch ------------------------

    def get_wake_on_ending(self, agent_name: str) -> Union[Optional[bool], _NoLiveRow]:
        """This agent's wake override: ``None`` for "follow the instance default".

        Four answers, and the fourth is the point: ``None`` is a live row whose
        column is NULL (every row that predates the migration — an owner who has
        not spoken); ``NO_LIVE_ROW`` is an unknown or soft-deleted agent, which
        nobody owns, so the resolver spends nothing on it rather than handing it
        the instance default. An explicit 0 is a real answer and comes back as
        ``False``. No COALESCE here — one would collapse exactly the
        distinctions this column exists to keep.
        """
        stmt = select(agent_ownership.c.wake_on_ending).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return NO_LIVE_ROW
        if row["wake_on_ending"] is None:
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
