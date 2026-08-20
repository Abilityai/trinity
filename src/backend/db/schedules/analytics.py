"""Deep analytics aggregation (#868 / #1107 / #1115)."""

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from sqlalchemy import select, and_, func, text, case

from ..engine import get_engine
from ..tables import (
    agent_schedules,
    schedule_executions,
)
from models import TaskExecutionStatus
from utils.helpers import iso_cutoff
from ._common import _norm_ts

logger = logging.getLogger("db.schedules")

# Cap on rows fed into percentile compute and tool-call aggregation
# (#868). Counts and the daily timeline always use the unsampled rowset;
# only the percentile / tool-call pool is bounded. Test-only override is
# via `monkeypatch.setattr(db.schedules.analytics, "_PERCENTILE_ROWSET_CAP", N)`.
_PERCENTILE_ROWSET_CAP = 5000


# #1107: user-facing grouping of raw `triggered_by` values for the agent
# Overview "executions by type" chart. Unmapped values fall into "Other"
# (see `_BUCKET_ORDER`) so a new trigger type stays visible instead of
# silently vanishing. Locked by /autoplan taste decision (extend buckets to
# fit real data — `manual` is the dominant real-world value).
_TRIGGER_BUCKETS = {
    "chat": "Chat/Tasks", "manual": "Chat/Tasks", "user": "Chat/Tasks",
    "session": "Chat/Tasks", "self_chat": "Chat/Tasks",
    "mcp": "MCP",
    "telegram": "Channels", "slack": "Channels", "whatsapp": "Channels",
    "public": "Public", "paid": "Public",
    "schedule": "Scheduled", "webhook": "Scheduled",
    "loop": "Loops",  # #1150: first-class bucket so loop bursts don't read as cron load
    "reminder": "Reminders",  # #1296: agent self-direction, not operator cron
    # ent#329: an operator answer waking a parked agent. Bucketed with the
    # queue it came from rather than with cron: it is human-initiated and its
    # spend is driven by how often operators answer, not by a schedule.
    "operator_response": "Operator queue",
    # ent#220: a room turn is an agent woken by an @mention in a shared room.
    # It was unmapped, so every one landed in `Other` — the catch-all that is
    # supposed to mean "a trigger nobody has classified yet", quietly turned
    # into "rooms". A first-class bucket for the same reason Loops and
    # Reminders got one: room traffic is a distinct spend shape (one human
    # message can bill N agent turns) and reading it as unclassified hides that.
    "room": "Rooms",
    "agent": "Agent-to-agent", "fan_out": "Agent-to-agent",
    "self_task": "Agent-to-agent", "a2a": "Agent-to-agent",  # ent#157 inbound A2A tasks
    "voip": "Voice", "voice": "Voice",
}


# Stack / legend order; "Other" last so unmapped triggers are visible.
_BUCKET_ORDER = [
    "Chat/Tasks", "MCP", "Channels", "Public",
    "Scheduled", "Loops", "Reminders", "Rooms", "Operator queue", "Agent-to-agent", "Voice", "Other",
]


_OTHER_BUCKET = "Other"


def _bucket_for_trigger(trigger: Optional[str]) -> str:
    """Map a raw `triggered_by` value to its user-facing bucket (#1107)."""
    return _TRIGGER_BUCKETS.get(trigger or "", _OTHER_BUCKET)


# #1115: max chars for the per-schedule "command" label derived from a
# schedule's message (the Overview/Schedules-tab scorecard headline).
_SCHEDULE_LABEL_MAX = 80


def _schedule_command_label(message: Optional[str]) -> str:
    """Short headline for a schedule's scorecard, derived from its message.

    Uses the first non-empty line (the command/intent, e.g. ``/do-something``),
    collapsed and truncated. Empty when the message is blank — the frontend
    falls back to the schedule name.
    """
    if not message:
        return ""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return (
                stripped[: _SCHEDULE_LABEL_MAX - 1] + "…"
                if len(stripped) > _SCHEDULE_LABEL_MAX
                else stripped
            )
    return ""

class ScheduleAnalyticsMixin:
    """Per-schedule + per-agent analytics aggregation."""

    def get_schedule_analytics(
        self,
        schedule_id: str,
        hours: int,
        agent_name: str,
    ) -> Optional[Dict]:
        """Compute per-schedule analytics over a rolling time window.

        Returns the analytics envelope, or `None` when the schedule
        does not exist, is soft-deleted, or belongs to a different
        agent than `agent_name`. The router maps `None` → 404, which
        is the load-bearing tenant-boundary check — `AuthorizedAgent`
        only validates the URL's agent name, not that the user-supplied
        `schedule_id` belongs to it.

        Counts and timeline buckets use the unsampled rowset;
        percentiles and tool-call distribution are computed over the
        newest `_PERCENTILE_ROWSET_CAP` success rows (`sampled=True`
        when the cap was hit). Bucketing is UTC — documented in the
        route's OpenAPI description.
        """
        schedule = self.get_schedule(schedule_id)
        if not schedule or schedule.agent_name != agent_name:
            return None

        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP

        # Converted to SQLAlchemy Core (#300) so it runs on SQLite + PostgreSQL.
        # The SQL is dialect-portable (substr/COALESCE/GROUP BY-alias/LIMIT all
        # valid on both); only the positional binds became named. `.mappings()`
        # preserves the `row["col"]` access the aggregation below relies on.
        with get_engine().connect() as conn:
            agg_rows = conn.execute(
                text(
                    """
                    SELECT
                        substr(started_at, 1, 10) AS day,
                        status,
                        COUNT(*) AS n,
                        SUM(COALESCE(cost, 0)) AS cost_sum
                    FROM schedule_executions
                    WHERE schedule_id = :sid AND started_at > :cutoff
                    GROUP BY day, status
                    """
                ),
                {"sid": schedule_id, "cutoff": cutoff},
            ).mappings().all()

            # `cap + 1` so we can detect sampling without a separate COUNT.
            detail_rows = conn.execute(
                text(
                    """
                    SELECT duration_ms, tool_calls
                    FROM schedule_executions
                    WHERE schedule_id = :sid
                      AND started_at > :cutoff
                      AND status = 'success'
                      AND duration_ms IS NOT NULL
                    ORDER BY started_at DESC
                    LIMIT :lim
                    """
                ),
                {"sid": schedule_id, "cutoff": cutoff, "lim": cap + 1},
            ).mappings().all()

        counts: Dict[str, int] = defaultdict(int)
        cost_by_status: Dict[str, float] = defaultdict(float)
        for row in agg_rows:
            counts[row["status"]] += int(row["n"] or 0)
            cost_by_status[row["status"]] += float(row["cost_sum"] or 0.0)

        total_executions = sum(counts.values())
        success_count = counts.get(TaskExecutionStatus.SUCCESS, 0)
        failed_count = counts.get(TaskExecutionStatus.FAILED, 0)
        cancelled_count = counts.get(TaskExecutionStatus.CANCELLED, 0)
        success_rate = (
            round(success_count / total_executions, 4) if total_executions else 0.0
        )
        cost_total = round(sum(cost_by_status.values()), 4)

        sampled = len(detail_rows) > cap
        sample_size = cap if sampled else len(detail_rows)
        capped_rows = detail_rows[:cap]
        durations = [int(r["duration_ms"]) for r in capped_rows]

        if len(durations) >= 2:
            # `inclusive` matches "x% of observations were ≤ this value"
            # for small N; the default `exclusive` shifts p99 noticeably.
            cuts = statistics.quantiles(durations, n=100, method="inclusive")
            p50, p95, p99 = int(cuts[49]), int(cuts[94]), int(cuts[98])
        elif len(durations) == 1:
            only = int(durations[0])
            p50 = p95 = p99 = only
        else:
            p50 = p95 = p99 = None

        # Top-5 weighted by total wall time (NOT raw count) — avoids
        # `Read`/`Bash` dominating because they're frequent but cheap.
        tool_duration: Dict[str, int] = defaultdict(int)
        tool_call_total = 0
        for row in capped_rows:
            raw = row["tool_calls"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "get_schedule_analytics: malformed tool_calls JSON "
                    "skipped (schedule_id=%s): %s",
                    schedule_id, exc,
                )
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or entry.get("tool")
                if not name:
                    continue
                tool_call_total += 1
                dur = entry.get("duration_ms")
                if isinstance(dur, (int, float)) and dur > 0:
                    tool_duration[name] += int(dur)
                # Entries without a usable duration count toward
                # total_calls but stay out of the top-5 ranking.

        tool_top5 = [
            {"name": name, "total_duration_ms": dur_total}
            for name, dur_total in sorted(
                tool_duration.items(), key=lambda kv: kv[1], reverse=True
            )[:5]
        ]

        timeline_by_day: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"success": 0, "failed": 0, "cost": 0.0}
        )
        for row in agg_rows:
            day = row["day"]
            if not day:
                continue
            n = int(row["n"] or 0)
            cost_sum = float(row["cost_sum"] or 0.0)
            timeline_by_day[day]["cost"] += cost_sum
            if row["status"] == TaskExecutionStatus.SUCCESS:
                timeline_by_day[day]["success"] += n
            elif row["status"] == TaskExecutionStatus.FAILED:
                timeline_by_day[day]["failed"] += n

        # Gap-fill so the chart x-axis stays continuous for zero-days.
        now_utc = datetime.now(timezone.utc).date()
        start_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
        timeline: List[Dict] = []
        day = start_utc
        while day <= now_utc:
            iso = day.isoformat()
            bucket = timeline_by_day.get(iso, {"success": 0, "failed": 0, "cost": 0.0})
            timeline.append({
                "date": iso,
                "success": int(bucket["success"]),
                "failed": int(bucket["failed"]),
                "cost": round(float(bucket["cost"]), 4),
            })
            day = day + timedelta(days=1)

        return {
            "window_hours": hours,
            "total_executions": total_executions,
            "success_count": success_count,
            "failed_count": failed_count,
            "cancelled_count": cancelled_count,
            "success_rate": success_rate,
            "duration_ms": {"p50": p50, "p95": p95, "p99": p99},
            "cost": {"total": cost_total},
            "tool_calls": {"top": tool_top5, "total_calls": tool_call_total},
            "timeline": timeline,
            "sampled": sampled,
            "sample_size": sample_size,
        }

    def get_agent_schedules_summary(self, agent_name: str, hours: int) -> Dict:
        """Per-schedule performance rollups for an agent over a window (#1115).

        ONE row per non-deleted schedule (zero-run schedules included, with
        zeros), so both the Overview "Schedules performance" section and the
        Schedules-tab inline stats render from a single call — no N per-schedule
        round-trips. The #868 deep view stays the drill-in target.

        Per schedule: terminal **success_rate** (success / (success + failed
        [incl. ``error``]); ``None`` when zero terminal runs so the UI shows
        ``—`` not a false 0%), **avg_duration_ms** (NULL-skipping AVG),
        **cost_total**, **context_avg** (NULL-skipping), **tool_call_total**,
        and last-run outcome. Read-only / DB-sourced (renders when stopped).

        Tool-call totals are parsed from the newest ``_PERCENTILE_ROWSET_CAP``
        rows agent-wide (matches the #868 sampling discipline); ``tool_calls_
        sampled`` flags when that cap was hit. Window uses ``iso_cutoff``
        (Invariant #16).
        """
        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP
        FAILED_STATES = (TaskExecutionStatus.FAILED, "error")

        # #300/#1115: ported to SQLAlchemy Core so the summary works on both
        # SQLite and PostgreSQL (the earlier raw-sqlite path NameError'd on the
        # dropped get_db_connection import, and the bare-column-with-MAX last-run
        # trick is SQLite-only — replaced by a portable ROW_NUMBER window below).
        se = schedule_executions.c
        sch = agent_schedules.c
        base_where = and_(se.agent_name == agent_name, se.started_at > cutoff)

        # Schedules (non-deleted) — the authoritative row set so a
        # zero-run schedule still appears.
        q_sched = (
            select(sch.id, sch.name, sch.message, sch.cron_expression, sch.enabled)
            .where(and_(sch.agent_name == agent_name, sch.deleted_at.is_(None)))
            .order_by(sch.created_at.asc())
        )

        # One grouped aggregate for every schedule's executions in window.
        # AVG skips NULLs natively, matching the prior CASE-wrapped AVG.
        q_agg = (
            select(
                se.schedule_id,
                func.count().label("total"),
                func.sum(case((se.status == "success", 1), else_=0)).label("success_count"),
                func.sum(case((se.status.in_(("failed", "error")), 1), else_=0)).label("failed_count"),
                func.sum(case((se.status == "cancelled", 1), else_=0)).label("cancelled_count"),
                func.sum(func.coalesce(se.cost, 0)).label("cost_total"),
                func.avg(se.duration_ms).label("avg_duration_ms"),
                func.avg(se.context_used).label("context_avg"),
            )
            .where(base_where)
            .group_by(se.schedule_id)
        )

        # Last-run outcome per schedule — portable ROW_NUMBER window (replaces
        # SQLite's bare-column-with-MAX, which errors on PostgreSQL) keeping the
        # in-window semantics of the original query.
        _last_rn = func.row_number().over(
            partition_by=se.schedule_id, order_by=se.started_at.desc()
        ).label("rn")
        _last_subq = (
            select(
                se.schedule_id,
                se.started_at.label("last_run_at"),
                se.status.label("last_status"),
                _last_rn,
            )
            .where(base_where)
            .subquery()
        )
        q_last = select(
            _last_subq.c.schedule_id,
            _last_subq.c.last_run_at,
            _last_subq.c.last_status,
        ).where(_last_subq.c.rn == 1)

        # Tool-call totals — bounded JSON parse over newest rows agent-wide
        # (cap + 1 to detect sampling), attributed back per schedule.
        q_tools = (
            select(se.schedule_id, se.tool_calls)
            .where(and_(base_where, se.tool_calls.isnot(None)))
            .order_by(se.started_at.desc())
            .limit(cap + 1)
        )

        with get_engine().connect() as conn:
            schedule_rows = conn.execute(q_sched).mappings().all()
            agg_by_sched = {r["schedule_id"]: r for r in conn.execute(q_agg).mappings().all()}
            last_by_sched = {r["schedule_id"]: r for r in conn.execute(q_last).mappings().all()}
            tool_rows = conn.execute(q_tools).mappings().all()

        tool_calls_sampled = len(tool_rows) > cap
        tool_total_by_sched: Dict[str, int] = defaultdict(int)
        for row in tool_rows[:cap]:
            raw = row["tool_calls"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, list):
                continue
            for entry in parsed:
                if isinstance(entry, dict) and (entry.get("name") or entry.get("tool")):
                    tool_total_by_sched[row["schedule_id"]] += 1

        schedules: List[Dict] = []
        for s in schedule_rows:
            sid = s["id"]
            agg = agg_by_sched.get(sid)
            last = last_by_sched.get(sid)

            if agg:
                total = int(agg["total"] or 0)
                success_count = int(agg["success_count"] or 0)
                failed_count = int(agg["failed_count"] or 0)
                cancelled_count = int(agg["cancelled_count"] or 0)
                cost_total = round(float(agg["cost_total"] or 0.0), 4)
                avg_duration_ms = (
                    int(agg["avg_duration_ms"]) if agg["avg_duration_ms"] is not None else None
                )
                context_avg = (
                    int(agg["context_avg"]) if agg["context_avg"] is not None else None
                )
            else:
                total = success_count = failed_count = cancelled_count = 0
                cost_total = 0.0
                avg_duration_ms = context_avg = None

            terminal = success_count + failed_count
            success_rate = round(success_count / terminal, 4) if terminal else None

            schedules.append({
                "schedule_id": sid,
                "name": s["name"],
                "command": _schedule_command_label(s["message"]),
                "cron_expression": s["cron_expression"],
                "enabled": bool(s["enabled"]),
                "total_executions": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "cancelled_count": cancelled_count,
                "success_rate": success_rate,
                "avg_duration_ms": avg_duration_ms,
                "cost_total": cost_total,
                "context_avg": context_avg,
                "tool_call_total": tool_total_by_sched.get(sid, 0),
                # #1474: normalize to UTC 'Z' — ScheduleSummaryRow.last_run_at is
                # a str field, so a naive scheduler-written value would pass
                # through unshifted and render wrong in a non-UTC browser.
                "last_run_at": _norm_ts(last["last_run_at"]) if last else None,
                "last_run_status": last["last_status"] if last else None,
            })

        return {
            "window_hours": hours,
            "schedule_count": len(schedules),
            "tool_calls_sampled": tool_calls_sampled,
            "schedules": schedules,
        }

    def get_agent_analytics(self, agent_name: str, hours: int) -> Dict:
        """Compute agent-scoped execution analytics over a rolling window (#1107).

        Generalises `get_schedule_analytics` to agent scope with a
        `triggered_by` breakdown grouped into user-facing buckets. Powers
        the Agent Detail "Overview" trend charts.

        Data-source discipline (locked by /autoplan engineering review):
          - Counts, per-day type stacks, per-day success-rate, per-day
            duration AVG, and per-day context AVG come from full-set
            aggregate queries.
          - Headline duration `avg` and `context_avg` also come from the
            full set — NEVER from the capped pool, since an average over a
            sampled subset would be silently wrong on high-traffic agents.
          - Only the headline duration `p95` uses the newest
            `_PERCENTILE_ROWSET_CAP` success rows (`sampled=True` when
            capped).
          - `success_rate` is terminal-based: success / (success + failed),
            where failed = status in ('failed', 'error'). Days with zero
            terminal rows report `success_rate=None` so the chart shows a
            gap, not a false 0%.
          - Bucketing is UTC-day; unmapped triggers → "Other".

        Always returns an envelope (zeros / empty when the agent has no
        executions). Access is gated by `AuthorizedAgent` at the router and
        the window is validated there, so there is no None/404 path here.
        """
        cutoff = iso_cutoff(hours)
        cap = _PERCENTILE_ROWSET_CAP
        FAILED_STATES = (TaskExecutionStatus.FAILED, "error")

        # #300 rebase: ported to SQLAlchemy Core (this #852 analytics method
        # landed on dev after the Step-C bulk conversion). substr/AVG/CASE/
        # COUNT are dialect-agnostic; the p95 is computed in Python below.
        day_col = func.substr(schedule_executions.c.started_at, 1, 10).label("day")
        base_where = and_(
            schedule_executions.c.agent_name == agent_name,
            schedule_executions.c.started_at > cutoff,
        )
        dur_avg_expr = func.avg(
            case((schedule_executions.c.status == "success", schedule_executions.c.duration_ms))
        ).label("dur_avg")
        ctx_avg_expr = func.avg(schedule_executions.c.context_used).label("ctx_avg")

        # Q1: counts + per-day type stacks (full set).
        q1 = (
            select(
                day_col,
                schedule_executions.c.status,
                schedule_executions.c.triggered_by,
                func.count().label("n"),
            )
            .where(base_where)
            .group_by(day_col, schedule_executions.c.status, schedule_executions.c.triggered_by)
        )

        # Q2: per-day duration AVG (success only) + context AVG
        # (NULL-skipped, all statuses). CASE→NULL means AVG skips
        # non-success durations; AVG(context_used) skips unmeasured rows.
        q2 = select(day_col, dur_avg_expr, ctx_avg_expr).where(base_where).group_by(day_col)

        # Q3: overall duration AVG + context AVG (full set, single row).
        q3 = select(dur_avg_expr, ctx_avg_expr).where(base_where)

        # Q4: capped success-duration pool for the headline p95 only.
        # `cap + 1` so we can detect sampling without a second COUNT.
        q4 = (
            select(schedule_executions.c.duration_ms)
            .where(
                and_(
                    schedule_executions.c.agent_name == agent_name,
                    schedule_executions.c.started_at > cutoff,
                    schedule_executions.c.status == "success",
                    schedule_executions.c.duration_ms.isnot(None),
                )
            )
            .order_by(schedule_executions.c.started_at.desc())
            .limit(cap + 1)
        )

        with get_engine().connect() as conn:
            count_rows = conn.execute(q1).mappings().all()
            daily_metric_rows = conn.execute(q2).mappings().all()
            overall = conn.execute(q3).mappings().first()
            dur_rows = conn.execute(q4).mappings().all()

        # --- counts, per-day stacks, per-bucket window totals ---
        success_count = 0
        failed_count = 0
        total_executions = 0
        bucket_totals: Dict[str, int] = defaultdict(int)
        day_counts: Dict[str, Dict] = defaultdict(
            lambda: {
                "total": 0, "success": 0, "failed": 0,
                "by_type": defaultdict(int),
            }
        )
        for row in count_rows:
            day = row["day"]
            row_status = row["status"]
            n = int(row["n"] or 0)
            bucket = _bucket_for_trigger(row["triggered_by"])
            total_executions += n
            bucket_totals[bucket] += n
            if day:
                d = day_counts[day]
                d["total"] += n
                d["by_type"][bucket] += n
                if row_status == TaskExecutionStatus.SUCCESS:
                    d["success"] += n
                elif row_status in FAILED_STATES:
                    d["failed"] += n
            if row_status == TaskExecutionStatus.SUCCESS:
                success_count += n
            elif row_status in FAILED_STATES:
                failed_count += n

        terminal_total = success_count + failed_count
        success_rate = (
            round(success_count / terminal_total, 4) if terminal_total else 0.0
        )

        # --- headline p95 (capped pool) ---
        sampled = len(dur_rows) > cap
        sample_size = cap if sampled else len(dur_rows)
        durations = [int(r["duration_ms"]) for r in dur_rows[:cap]]
        if len(durations) >= 2:
            cuts = statistics.quantiles(durations, n=100, method="inclusive")
            p95 = int(cuts[94])
        elif len(durations) == 1:
            p95 = int(durations[0])
        else:
            p95 = None

        # --- headline avg duration + context (full set, never sampled) ---
        dur_avg = (
            int(overall["dur_avg"])
            if overall and overall["dur_avg"] is not None else None
        )
        ctx_avg = (
            int(overall["ctx_avg"])
            if overall and overall["ctx_avg"] is not None else None
        )

        # --- per-day duration / context AVG lookup ---
        daily_metrics: Dict[str, Dict] = {}
        for row in daily_metric_rows:
            daily_metrics[row["day"]] = {
                "duration_avg_ms": (
                    int(row["dur_avg"]) if row["dur_avg"] is not None else None
                ),
                "context_avg": (
                    int(row["ctx_avg"]) if row["ctx_avg"] is not None else None
                ),
            }

        # --- gap-filled timeline (continuous UTC-day x-axis) ---
        now_utc = datetime.now(timezone.utc).date()
        start_utc = (datetime.now(timezone.utc) - timedelta(hours=hours)).date()
        timeline: List[Dict] = []
        day = start_utc
        while day <= now_utc:
            iso = day.isoformat()
            c = day_counts.get(iso)
            m = daily_metrics.get(iso, {})
            if c:
                day_terminal = c["success"] + c["failed"]
                day_sr = (
                    round(c["success"] / day_terminal, 4)
                    if day_terminal else None
                )
                by_type = {
                    b: c["by_type"][b]
                    for b in _BUCKET_ORDER if c["by_type"].get(b)
                }
                timeline.append({
                    "date": iso,
                    "total": c["total"],
                    "success": c["success"],
                    "failed": c["failed"],
                    "success_rate": day_sr,
                    "duration_avg_ms": m.get("duration_avg_ms"),
                    "context_avg": m.get("context_avg"),
                    "by_type": by_type,
                })
            else:
                timeline.append({
                    "date": iso, "total": 0, "success": 0, "failed": 0,
                    "success_rate": None, "duration_avg_ms": None,
                    "context_avg": None, "by_type": {},
                })
            day = day + timedelta(days=1)

        by_type_totals = [
            {"bucket": b, "total": bucket_totals[b]}
            for b in _BUCKET_ORDER if bucket_totals.get(b)
        ]
        buckets_present = [b for b in _BUCKET_ORDER if bucket_totals.get(b)]

        return {
            "window_hours": hours,
            "total_executions": total_executions,
            "success_count": success_count,
            "failed_count": failed_count,
            "success_rate": success_rate,
            "duration_ms": {"avg": dur_avg, "p95": p95},
            "context_avg": ctx_avg,
            "by_type": by_type_totals,
            "buckets": buckets_present,
            "timeline": timeline,
            "sampled": sampled,
            "sample_size": sample_size,
        }

    def get_all_agents_schedule_counts(self) -> Dict[str, Dict[str, int]]:
        """Get schedule counts (total and enabled) for all agents.

        Returns:
            Dict mapping agent_name to {"total": X, "enabled": Y}
        """
        with get_engine().connect() as conn:
            rows = conn.execute(
                text("""
                SELECT
                    agent_name,
                    COUNT(*) as total,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) as enabled
                FROM agent_schedules
                WHERE deleted_at IS NULL
                GROUP BY agent_name
                """)
            ).mappings().all()

            results = {}
            for row in rows:
                results[row["agent_name"]] = {
                    "total": row["total"],
                    "enabled": row["enabled"]
                }

            return results
