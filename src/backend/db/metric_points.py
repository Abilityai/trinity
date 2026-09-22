"""Recorded metric points — database operations (trinity-enterprise#478).

The append-only store behind `record_metrics`: one row per observation of a
metric the ent#477 registry declares for that agent. Writes insert, the
retention sweep deletes by `ts` range, nothing updates.

## Identity, not a surrogate key

The primary key is `(agent_name, ts, idempotency_key)`, where the service
computes `idempotency_key = sha256(metric \0 ts \0 canonical_dims)`. The same
observation posted twice therefore conflicts with itself and
`on_conflict_do_nothing` drops the second copy — the row-level guarantee that
holds with no client key, no Redis and no execution id. `value` is deliberately
outside the identity: a corrected number at the same instant with the same
dimensions is the same observation, and a genuine correction is a new `ts`.

`insert_points` counts what it actually wrote with `.returning(...)` rather
than `rowcount`: across a multi-VALUES insert with `DO NOTHING`, `rowcount` is
not a portable count of the rows that survived the conflict.

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
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select

from .engine import get_engine, make_insert
from .tables import metric_points
from utils.helpers import iso_cutoff

logger = logging.getLogger(__name__)

# One prune call never deletes more than this many rows, so the 300 s cleanup
# cycle cannot be monopolised by a table whose window just narrowed (TD-14).
MAX_CHUNKS_PER_PRUNE = 20


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
    ) -> Tuple[int, int]:
        """Insert validated rows, returning `(recorded, deduplicated)`.

        `rows` are the service's output: each carries `metric`, `ts`,
        `idempotency_key`, `value_numeric` / `value_text`, `dims`,
        `execution_id`, `created_at`. `agent_name` is stamped here from the
        AUTH-resolved name, never from the body.
        """
        if not rows:
            return (0, 0)
        payload = [dict(r, agent_name=agent_name) for r in rows]
        stmt = (
            make_insert(metric_points)
            .values(payload)
            .on_conflict_do_nothing(
                index_elements=[
                    metric_points.c.agent_name,
                    metric_points.c.ts,
                    metric_points.c.idempotency_key,
                ]
            )
            .returning(metric_points.c.idempotency_key)
        )
        with get_engine().begin() as conn:
            recorded = len(conn.execute(stmt).fetchall())
        return (recorded, len(payload) - recorded)

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

    def metric_points_remaining(
        self, retention_days: int, limit: int
    ) -> int:
        """Alias of the candidate count, read AFTER a prune.

        The sweep consumes its single-use acknowledgement only when what is
        left has fallen under the guard floor (TD-14); without this the first
        bounded prune would burn the ack and leave the rest of the backlog
        blocked behind a new one.
        """
        return self.count_metric_points_candidates(retention_days, limit)


def build_point_row(
    *,
    metric: str,
    ts: str,
    idempotency_key: str,
    value_numeric: Optional[float],
    value_text: Optional[str],
    dims: Optional[Dict[str, str]],
    execution_id: Optional[str],
    created_at: str,
) -> Dict[str, Any]:
    """The column dict `insert_points` expects, spelled once.

    Exported so the service layer can build rows without importing the table,
    keeping Invariant #1's direction (db holds no validation, service holds no
    SQL) honest in both directions.
    """
    return {
        "metric": metric,
        "ts": ts,
        "idempotency_key": idempotency_key,
        "value_numeric": value_numeric,
        "value_text": value_text,
        "dims": dims or None,
        "execution_id": execution_id,
        "created_at": created_at,
    }
