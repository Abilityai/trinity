"""
Idempotency-key persistence (RELIABILITY-006, #525).

Backs the `Idempotency-Key` contract at every execution trigger boundary.
A `(scope, idempotency_key)` pair is claimed atomically before an execution is
dispatched; a duplicate within the TTL short-circuits with the original result
instead of dispatching a second execution.

Atomicity relies on the table's `PRIMARY KEY (scope, idempotency_key)` and
database-level write locking: concurrent claimers serialize, the loser gets an
IntegrityError and reads the existing row. This holds across processes
(multiple uvicorn workers + the standalone scheduler share one DB).
"""

import json
import logging
from typing import Optional

from sqlalchemy import select, insert, update, delete, func, and_
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import idempotency_keys
from utils.helpers import utc_now_iso, iso_cutoff

logger = logging.getLogger(__name__)

# Claim states returned by claim()
STATE_NEW = "new"            # first-seen — caller proceeds to dispatch
STATE_IN_FLIGHT = "in_flight"  # a prior claim is still running
STATE_COMPLETED = "completed"  # a prior claim finished — replay its snapshot


class IdempotencyOperations:
    """CRUD for the idempotency_keys table."""

    def claim(self, scope: str, key: str, ttl_hours: int = 24) -> dict:
        """Atomically claim (scope, key).

        Returns a dict: {state, execution_id, snapshot}.
        - state == "new":      row inserted as in_flight; caller dispatches.
        - state == "in_flight": a prior claim is mid-dispatch (return 409).
        - state == "completed": replay {execution_id, snapshot}.

        An existing row older than ttl_hours is treated as expired: it is
        deleted and the claim re-taken as new.
        """
        now = utc_now_iso()
        cutoff = iso_cutoff(hours=ttl_hours)
        with get_engine().begin() as conn:
            # Drop an expired row for this key so it can be re-claimed.
            conn.execute(
                delete(idempotency_keys).where(
                    and_(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == key,
                        idempotency_keys.c.created_at < cutoff,
                    )
                )
            )
            try:
                # SAVEPOINT so a PK conflict rolls back ONLY this INSERT, not the
                # whole transaction — PostgreSQL aborts the entire transaction on
                # any error (InFailedSqlTransaction) and would reject the
                # follow-up SELECT otherwise (#300). SQLite emulates savepoints.
                with conn.begin_nested():
                    conn.execute(
                        insert(idempotency_keys).values(
                            scope=scope,
                            idempotency_key=key,
                            execution_id=None,
                            status=STATE_IN_FLIGHT,
                            response_snapshot=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                return {"state": STATE_NEW, "execution_id": None, "snapshot": None}
            except IntegrityError:
                # Lost the race / genuine duplicate — read the surviving row.
                row = conn.execute(
                    select(
                        idempotency_keys.c.status,
                        idempotency_keys.c.execution_id,
                        idempotency_keys.c.response_snapshot,
                    ).where(
                        and_(
                            idempotency_keys.c.scope == scope,
                            idempotency_keys.c.idempotency_key == key,
                        )
                    )
                ).mappings().first()
                if row is None:
                    # Extremely unlikely (row deleted between INSERT-fail and
                    # SELECT). Treat as new so the caller doesn't wedge.
                    return {"state": STATE_NEW, "execution_id": None, "snapshot": None}
                snapshot = None
                if row["response_snapshot"]:
                    try:
                        snapshot = json.loads(row["response_snapshot"])
                    except (ValueError, TypeError):
                        snapshot = None
                return {
                    "state": row["status"],
                    "execution_id": row["execution_id"],
                    "snapshot": snapshot,
                }

    def attach_execution(self, scope: str, key: str, execution_id: str) -> None:
        """Record the execution_id for an in-flight claim (best-effort)."""
        with get_engine().begin() as conn:
            conn.execute(
                update(idempotency_keys)
                .where(
                    and_(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == key,
                    )
                )
                .values(execution_id=execution_id, updated_at=utc_now_iso())
            )

    def complete(
        self,
        scope: str,
        key: str,
        execution_id: Optional[str],
        snapshot: Optional[dict],
    ) -> None:
        """Mark a claim completed and store the response snapshot for replay."""
        snapshot_json = None
        if snapshot is not None:
            try:
                snapshot_json = json.dumps(snapshot, default=str)
            except (TypeError, ValueError):
                snapshot_json = None
        with get_engine().begin() as conn:
            conn.execute(
                update(idempotency_keys)
                .where(
                    and_(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == key,
                    )
                )
                .values(
                    status=STATE_COMPLETED,
                    execution_id=func.coalesce(
                        execution_id, idempotency_keys.c.execution_id
                    ),
                    response_snapshot=snapshot_json,
                    updated_at=utc_now_iso(),
                )
            )

    def release(self, scope: str, key: str) -> None:
        """Delete an in-flight claim so a failed first attempt can be retried.

        Only deletes rows still in_flight — never removes a completed row
        (which must stay to keep replaying the original result).
        """
        with get_engine().begin() as conn:
            conn.execute(
                delete(idempotency_keys).where(
                    and_(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == key,
                        idempotency_keys.c.status == STATE_IN_FLIGHT,
                    )
                )
            )

    def discard_completed(self, scope: str, key: str) -> None:
        """Delete a COMPLETED row whose recorded resource no longer exists.

        The completed-row-stays rule exists so a retry replays the original
        result — but a replay is only truthful while the resource it reports
        still exists. #2040 review F3: a delete-then-identical-recreate via a
        deterministic client key would otherwise replay a 200 naming an agent
        that is gone. Callers must verify non-existence BEFORE discarding.
        """
        with get_engine().begin() as conn:
            conn.execute(
                delete(idempotency_keys).where(
                    and_(
                        idempotency_keys.c.scope == scope,
                        idempotency_keys.c.idempotency_key == key,
                        idempotency_keys.c.status == STATE_COMPLETED,
                    )
                )
            )

    def purge_expired(self, ttl_hours: int = 24) -> int:
        """Delete rows older than ttl_hours. Returns rows removed."""
        cutoff = iso_cutoff(hours=ttl_hours)
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(idempotency_keys).where(
                    idempotency_keys.c.created_at < cutoff
                )
            )
            return result.rowcount or 0
