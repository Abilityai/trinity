"""
Monitoring database operations for agent health checks.

Handles storage and retrieval of health check results, alert cooldowns,
and historical data for trend analysis.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged on
both SQLite and PostgreSQL. Queries are built from the table handles in
``db/tables.py``; the engine is resolved from ``DATABASE_URL`` via
``db/engine.py``. Public method signatures and return shapes are unchanged.
"""

import json
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select, insert, delete, func, and_, text

from .engine import get_engine, make_insert
from .tables import agent_health_checks, monitoring_alert_cooldowns
from utils.helpers import iso_cutoff, utc_now_iso


def _health_check_prune_predicate(cutoff: str):
    """WHERE clause for the #772 health-check retention sweep.

    #1644: shared with `count_health_check_candidates` so the guard's count and
    the prune's delete can never describe different row sets.
    """
    return agent_health_checks.c.checked_at < cutoff


class MonitoringOperations:
    """Database operations for agent health monitoring."""

    # =========================================================================
    # Health Check Records
    # =========================================================================

    def create_health_check(
        self,
        agent_name: str,
        check_type: str,
        status: str,
        docker_metrics: Optional[Dict] = None,
        network_metrics: Optional[Dict] = None,
        business_metrics: Optional[Dict] = None,
        error_message: Optional[str] = None,
    ) -> str:
        """
        Create a new health check record.

        Args:
            agent_name: Name of the agent
            check_type: Type of check (docker, network, business, aggregate)
            status: Health status (healthy, degraded, unhealthy, critical)
            docker_metrics: Docker layer metrics
            network_metrics: Network layer metrics
            business_metrics: Business logic metrics
            error_message: Optional error message

        Returns:
            ID of created record
        """
        check_id = f"hc_{secrets.token_urlsafe(12)}"
        now = utc_now_iso()

        # Extract metrics from dicts
        docker = docker_metrics or {}
        network = network_metrics or {}
        business = business_metrics or {}

        stmt = insert(agent_health_checks).values(
            id=check_id,
            agent_name=agent_name,
            check_type=check_type,
            status=status,
            # Docker metrics
            container_status=docker.get("container_status"),
            cpu_percent=docker.get("cpu_percent"),
            memory_percent=docker.get("memory_percent"),
            memory_mb=docker.get("memory_mb"),
            restart_count=docker.get("restart_count"),
            oom_killed=1 if docker.get("oom_killed") else 0 if docker.get("oom_killed") is False else None,
            # Network metrics
            reachable=1 if network.get("reachable") else 0 if network.get("reachable") is False else None,
            latency_ms=network.get("latency_ms"),
            # Business metrics
            runtime_available=1 if business.get("runtime_available") else 0 if business.get("runtime_available") is False else None,
            claude_available=1 if business.get("claude_available") else 0 if business.get("claude_available") is False else None,
            context_percent=business.get("context_percent"),
            active_executions=business.get("active_executions"),
            error_rate=business.get("error_rate"),
            # Common fields
            error_message=error_message,
            checked_at=now,
            created_at=now,
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

        return check_id

    def get_latest_health_check(
        self,
        agent_name: str,
        check_type: str = "aggregate"
    ) -> Optional[Dict]:
        """Get the most recent health check for an agent."""
        stmt = (
            select(agent_health_checks)
            .where(
                and_(
                    agent_health_checks.c.agent_name == agent_name,
                    agent_health_checks.c.check_type == check_type,
                )
            )
            .order_by(agent_health_checks.c.checked_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        return self._row_to_health_check(row)

    def get_agent_health_history(
        self,
        agent_name: str,
        check_type: str = "aggregate",
        hours: int = 24,
        limit: int = 100
    ) -> List[Dict]:
        """Get health check history for an agent."""
        # #1265 / Invariant #16: compare ISO-Z columns against an iso_cutoff()
        # value, not datetime.utcnow() (whose format breaks lexicographic order).
        since = iso_cutoff(hours)

        stmt = (
            select(agent_health_checks)
            .where(
                and_(
                    agent_health_checks.c.agent_name == agent_name,
                    agent_health_checks.c.check_type == check_type,
                    agent_health_checks.c.checked_at >= since,
                )
            )
            .order_by(agent_health_checks.c.checked_at.desc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_health_check(row) for row in rows]

    def get_all_latest_health_checks(
        self,
        agent_names: Optional[List[str]] = None,
        check_type: str = "aggregate"
    ) -> Dict[str, Dict]:
        """
        Get the latest health check for multiple agents.

        Returns:
            Dict mapping agent_name -> health check record
        """
        # Kept as a parameterised text() query: the self-join against a
        # per-agent MAX(checked_at) subquery is awkward to express in Core and
        # was already hand-tuned SQL. All sqlite-only constructs were removed
        # and placeholders are named (portable across SQLite + PostgreSQL).
        with get_engine().connect() as conn:
            if agent_names:
                params: Dict[str, Any] = {"check_type": check_type}
                name_keys = []
                for i, name in enumerate(agent_names):
                    key = f"name_{i}"
                    name_keys.append(f":{key}")
                    params[key] = name
                placeholders = ",".join(name_keys)
                rows = conn.execute(
                    text(f"""
                        SELECT h1.* FROM agent_health_checks h1
                        INNER JOIN (
                            SELECT agent_name, MAX(checked_at) as max_checked
                            FROM agent_health_checks
                            WHERE check_type = :check_type AND agent_name IN ({placeholders})
                            GROUP BY agent_name
                        ) h2 ON h1.agent_name = h2.agent_name AND h1.checked_at = h2.max_checked
                        WHERE h1.check_type = :check_type
                    """),
                    params,
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("""
                        SELECT h1.* FROM agent_health_checks h1
                        INNER JOIN (
                            SELECT agent_name, MAX(checked_at) as max_checked
                            FROM agent_health_checks
                            WHERE check_type = :check_type
                            GROUP BY agent_name
                        ) h2 ON h1.agent_name = h2.agent_name AND h1.checked_at = h2.max_checked
                        WHERE h1.check_type = :check_type
                    """),
                    {"check_type": check_type},
                ).mappings().all()

        return {
            self._row_to_health_check(row)["agent_name"]: self._row_to_health_check(row)
            for row in rows
        }

    def get_health_summary(
        self,
        agent_names: Optional[List[str]] = None
    ) -> Dict[str, int]:
        """
        Get summary counts of health statuses.

        Returns:
            Dict with keys: healthy, degraded, unhealthy, critical, unknown
        """
        latest = self.get_all_latest_health_checks(agent_names, "aggregate")

        summary = {"healthy": 0, "degraded": 0, "unhealthy": 0, "critical": 0, "unknown": 0}
        for check in latest.values():
            status = check.get("status", "unknown")
            if status in summary:
                summary[status] += 1
            else:
                summary["unknown"] += 1

        return summary

    def calculate_uptime_percent(
        self,
        agent_name: str,
        hours: int = 24
    ) -> Optional[float]:
        """Calculate uptime percentage for an agent over the specified period."""
        history = self.get_agent_health_history(agent_name, "aggregate", hours, limit=1000)
        if not history:
            return None

        healthy_count = sum(1 for h in history if h["status"] in ["healthy", "degraded"])
        return (healthy_count / len(history)) * 100 if history else None

    def calculate_avg_latency(
        self,
        agent_name: str,
        hours: int = 24
    ) -> Optional[float]:
        """Calculate average latency for an agent over the specified period."""
        history = self.get_agent_health_history(agent_name, "network", hours, limit=1000)
        if not history:
            return None

        latencies = [h["latency_ms"] for h in history if h.get("latency_ms") is not None]
        return sum(latencies) / len(latencies) if latencies else None

    def count_health_check_candidates(self, days: int, limit: int) -> int:
        """#1644: how many rows `cleanup_old_records(days)` would DELETE.

        Bounded at `limit` — the guard only asks "more than N?", never "how many?".
        """
        if days <= 0 or limit <= 0:
            return 0
        cutoff = iso_cutoff(hours=days * 24)
        inner = (
            select(agent_health_checks.c.id)
            .where(_health_check_prune_predicate(cutoff))
            .limit(limit)
            .subquery()
        )
        with get_engine().connect() as conn:
            return int(
                conn.execute(select(func.count()).select_from(inner)).scalar() or 0
            )

    def cleanup_old_records(self, days: int = 7, chunk_size: int = 1000) -> int:
        """
        Delete health check records older than specified days.

        Issue #772: chunked DELETE so production tables with ~750k rows don't
        hold the write lock for the duration of a full purge. Each chunk
        commits before the next; total returned is the sum across chunks.
        Uses `iso_cutoff()` so the lex comparison aligns with `utc_now_iso()`
        written by `create_health_check` (Architectural Invariant #16).

        Args:
            days: Delete rows older than this. 0 disables the sweep.
            chunk_size: Max rows deleted per commit cycle (default 1000).

        Returns:
            Total rows deleted across all chunks.
        """
        if days <= 0 or chunk_size <= 0:
            return 0

        cutoff = iso_cutoff(hours=days * 24)
        total = 0
        while True:
            with get_engine().begin() as conn:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        select(agent_health_checks.c.id)
                        .where(_health_check_prune_predicate(cutoff))
                        .limit(chunk_size)
                    ).mappings()
                ]
                if not ids:
                    break
                result = conn.execute(
                    delete(agent_health_checks).where(
                        agent_health_checks.c.id.in_(ids)
                    )
                )
                total += result.rowcount
            if len(ids) < chunk_size:
                break

        return total

    # =========================================================================
    # Alert Cooldowns
    # =========================================================================

    def get_cooldown(
        self,
        agent_name: str,
        condition: str
    ) -> Optional[str]:
        """Get the last alert timestamp for a condition."""
        stmt = select(monitoring_alert_cooldowns.c.last_alert_at).where(
            and_(
                monitoring_alert_cooldowns.c.agent_name == agent_name,
                monitoring_alert_cooldowns.c.condition == condition,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return row[0] if row else None

    def set_cooldown(
        self,
        agent_name: str,
        condition: str
    ) -> None:
        """Set or update the cooldown timestamp for a condition."""
        now = utc_now_iso()
        cooldown_id = f"cd_{secrets.token_urlsafe(8)}"

        stmt = (
            make_insert(monitoring_alert_cooldowns)
            .values(
                id=cooldown_id,
                agent_name=agent_name,
                condition=condition,
                last_alert_at=now,
            )
            .on_conflict_do_update(
                index_elements=["agent_name", "condition"],
                set_={"last_alert_at": now},
            )
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def clear_cooldown(
        self,
        agent_name: str,
        condition: str
    ) -> bool:
        """Clear a cooldown entry. Returns True if entry existed."""
        stmt = delete(monitoring_alert_cooldowns).where(
            and_(
                monitoring_alert_cooldowns.c.agent_name == agent_name,
                monitoring_alert_cooldowns.c.condition == condition,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def is_in_cooldown(
        self,
        agent_name: str,
        condition: str,
        cooldown_seconds: int
    ) -> bool:
        """Check if a condition is still in cooldown period."""
        last_alert = self.get_cooldown(agent_name, condition)
        if not last_alert:
            return False

        try:
            last_alert_time = datetime.fromisoformat(last_alert.replace("Z", "+00:00"))
            cooldown_end = last_alert_time + timedelta(seconds=cooldown_seconds)
            now = datetime.utcnow().replace(tzinfo=last_alert_time.tzinfo)
            return now < cooldown_end
        except (ValueError, TypeError):
            return False

    def cleanup_cooldowns(self, agent_name: Optional[str] = None) -> int:
        """
        Clear all cooldowns for an agent (or all agents if not specified).

        Returns:
            Number of deleted records
        """
        if agent_name:
            stmt = delete(monitoring_alert_cooldowns).where(
                monitoring_alert_cooldowns.c.agent_name == agent_name
            )
        else:
            stmt = delete(monitoring_alert_cooldowns)
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _row_to_health_check(self, row) -> Dict[str, Any]:
        """Convert a database row (RowMapping) to a health check dict."""
        result = dict(row)

        # Convert boolean fields
        for bool_field in ["oom_killed", "reachable", "runtime_available", "claude_available"]:
            if result.get(bool_field) is not None:
                result[bool_field] = bool(result[bool_field])

        return result
