"""Evaluation scorecard writer (trinity-enterprise#937, checkpoint C4).

The platform's own daily `eval.*` points, written under the reserved owner
`platform:eval` (`:` is not legal in an agent name, and `record_metrics`
refuses the `eval.` prefix for every principal — `eval_namespace_reserved`),
so a scorecard can only ever come from here.

One series per agent (`dims={"agent": a}`), one per delegation pair
(`dims={"caller": c, "callee": a}`) and one for the whole fleet
(`dims={"scope": "fleet"}`, ent#938 — never `{}`, which the dims reader
cannot select), one point per UTC day the runs
COMPLETED in (the read layer's bucket). Each point is the fold of that day's
Tier-0 rows — the five counts (`evaluated`, `completed`, `graded`, `passed`,
`judged`) beside the rates, so a multi-day window sums counts and rebuilds
every rate exactly (T4 + the 08:50Z ruling). A rate with no denominator is not
written: absence, not a zero that reads as "all failed".
A pair series also carries the edge checks (`eval.pair_graded`,
`eval.pair_passed`, `eval.pair_checks_passed_rate`, ent#940) from the fold's
`pair` block; an agent or fleet series never does (TD-6).

Two writers, one result. `on_tier0_row` restates the day of the run just
graded (called by `tier0_evaluator` after its write); `nightly_pass` restates
the last `NIGHTLY_DAYS` days for every agent, which also catches a late
Tier-1 verdict or a human rating. Only the nightly pass's UNSCOPED read writes
the fleet point: the per-row restate and the over-cap per-agent retry each see
one agent's rows, and a fleet point folded from those would be a partial
number stored as a whole one. So the fleet series is complete through
yesterday and has a gap on a day whose fleet window was over the cap. Both
compute from the same rows, and a
point's identity is `(metric, day, dims)`, so a second write is a correction
(ent#729 `corrected`), never a duplicate.

Fence: takes an execution id or nothing, imports nothing from FastAPI or the
auth layer, and never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from database import db
from services.evaluation_read_service import (
    EVAL_METRICS,
    EVAL_PAIR_METRICS,
    FLEET_DIMS,
    PLATFORM_EVAL_OWNER,
    fold,
)
from services.metric_points_service import point_identity
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

NIGHTLY_DAYS = 3
# The first sweep tick at or after this UTC time runs the nightly restate.
NIGHTLY_AFTER = (0, 10)

# fold() key → stored metric. The judge-score rates are written like the rest;
# the trend route is what keeps them from a machine reader.
_FIELDS = {
    "eval.evaluated": "evaluated",
    "eval.completed": "completed",
    "eval.graded": "graded",
    "eval.passed": "passed",
    "eval.judged": "judged",
    "eval.completion_rate": "completion_rate",
    "eval.checks_passed_rate": "checks_passed_rate",
    "eval.quality_mean": "quality_mean",
    "eval.judge_human_agreement": "judge_human_agreement",
    # ent#940 — from the fold's `pair` block, on the pair series only.
    "eval.pair_graded": "pair_graded",
    "eval.pair_passed": "pair_passed",
    "eval.pair_checks_passed_rate": "pair_checks_passed_rate",
}
assert set(_FIELDS) == set(EVAL_METRICS), "every scorecard metric has one writer field"


def day_bounds(day: datetime) -> tuple[str, str]:
    """`[00:00, next 00:00)` UTC of the day `day` falls in, as ISO-Z strings."""
    start = day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc_iso(start), to_utc_iso(start + timedelta(days=1))


def _series(rows: List[dict], *, fleet: bool) -> Dict[tuple, List[dict]]:
    """Rows grouped by series key: `("agent", a)`, `("pair", caller, callee)`
    and, when `fleet`, `("fleet",)` over every row."""
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        agent = r.get("agent_name")
        if not agent:
            continue
        if fleet:
            groups.setdefault(("fleet",), []).append(r)
        groups.setdefault(("agent", agent), []).append(r)
        if r.get("caller_agent"):
            groups.setdefault(("pair", r["caller_agent"], agent), []).append(r)
    return groups


def _dims(key: tuple) -> Dict[str, str]:
    if key[0] == "fleet":
        return dict(FLEET_DIMS)
    return {"agent": key[1]} if key[0] == "agent" else {"caller": key[1], "callee": key[2]}


def points_for_day(day_ts: str, rows: List[dict],
                   human_labels: Dict[str, List[float]], *, fleet: bool) -> List[Dict[str, Any]]:
    """The validated-shape point rows for one day's window rows (pure).

    `fleet` is required, not defaulted: only a caller holding the WHOLE day's
    rows may say so."""
    created_at = utc_now_iso()
    out: List[Dict[str, Any]] = []
    for key, group in _series(rows, fleet=fleet).items():
        is_pair = key[0] == "pair"
        folded = fold(group, human_labels, failed_limit=0, pair=is_pair)
        dims = _dims(key)
        for metric, field in _FIELDS.items():
            if metric in EVAL_PAIR_METRICS:
                if not is_pair:
                    continue
                value = folded["pair"].get(field)
            else:
                value = folded.get(field)
            if value is None:
                continue
            out.append({
                "metric": metric,
                "ts": day_ts,
                "idempotency_key": point_identity(metric, day_ts, dims),
                "value_numeric": float(value),
                "value_text": None,
                "dims": dims,
                "execution_id": None,
                "created_at": created_at,
            })
    return out


class _Truncated(Exception):
    def __init__(self):
        super().__init__("window over the row cap")


def _write_day(day: datetime, *, whole_day: bool = False, **scope) -> Optional[Any]:
    since, until = day_bounds(day)
    rows, truncated = db.evaluation_window_rows(since=since, until=until, **scope)
    if truncated:
        # A count over a capped window would be a wrong number stored as a
        # right one: never written. The fleet pass retries per agent.
        raise _Truncated()
    if not rows:
        return None
    labels = db.evaluation_human_labels([r["execution_id"] for r in rows])
    # An unscoped read of a COMPLETED day is the whole fleet's day; any
    # `agents=` scope is not, and nor is today, which is still filling — and
    # nothing restates the fleet until the next nightly (review C1).
    points = points_for_day(since, rows, labels, fleet=whole_day and not scope)
    if not points:
        return None
    return db.insert_metric_points(PLATFORM_EVAL_OWNER, points)


def on_tier0_row(execution_id: str) -> Optional[Any]:
    """Restate the day of one just-graded run for its agent and the pairs it
    is the callee of. Returns the write counts, or None. Never raises."""
    try:
        row = db.get_execution(execution_id)
        if row is None or not row.completed_at:
            return None
        completed = row.completed_at
        if isinstance(completed, str):
            completed = parse_iso_timestamp(completed)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        return _write_day(completed, agents=[row.agent_name])
    except _Truncated:
        logger.warning("[eval-scorecard] %s's day is over the row cap; not written",
                       execution_id)
        return None
    except Exception as e:  # noqa: BLE001 — the grade is written; the scorecard self-heals nightly
        logger.warning("[eval-scorecard] restate failed for %s: %s", execution_id, type(e).__name__)
        return None


def nightly_pass(now: Optional[datetime] = None, *, days: int = NIGHTLY_DAYS) -> Dict[str, int]:
    """Restate the last `days` completed days (yesterday back) and today for
    the whole fleet — the fleet point for completed days only. Idempotent.
    Never raises."""
    now = now or datetime.now(timezone.utc)
    result = {"days": 0, "written": 0, "failed": 0}
    for back in range(days, -1, -1):
        day = now - timedelta(days=back)
        try:
            try:
                written = [_write_day(day, whole_day=back > 0)]
            except _Truncated:
                # The capped page names only the newest agents; the retry list
                # is every agent graded that day.
                since, until = day_bounds(day)
                written = []
                for agent in db.evaluation_window_agents(since=since, until=until):
                    try:
                        written.append(_write_day(day, agents=[agent]))
                    except _Truncated:
                        result["failed"] += 1
                        logger.warning("[eval-scorecard] %s over the row cap; not written", agent)
            result["days"] += 1
            result["written"] += sum(c.recorded + c.corrected for c in written if c is not None)
        except Exception as e:  # noqa: BLE001 — one bad day costs itself only
            result["failed"] += 1
            logger.warning("[eval-scorecard] nightly day -%s failed: %s", back, type(e).__name__)
    return result


def nightly_due(now: datetime, last_day: Optional[str]) -> bool:
    """True at the first tick at or after 00:10 UTC of a day not yet run."""
    now = now.astimezone(timezone.utc)
    return (now.hour, now.minute) >= NIGHTLY_AFTER and last_day != now.strftime("%Y-%m-%d")
