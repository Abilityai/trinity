"""Guardrail events — database operations (trinity-enterprise#165, GUARD-005).

The store behind `POST /api/agents/{name}/guardrail-events`: one row per event an
agent's guardrail hooks reported. Rows arrive already normalized by
`services/guardrail_event_service.py`; this module holds the SQL only
(Invariant #1).

* **Idempotent insert.** `UNIQUE(agent_name, event_id)` plus
  `ON CONFLICT DO NOTHING ... RETURNING id` on both dialects (`make_insert`), so
  a resend writes nothing and the caller learns exactly how many rows were new.
  `rowcount` is not a portable count across a multi-VALUES upsert (the
  `db/metric_points.py` note), RETURNING is.
* **Keyset paging.** Newest first on `(occurred_at, id)`; the cursor is that
  pair, so a page boundary inside one timestamp neither repeats nor skips.
* **Sweep primitives.** `count_guardrail_event_candidates` and
  `prune_guardrail_events` share ONE predicate (`received_at < cutoff`) by
  construction, so the blast-radius guard counts the rows the delete removes.
  The sweep keys on the SERVER receive time: `occurred_at` is agent-reported,
  and a far-future value must not make a row immortal.

SQLAlchemy Core over `db/tables.py`, so it runs on SQLite and PostgreSQL.
"""

from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, func, or_, select

from .engine import get_engine, make_insert
from .tables import guardrail_events
from utils.helpers import iso_cutoff

# One prune call deletes at most this many chunks, so the cleanup cycle cannot
# be monopolised by a table whose window just narrowed (TD-14).
MAX_CHUNKS_PER_PRUNE = 20

_COLUMNS = ("event_id", "occurred_at", "received_at", "kind", "hook", "tool",
            "code", "execution_id", "details")


def _prune_predicate(cutoff: str):
    """The ONE expression both the candidate count and the delete use."""
    return guardrail_events.c.received_at < cutoff


class GuardrailEventOperations:
    """Database operations for the guardrail_events table."""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert_events(self, agent_name: str, rows: List[Dict]) -> Tuple[int, int]:
        """Insert normalized rows for `agent_name`; returns `(accepted, duplicates)`.

        The agent name is stamped here from the caller's AUTH-resolved value, so
        a row dict can never carry another agent's name. A repeat of an
        `event_id` — inside the batch or already stored — is a duplicate.
        """
        if not rows:
            return 0, 0
        seen = set()
        unique = []
        for row in rows:
            if row["event_id"] in seen:
                continue
            seen.add(row["event_id"])
            unique.append({**{c: row.get(c) for c in _COLUMNS}, "agent_name": agent_name})
        stmt = (
            make_insert(guardrail_events)
            .values(unique)
            .on_conflict_do_nothing(index_elements=["agent_name", "event_id"])
            .returning(guardrail_events.c.id)
        )
        with get_engine().begin() as conn:
            accepted = len(conn.execute(stmt).fetchall())
        return accepted, len(rows) - accepted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_events(
        self,
        agent_name: str,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        kind: Optional[str] = None,
        execution_id: Optional[str] = None,
        limit: int = 50,
        before: Optional[Tuple[str, int]] = None,
    ) -> List[Dict]:
        """Newest-first rows for one agent, at most `limit`.

        `since` is inclusive and `until` exclusive (both ISO-Z, compared as
        text — Invariant #16). `before` is the `(occurred_at, id)` of the last
        row of the previous page.
        """
        t = guardrail_events
        conditions = [t.c.agent_name == agent_name]
        if since is not None:
            conditions.append(t.c.occurred_at >= since)
        if until is not None:
            conditions.append(t.c.occurred_at < until)
        if kind is not None:
            conditions.append(t.c.kind == kind)
        if execution_id is not None:
            conditions.append(t.c.execution_id == execution_id)
        if before is not None:
            occurred_at, row_id = before
            conditions.append(or_(
                t.c.occurred_at < occurred_at,
                and_(t.c.occurred_at == occurred_at, t.c.id < row_id),
            ))
        stmt = (
            select(t.c.id, *[t.c[c] for c in _COLUMNS])
            .where(*conditions)
            .order_by(t.c.occurred_at.desc(), t.c.id.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def latest_received_at(self, agent_name: str) -> Optional[str]:
        """When the newest stored row for this agent arrived, or None."""
        stmt = select(func.max(guardrail_events.c.received_at)).where(
            guardrail_events.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            return conn.execute(stmt).scalar()

    def count_by_execution(
        self, agent_name: str, execution_ids: Iterable[str]
    ) -> List[Dict]:
        """`(execution_id, kind, hook, code, n)` groups for this agent's rows in
        the named executions. One query, whatever the number of ids."""
        ids = list(execution_ids)
        if not ids:
            return []
        t = guardrail_events
        stmt = (
            select(t.c.execution_id, t.c.kind, t.c.hook, t.c.code, func.count().label("n"))
            .where(t.c.agent_name == agent_name, t.c.execution_id.in_(ids))
            .group_by(t.c.execution_id, t.c.kind, t.c.hook, t.c.code)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def count_by_agent(
        self, since: str, agent_names: Optional[Iterable[str]] = None
    ) -> List[Dict]:
        """`(agent_name, kind, n)` groups for rows that occurred at or after
        `since`; scoped to `agent_names` when given (an empty list matches none)."""
        t = guardrail_events
        conditions = [t.c.occurred_at >= since]
        if agent_names is not None:
            names = list(agent_names)
            if not names:
                return []
            conditions.append(t.c.agent_name.in_(names))
        stmt = (
            select(t.c.agent_name, t.c.kind, func.count().label("n"))
            .where(*conditions)
            .group_by(t.c.agent_name, t.c.kind)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def count_guardrail_event_candidates(self, retention_days: int, limit: int) -> int:
        """Bounded candidate count for the #1644 blast-radius guard."""
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        sub = (
            select(guardrail_events.c.id)
            .where(_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(conn.execute(select(func.count()).select_from(sub)).scalar_one())

    def prune_guardrail_events(self, retention_days: int = 90, chunk_size: int = 5000) -> int:
        """Delete rows received more than `retention_days` ago, by id chunks, at
        most `MAX_CHUNKS_PER_PRUNE` per call. `0` disables. Returns rows deleted."""
        if retention_days <= 0 or chunk_size <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        for _ in range(MAX_CHUNKS_PER_PRUNE):
            with get_engine().begin() as conn:
                ids = [r[0] for r in conn.execute(
                    select(guardrail_events.c.id)
                    .where(_prune_predicate(cutoff))
                    .order_by(guardrail_events.c.id)
                    .limit(chunk_size)
                )]
                if not ids:
                    break
                deleted = conn.execute(
                    delete(guardrail_events).where(guardrail_events.c.id.in_(ids))
                ).rowcount or 0
            total += deleted
            if len(ids) < chunk_size:
                break
        return total
