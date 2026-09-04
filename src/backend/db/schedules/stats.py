"""Light per-agent/fleet execution stats + token stats (EXEC-022 / #18)."""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from sqlalchemy import text

from ..engine import get_engine
from utils.helpers import iso_cutoff
from ._common import _norm_ts
from .analytics import _BUCKET_ORDER, _bucket_for_trigger

class ScheduleStatsMixin:
    """Execution stat rollups + fleet queries."""

    def get_agent_last_execution_at(self, agent_name: str) -> Optional[str]:
        """All-time ``MAX(started_at)`` for the agent, or None (#1854).

        Backs the `stale` MCP-key health state: a key whose ``last_used_at``
        materially predates the agent's most recent execution is the motivating
        incident's exact signature ("the agent-scoped key sat unused for
        months") — non-NULL but old, which a binary used/unused predicate
        renders as green. Deliberately unwindowed: the comparison is
        key-vs-agent, not "in the last N hours".
        """
        with get_engine().connect() as conn:
            return conn.execute(
                text(
                    "SELECT MAX(started_at) FROM schedule_executions "
                    "WHERE agent_name = :agent_name"
                ),
                {"agent_name": agent_name},
            ).scalar()

    def get_agent_execution_stats(self, agent_name: str, hours: int = 24) -> Dict:
        """Get execution statistics for a single agent.

        Used for platform metrics injection in dashboard (DASH-001).

        Args:
            agent_name: Name of the agent
            hours: Time window in hours (default: 24)

        Returns:
            Dict with execution stats: task_count, success_count, failed_count,
            running_count, success_rate, total_cost, avg_duration_ms, last_execution_at
        """
        with get_engine().connect() as conn:
            row = conn.execute(
                text("""
                SELECT
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    AVG(duration_ms) as avg_duration_ms,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE agent_name = :agent_name
                AND started_at > :cutoff
                """),
                {"agent_name": agent_name, "cutoff": iso_cutoff(hours)},
            ).mappings().first()
            if not row or row["task_count"] == 0:
                return {
                    "task_count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "running_count": 0,
                    "success_rate": 0,
                    "total_cost": 0,
                    "avg_duration_ms": None,
                    "last_execution_at": None
                }

            task_count = row["task_count"]
            success_count = row["success_count"] or 0
            success_rate = round((success_count / task_count * 100), 1) if task_count > 0 else 0

            return {
                "task_count": task_count,
                "success_count": success_count,
                "failed_count": row["failed_count"] or 0,
                "running_count": row["running_count"] or 0,
                "success_rate": success_rate,
                "total_cost": round(row["total_cost"] or 0, 4),
                "avg_duration_ms": int(row["avg_duration_ms"]) if row["avg_duration_ms"] else None,
                "last_execution_at": row["last_execution_at"]
            }

    def get_all_agents_execution_stats(self, hours: int = 24) -> List[Dict]:
        """Get execution statistics for all agents.

        Returns aggregated stats per agent for the specified time window.

        Args:
            hours: Time window in hours (default: 24)

        Returns:
            List of dicts with agent execution stats
        """
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    COUNT(*) as task_count,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
                    SUM(COALESCE(cost, 0)) as total_cost,
                    MAX(started_at) as last_execution_at
                FROM schedule_executions
                WHERE started_at > :cutoff
                GROUP BY agent_name
                """),
                {"cutoff": iso_cutoff(hours)},
            ).mappings().all()

            results = []
            for row in rows:
                task_count = row["task_count"]
                success_count = row["success_count"]
                success_rate = round((success_count / task_count * 100), 1) if task_count > 0 else 0

                results.append({
                    "name": row["agent_name"],
                    "task_count_24h": task_count,
                    "success_count": success_count,
                    "failed_count": row["failed_count"],
                    "running_count": row["running_count"],
                    "success_rate": success_rate,
                    "total_cost": round(row["total_cost"], 4) if row["total_cost"] else 0,
                    "last_execution_at": row["last_execution_at"]
                })

            return results

    def get_all_agents_execution_stats_dual(self) -> List[Dict]:
        """Get execution statistics for all agents with both 24h and 7d windows.

        Single SQL query using CASE WHEN to compute both time windows efficiently.

        Returns:
            List of dicts with agent execution stats for both 24h and 7d windows.
        """
        cutoff_24h = iso_cutoff(24)
        cutoff_7d = iso_cutoff(168)
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    SUM(CASE WHEN started_at > :c24 THEN 1 ELSE 0 END) as task_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'success' THEN 1 ELSE 0 END) as success_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'failed' THEN 1 ELSE 0 END) as failed_count_24h,
                    SUM(CASE WHEN started_at > :c24 AND status = 'running' THEN 1 ELSE 0 END) as running_count_24h,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(cost, 0) ELSE 0 END) as total_cost_24h,
                    MAX(CASE WHEN started_at > :c24 THEN started_at ELSE NULL END) as last_execution_at_24h,
                    COUNT(*) as task_count_7d,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count_7d,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count_7d,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count_7d,
                    SUM(COALESCE(cost, 0)) as total_cost_7d,
                    MAX(started_at) as last_execution_at_7d
                FROM schedule_executions
                WHERE started_at > :c7d
                GROUP BY agent_name
                """),
                {"c24": cutoff_24h, "c7d": cutoff_7d},
            ).mappings().all()

            results = []
            for row in rows:
                task_count_24h = row["task_count_24h"] or 0
                success_count_24h = row["success_count_24h"] or 0
                success_rate_24h = round((success_count_24h / task_count_24h * 100), 1) if task_count_24h > 0 else 0

                task_count_7d = row["task_count_7d"] or 0
                success_count_7d = row["success_count_7d"] or 0
                success_rate_7d = round((success_count_7d / task_count_7d * 100), 1) if task_count_7d > 0 else 0

                results.append({
                    "name": row["agent_name"],
                    "task_count_24h": task_count_24h,
                    "success_count": success_count_24h,
                    "failed_count": row["failed_count_24h"] or 0,
                    "running_count": row["running_count_24h"] or 0,
                    "success_rate": success_rate_24h,
                    "total_cost": round(row["total_cost_24h"] or 0, 4),
                    "last_execution_at": row["last_execution_at_24h"],
                    "task_count_7d": task_count_7d,
                    "success_count_7d": success_count_7d,
                    "failed_count_7d": row["failed_count_7d"] or 0,
                    "running_count_7d": row["running_count_7d"] or 0,
                    "success_rate_7d": success_rate_7d,
                    "total_cost_7d": round(row["total_cost_7d"] or 0, 4),
                    "last_execution_at_7d": row["last_execution_at_7d"]
                })

            return results

    def get_agent_token_stats(self, agent_name: str) -> Dict:
        """Get token usage statistics for an agent: lifetime, 24h, 7d, and 7-day daily breakdown.

        Used for the agent detail token usage display (issue #250).
        """
        cutoff_24h = iso_cutoff(24)
        cutoff_7d = iso_cutoff(168)

        with get_engine().connect() as conn:
            # Lifetime + 24h + 7d in one pass
            row = conn.execute(
                text("""
                SELECT
                    COUNT(*) as lifetime_executions,
                    SUM(COALESCE(cost, 0)) as lifetime_cost,
                    SUM(COALESCE(context_used, 0)) as lifetime_context_tokens,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(cost, 0) ELSE 0 END) as cost_24h,
                    SUM(CASE WHEN started_at > :c24 THEN COALESCE(context_used, 0) ELSE 0 END) as context_tokens_24h,
                    SUM(CASE WHEN started_at > :c24 THEN 1 ELSE 0 END) as executions_24h,
                    SUM(CASE WHEN started_at > :c7d THEN COALESCE(cost, 0) ELSE 0 END) as cost_7d,
                    SUM(CASE WHEN started_at > :c7d THEN COALESCE(context_used, 0) ELSE 0 END) as context_tokens_7d,
                    SUM(CASE WHEN started_at > :c7d THEN 1 ELSE 0 END) as executions_7d
                FROM schedule_executions
                WHERE agent_name = :agent_name
                  AND status IN ('success', 'failed')
                """),
                {"c24": cutoff_24h, "c7d": cutoff_7d, "agent_name": agent_name},
            ).mappings().first()

            lifetime_cost = round(row["lifetime_cost"] or 0, 6)
            lifetime_context_tokens = row["lifetime_context_tokens"] or 0
            lifetime_executions = row["lifetime_executions"] or 0
            cost_24h = round(row["cost_24h"] or 0, 6)
            context_tokens_24h = row["context_tokens_24h"] or 0
            executions_24h = row["executions_24h"] or 0
            cost_7d = round(row["cost_7d"] or 0, 6)
            context_tokens_7d = row["context_tokens_7d"] or 0
            executions_7d = row["executions_7d"] or 0

            # Per-day breakdown for last 7 days.
            #
            # `started_at` is an ISO-Z TEXT column (Invariant #16), so the day
            # bucket is a SUBSTRING, never a date function — byte-identical
            # to the bucketer `get_agent_analytics` (#1107) already uses,
            # so the two day series cannot drift.
            #
            # `DATE(started_at)` was NOT dialect-agnostic and failed SILENTLY on
            # PostgreSQL: `date(x)` there is the type-name-as-function cast, so
            # the column comes back as a `datetime.date`, while the gap-filled
            # axis below is keyed by `strftime("%Y-%m-%d")` STRINGS. Every
            # lookup missed, so the whole series read as legitimate zeros — the
            # scalar `cost_24h` / `cost_7d` / lifetime fields are computed by a
            # different query and stayed correct, which is why the agent header
            # showed a real "Today $100" above a permanently flat sparkline.
            # Nothing raised, on either backend.
            #
            # Pre-#1474 scheduler rows (`YYYY-MM-DD HH:MM:SS`, space separator,
            # no Z) need no special handling HERE: the separator sits at
            # position 11, outside a 10-char slice, so both shapes yield the
            # same day. ent#326's hour bucket does need `replace`, because its
            # 13-char slice spans the separator — do not copy that expression
            # here on the strength of the similar name.
            day_rows = conn.execute(
                text("""
                SELECT
                    substr(started_at, 1, 10) as day,
                    SUM(COALESCE(cost, 0)) as day_cost,
                    SUM(COALESCE(context_used, 0)) as day_context_tokens,
                    COUNT(*) as day_executions
                FROM schedule_executions
                WHERE agent_name = :agent_name
                  AND started_at > :c7d
                  AND status IN ('success', 'failed')
                GROUP BY substr(started_at, 1, 10)
                ORDER BY day ASC
                """),
                {"agent_name": agent_name, "c7d": cutoff_7d},
            ).mappings().all()

            # Belt to the SQL's braces. The bug above was a KEY-TYPE mismatch
            # that read as data, so the join is normalized on the Python side
            # too: whatever a backend hands back, the axis is matched on
            # `YYYY-MM-DD` text. A future dialect that types this column
            # differently degrades to correct instead of to silent zeros.
            raw_days = {_day_bucket_key(row["day"]): row for row in day_rows}

            # Build complete 7-day series (fill gaps with zero)
            now_utc = datetime.now(timezone.utc)
            daily_breakdown = []
            for i in range(6, -1, -1):
                d = (now_utc - timedelta(days=i)).strftime("%Y-%m-%d")
                if d in raw_days:
                    r = raw_days[d]
                    daily_breakdown.append({
                        "date": d,
                        "cost": round(r["day_cost"] or 0, 6),
                        "context_tokens": r["day_context_tokens"] or 0,
                        "executions": r["day_executions"] or 0,
                    })
                else:
                    daily_breakdown.append({"date": d, "cost": 0.0, "context_tokens": 0, "executions": 0})

            # Trend: today vs 7d daily average (excluding today to avoid comparison bias)
            avg_daily_cost = cost_7d / 7.0 if cost_7d > 0 else 0.0
            if avg_daily_cost > 0:
                trend_pct = round(((cost_24h - avg_daily_cost) / avg_daily_cost) * 100, 1)
            else:
                trend_pct = 0.0

            return {
                "lifetime_cost": lifetime_cost,
                "lifetime_context_tokens": lifetime_context_tokens,
                "lifetime_executions": lifetime_executions,
                "cost_24h": cost_24h,
                "context_tokens_24h": context_tokens_24h,
                "executions_24h": executions_24h,
                "cost_7d": cost_7d,
                "context_tokens_7d": context_tokens_7d,
                "executions_7d": executions_7d,
                "avg_daily_cost": round(avg_daily_cost, 6),
                "trend_cost_pct": trend_pct,
                "daily_breakdown": daily_breakdown,
            }

    # -------------------------------------------------------------------------
    # Fleet-level execution queries (EXEC-022 / Issue #18)
    # -------------------------------------------------------------------------

    def get_fleet_executions(
        self,
        agent_names: Optional[List[str]],  # None = admin (no agent filter)
        *,
        status: Optional[str] = None,
        triggered_by: Optional[str] = None,
        hours: Optional[int] = 24,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """Cross-fleet execution list for the Unified Executions Dashboard.

        Returns summary rows (no large text fields: response, tool_calls,
        execution_log). The error column is truncated to 200 chars for the
        failed-row one-liner.

        agent_names=None → admin path, no agent_name filter applied.
        agent_names=[]   → non-admin with zero accessible agents, returns [].
        hours=0 or None  → no time-window filter (all-time).
        """
        if agent_names is not None and len(agent_names) == 0:
            return []

        conditions = []
        bind: Dict = {}

        if agent_names is not None:
            keys = [f"an{i}" for i in range(len(agent_names))]
            conditions.append("agent_name IN (%s)" % ",".join(f":{k}" for k in keys))
            bind.update(dict(zip(keys, agent_names)))

        if status:
            conditions.append("status = :status")
            bind["status"] = status

        if triggered_by:
            conditions.append("triggered_by = :triggered_by")
            bind["triggered_by"] = triggered_by

        if hours:
            conditions.append("started_at > :cutoff")
            bind["cutoff"] = iso_cutoff(hours)

        if search:
            conditions.append("message LIKE :search")
            bind["search"] = f"%{search}%"

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        bind["lim"] = limit
        bind["off"] = offset

        # Core (#300): named binds; SELECT body is dialect-portable (the only
        # SQLite-ism, SUBSTR, exists on PostgreSQL too). `.mappings()` keeps the
        # dict-row shape the router expects.
        with get_engine().connect() as conn:
            rows = conn.execute(text(f"""
                SELECT
                    id, schedule_id, agent_name, status, started_at, completed_at,
                    duration_ms, message, triggered_by,
                    context_used, context_max, cost,
                    CASE WHEN status IN ('failed', 'error')
                         THEN SUBSTR(error, 1, 200)
                         ELSE NULL END AS error_summary,
                    source_user_id, source_user_email, source_agent_name,
                    source_mcp_key_id, source_mcp_key_name,
                    model_used, fan_out_id, business_status, validation_execution_id,
                    turn_integrity,
                    queued_at
                FROM schedule_executions
                {where}
                ORDER BY started_at DESC
                LIMIT :lim OFFSET :off
            """), bind).mappings().all()
            out = []
            for row in rows:
                d = dict(row)
                # #1474: normalize naive scheduler-written timestamps to UTC 'Z'
                # so the FleetExecutionSummary response (ExecutionsPanel) never
                # serializes naive.
                d["started_at"] = _norm_ts(d.get("started_at"))
                d["completed_at"] = _norm_ts(d.get("completed_at"))
                d["queued_at"] = _norm_ts(d.get("queued_at"))
                out.append(d)
            return out

    def get_fleet_execution_stats(
        self,
        agent_names: Optional[List[str]],  # None = admin (no agent filter)
        hours: int = 24,
    ) -> Dict:
        """Aggregate stats for the fleet executions stat cards.

        Returns total/success/failed/cost within the time window, plus a
        current running + queued count (not time-windowed — a run that started
        before the window is still live).
        """
        if agent_names is not None and len(agent_names) == 0:
            return {
                "total": 0, "success_count": 0, "failed_count": 0,
                "total_cost": 0.0, "success_rate": 0.0,
                "running_count": 0, "queued_count": 0, "hours": hours,
                "deleted_agent_count": 0, "deleted_agent_cost": 0.0,
            }

        bind: Dict = {}
        agent_where = ""
        if agent_names is not None:
            keys = [f"an{i}" for i in range(len(agent_names))]
            # Qualified: the #1743 LEFT JOIN puts agent_name in both tables.
            agent_where = "WHERE se.agent_name IN (%s)" % ",".join(f":{k}" for k in keys)
            bind.update(dict(zip(keys, agent_names)))

        # Single-pass query: windowed totals via conditional aggregation +
        # live running/queued counts without a time filter, all in one scan.
        # time_cond = "1=1" when hours=0 (all-time) so CASE stays unconditional.
        # ("1=1", not "1": PostgreSQL requires a boolean in CASE WHEN — a bare
        # integer raises; SQLite tolerated it. #300.) The named :cutoff bind is
        # reused across all four CASE expressions.
        if hours:
            time_cond = "se.started_at > :cutoff"
            bind["cutoff"] = iso_cutoff(hours)
        else:
            time_cond = "1=1"

        with get_engine().connect() as conn:
            row = conn.execute(text(f"""
                SELECT
                    SUM(CASE WHEN {time_cond} THEN 1 ELSE 0 END) AS total,
                    SUM(CASE WHEN {time_cond} AND se.status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN {time_cond} AND se.status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed_count,
                    SUM(CASE WHEN {time_cond} THEN COALESCE(se.cost, 0) ELSE 0 END) AS total_cost,
                    SUM(CASE WHEN se.status = 'running' THEN 1 ELSE 0 END) AS running_count,
                    SUM(CASE WHEN se.status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                    -- #1743: the slice of the window that belongs to no LIVE agent.
                    -- Execution rows outlive their agent on purpose (cost is
                    -- billing truth, and a soft-deleted agent is recoverable), but
                    -- the per-agent surfaces only render live agents — so without
                    -- this the fleet total and the sum of the tiles differ by an
                    -- amount nothing explains. Reported, never silently dropped.
                    SUM(CASE WHEN {time_cond} AND ao.agent_name IS NULL THEN 1 ELSE 0 END)
                        AS deleted_agent_count,
                    SUM(CASE WHEN {time_cond} AND ao.agent_name IS NULL
                             THEN COALESCE(se.cost, 0) ELSE 0 END) AS deleted_agent_cost
                FROM schedule_executions se
                -- Joins only LIVE agents, so both a soft-deleted row (deleted_at
                -- set) and a hard-purged one (no row at all) fall to NULL.
                LEFT JOIN agent_ownership ao
                       ON ao.agent_name = se.agent_name AND ao.deleted_at IS NULL
                {agent_where}
            """), bind).mappings().first()
            total = row["total"] or 0
            success_count = row["success_count"] or 0
            failed_count = row["failed_count"] or 0
            total_cost = row["total_cost"] or 0.0
            terminal = success_count + failed_count
            success_rate = round(success_count / terminal * 100, 1) if terminal > 0 else 0.0

            return {
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "total_cost": round(total_cost, 4),
                "success_rate": success_rate,
                "running_count": row["running_count"] or 0,
                "queued_count": row["queued_count"] or 0,
                "hours": hours,
                "deleted_agent_count": row["deleted_agent_count"] or 0,
                "deleted_agent_cost": round(row["deleted_agent_cost"] or 0.0, 4),
            }

    # ------------------------------------------------------------------
    # Telemetry outcome readers (ent#437)
    # ------------------------------------------------------------------

    def count_terminal_executions_by_status(self, hours: int = 24) -> Dict[str, int]:
        """Terminal rows grouped by raw ``status`` within the window (ent#437).

        Feeds the telemetry aggregate's ``outcomes.by_status``. ``hours=0`` is
        all-time, matching :meth:`get_fleet_execution_stats`. Counts only — no
        agent names, no ids. In-flight rows (``running`` / ``queued`` /
        ``pending_retry``) are excluded: they are not outcomes yet.
        """
        bind: Dict = {}
        where = ["status NOT IN ('running', 'queued', 'pending_retry')"]
        if hours:
            where.append("started_at > :cutoff")
            bind["cutoff"] = iso_cutoff(hours)
        sql = (
            "SELECT status, COUNT(*) AS cnt FROM schedule_executions "
            f"WHERE {' AND '.join(where)} GROUP BY status"
        )
        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), bind).mappings().all()
        return {str(r["status"] or ""): int(r["cnt"] or 0) for r in rows}

    def first_autonomous_success_at(self) -> Optional[str]:
        """``started_at`` of the earliest SUCCESS with an autonomous trigger
        (schedule / webhook), or None (ent#437).

        The warm-ask milestone. One ``LIMIT 1`` read; the caller memoises the
        answer in ``system_settings`` once it exists, so this runs only while
        the install has never completed an autonomous run.
        """
        sql = (
            "SELECT started_at FROM schedule_executions "
            "WHERE status = 'success' AND triggered_by IN ('schedule', 'webhook') "
            "ORDER BY started_at ASC LIMIT 1"
        )
        with get_engine().connect() as conn:
            row = conn.execute(text(sql)).first()
        return str(row[0]) if row and row[0] else None

    # ------------------------------------------------------------------
    # Bucketed fleet timeline (ent#326)
    # ------------------------------------------------------------------

    def get_fleet_execution_timeline(
        self,
        agent_names: Optional[List[str]],  # None = admin (no agent filter)
        group_by: str,
        hours: int,
        split: Optional[str] = None,
    ) -> List[Dict]:
        """Bucketed fleet execution rollups — the time-series sibling of
        :meth:`get_fleet_execution_stats` (ent#326).

        One endpoint for the grid's data tiles (ent#94 #96/#98/#101) so three
        tiles don't each grow their own query. Same table, same access model as
        `/api/executions`; time-series shape instead of scalars.

        `group_by`:
          * ``hour`` / ``day`` — UTC time buckets, **gap-filled** by the caller
            so a chart renders a real zero instead of skipping the interval
            (the #1107 convention).
          * ``trigger`` — grouped through ``_TRIGGER_BUCKETS`` with its explicit
            ``Other`` catch-all, so a newly-added trigger type never silently
            vanishes from a chart.
          * ``agent`` — per agent_name.

        `split="trigger"` adds a SECOND dimension to a time grouping: each hour
        (or day) additionally carries its own trigger breakdown, so a stacked
        chart is one request rather than one per bucket name (ent#96). It is
        only meaningful over a time axis — the router refuses it for
        `trigger`/`agent`, where it would mean "group by trigger, then by
        trigger".

        Per bucket: execution count, failure count, cost sum, context-token sum.

        NOTE on "tokens": ``schedule_executions`` has NO token columns — it
        carries ``cost``, ``context_used`` and ``context_max``. ``output_tokens``
        exists only on ``chat_messages``, which covers chat turns rather than
        fleet executions. So ``context_used`` is reported as what it is,
        **context-window occupancy**, and the field is named accordingly. A tile
        labelling this "tokens consumed" would be the liveness-vs-quality
        mislabel ent#326 explicitly warns against.
        """
        if agent_names is not None and len(agent_names) == 0:
            return []

        bind: Dict = {}
        where = ["1=1"]
        if agent_names is not None:
            keys = [f"an{i}" for i in range(len(agent_names))]
            where.append("agent_name IN (%s)" % ",".join(f":{k}" for k in keys))
            bind.update(dict(zip(keys, agent_names)))
        if hours:
            where.append("started_at > :cutoff")
            bind["cutoff"] = iso_cutoff(hours)

        # `started_at` is an ISO-Z TEXT column (Invariant #16), so substr is the
        # dialect-agnostic bucketer on both SQLite and PostgreSQL — no date
        # functions, no timezone conversion, and it slices the SAME UTC string
        # the row was written with.
        # Pre-#1474 scheduler rows are `YYYY-MM-DD HH:MM:SS` (space separator,
        # no Z). They pass the lexicographic cutoff, so they are COUNTED — but
        # `substr` then yields `2026-08-06 10`, which never matches the
        # gap-filled axis key `2026-08-06T10`, and the bucket renders as a real
        # zero while `group_by=trigger|agent` and `/stats` still include them.
        # That is the "chart contradicts the stat card above it" failure this
        # endpoint counts `error` as failed to avoid, arriving by another route.
        # `replace` is ANSI SQL and present on both backends.
        normalized = "replace(started_at, ' ', 'T')"
        if group_by == "hour":
            key_sql = f"substr({normalized}, 1, 13)"   # YYYY-MM-DDTHH
        elif group_by == "day":
            key_sql = f"substr({normalized}, 1, 10)"   # YYYY-MM-DD
        elif group_by == "trigger":
            key_sql = "triggered_by"
        elif group_by == "agent":
            key_sql = "agent_name"
        else:  # pragma: no cover - the router validates first
            raise ValueError(f"unsupported group_by: {group_by}")

        # ent#96: the split is a second GROUP BY column, not a second query.
        # `triggered_by` is folded to its user-facing bucket in Python (see
        # `_fold_split_buckets`) for the same reason `group_by=trigger` is: the
        # `Other` catch-all is a Python map, and a CASE would drop the guarantee
        # that a newly-added trigger shows up rather than vanishing.
        split_select = ", triggered_by AS split_key" if split == "trigger" else ""
        split_group = ", triggered_by" if split == "trigger" else ""

        sql = f"""
            SELECT {key_sql} AS bucket{split_select},
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                   SUM(COALESCE(cost, 0)) AS cost,
                   SUM(COALESCE(context_used, 0)) AS context_used
            FROM schedule_executions
            WHERE {" AND ".join(where)}
            GROUP BY {key_sql}{split_group}
        """

        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), bind).mappings().all()

        out = []
        for r in rows:
            row = {
                "bucket": r["bucket"],
                "total": int(r["total"] or 0),
                "failed": int(r["failed"] or 0),
                "success": int(r["success"] or 0),
                "cost": round(float(r["cost"] or 0.0), 6),
                "context_used": int(r["context_used"] or 0),
            }
            if split == "trigger":
                row["split_key"] = r["split_key"]
            out.append(row)
        return out

    def trigger_bucket_order(self) -> List[str]:
        """The stack/legend order for trigger buckets (ent#96).

        Exposed through the facade so a caller — the timeline route, and any
        future tile — reads the SAME `_BUCKET_ORDER` the folding uses instead
        of holding its own copy. A second list is how a chart and its legend
        come to disagree about where `Other` sits (the #1107 lesson this
        module's `_fold_trigger_buckets` already records).
        """
        return list(_BUCKET_ORDER)

    def shape_execution_timeline(
        self, rows: List[Dict], *, group_by: str, hours: int,
        split: Optional[str] = None,
    ) -> List[Dict]:
        """Post-SQL shaping for :meth:`get_fleet_execution_timeline` (ent#326).

        A method (not a bare function) so it reaches the router through the
        same `DatabaseManager` facade as the query it shapes, rather than the
        router importing a private db symbol directly.
        """
        return _shape_execution_timeline(
            rows, group_by=group_by, hours=hours, split=split
        )


def _day_bucket_key(value) -> str:
    """Normalize a SQL day bucket to the `YYYY-MM-DD` text the axis is keyed by.

    `get_agent_token_stats` joins SQL buckets to a Python-generated
    `strftime("%Y-%m-%d")` axis, and a dict lookup between a `str` and a
    `datetime.date` does not raise — it MISSES, and a missed bucket is
    indistinguishable from a day with no executions. That is exactly how the
    header sparkline went flat on PostgreSQL while every scalar beside it
    stayed right.

    So the join key is normalized rather than trusted. `date`/`datetime`
    stringify to an ISO prefix, so one slice covers text and both temporal
    types; anything else is coerced first so a surprising type degrades to a
    non-matching STRING (a real gap) instead of an unhashable or a type-based
    silent miss.

    Scope, stated precisely: this closes the type mismatch, NOT every possible
    key hazard. The call site builds a dict comprehension, so if a backend ever
    returned SUB-DAY buckets they would collide here and the last row would win
    rather than being summed. That cannot happen against the `substr(..., 1, 10)`
    above — one bucket per day by construction — which is why the call site is
    a comprehension and not an accumulator; a different bucket expression would
    have to revisit this.
    """
    return str(value)[:10]


def _empty_bucket(key: str) -> Dict:
    return {"bucket": key, "total": 0, "success": 0, "failed": 0,
            "cost": 0.0, "context_used": 0}


def _accumulate(into: Dict, row: Dict) -> None:
    into["total"] += row["total"]
    into["success"] += row["success"]
    into["failed"] += row["failed"]
    into["cost"] = round(into["cost"] + row["cost"], 6)
    into["context_used"] += row["context_used"]


def _fold_trigger_buckets(rows: List[Dict]) -> List[Dict]:
    """Collapse raw `triggered_by` values into the user-facing buckets.

    Folded HERE rather than in SQL because `_TRIGGER_BUCKETS` is a Python map
    with an explicit `Other` catch-all — the property that makes a newly-added
    trigger type show up as `Other` instead of silently vanishing from a chart.
    Encoding the mapping as a CASE would drop that guarantee the first time
    someone adds a trigger and forgets the SQL.
    """
    folded: Dict = {}
    for row in rows:
        label = _bucket_for_trigger(row["bucket"])
        _accumulate(folded.setdefault(label, _empty_bucket(label)), row)
    # `_BUCKET_ORDER`, not alphabetical: it exists so `Other` sorts LAST and
    # the legend/stack order is stable. Sorting by name puts `Other` sixth,
    # between `MCP` and `Public`, so the #1107 Overview chart and this
    # endpoint's tile would render the same buckets in different orders on the
    # same page. Unknown labels (a bucket added to the map but not the order)
    # follow, alphabetically, rather than being dropped.
    rank = {name: n for n, name in enumerate(_BUCKET_ORDER)}
    return sorted(
        folded.values(),
        key=lambda b: (rank.get(b["bucket"], len(rank)), b["bucket"]),
    )


def _gap_fill(rows: List[Dict], *, group_by: str, hours: int) -> List[Dict]:
    """Emit a continuous UTC axis so a chart shows a real zero, not a skip.

    A missing bucket and a zero bucket mean different things to a reader — "no
    executions that hour" versus "no data" — and a sparse series renders them
    identically (#1107).
    """
    by_key = {r["bucket"]: r for r in rows}
    now = datetime.now(timezone.utc)
    out: List[Dict] = []
    if group_by == "hour":
        start = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        step, fmt = timedelta(hours=1), "%Y-%m-%dT%H"
    else:
        start = (now - timedelta(hours=hours)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        step, fmt = timedelta(days=1), "%Y-%m-%d"
    cursor = start
    while cursor <= now:
        key = cursor.strftime(fmt)
        out.append(by_key.get(key) or _empty_bucket(key))
        cursor += step
    return out


def _fold_split_buckets(rows: List[Dict]) -> List[Dict]:
    """Collapse `(time bucket, raw trigger)` rows into one row per time bucket
    carrying a `by_trigger` breakdown (ent#96).

    The totals are re-summed from the split rows rather than queried twice, so
    the stack can never disagree with the column it sits in — the failure mode
    a second query invites, and the reason AC1 asks for one grouping shared by
    tile, legend and Overview.

    Trigger labels come from `_bucket_for_trigger`, i.e. the SAME `Other`
    catch-all `group_by=trigger` uses. Per label we keep `total` AND `failed`:
    a stacked chart that showed only totals would hide failures inside them,
    which this tile exists not to do.
    """
    folded: Dict = {}
    for row in rows:
        key = row["bucket"]
        target = folded.setdefault(key, _empty_bucket(key))
        target.setdefault("by_trigger", {})
        _accumulate(target, row)
        label = _bucket_for_trigger(row.get("split_key"))
        entry = target["by_trigger"].setdefault(label, {"total": 0, "failed": 0})
        entry["total"] += row["total"]
        entry["failed"] += row["failed"]
    return list(folded.values())


def _shape_execution_timeline(
    rows: List[Dict], *, group_by: str, hours: int, split: Optional[str] = None
) -> List[Dict]:
    """Post-SQL shaping for :meth:`get_fleet_execution_timeline` (ent#326).

    Lives here rather than in the router (Invariant #1): the trigger mapping
    and the continuous UTC axis are domain transforms this package already
    owns for #1107, and a second copy in a router is how the two drift — the
    first draft of this endpoint inlined `_TRIGGER_BUCKETS.get(..., "Other")`
    with a literal fallback and sorted alphabetically, which put `Other` in
    the middle of a chart the Overview page renders `Other`-last.
    """
    if split == "trigger":
        # Fold the second dimension away FIRST, then gap-fill: the axis has to
        # be built from one row per interval, and a gap-filled empty bucket
        # carries an empty breakdown rather than a missing key, so a chart never
        # has to distinguish "no runs" from "no field".
        rows = _fold_split_buckets(rows)
    if group_by == "trigger":
        return _fold_trigger_buckets(rows)
    if group_by in ("hour", "day"):
        filled = _gap_fill(rows, group_by=group_by, hours=hours)
        if split == "trigger":
            for row in filled:
                row.setdefault("by_trigger", {})
        return filled
    return rows
