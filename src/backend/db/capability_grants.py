"""Capabilities an instance admin grants to a named agent (trinity-enterprise#596).

One row per (agent, capability): who granted it and when. `skills.manage` is the
first — only a holder may change an agent's skills, its own included. The grant
is the ONLY thing that lets an agent-scoped key do that: an agent key resolves
to its owner carrying the owner's role (Invariant #8), so without a row here it
would pass every owner fence for every sibling the owner holds.

Rows only. Who may GRANT is the router's decision (admin + interactive); who may
USE a capability is `dependencies.capability_refusal` (via `enforce_agent_capability`). A soft-deleted agent
holds nothing — every read joins `agent_ownership` and filters `deleted_at` — and
recovery restores the grant with the rest of the agent's configuration, the way
every other per-agent setting behaves. SQLAlchemy Core, so it runs unchanged on
SQLite and PostgreSQL.
"""
from typing import List

from sqlalchemy import and_, delete, insert, select
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import agent_capability_grants, agent_ownership
from utils.helpers import utc_now_iso

#: Change an agent's skills — assign, replace, sync, remove — on any agent the
#: holder's owner holds, including the holder itself.
CAPABILITY_SKILLS_MANAGE = "skills.manage"

#: trinity-enterprise#164 (re-scoped 2026-10-02): the rest of "an agent changing
#: its own shape". Each is a PERMISSION an admin grants, never a per-action
#: approval — without it the call is refused and names what is missing.
#: Create / update / delete / enable / disable an agent's schedules, and its
#: schedules' webhooks.
CAPABILITY_SCHEDULES_MANAGE = "schedules.manage"
#: Platform writes to an agent's CLAUDE.md, AGENTS.md and `.claude/**` (except
#: `.claude/skills/**`, which stays `skills.manage`), and git reset-to-main.
CAPABILITY_INSTRUCTIONS_MANAGE = "instructions.manage"
#: Create (non-ephemeral) / delete / deploy agents and systems, and reconfigure
#: them (resources, timeout, guardrails, read-only, models, rename). Autonomy is
#: NOT in it: raising autonomy stays person-only and is never granted.
CAPABILITY_AGENTS_MANAGE = "agents.manage"

#: The closed set. A grant for anything else is refused at the sink, so a typo
#: can never persist a capability nothing checks.
CAPABILITIES = frozenset({
    CAPABILITY_SKILLS_MANAGE, CAPABILITY_SCHEDULES_MANAGE,
    CAPABILITY_INSTRUCTIONS_MANAGE, CAPABILITY_AGENTS_MANAGE,
})


def _live_holder(capability: str):
    """The join every read uses: a grant counts only while its agent is live."""
    return (
        select(
            agent_capability_grants.c.agent_name,
            agent_capability_grants.c.granted_by,
            agent_capability_grants.c.granted_at,
        )
        .select_from(
            agent_capability_grants.join(
                agent_ownership,
                agent_ownership.c.agent_name == agent_capability_grants.c.agent_name,
            )
        )
        .where(
            agent_capability_grants.c.capability == capability,
            agent_ownership.c.deleted_at.is_(None),
        )
    )


class CapabilityGrantOperations:
    """Agent capability grant operations."""

    def agent_has_capability(self, agent_name: str, capability: str) -> bool:
        """True only for a LIVE agent holding the grant. Unknown → False."""
        if not agent_name or capability not in CAPABILITIES:
            return False
        stmt = _live_holder(capability).where(
            agent_capability_grants.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def list_capability_holders(self, capability: str) -> List[dict]:
        """Every live agent holding `capability`, oldest grant first."""
        if capability not in CAPABILITIES:
            return []
        stmt = _live_holder(capability).order_by(
            agent_capability_grants.c.granted_at, agent_capability_grants.c.agent_name
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def list_agent_capabilities(self, agent_name: str) -> List[dict]:
        """Every capability this LIVE agent holds, with who and when (ent#164 —
        the Settings view, ent#756). A soft-deleted agent holds nothing."""
        if not agent_name:
            return []
        out = []
        for capability in sorted(CAPABILITIES):
            stmt = _live_holder(capability).where(
                agent_capability_grants.c.agent_name == agent_name)
            with get_engine().connect() as conn:
                row = conn.execute(stmt).mappings().first()
            if row:
                out.append({"capability": capability, "granted_by": row["granted_by"],
                            "granted_at": row["granted_at"]})
        return out

    def grant_agent_capability(self, agent_name: str, capability: str, granted_by: str) -> bool:
        """Grant. Idempotent: an existing grant keeps its original who/when.

        Returns True if a row was created, False if it already existed.
        """
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability: {capability!r}")
        try:
            with get_engine().begin() as conn:
                conn.execute(insert(agent_capability_grants).values(
                    agent_name=agent_name,
                    capability=capability,
                    granted_by=granted_by,
                    granted_at=utc_now_iso(),
                ))
            return True
        except IntegrityError:
            return False

    def revoke_agent_capability(self, agent_name: str, capability: str) -> bool:
        """Revoke. Returns True if a grant was removed."""
        with get_engine().begin() as conn:
            return conn.execute(
                delete(agent_capability_grants).where(and_(
                    agent_capability_grants.c.agent_name == agent_name,
                    agent_capability_grants.c.capability == capability,
                ))
            ).rowcount > 0

    def delete_agent_capability_grants(self, agent_name: str) -> int:
        """Every grant an agent holds (hard delete; the CASCADE AgentRef's twin)."""
        with get_engine().begin() as conn:
            return conn.execute(
                delete(agent_capability_grants).where(
                    agent_capability_grants.c.agent_name == agent_name
                )
            ).rowcount
