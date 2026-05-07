"""
Canary invariant violations database operations (CANARY-001 / Issue #411).

Append-mostly access to the `canary_violations` table populated by the
continuous orchestration-invariant harness. `services/canary_service.py`
writes one row per fired check each cycle; the read API surfaces them to
admins for triage.

`observed_state` is stored as a JSON string per invariant; the helpers
parse it on the way out so callers see a dict.
"""

import json
from typing import Any, Dict, List, Optional

from .connection import get_db_connection


# Tier and severity values are validated at write time so the read API can
# expose them as plain strings without a DB-level CHECK constraint.
_VALID_TIERS = {"A", "B"}
_VALID_SEVERITIES = {"critical", "major", "minor"}


class CanaryOperations:
    """Database operations for the canary invariant violations table."""

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def insert_violation(
        self,
        invariant_id: str,
        tier: str,
        severity: str,
        snapshot_time: str,
        observed_state: Dict[str, Any],
        signal_query: Optional[str] = None,
    ) -> int:
        """Insert a violation row, returning the new id.

        `observed_state` is JSON-serialized here so the caller passes a dict.
        """
        if tier not in _VALID_TIERS:
            raise ValueError(f"invalid tier {tier!r}; expected one of {_VALID_TIERS}")
        if severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"invalid severity {severity!r}; expected one of {_VALID_SEVERITIES}"
            )

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO canary_violations (
                    invariant_id, tier, severity, snapshot_time,
                    observed_state, signal_query
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    invariant_id,
                    tier,
                    severity,
                    snapshot_time,
                    json.dumps(observed_state),
                    signal_query,
                ),
            )
            return int(cursor.lastrowid)

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def list_violations(
        self,
        invariant_id: Optional[str] = None,
        severity: Optional[str] = None,
        tier: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query violations with optional filters, newest first."""
        conditions: List[str] = []
        params: List[Any] = []

        if invariant_id:
            conditions.append("invariant_id = ?")
            params.append(invariant_id)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if tier:
            conditions.append("tier = ?")
            params.append(tier)
        if start_time:
            conditions.append("snapshot_time >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("snapshot_time <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.extend([int(limit), int(offset)])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT * FROM canary_violations
                WHERE {where_clause}
                ORDER BY snapshot_time DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count_violations(
        self,
        invariant_id: Optional[str] = None,
        severity: Optional[str] = None,
        tier: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> int:
        """Return total count for a filter (independent of limit/offset)."""
        conditions: List[str] = []
        params: List[Any] = []

        if invariant_id:
            conditions.append("invariant_id = ?")
            params.append(invariant_id)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        if tier:
            conditions.append("tier = ?")
            params.append(tier)
        if start_time:
            conditions.append("snapshot_time >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("snapshot_time <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM canary_violations WHERE {where_clause}",
                params,
            )
            return int(cursor.fetchone()[0])

    def get_violation(self, violation_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single violation by primary key."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM canary_violations WHERE id = ?",
                (int(violation_id),),
            )
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def get_latest_per_invariant(self) -> Dict[str, Dict[str, Any]]:
        """Return the most recent violation per invariant_id.

        Used by `CanaryService` for green→red transition detection: if the
        latest stored violation for an invariant predates the current
        snapshot, this cycle is a fresh transition that warrants a Slack
        webhook post.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT v.* FROM canary_violations v
                JOIN (
                    SELECT invariant_id, MAX(id) AS max_id
                    FROM canary_violations
                    GROUP BY invariant_id
                ) latest ON v.id = latest.max_id
                """
            )
            return {row["invariant_id"]: self._row_to_dict(row) for row in cursor.fetchall()}

    def stats_by_invariant(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate counts by invariant_id and severity for dashboard tiles."""
        time_filter = ""
        params: List[Any] = []
        if start_time:
            time_filter += " AND snapshot_time >= ?"
            params.append(start_time)
        if end_time:
            time_filter += " AND snapshot_time <= ?"
            params.append(end_time)

        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT COUNT(*) FROM canary_violations WHERE 1=1 {time_filter}",
                params,
            )
            total = int(cursor.fetchone()[0])

            cursor.execute(
                f"""
                SELECT invariant_id, COUNT(*) AS cnt
                FROM canary_violations
                WHERE 1=1 {time_filter}
                GROUP BY invariant_id
                ORDER BY cnt DESC
                """,
                params,
            )
            by_invariant = {row["invariant_id"]: int(row["cnt"]) for row in cursor.fetchall()}

            cursor.execute(
                f"""
                SELECT severity, COUNT(*) AS cnt
                FROM canary_violations
                WHERE 1=1 {time_filter}
                GROUP BY severity
                ORDER BY cnt DESC
                """,
                params,
            )
            by_severity = {row["severity"]: int(row["cnt"]) for row in cursor.fetchall()}

            return {
                "total": total,
                "by_invariant": by_invariant,
                "by_severity": by_severity,
            }

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert sqlite3.Row to dict; parse `observed_state` JSON."""
        result = {key: row[key] for key in row.keys()}
        observed = result.get("observed_state")
        if observed:
            try:
                result["observed_state"] = json.loads(observed)
            except (TypeError, ValueError):
                # Leave as raw string if not valid JSON.
                pass
        return result
