"""
Dashboard history operations for Trinity platform (DASH-001).

Handles capturing and querying historical dashboard widget values for:
- Sparkline visualization in the UI
- Trend calculation (up/down/stable)
- Platform metrics injection
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select, insert, delete

from .engine import get_engine, make_insert
from .tables import agent_dashboard_values, agent_dashboard_cache
from utils.helpers import iso_cutoff, utc_now_iso

logger = logging.getLogger(__name__)


class DashboardHistoryOperations:
    """Dashboard history database operations."""

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)

    def capture_dashboard_snapshot(
        self,
        agent_name: str,
        config: Dict[str, Any],
        dashboard_mtime: str
    ) -> int:
        """Capture snapshot of all trackable widget values from a dashboard config.

        Trackable widgets are: metric, progress, status (with numeric values).
        Each widget is stored with a key generated from its position or explicit ID.

        Args:
            agent_name: Name of the agent
            config: Dashboard configuration dict (with sections/widgets)
            dashboard_mtime: Modification time of the dashboard.yaml file

        Returns:
            Number of widget values captured
        """
        if not config or "sections" not in config:
            return 0

        captured_at = utc_now_iso()
        captured_count = 0

        with get_engine().begin() as conn:
            for section_idx, section in enumerate(config.get("sections", [])):
                for widget_idx, widget in enumerate(section.get("widgets", [])):
                    widget_type = widget.get("type")

                    # Only track widgets with meaningful values
                    if widget_type not in ("metric", "progress", "status"):
                        continue

                    # ent#479: a widget BOUND to a declared metric already has
                    # a real time series behind it (`metric_points`). Snapshotting
                    # it would make this table a second source of truth for the
                    # same number — and the two would disagree the moment the
                    # dashboard poll and the recording cadence drift apart.
                    if widget.get("metric"):
                        continue

                    # Generate widget key: use explicit id if provided, else position-based
                    widget_key = widget.get("id") or f"s{section_idx}_w{widget_idx}"
                    widget_label = widget.get("label", "")
                    value = widget.get("value")

                    # Extract numeric and text values
                    value_numeric = None
                    value_text = None

                    if value is not None:
                        if isinstance(value, (int, float)):
                            value_numeric = float(value)
                        elif isinstance(value, str):
                            # Try to parse numeric from string (e.g., "1,234" or "95%")
                            value_text = value
                            try:
                                # Remove common formatting
                                cleaned = value.replace(",", "").replace("%", "").replace("$", "").strip()
                                value_numeric = float(cleaned)
                            except (ValueError, TypeError):
                                pass

                    # Skip if no value to store
                    if value_numeric is None and value_text is None:
                        continue

                    record_id = self._generate_id()
                    conn.execute(insert(agent_dashboard_values).values(
                        id=record_id,
                        agent_name=agent_name,
                        widget_key=widget_key,
                        widget_label=widget_label,
                        widget_type=widget_type,
                        value_numeric=value_numeric,
                        value_text=value_text,
                        dashboard_mtime=dashboard_mtime,
                        captured_at=captured_at,
                    ))
                    captured_count += 1

        if captured_count > 0:
            logger.debug(f"Captured {captured_count} dashboard values for agent {agent_name}")

        return captured_count

    def get_widget_history(
        self,
        agent_name: str,
        widget_key: str,
        hours: int = 24,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get historical values for a specific widget.

        Args:
            agent_name: Name of the agent
            widget_key: Widget identifier (explicit id or position-based)
            hours: How many hours of history to retrieve
            limit: Maximum number of records

        Returns:
            List of dicts with 't' (ISO timestamp) and 'v' (numeric value)
        """
        stmt = (
            select(
                agent_dashboard_values.c.captured_at,
                agent_dashboard_values.c.value_numeric,
                agent_dashboard_values.c.value_text,
            )
            .where(
                agent_dashboard_values.c.agent_name == agent_name,
                agent_dashboard_values.c.widget_key == widget_key,
                agent_dashboard_values.c.captured_at > iso_cutoff(hours),
            )
            .order_by(agent_dashboard_values.c.captured_at.asc())
            .limit(limit)
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

            results = []
            for row in rows:
                value = row["value_numeric"]
                if value is None and row["value_text"]:
                    # Use text value if no numeric
                    value = row["value_text"]
                results.append({
                    "t": row["captured_at"],
                    "v": value
                })

            return results

    def get_all_widget_history(
        self,
        agent_name: str,
        hours: int = 24
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get history for all widgets of an agent, keyed by widget_key.

        Args:
            agent_name: Name of the agent
            hours: How many hours of history to retrieve

        Returns:
            Dict mapping widget_key to list of {t, v} values
        """
        stmt = (
            select(
                agent_dashboard_values.c.widget_key,
                agent_dashboard_values.c.captured_at,
                agent_dashboard_values.c.value_numeric,
                agent_dashboard_values.c.value_text,
            )
            .where(
                agent_dashboard_values.c.agent_name == agent_name,
                agent_dashboard_values.c.captured_at > iso_cutoff(hours),
            )
            .order_by(
                agent_dashboard_values.c.widget_key.asc(),
                agent_dashboard_values.c.captured_at.asc(),
            )
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).mappings().all()

            results: Dict[str, List[Dict[str, Any]]] = {}
            for row in rows:
                widget_key = row["widget_key"]
                if widget_key not in results:
                    results[widget_key] = []

                value = row["value_numeric"]
                if value is None and row["value_text"]:
                    value = row["value_text"]

                results[widget_key].append({
                    "t": row["captured_at"],
                    "v": value
                })

            return results

    def calculate_widget_stats(
        self,
        values: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate statistics from a list of historical values.

        Args:
            values: List of {t, v} dicts from get_widget_history

        Returns:
            Dict with min, max, avg, trend, trend_percent
        """
        if not values:
            return {
                "min": None,
                "max": None,
                "avg": None,
                "trend": "stable",
                "trend_percent": 0
            }

        # Extract numeric values only
        numeric_values = [v["v"] for v in values if isinstance(v.get("v"), (int, float))]

        if not numeric_values:
            return {
                "min": None,
                "max": None,
                "avg": None,
                "trend": "stable",
                "trend_percent": 0
            }

        min_val = min(numeric_values)
        max_val = max(numeric_values)
        avg_val = sum(numeric_values) / len(numeric_values)

        # Calculate trend: compare first half avg to second half avg
        trend = "stable"
        trend_percent = 0

        if len(numeric_values) >= 2:
            mid = len(numeric_values) // 2
            first_half_avg = sum(numeric_values[:mid]) / mid if mid > 0 else numeric_values[0]
            second_half_avg = sum(numeric_values[mid:]) / (len(numeric_values) - mid)

            if first_half_avg > 0:
                trend_percent = ((second_half_avg - first_half_avg) / first_half_avg) * 100

                if trend_percent > 5:
                    trend = "up"
                elif trend_percent < -5:
                    trend = "down"

        return {
            "min": round(min_val, 2),
            "max": round(max_val, 2),
            "avg": round(avg_val, 2),
            "trend": trend,
            "trend_percent": round(trend_percent, 1)
        }

    def get_last_captured_mtime(self, agent_name: str) -> Optional[str]:
        """Get the most recent dashboard_mtime that was captured for an agent.

        Used for change detection - only capture new snapshots when mtime changes.

        Args:
            agent_name: Name of the agent

        Returns:
            Last captured dashboard_mtime or None if no history
        """
        stmt = (
            select(agent_dashboard_values.c.dashboard_mtime)
            .where(agent_dashboard_values.c.agent_name == agent_name)
            .order_by(agent_dashboard_values.c.captured_at.desc())
            .limit(1)
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return row["dashboard_mtime"] if row else None

    def cleanup_old_snapshots(self, days: int = 30) -> int:
        """Delete dashboard value records older than specified days.

        Args:
            days: Number of days to retain

        Returns:
            Number of records deleted
        """
        stmt = delete(agent_dashboard_values).where(
            agent_dashboard_values.c.captured_at < iso_cutoff(days * 24)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            deleted = result.rowcount
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old dashboard value records")
            return deleted

    # =========================================================================
    # Dashboard Cache (survives backend restarts)
    # =========================================================================

    def cache_valid_dashboard(
        self,
        agent_name: str,
        config: Dict[str, Any],
        last_modified: Optional[str] = None
    ) -> None:
        """Cache a valid dashboard config in the database.

        Called whenever the agent returns a valid dashboard.yaml parse.
        Replaces in-memory _last_valid_dashboard dict.
        """
        import json
        now = utc_now_iso()
        stmt = make_insert(agent_dashboard_cache).values(
            agent_name=agent_name,
            config_json=json.dumps(config),
            last_modified=last_modified,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[agent_dashboard_cache.c.agent_name],
            set_={
                "config_json": stmt.excluded.config_json,
                "last_modified": stmt.excluded.last_modified,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def get_cached_dashboard(self, agent_name: str) -> Optional[Dict[str, Any]]:
        """Get the last valid dashboard config from cache.

        Returns None if no cached dashboard exists.
        """
        import json
        stmt = select(
            agent_dashboard_cache.c.config_json,
            agent_dashboard_cache.c.last_modified,
            agent_dashboard_cache.c.updated_at,
        ).where(agent_dashboard_cache.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            return {
                "has_dashboard": True,
                "config": json.loads(row["config_json"]),
                "last_modified": row["last_modified"],
                "error": None
            }

    def delete_cached_dashboard(self, agent_name: str) -> None:
        """Delete cached dashboard for an agent."""
        stmt = delete(agent_dashboard_cache).where(
            agent_dashboard_cache.c.agent_name == agent_name
        )
        with get_engine().begin() as conn:
            conn.execute(stmt)

    def has_cached_dashboard(self, agent_name: str) -> bool:
        """Check if an agent has ever had a valid dashboard (for tab visibility)."""
        stmt = (
            select(agent_dashboard_cache.c.agent_name)
            .where(agent_dashboard_cache.c.agent_name == agent_name)
            .limit(1)
        )
        with get_engine().connect() as conn:
            return conn.execute(stmt).first() is not None

    def delete_agent_dashboard_history(self, agent_name: str) -> int:
        """Delete all dashboard history for an agent (when agent is deleted).

        Args:
            agent_name: Name of the agent

        Returns:
            Number of records deleted
        """
        with get_engine().begin() as conn:
            conn.execute(
                delete(agent_dashboard_values).where(
                    agent_dashboard_values.c.agent_name == agent_name
                )
            )
            # Also delete cached dashboard config
            result = conn.execute(
                delete(agent_dashboard_cache).where(
                    agent_dashboard_cache.c.agent_name == agent_name
                )
            )
            # Preserve original behavior: cursor.rowcount reflected the LAST
            # executed statement (the cache delete), not the values delete.
            return result.rowcount
