"""
Platform Audit Log database operations (SEC-001 / Issue #20 — Phase 1).

Append-only access to the `audit_log` table. The old Process Engine audit
(`audit_entries` table) was removed in #430 (2026-04-24).

Insertions go through this layer; UPDATE and DELETE are blocked by SQLite
triggers in `db/schema.py` and `db/migrations.py` to enforce immutability.
"""

import json
from typing import Any, Dict, List, Optional

from .connection import get_db_connection


class PlatformAuditOperations:
    """Database operations for the platform audit log."""

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def create_audit_entry(self, entry: Dict[str, Any]) -> None:
        """Insert an audit log entry (append-only).

        The `entry` dict must contain at least `event_id`, `event_type`,
        `event_action`, `actor_type`, `timestamp`, and `source`. All other
        fields are optional — missing keys are stored as NULL.

        The caller is expected to JSON-encode `details` before passing it
        in. Hash chain fields (`previous_hash`, `entry_hash`) are optional;
        Phase 4 enables them.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO audit_log (
                    event_id, event_type, event_action,
                    actor_type, actor_id, actor_email, actor_ip,
                    mcp_key_id, mcp_key_name, mcp_scope,
                    target_type, target_id,
                    timestamp, details, request_id, source, endpoint,
                    previous_hash, entry_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["event_id"],
                    entry["event_type"],
                    entry["event_action"],
                    entry["actor_type"],
                    entry.get("actor_id"),
                    entry.get("actor_email"),
                    entry.get("actor_ip"),
                    entry.get("mcp_key_id"),
                    entry.get("mcp_key_name"),
                    entry.get("mcp_scope"),
                    entry.get("target_type"),
                    entry.get("target_id"),
                    entry["timestamp"],
                    entry.get("details"),
                    entry.get("request_id"),
                    entry["source"],
                    entry.get("endpoint"),
                    entry.get("previous_hash"),
                    entry.get("entry_hash"),
                ),
            )
            conn.commit()

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def get_audit_entries(
        self,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        source: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query audit entries with optional filters, newest first."""
        conditions: List[str] = []
        params: List[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_type:
            conditions.append("actor_type = ?")
            params.append(actor_type)
        if actor_id:
            conditions.append("actor_id = ?")
            params.append(actor_id)
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.extend([int(limit), int(offset)])

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT * FROM audit_log
                WHERE {where_clause}
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def count_audit_entries(
        self,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        source: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> int:
        """Return total count for a filter (independent of limit/offset)."""
        conditions: List[str] = []
        params: List[Any] = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_type:
            conditions.append("actor_type = ?")
            params.append(actor_type)
        if actor_id:
            conditions.append("actor_id = ?")
            params.append(actor_id)
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE {where_clause}",
                params,
            )
            return int(cursor.fetchone()[0])

    def get_audit_entry(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single entry by its UUID event_id."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM audit_log WHERE event_id = ?",
                (event_id,),
            )
            row = cursor.fetchone()
            return self._row_to_dict(row) if row else None

    def get_audit_entries_range(self, start_id: int, end_id: int) -> List[Dict[str, Any]]:
        """Return entries by primary key range (used by Phase 4 hash-chain verification)."""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM audit_log WHERE id BETWEEN ? AND ? ORDER BY id",
                (int(start_id), int(end_id)),
            )
            return [self._row_to_dict(row) for row in cursor.fetchall()]

    def get_distinct_event_types(self) -> List[str]:
        """Return sorted unique event_type values across the audit log.

        Used by the dashboard (#941) to populate filter dropdowns without
        hardcoding the enum. Indexed column + low cardinality → cheap.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT event_type FROM audit_log "
                "WHERE event_type IS NOT NULL ORDER BY event_type"
            )
            return [row[0] for row in cursor.fetchall()]

    def get_distinct_actor_types(self) -> List[str]:
        """Return sorted unique actor_type values across the audit log.

        Companion to get_distinct_event_types — drives the actor_type
        dropdown on the audit dashboard.
        """
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT actor_type FROM audit_log "
                "WHERE actor_type IS NOT NULL ORDER BY actor_type"
            )
            return [row[0] for row in cursor.fetchall()]

    def get_audit_heatmap(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bucket audit rows into a 7×24 day-of-week × hour-of-day grid.

        SQLite ``strftime('%w', timestamp)`` returns weekday 0..6 with
        Sunday=0; ``%H`` returns hour 0..23. Both accept the ISO-8601
        ``YYYY-MM-DDTHH:MM:SSZ`` form used across `audit_log.timestamp`
        (architectural invariant #16 — timestamps written via
        ``utc_now_iso()``).

        Returns a sparse cell list — rows with zero counts are omitted to
        keep the payload small on quiet windows. Frontend lays cells onto
        the implicit 7×24 grid.
        """
        conditions: List[str] = []
        params: List[Any] = []
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_type:
            conditions.append("actor_type = ?")
            params.append(actor_type)
        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT
                    CAST(strftime('%w', timestamp) AS INTEGER) AS dow,
                    CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                    COUNT(*) AS cnt
                FROM audit_log
                WHERE {where_clause}
                GROUP BY dow, hour
                """,
                params,
            )
            cells = []
            total = 0
            max_count = 0
            for row in cursor.fetchall():
                # Defensive — strftime returns NULL for unparseable
                # timestamps; skip those instead of crashing the dashboard.
                if row["dow"] is None or row["hour"] is None:
                    continue
                count = int(row["cnt"])
                cells.append(
                    {
                        "dow": int(row["dow"]),
                        "hour": int(row["hour"]),
                        "count": count,
                    }
                )
                total += count
                if count > max_count:
                    max_count = count

            return {
                "cells": cells,
                "total": total,
                "max_count": max_count,
            }

    def get_audit_calendar(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bucket audit rows into per-day counts (GitHub-style calendar).

        Returns sparse `[{date: 'YYYY-MM-DD', count: N}]` pairs — quiet
        days are omitted. Frontend lays the pairs onto a date-anchored
        grid so the layout is canonical-calendar-week regardless of
        which days appear in the payload.

        SQLite ``strftime('%Y-%m-%d', timestamp)`` accepts the ISO-Z
        timestamps stored in `audit_log` (invariant #16) and emits UTC
        date strings — no timezone shift.
        """
        conditions: List[str] = []
        params: List[Any] = []
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_type:
            conditions.append("actor_type = ?")
            params.append(actor_type)
        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT
                    strftime('%Y-%m-%d', timestamp) AS date,
                    COUNT(*) AS cnt
                FROM audit_log
                WHERE {where_clause}
                GROUP BY date
                ORDER BY date
                """,
                params,
            )
            days = []
            total = 0
            max_count = 0
            for row in cursor.fetchall():
                # Skip unparseable timestamps (strftime → NULL) instead
                # of crashing the dashboard for one malformed row.
                if row["date"] is None:
                    continue
                count = int(row["cnt"])
                days.append({"date": row["date"], "count": count})
                total += count
                if count > max_count:
                    max_count = count

            return {
                "days": days,
                "total": total,
                "max_count": max_count,
            }

    def get_audit_stats(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate counts by event_type and actor_type for the dashboard."""
        time_filter = ""
        params: List[Any] = []
        if start_time:
            time_filter += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            time_filter += " AND timestamp <= ?"
            params.append(end_time)

        with get_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE 1=1 {time_filter}",
                params,
            )
            total = int(cursor.fetchone()[0])

            cursor.execute(
                f"""
                SELECT event_type, COUNT(*) as cnt FROM audit_log
                WHERE 1=1 {time_filter}
                GROUP BY event_type ORDER BY cnt DESC
                """,
                params,
            )
            by_event_type = {row["event_type"]: int(row["cnt"]) for row in cursor.fetchall()}

            cursor.execute(
                f"""
                SELECT actor_type, COUNT(*) as cnt FROM audit_log
                WHERE 1=1 {time_filter}
                GROUP BY actor_type ORDER BY cnt DESC
                """,
                params,
            )
            by_actor_type = {row["actor_type"]: int(row["cnt"]) for row in cursor.fetchall()}

            return {
                "total": total,
                "by_event_type": by_event_type,
                "by_actor_type": by_actor_type,
            }

    # ---------------------------------------------------------------------
    # Retention
    # ---------------------------------------------------------------------

    def prune_audit_log(self, retention_days: int) -> int:
        """Delete entries older than ``retention_days``. Returns rows removed.

        The append-only trigger ``audit_log_no_delete`` blocks DELETEs of
        rows whose ``timestamp > datetime('now', '-365 days')``. Callers
        must not pass ``retention_days < 365`` — the trigger would raise
        on every candidate row and the bulk DELETE would abort.

        Note (architectural invariant #16): we intentionally use SQLite's
        ``datetime('now', ?)`` here — not ``iso_cutoff()`` — so the prune
        WHERE filter and the trigger's WHEN clause apply the *same*
        format-mismatched comparison. Aligning with the trigger avoids
        IntegrityError on the day-of-cutoff boundary. Fixing the trigger
        to use ISO-Z form is tracked separately.
        """
        if retention_days < 365:
            raise ValueError(
                "retention_days must be >= 365 (audit_log_no_delete trigger floor)"
            )
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM audit_log WHERE timestamp < datetime('now', ?)",
                (f"-{int(retention_days)} days",),
            )
            removed = cursor.rowcount
            conn.commit()
            return int(removed)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a sqlite3.Row to a plain dict, parsing the JSON `details` column."""
        result = {key: row[key] for key in row.keys()}
        details = result.get("details")
        if details:
            try:
                result["details"] = json.loads(details)
            except (TypeError, ValueError):
                # Leave as raw string if it isn't valid JSON.
                pass
        return result
