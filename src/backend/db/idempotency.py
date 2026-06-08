"""
Idempotency-key persistence (RELIABILITY-006, #525).

Backs the `Idempotency-Key` contract at every execution trigger boundary.
A `(scope, idempotency_key)` pair is claimed atomically before an execution is
dispatched; a duplicate within the TTL short-circuits with the original result
instead of dispatching a second execution.

Atomicity relies on the table's `PRIMARY KEY (scope, idempotency_key)` and
SQLite database-level write locking: concurrent claimers serialize, the loser
gets an IntegrityError and reads the existing row. This holds across processes
(multiple uvicorn workers + the standalone scheduler share one DB file).
"""

import json
import logging
import sqlite3
from typing import Optional

from .connection import get_db_connection
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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Drop an expired row for this key so it can be re-claimed.
            cursor.execute(
                "DELETE FROM idempotency_keys "
                "WHERE scope = ? AND idempotency_key = ? AND created_at < ?",
                (scope, key, cutoff),
            )
            try:
                cursor.execute(
                    "INSERT INTO idempotency_keys "
                    "(scope, idempotency_key, execution_id, status, "
                    " response_snapshot, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (scope, key, None, STATE_IN_FLIGHT, None, now, now),
                )
                return {"state": STATE_NEW, "execution_id": None, "snapshot": None}
            except sqlite3.IntegrityError:
                # Lost the race / genuine duplicate — read the surviving row.
                row = cursor.execute(
                    "SELECT status, execution_id, response_snapshot "
                    "FROM idempotency_keys WHERE scope = ? AND idempotency_key = ?",
                    (scope, key),
                ).fetchone()
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
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE idempotency_keys SET execution_id = ?, updated_at = ? "
                "WHERE scope = ? AND idempotency_key = ?",
                (execution_id, utc_now_iso(), scope, key),
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
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE idempotency_keys "
                "SET status = ?, execution_id = COALESCE(?, execution_id), "
                "    response_snapshot = ?, updated_at = ? "
                "WHERE scope = ? AND idempotency_key = ?",
                (
                    STATE_COMPLETED,
                    execution_id,
                    snapshot_json,
                    utc_now_iso(),
                    scope,
                    key,
                ),
            )

    def release(self, scope: str, key: str) -> None:
        """Delete an in-flight claim so a failed first attempt can be retried.

        Only deletes rows still in_flight — never removes a completed row
        (which must stay to keep replaying the original result).
        """
        with get_db_connection() as conn:
            conn.execute(
                "DELETE FROM idempotency_keys "
                "WHERE scope = ? AND idempotency_key = ? AND status = ?",
                (scope, key, STATE_IN_FLIGHT),
            )

    def purge_expired(self, ttl_hours: int = 24) -> int:
        """Delete rows older than ttl_hours. Returns rows removed."""
        cutoff = iso_cutoff(hours=ttl_hours)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,)
            )
            return cursor.rowcount or 0
