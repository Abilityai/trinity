"""Recorded metric points — database operations (trinity-enterprise#478).

The store behind `record_metrics`: one row per observation of a metric the
ent#477 registry declares for that agent. Writes insert or restate, the
retention sweep deletes by `ts` range.

## Identity, not a surrogate key

The primary key is `(agent_name, ts, idempotency_key)`, where the service
computes `idempotency_key = sha256(metric \0 ts \0 canonical_dims)`. The same
observation posted twice therefore conflicts with itself — the row-level
guarantee that holds with no client key, no Redis and no execution id. `value`
is deliberately outside the identity, so one identity is always ONE row:

* the same value again is a duplicate — the conflict's `WHERE` matches nothing,
  so nothing is written;
* a different value is a correction (ent#729, ruling R45) — the row is restated
  in place: value, `execution_id` and `recorded_at` follow the correcting
  write, `revision` goes up by one, and `ts` / `created_at` never move. The
  `ts` is the period the number describes, so the chart stays one point per
  period, and `created_at` is the first write the daily cap counts.

`insert_points` counts what it actually wrote with `.returning(revision)`
rather than `rowcount`: an inserted row comes back at revision 0, a restated
one at >= 1, and a duplicate does not come back at all. Across a multi-VALUES
upsert `rowcount` is not a portable count of any of the three.

## Sweep primitives

`count_metric_points_candidates` and `prune_metric_points` share ONE predicate
(`ts < cutoff`) by construction — a blast-radius guard that counts a different
row set than the delete is about to remove protects nothing (the ent#433 rule).
The prune works in `ts`-ranges rather than an id list (there are no ids), and
is bounded per call so one cycle cannot monopolise the cleanup loop.

SQLAlchemy Core (like `db/metric_definitions.py`) so it runs unchanged on
SQLite and PostgreSQL.
"""

import logging
from typing import Any, Dict, List, NamedTuple, Optional

from sqlalchemy import delete, func, or_, select

from .engine import get_engine, make_insert
from .tables import metric_points
from utils.helpers import iso_cutoff

logger = logging.getLogger(__name__)

# One prune call never deletes more than this many rows, so the 300 s cleanup
# cycle cannot be monopolised by a table whose window just narrowed (TD-14).
MAX_CHUNKS_PER_PRUNE = 20


class PointWriteCounts(NamedTuple):
    """What one `insert_points` call did. Named, so a caller reads
    `.corrected` rather than a position it could silently swap."""

    recorded: int        # new rows
    deduplicated: int    # identical repeats — nothing written
    corrected: int       # rows restated with a different value (ent#729)


def _prune_predicate(cutoff: str):
    """The ONE expression both the count and the delete are built from."""
    return metric_points.c.ts < cutoff


class MetricPointOperations:
    """Database operations for the recorded metric point store."""

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def insert_points(
        self, agent_name: str, rows: List[Dict[str, Any]]
    ) -> PointWriteCounts:
        """Insert or restate validated rows; see `PointWriteCounts`.

        `rows` are the service's output: each carries `metric`, `ts`,
        `idempotency_key`, `value_numeric` / `value_text`, `dims`,
        `execution_id`, `created_at`. The store stamps `agent_name` (the
        AUTH-resolved name, never the body's), `revision` and `recorded_at` (the
        batch's `created_at` — the wall clock of THIS write), so a row dict can
        smuggle none of them.

        Refuses two rows with one identity: PostgreSQL raises a cardinality
        violation on that and SQLite silently applies both, so the dialects
        would disagree. The service already rejects it (`duplicate_in_batch`);
        this makes the divergence unreachable rather than merely unlikely.

        Rows are written in `(ts, idempotency_key)` order. `DO UPDATE` locks
        every conflicting row — even one its `WHERE` then skips — so two
        overlapping batches locking in arrival order could deadlock on
        PostgreSQL; one canonical order cannot.
        """
        if not rows:
            return PointWriteCounts(0, 0, 0)
        identities = {(r["ts"], r["idempotency_key"]) for r in rows}
        if len(identities) != len(rows):
            raise ValueError(
                "two rows share one point identity (ts, idempotency_key); "
                "one identity is one observation per write")
        payload = sorted(
            (
                dict(r, agent_name=agent_name, revision=0,
                     recorded_at=r["created_at"])
                for r in rows
            ),
            key=lambda r: (r["ts"], r["idempotency_key"]),
        )
        insert = make_insert(metric_points).values(payload)
        stored, incoming = metric_points.c, insert.excluded
        stmt = insert.on_conflict_do_update(
            index_elements=[stored.agent_name, stored.ts, stored.idempotency_key],
            set_={
                "value_numeric": incoming.value_numeric,
                "value_text": incoming.value_text,
                "execution_id": incoming.execution_id,
                "recorded_at": incoming.recorded_at,
                "revision": stored.revision + 1,
            },
            # Null-safe, because one value column is NULL on every row (by
            # type). For same-type values a plain `!=` would happen to agree —
            # the non-NULL column decides the `OR` — but a value that moves
            # between the columns makes both `x != NULL` = NULL and a real
            # change would read as a duplicate. `IS DISTINCT FROM` on
            # PostgreSQL, `IS NOT` on SQLite.
            where=or_(
                stored.value_numeric.is_distinct_from(incoming.value_numeric),
                stored.value_text.is_distinct_from(incoming.value_text),
            ),
        ).returning(stored.revision)
        with get_engine().begin() as conn:
            revisions = [r[0] for r in conn.execute(stmt).fetchall()]
        recorded = sum(1 for rev in revisions if rev == 0)
        corrected = len(revisions) - recorded
        return PointWriteCounts(
            recorded=recorded,
            deduplicated=len(payload) - len(revisions),
            corrected=corrected,
        )

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def count_points_today(
        self, agent_name: str, day_start_iso: str, limit: int
    ) -> int:
        """Points this agent WROTE since `day_start_iso`, counted to `limit`.

        Bounded like the #1644 guard's count: the caller only needs to know
        whether the batch would cross the cap, so the scan stops one row past
        the answer instead of counting a full agent-day.

        Counts `created_at`, not `ts` — the cap is a write budget, so
        backfilling last year's points still spends today's.
        """
        if limit <= 0:
            return 0
        sub = (
            select(metric_points.c.idempotency_key)
            .where(
                metric_points.c.agent_name == agent_name,
                metric_points.c.created_at >= day_start_iso,
            )
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(sub)).scalar_one()
            )

    def latest_points_for(
        self,
        agent_name: str,
        metric_names: List[str],
        per_metric_limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """The newest `per_metric_limit` points of each named metric (ent#479).

        One index seek per declared name (`ORDER BY ts DESC, idempotency_key
        DESC LIMIT n`) on ONE connection, rather than a `row_number()`
        partition over `agent_name` — the partition would number every row the
        agent ever recorded before discarding all but the newest, while the
        seek stops at the limit on `idx_metric_points_agent_metric_ts`. The
        `idempotency_key` tiebreak is what makes "the latest point" the same
        row on SQLite and PostgreSQL when two observations share a `ts`.

        A metric's per-dimension series are folded in the service (dims are
        stored in caller key order, so they cannot be partitioned in SQL);
        this layer only guarantees the newest rows, newest first.
        """
        if not metric_names or per_metric_limit <= 0:
            return []
        t = metric_points
        rows: List[Dict[str, Any]] = []
        with get_engine().connect() as conn:
            for metric in metric_names:
                stmt = (
                    select(
                        t.c.metric,
                        t.c.ts,
                        t.c.value_numeric,
                        t.c.value_text,
                        t.c.dims,
                        t.c.idempotency_key,
                    )
                    .where(t.c.agent_name == agent_name, t.c.metric == metric)
                    .order_by(t.c.ts.desc(), t.c.idempotency_key.desc())
                    .limit(per_metric_limit)
                )
                rows.extend(dict(r) for r in conn.execute(stmt).mappings())
        return rows

    def series_points(
        self,
        agent_name: str,
        metric: str,
        since_iso: str,
        until_iso: Optional[str] = None,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Points of one metric inside a window, NEWEST first, `limit + 1` deep.

        Newest-first on purpose: `ASC + LIMIT` would keep the OLDEST points of
        a busy window, which is the opposite of what a sparkline or a
        freshness read needs. The caller reverses, and reads `len > limit` as
        "truncated" — one extra row is cheaper than a second COUNT over a
        table the sweep keeps at 36 M rows.

        `since_iso`/`until_iso` are bound parameters compared against an
        ISO-Z TEXT column the write path normalises (Invariant #16).
        """
        if limit <= 0:
            return []
        t = metric_points
        conditions = [
            t.c.agent_name == agent_name,
            t.c.metric == metric,
            t.c.ts >= since_iso,
        ]
        if until_iso:
            conditions.append(t.c.ts <= until_iso)
        stmt = (
            select(
                t.c.metric,
                t.c.ts,
                t.c.value_numeric,
                t.c.value_text,
                t.c.dims,
                t.c.idempotency_key,
            )
            .where(*conditions)
            .order_by(t.c.ts.desc(), t.c.idempotency_key.desc())
            .limit(limit + 1)
        )
        with get_engine().connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]

    def count_metric_points_candidates(
        self, retention_days: int, limit: int
    ) -> int:
        """Bounded candidate count for the #1644 blast-radius guard.

        Shares `_prune_predicate` with the prune BY CONSTRUCTION.
        """
        if retention_days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        sub = (
            select(metric_points.c.idempotency_key)
            .where(_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(sub)).scalar_one()
            )

    # ---------------------------------------------------------------------
    # Retention
    # ---------------------------------------------------------------------

    def prune_metric_points(
        self, retention_days: int = 365, chunk_size: int = 5000
    ) -> int:
        """Delete points older than ``retention_days``, bounded per call.

        Chunked by `ts` RANGE rather than by an id list, because this table has
        no id: each chunk reads the `ts` of the chunk-th oldest candidate and
        deletes everything at or below it that is still under the cutoff (ties
        included, so a chunk boundary inside one busy millisecond cannot wedge
        the loop). At most `MAX_CHUNKS_PER_PRUNE` chunks per call — a table
        whose window just narrowed drains over several cycles instead of
        blocking the other sweeps for one very long one (TD-14).

        `0` disables the sweep. Returns the number of rows deleted.
        """
        if retention_days <= 0 or chunk_size <= 0:
            return 0
        cutoff = iso_cutoff(hours=retention_days * 24)
        total = 0
        for _ in range(MAX_CHUNKS_PER_PRUNE):
            with get_engine().begin() as conn:
                boundary = conn.execute(
                    select(metric_points.c.ts)
                    .where(_prune_predicate(cutoff))
                    .order_by(metric_points.c.ts)
                    .limit(1)
                    .offset(chunk_size - 1)
                ).scalar()
                if boundary is None:
                    # Fewer than one chunk left — take the remainder and stop.
                    deleted = conn.execute(
                        delete(metric_points).where(_prune_predicate(cutoff))
                    ).rowcount
                    total += deleted or 0
                    return total
                deleted = conn.execute(
                    delete(metric_points).where(
                        _prune_predicate(cutoff),
                        metric_points.c.ts <= boundary,
                    )
                ).rowcount
            total += deleted or 0
            if not deleted:
                break
        return total

    # ---------------------------------------------------------------------
    # Introspection (the sweep's "is there more?" question)
    # ---------------------------------------------------------------------
