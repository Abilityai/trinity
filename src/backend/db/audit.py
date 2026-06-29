"""
Platform Audit Log database operations (SEC-001 / Issue #20 — Phase 1).

Append-only access to the `audit_log` table. The old Process Engine audit
(`audit_entries` table) was removed in #430 (2026-04-24).

Insertions go through this layer; UPDATE and DELETE are blocked by SQLite
triggers in `db/schema.py` and `db/migrations.py` to enforce immutability.

Converted from raw sqlite3 to SQLAlchemy Core for the configurable database
backend (#300 Phase 2): runs unchanged on both SQLite and PostgreSQL. The
day-of-week / hour / date bucketing for the heatmap and calendar — previously
done with SQLite-only ``strftime`` in SQL — is now computed in Python from the
ISO-Z timestamps so the aggregation is dialect-agnostic.
"""

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select, insert, func, and_, text

from .engine import get_engine
from .tables import audit_log


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
        stmt = insert(audit_log).values(
            event_id=entry["event_id"],
            event_type=entry["event_type"],
            event_action=entry["event_action"],
            actor_type=entry["actor_type"],
            actor_id=entry.get("actor_id"),
            actor_email=entry.get("actor_email"),
            actor_ip=entry.get("actor_ip"),
            mcp_key_id=entry.get("mcp_key_id"),
            mcp_key_name=entry.get("mcp_key_name"),
            mcp_scope=entry.get("mcp_scope"),
            target_type=entry.get("target_type"),
            target_id=entry.get("target_id"),
            timestamp=entry["timestamp"],
            details=entry.get("details"),
            request_id=entry.get("request_id"),
            source=entry["source"],
            endpoint=entry.get("endpoint"),
            previous_hash=entry.get("previous_hash"),
            entry_hash=entry.get("entry_hash"),
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def _filter_conditions(
        self,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        source: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> List[Any]:
        """Build a list of Core WHERE conditions from optional filters."""
        conditions: List[Any] = []
        if event_type:
            conditions.append(audit_log.c.event_type == event_type)
        if actor_type:
            conditions.append(audit_log.c.actor_type == actor_type)
        if actor_id:
            conditions.append(audit_log.c.actor_id == actor_id)
        if target_type:
            conditions.append(audit_log.c.target_type == target_type)
        if target_id:
            conditions.append(audit_log.c.target_id == target_id)
        if source:
            conditions.append(audit_log.c.source == source)
        if start_time:
            conditions.append(audit_log.c.timestamp >= start_time)
        if end_time:
            conditions.append(audit_log.c.timestamp <= end_time)
        # #905: filter by request correlation id so an MCP `mcp_operation` row and
        # the backend `git_operation` row it triggered can be retrieved together.
        if request_id:
            conditions.append(audit_log.c.request_id == request_id)
        return conditions

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
        request_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query audit entries with optional filters, newest first."""
        conditions = self._filter_conditions(
            event_type, actor_type, actor_id, target_type,
            target_id, source, start_time, end_time, request_id,
        )
        stmt = (
            select(audit_log)
            .where(and_(*conditions))
            .order_by(audit_log.c.timestamp.desc(), audit_log.c.id.desc())
            .limit(int(limit))
            .offset(int(offset))
        )
        with get_engine().connect() as conn:
            return [self._row_to_dict(row) for row in conn.execute(stmt).mappings()]

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
        request_id: Optional[str] = None,
    ) -> int:
        """Return total count for a filter (independent of limit/offset)."""
        conditions = self._filter_conditions(
            event_type, actor_type, actor_id, target_type,
            target_id, source, start_time, end_time, request_id,
        )
        stmt = select(func.count()).select_from(audit_log).where(and_(*conditions))
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def get_audit_entry(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single entry by its UUID event_id."""
        stmt = select(audit_log).where(audit_log.c.event_id == event_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None

    def get_audit_entries_range(self, start_id: int, end_id: int) -> List[Dict[str, Any]]:
        """Return entries by primary key range (used by Phase 4 hash-chain verification)."""
        stmt = (
            select(audit_log)
            .where(audit_log.c.id.between(int(start_id), int(end_id)))
            .order_by(audit_log.c.id)
        )
        with get_engine().connect() as conn:
            return [self._row_to_dict(row) for row in conn.execute(stmt).mappings()]

    def get_distinct_event_types(self) -> List[str]:
        """Return sorted unique event_type values across the audit log.

        Used by the dashboard (#941) to populate filter dropdowns without
        hardcoding the enum. Indexed column + low cardinality → cheap.
        """
        stmt = (
            select(audit_log.c.event_type)
            .where(audit_log.c.event_type.isnot(None))
            .distinct()
            .order_by(audit_log.c.event_type)
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt)]

    def get_distinct_actor_types(self) -> List[str]:
        """Return sorted unique actor_type values across the audit log.

        Companion to get_distinct_event_types — drives the actor_type
        dropdown on the audit dashboard.
        """
        stmt = (
            select(audit_log.c.actor_type)
            .where(audit_log.c.actor_type.isnot(None))
            .distinct()
            .order_by(audit_log.c.actor_type)
        )
        with get_engine().connect() as conn:
            return [row[0] for row in conn.execute(stmt)]

    def get_audit_heatmap(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        event_type: Optional[str] = None,
        actor_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bucket audit rows into a 7×24 day-of-week × hour-of-day grid.

        Buckets are computed in Python from the ISO-8601
        ``YYYY-MM-DDTHH:MM:SSZ`` form used across `audit_log.timestamp`
        (architectural invariant #16 — timestamps written via
        ``utc_now_iso()``). Day-of-week uses Sunday=0..Saturday=6 to match
        the previous SQLite ``strftime('%w')`` semantics; hour is 0..23.

        Returns a sparse cell list — rows with zero counts are omitted to
        keep the payload small on quiet windows. Frontend lays cells onto
        the implicit 7×24 grid.
        """
        conditions = self._filter_conditions(
            event_type=event_type,
            actor_type=actor_type,
            start_time=start_time,
            end_time=end_time,
        )
        stmt = select(audit_log.c.timestamp).where(and_(*conditions))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        buckets: Dict[tuple, int] = {}
        for row in rows:
            ts = row[0]
            parsed = self._parse_ts(ts)
            if parsed is None:
                # Defensive — skip unparseable timestamps instead of
                # crashing the dashboard for one malformed row.
                continue
            dow, hour, _date = parsed
            key = (dow, hour)
            buckets[key] = buckets.get(key, 0) + 1

        cells = []
        total = 0
        max_count = 0
        for (dow, hour), count in buckets.items():
            cells.append({"dow": dow, "hour": hour, "count": count})
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

        The per-day bucket is the UTC date prefix of the ISO-Z timestamps
        stored in `audit_log` (invariant #16) — no timezone shift. Days are
        returned in ascending order to match the previous ``ORDER BY date``.
        """
        conditions = self._filter_conditions(
            event_type=event_type,
            actor_type=actor_type,
            start_time=start_time,
            end_time=end_time,
        )
        stmt = select(audit_log.c.timestamp).where(and_(*conditions))
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).all()

        buckets: Dict[str, int] = {}
        for row in rows:
            parsed = self._parse_ts(row[0])
            if parsed is None:
                # Skip unparseable timestamps instead of crashing the
                # dashboard for one malformed row.
                continue
            _dow, _hour, date = parsed
            buckets[date] = buckets.get(date, 0) + 1

        days = []
        total = 0
        max_count = 0
        for date in sorted(buckets.keys()):
            count = buckets[date]
            days.append({"date": date, "count": count})
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
        conditions = self._filter_conditions(start_time=start_time, end_time=end_time)

        with get_engine().connect() as conn:
            total = int(
                conn.execute(
                    select(func.count()).select_from(audit_log).where(and_(*conditions))
                ).scalar_one()
            )

            event_stmt = (
                select(audit_log.c.event_type, func.count().label("cnt"))
                .where(and_(*conditions))
                .group_by(audit_log.c.event_type)
                .order_by(func.count().desc())
            )
            by_event_type = {
                row["event_type"]: int(row["cnt"])
                for row in conn.execute(event_stmt).mappings()
            }

            actor_stmt = (
                select(audit_log.c.actor_type, func.count().label("cnt"))
                .where(and_(*conditions))
                .group_by(audit_log.c.actor_type)
                .order_by(func.count().desc())
            )
            by_actor_type = {
                row["actor_type"]: int(row["cnt"])
                for row in conn.execute(actor_stmt).mappings()
            }

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

        Note (architectural invariant #16): we intentionally compare against
        the SQLite trigger's own ``datetime('now', '-N days')`` cutoff here —
        not ``iso_cutoff()`` — so the prune WHERE filter and the trigger's
        WHEN clause apply the *same* format-mismatched comparison. Aligning
        with the trigger avoids IntegrityError on the day-of-cutoff boundary.
        This path is SQLite-specific (the trigger only exists on SQLite);
        on PostgreSQL there is no such trigger, so the same ISO-string cutoff
        is computed in Python and bound as a value. Fixing the trigger to use
        ISO-Z form is tracked separately.
        """
        if retention_days < 365:
            raise ValueError(
                "retention_days must be >= 365 (audit_log_no_delete trigger floor)"
            )
        days = int(retention_days)
        with get_engine().begin() as conn:
            if conn.dialect.name == "sqlite":
                # Match the trigger's cutoff exactly (space-separated form).
                result = conn.execute(
                    text(
                        "DELETE FROM audit_log "
                        "WHERE timestamp < datetime('now', :offset)"
                    ),
                    {"offset": f"-{days} days"},
                )
            else:
                from datetime import datetime, timezone, timedelta

                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                result = conn.execute(
                    text("DELETE FROM audit_log WHERE timestamp < :cutoff"),
                    {"cutoff": cutoff},
                )
            return int(result.rowcount)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _parse_ts(ts: Optional[str]):
        """Parse an ISO-Z ``audit_log.timestamp`` into ``(dow, hour, date)``.

        ``dow`` follows SQLite ``strftime('%w')`` (Sunday=0..Saturday=6);
        ``hour`` is 0..23; ``date`` is the ``YYYY-MM-DD`` UTC prefix.
        Returns ``None`` when the value can't be parsed.
        """
        if not ts or not isinstance(ts, str):
            return None
        from datetime import datetime

        candidate = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        try:
            dt = datetime.fromisoformat(candidate)
        except (TypeError, ValueError):
            return None
        # isoweekday(): Monday=1..Sunday=7 → strftime('%w'): Sunday=0..Saturday=6
        dow = dt.isoweekday() % 7
        return dow, dt.hour, dt.strftime("%Y-%m-%d")

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a RowMapping to a plain dict, parsing the JSON `details` column."""
        result = dict(row)
        details = result.get("details")
        if details:
            try:
                result["details"] = json.loads(details)
            except (TypeError, ValueError):
                # Leave as raw string if it isn't valid JSON.
                pass
        return result
