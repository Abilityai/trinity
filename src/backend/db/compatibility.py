"""
Agent compatibility results database operations (#668).

Persists the *latest* compatibility report per agent (one row, upserted) so the
Overview tab loads instantly with the last-known AI verdicts and the fleet can
aggregate "N agents have HARD findings" without re-spending tokens.

This is a deliberate departure from the issue's original "no DB table" note: the
report is **not** cheaply recomputable (the AI checks cost real API calls), so a
durable latest-snapshot is what lets AI findings show on every visit and unlocks
fleet aggregation + cheap post-fix re-checks. STATIC checks are recomputed live
on each read; the persisted AI verdicts are merged in (see
`services/compatibility/__init__.py`).

`checks_json` stores the full last report's check list as a JSON string; the
helper parses it on the way out so callers see a list of dicts.

SQLAlchemy Core (like db/canary.py) so it runs unchanged on SQLite + PostgreSQL.
"""

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select, insert, update, func, and_

from .engine import get_engine
from .tables import agent_compatibility_results
from utils.helpers import utc_now_iso


class CompatibilityOperations:
    """Database operations for the per-agent compatibility results table."""

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def upsert_result(
        self,
        agent_name: str,
        *,
        overall_status: str,
        checks: List[Dict[str, Any]],
        hard_count: int,
        soft_count: int,
        info_count: int,
        container_running: bool,
        ai_ran_at: Optional[str],
        static_ran_at: Optional[str],
    ) -> None:
        """Insert or replace the single latest-snapshot row for an agent."""
        payload = dict(
            overall_status=overall_status,
            checks_json=json.dumps(checks),
            hard_count=int(hard_count),
            soft_count=int(soft_count),
            info_count=int(info_count),
            container_running=1 if container_running else 0,
            ai_ran_at=ai_ran_at,
            static_ran_at=static_ran_at,
            updated_at=utc_now_iso(),
        )
        t = agent_compatibility_results
        with get_engine().begin() as conn:
            existing = conn.execute(
                select(t.c.agent_name).where(t.c.agent_name == agent_name)
            ).first()
            if existing:
                conn.execute(
                    update(t).where(t.c.agent_name == agent_name).values(**payload)
                )
            else:
                conn.execute(insert(t).values(agent_name=agent_name, **payload))

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def get_result(self, agent_name: str) -> Optional[Dict[str, Any]]:
        """Fetch the latest persisted report for an agent (checks parsed), or None."""
        t = agent_compatibility_results
        stmt = select(t).where(t.c.agent_name == agent_name)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
            return self._row_to_dict(row) if row else None

    def count_agents_with_hard_findings(self) -> int:
        """Fleet aggregation: how many agents currently have ≥1 HARD finding."""
        t = agent_compatibility_results
        stmt = (
            select(func.count())
            .select_from(t)
            .where(t.c.hard_count > 0)
        )
        with get_engine().connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    # Delete-on-agent-delete and re-key-on-rename are handled generically by the
    # AGENT_REFS registry in db/agent_cleanup.py (this table is registered there
    # with Policy.CASCADE) — no per-table method needed here.

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a RowMapping to dict; parse `checks_json` into a list."""
        result = dict(row)
        raw = result.pop("checks_json", None)
        checks: List[Dict[str, Any]] = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    checks = parsed
            except (TypeError, ValueError):
                checks = []
        result["checks"] = checks
        result["container_running"] = bool(result.get("container_running"))
        return result
