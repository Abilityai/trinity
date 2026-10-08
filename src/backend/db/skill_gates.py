"""The per-agent skill gate map (trinity-enterprise#753). SQL only.

One row per (agent, skill) that needs approval before it runs. ``skill_name`` is
stored lowercased: skill names are ASCII (``SKILL_NAME_RE``) and every matcher —
the dispatch check, the in-container hook, the clearance read — casefolds, so
the key is unique however a caller spells it.

``origin`` says where a row came from:

* ``set`` — a person or an orchestrator set it;
* ``library_default`` — the library recommends approval for the skill and it
  is assigned to the agent;
* ``cleared`` — a tombstone for a default the owner cleared. It gates nothing
  (``skill_gate_service.list_skill_gates`` skips it) and stops the reconcile
  re-applying the default while the skill stays assigned.

Every write runs under ``lock_agent_rows`` and re-reads what it depends on
(the agent's gate rows, its assignment rows) inside that transaction, so two
gate writes on one agent never act on each other's stale snapshot. The skill
SET writers take the same lock; ``db.assign_skill`` / ``db.unassign_skill`` do
not, so on PostgreSQL a single assign committing mid-transaction can still be
missed (SQLite's database-wide write lock closes it). A write lands only while
the agent has a live ownership row: a rename or purge that wins the race leaves
nothing under a name a later agent could inherit. SQLAlchemy Core, so it runs
unchanged on SQLite and PostgreSQL.
"""
from typing import Any, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import and_, delete, select

from .engine import get_engine, make_insert
from .skill_sets import lock_agent_rows
from .tables import agent_ownership, agent_skill_gates, agent_skills
from utils.helpers import utc_now_iso

ORIGIN_SET = "set"
ORIGIN_LIBRARY_DEFAULT = "library_default"
ORIGIN_CLEARED = "cleared"

_COLUMNS = (
    agent_skill_gates.c.agent_name,
    agent_skill_gates.c.skill_name,
    agent_skill_gates.c.approver,
    agent_skill_gates.c.deadline_hours,
    agent_skill_gates.c.origin,
    agent_skill_gates.c.set_by,
    agent_skill_gates.c.set_by_agent,
    agent_skill_gates.c.set_at,
)


def _key(skill_name: str) -> str:
    return (skill_name or "").lower()


def _row(conn, agent_name: str, skill_name: str) -> Optional[dict]:
    row = conn.execute(select(*_COLUMNS).where(
        agent_skill_gates.c.agent_name == agent_name,
        agent_skill_gates.c.skill_name == _key(skill_name),
    )).mappings().first()
    return dict(row) if row else None


def _live(conn, agent_name: str) -> bool:
    return conn.execute(select(agent_ownership.c.agent_name).where(
        agent_ownership.c.agent_name == agent_name,
        agent_ownership.c.deleted_at.is_(None),
    )).first() is not None


def _assigned(conn, agent_name: str) -> Set[str]:
    """The agent's library-assigned skill names, lowercased, read in `conn`."""
    return {r[0].lower() for r in conn.execute(
        select(agent_skills.c.skill_name).where(agent_skills.c.agent_name == agent_name)
    ) if r[0]}


def _delete(conn, agent_name: str, names: Iterable[str]) -> None:
    names = sorted(set(names))
    if names:
        conn.execute(delete(agent_skill_gates).where(
            agent_skill_gates.c.agent_name == agent_name,
            agent_skill_gates.c.skill_name.in_(names),
        ))


class SkillGateOperations:
    """Agent skill gate map operations."""

    def list_agent_skill_gates(self, agent_name: str) -> List[dict]:
        """Every row for the agent — tombstones included — by skill name.

        Raises on a failed read: the caller decides what an unreadable map
        means, and "nothing is gated" is never it."""
        stmt = (select(*_COLUMNS)
                .where(agent_skill_gates.c.agent_name == agent_name)
                .order_by(agent_skill_gates.c.skill_name))
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def write_skill_gate(self, agent_name: str, skill_name: str, *, changes: Mapping[str, Any],
                         origin: str, set_by: str,
                         set_by_agent: Optional[str]) -> Tuple[Optional[dict], Optional[dict], bool]:
        """Insert or update one row. ``changes`` holds only the fields to set
        (``approver``, ``deadline_hours``); a field it does not hold keeps the
        stored value (a tombstone has none: ``primary``, no deadline). The merge
        happens here, under the lock, so two partial writes never drop each
        other's field. A mapping, not a sentinel: a module reload must never
        turn "keep" into a value written to the row.

        Returns ``(previous, current, changed)``; ``(None, None, False)`` when the
        agent has no live ownership row, and ``changed`` False when the row
        already said exactly this (nothing is written)."""
        with get_engine().begin() as conn:
            lock_agent_rows(conn, agent_name)
            if not _live(conn, agent_name):
                return None, None, False
            previous = _row(conn, agent_name, skill_name)
            live = previous if previous and previous["origin"] != ORIGIN_CLEARED else None
            approver = changes["approver"] if "approver" in changes else (
                live["approver"] if live else "primary")
            deadline_hours = changes["deadline_hours"] if "deadline_hours" in changes else (
                live["deadline_hours"] if live else None)
            if (previous and previous["origin"] == origin
                    and (previous["approver"], previous["deadline_hours"]) == (approver, deadline_hours)):
                return previous, previous, False
            values = dict(approver=approver, deadline_hours=deadline_hours, origin=origin,
                          set_by=set_by, set_by_agent=set_by_agent, set_at=utc_now_iso())
            conn.execute(
                make_insert(agent_skill_gates)
                .values(agent_name=agent_name, skill_name=_key(skill_name), **values)
                .on_conflict_do_update(
                    index_elements=[agent_skill_gates.c.agent_name, agent_skill_gates.c.skill_name],
                    set_=values,
                )
            )
            return previous, _row(conn, agent_name, skill_name), True

    def clear_skill_gate(self, agent_name: str, skill_name: str, *, set_by: str,
                         set_by_agent: Optional[str],
                         recommended: Optional[Set[str]]) -> Tuple[Optional[dict], Optional[str]]:
        """Clear a gate. Returns ``(previous, action)``, action ``"deleted"``,
        ``"tombstoned"`` or None (nothing was gated).

        A name that is library-assigned AND recommended (``recommended`` holds
        the lowercased names; None = the library could not be read, so assume
        it is) gets a ``cleared`` tombstone instead of a delete: without it the
        reconcile would re-apply the default the owner just cleared. Any other
        clear deletes, so a later recommendation can still apply. The tombstone
        goes when the skill is unassigned.
        """
        with get_engine().begin() as conn:
            lock_agent_rows(conn, agent_name)
            previous = _row(conn, agent_name, skill_name)
            if previous is None or previous["origin"] == ORIGIN_CLEARED:
                return previous, None
            if (previous["skill_name"] in _assigned(conn, agent_name)
                    and (recommended is None or previous["skill_name"] in recommended)):
                values = dict(origin=ORIGIN_CLEARED, set_by=set_by, set_by_agent=set_by_agent,
                              set_at=utc_now_iso(), deadline_hours=None)
                conn.execute(agent_skill_gates.update().where(
                    agent_skill_gates.c.agent_name == agent_name,
                    agent_skill_gates.c.skill_name == previous["skill_name"],
                ).values(**values))
                return previous, "tombstoned"
            _delete(conn, agent_name, [previous["skill_name"]])
            return previous, "deleted"

    def reconcile_library_skill_gates(self, agent_name: str, recommended: Optional[Set[str]], *,
                                      set_by: str, may_drop) -> Tuple[List[dict], List[dict]]:
        """Bring the library-driven rows in line with the agent's assignments.

        Removes ``cleared`` tombstones whose skill is no longer assigned, and the
        ``library_default`` rows of unassigned skills that ``may_drop(name)``
        allows — the caller knows whether the package has left the agent, and a
        default stays while it may still be there. When ``recommended``
        (lowercased names) is not None, inserts a ``library_default`` row
        (``primary``, no deadline) for every assigned recommended name with no
        row. Never touches a ``set`` row, and never removes a default because the
        metadata changed. Returns ``(removed rows, inserted rows)``.
        """
        with get_engine().begin() as conn:
            lock_agent_rows(conn, agent_name)
            if not _live(conn, agent_name):
                return [], []
            assigned = _assigned(conn, agent_name)
            rows = [dict(r) for r in conn.execute(select(*_COLUMNS).where(
                agent_skill_gates.c.agent_name == agent_name)).mappings()]
            have = {r["skill_name"] for r in rows}
            removed = [r for r in rows
                       if r["skill_name"] not in assigned
                       and (r["origin"] == ORIGIN_CLEARED
                            or (r["origin"] == ORIGIN_LIBRARY_DEFAULT and may_drop(r["skill_name"])))]
            _delete(conn, agent_name, [r["skill_name"] for r in removed])
            inserted = []
            if recommended is not None:
                now = utc_now_iso()
                for name in sorted((assigned & {_key(n) for n in recommended}) - have):
                    values = dict(agent_name=agent_name, skill_name=name, approver="primary",
                                  deadline_hours=None, origin=ORIGIN_LIBRARY_DEFAULT,
                                  set_by=set_by, set_by_agent=None, set_at=now)
                    done = conn.execute(
                        make_insert(agent_skill_gates).values(**values).on_conflict_do_nothing(
                            index_elements=[agent_skill_gates.c.agent_name,
                                            agent_skill_gates.c.skill_name])
                    ).rowcount
                    if done:
                        inserted.append(values)
            return removed, inserted

    def drop_unassigned_skill_gates(self, agent_name: str, names: Iterable[str], *,
                                    keep: Iterable[str] = ()) -> List[dict]:
        """Delete the ``set`` rows for ``names`` that are STILL not assigned
        (a concurrent re-assign keeps its gate), except those in ``keep``.
        Returns the rows removed."""
        wanted = {_key(n) for n in names} - {_key(n) for n in keep}
        if not wanted:
            return []
        with get_engine().begin() as conn:
            lock_agent_rows(conn, agent_name)
            gone = wanted - _assigned(conn, agent_name)
            rows = [dict(r) for r in conn.execute(select(*_COLUMNS).where(and_(
                agent_skill_gates.c.agent_name == agent_name,
                agent_skill_gates.c.origin == ORIGIN_SET,
                agent_skill_gates.c.skill_name.in_(sorted(gone)),
            ))).mappings()] if gone else []
            _delete(conn, agent_name, [r["skill_name"] for r in rows])
            return sorted(rows, key=lambda r: r["skill_name"])
