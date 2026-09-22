"""Recorded metrics — the read composition (trinity-enterprise#479).

The ONE place the platform answers "what is this agent's number, and is it
still true?". `GET /api/agents/{name}/metrics`, the MCP `get_metrics` tool,
the `get_agent_health` block, the declared-metric tiles and the
`dashboard.yaml` `metric:` binding all come through here, so there is exactly
one stale rule and exactly one series shape to bind to.

## The one stale rule

    stale  ⟺  cadence declared  AND  now − last_point_at > 2 × cadence

`freshness()` is a pure function at module top with **no database import at
call time** — `services/client_portal/role_card.py` (#2927) and the ent#666
objective join import it without pulling the store in, which is what keeps
"stale" from being re-derived a fourth time with a fourth threshold. Three
answers are deliberately NOT `stale = True`:

* **no cadence declared** → `stale: None`, `freshness: "no_cadence"`. A metric
  whose author never promised a rhythm cannot be late.
* **no points at all** → `stale: False`, `freshness: "no_points"`. "Never
  measured" is an empty state with its own copy, not a broken measurement.
* **a point in the future** (the write path allows ≤ 300 s of clock skew) →
  the age clamps at 0, so skew reads as fresh, never as a negative age.

The comparison is strict `>`: a point at exactly 2 × cadence is not yet stale
(issue AC, pinned by a boundary test).

## Store-only — it answers on a stopped agent

Nothing here performs container I/O. That is the whole point of re-backing the
route with `metric_points`: the legacy proxy this replaces had to find a
RUNNING container before it could say anything, so every number vanished the
moment an agent was stopped. The `metrics.json` that proxy used to read is now
a compatibility finding (D-010), echoed from the persisted compat row — never
a container probe on a polled read path.

## Two entry points, one fold

`read_agent_metrics` is the route's full composition (latest + series + chart +
stats). `latest_by_metric` is the cheap half — `{name: tile}` with no series
work — for a consumer that needs the current number and its freshness and
nothing else (the ent#666 objective join). Both go through `_latest_entry`, so
"the objective's actual" and "the number on the tile" are the same computation,
not two that happen to agree.

## Series identity is `canonical_dims`, computed here

`dims` is stored in caller key order (the engine serialiser sorts nothing), so
a dimensioned metric cannot be partitioned by dimension in SQL. The store
returns the newest rows; this module groups them by
`metric_points_service.canonical_dims` into `series: [{dims, points, latest}]`
and folds the per-series latest into ONE tile value using the registry's
declared `aggregation` (`sum` / `avg` / `last`) — the ent#477 field that
existed for exactly this and that nothing read until now.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.metric_points_service import canonical_dims
from utils.helpers import parse_iso_timestamp, to_utc_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract constants — the bounds the response STATES rather than applies
# silently (`series_truncated`, `series_count`, the bucket count in §49).
# ---------------------------------------------------------------------------

#: Window names the route accepts. `auto` is cadence-aware (see `resolve_window`).
WINDOW_CHOICES: Tuple[str, ...] = ("auto", "24h", "7d", "30d", "90d")

_WINDOW_HOURS: Dict[str, int] = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30,
                                 "90d": 24 * 90}

#: How deep the latest-per-metric seek goes. A dimension whose series has not
#: reported inside this many points is treated as gone (documented in §49).
LATEST_POINTS_PER_METRIC = 200

#: Buckets in the default (all-metrics) read. A fixed count keeps the payload
#: bounded whatever the window is: 50 metrics × 120 buckets, not 50 × 43 200.
SERIES_BUCKETS = 120

#: Raw points are served only on the single-metric path, newest-first-capped.
DEFAULT_SERIES_LIMIT = 500
MAX_SERIES_LIMIT = 2000

#: Per-dimension series listed on a tile before the rest are summarised away.
MAX_SERIES_PER_METRIC = 50

#: The `auto` window is at least a day and at most a quarter.
_AUTO_MIN_HOURS = 24
_AUTO_MAX_HOURS = 24 * 90
_AUTO_CADENCE_MULTIPLE = 12

STALE_RULE = "2x cadence"

_NUMERIC_TYPES = frozenset({"counter", "gauge", "percentage", "duration",
                            "bytes"})


class MetricReadError(Exception):
    """A named refusal the route maps to a status code.

    `reason` is the machine code (`window_invalid`, `metric_undeclared`) and
    `message` is the operator sentence; both travel to the MCP tool, so the
    agent is told what to change rather than handed a 500 (Bar 6).
    """

    def __init__(self, reason: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# The one stale rule (pure — no db import at call time)
# ---------------------------------------------------------------------------


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return parse_iso_timestamp(str(value))
    except (TypeError, ValueError):
        return None


def freshness(
    cadence_seconds: Optional[int],
    last_point_at: Optional[Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The platform's single definition of "is this number still true?".

    Returns `{stale, freshness, stale_after, age_seconds}` where `freshness`
    is one of `fresh` / `stale` / `no_cadence` / `no_points`. `stale` is
    `None` — not `False` — when no cadence is declared, because "cannot be
    late" and "is not late" are different answers and a tile renders them with
    different copy.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    last = _as_datetime(last_point_at)
    if last is None:
        return {"stale": False, "freshness": "no_points",
                "stale_after": None, "age_seconds": None}

    # Clock skew: the write path accepts a point up to 300 s in the future, so
    # a negative age is legal input. Clamping at 0 makes it read as fresh
    # rather than as an enormous positive age after an unsigned subtraction.
    age = max((now - last).total_seconds(), 0.0)

    try:
        cadence = int(cadence_seconds) if cadence_seconds else 0
    except (TypeError, ValueError):
        cadence = 0
    if cadence <= 0:
        return {"stale": None, "freshness": "no_cadence",
                "stale_after": None, "age_seconds": age}

    deadline = last + timedelta(seconds=2 * cadence)
    is_stale = age > 2 * cadence  # strict: exactly 2x is not yet stale
    return {
        "stale": is_stale,
        "freshness": "stale" if is_stale else "fresh",
        "stale_after": to_utc_iso(deadline),
        "age_seconds": age,
    }


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------


def resolve_window(
    window: Optional[str],
    since: Optional[str] = None,
    until: Optional[str] = None,
    *,
    definitions: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    retention_days: Optional[int] = None,
) -> Dict[str, Any]:
    """`{kind, since, until, hours}` — the span this read covers.

    An explicit `since` (optionally with `until`) overrides the enum, because
    a weekly metric in a `24h` window is four points short of a chart and
    ent#666 needs month-to-date. The span is bounded by retention: asking for
    more history than the sweep keeps is a named 422, not a silent empty
    answer.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if since:
        start = _as_datetime(since)
        if start is None:
            raise MetricReadError(
                "window_invalid",
                "since must be an ISO-8601 timestamp (e.g. 2026-09-01T00:00:00Z)",
            )
        end = _as_datetime(until) if until else now
        if until and end is None:
            raise MetricReadError(
                "window_invalid",
                "until must be an ISO-8601 timestamp (e.g. 2026-09-08T00:00:00Z)",
            )
        if end <= start:
            raise MetricReadError(
                "window_invalid", "until must be after since")
        if retention_days and retention_days > 0:
            span_days = (end - start).total_seconds() / 86400.0
            if span_days > retention_days:
                raise MetricReadError(
                    "window_invalid",
                    f"window spans {span_days:.0f} days but only "
                    f"{retention_days} days of points are retained",
                )
        return {"kind": "custom", "since": to_utc_iso(start),
                "until": to_utc_iso(end),
                "hours": (end - start).total_seconds() / 3600.0}

    name = (window or "auto").strip().lower()
    if name not in WINDOW_CHOICES:
        raise MetricReadError(
            "window_invalid",
            "window must be one of " + ", ".join(WINDOW_CHOICES)
            + " (or pass since/until)",
        )

    if name == "auto":
        cadences = [
            int(d.get("cadence_seconds") or 0)
            for d in (definitions or [])
            if d.get("cadence_seconds")
        ]
        hours = _AUTO_MIN_HOURS
        if cadences:
            widest = max(cadences) * _AUTO_CADENCE_MULTIPLE / 3600.0
            hours = min(max(_AUTO_MIN_HOURS, widest), _AUTO_MAX_HOURS)
    else:
        hours = _WINDOW_HOURS[name]

    start = now - timedelta(hours=hours)
    return {"kind": name, "since": to_utc_iso(start), "until": to_utc_iso(now),
            "hours": hours}


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


def _row_value(row: Dict[str, Any]) -> Any:
    """The point's value — numeric when the column holds one, else the label."""
    numeric = row.get("value_numeric")
    if numeric is not None:
        return numeric
    return row.get("value_text")


def _row_dims(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    dims = row.get("dims")
    if isinstance(dims, str):
        try:
            dims = json.loads(dims)
        except (TypeError, ValueError):
            return None
    return dims or None


def fold(values: List[Any], aggregation: Optional[str]) -> Any:
    """Fold one value per dimension series into the ONE number a tile shows.

    `last` (and anything unknown, and every non-numeric value) means the
    newest observation wins; `sum` and `avg` are the registry's other two
    declared aggregations. A metric that stopped reporting one region keeps
    contributing its last value to `sum` — and its `stale` flag is what says
    so (TD-2): dropping it silently would make a total fall without an event.
    """
    if not values:
        return None
    numeric = [v for v in values if isinstance(v, (int, float))
               and not isinstance(v, bool)]
    mode = (aggregation or "last").lower()
    if mode == "sum" and numeric:
        return sum(numeric)
    if mode == "avg" and numeric:
        return sum(numeric) / len(numeric)
    return values[0]


def _bucket(points: List[Dict[str, Any]], window: Dict[str, Any],
            aggregation: Optional[str]) -> List[Dict[str, Any]]:
    """Downsample a series to ≤ `SERIES_BUCKETS` evenly spaced buckets.

    Bucketed rather than thinned: dropping every k-th point loses a spike,
    folding a bucket with the declared aggregation keeps it. Buckets with no
    point are omitted (a gap is data — `SparklineChart` draws what it is
    given, and inventing a zero would draw a cliff that never happened).

    **A point outside `[since, until]` is DROPPED, never clamped.** The default
    path hands this function the newest `LATEST_POINTS_PER_METRIC` points for
    the metric, which is a "newest N" slice and not a window: a clamp
    (`max(index, 0)`) folded every pre-window point into bucket 0, so a metric
    with ten days of history read over 24 h opened with a fabricated spike —
    the sum of points the window excludes, stamped at a `ts` BEFORE `since` —
    and `stats` (min/max/trend) was computed from it. Filtering is the window;
    the `SERIES_BUCKETS - 1` clamp survives only to put `ts == until` in the
    last bucket rather than one past the end.

    Each bucket carries its index `i` as well as `{ts, value}`: the index is
    what makes "the same moment in two dimension series" well defined, which
    is what `_fold_across_series` folds by (a bucket with no point is omitted,
    so position in the list is not the same thing).
    """
    if not points:
        return []
    start = _as_datetime(window["since"])
    end = _as_datetime(window["until"])
    if start is None or end is None or end <= start:
        return [{"i": i, "ts": p["ts"], "value": p["value"]}
                for i, p in enumerate(points[-SERIES_BUCKETS:])]
    span = (end - start).total_seconds()
    width = max(span / SERIES_BUCKETS, 1e-6)

    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for point in points:
        ts = _as_datetime(point["ts"])
        if ts is None or ts < start or ts > end:
            continue
        index = min(int((ts - start).total_seconds() / width), SERIES_BUCKETS - 1)
        grouped.setdefault(index, []).append(point)

    buckets = []
    for index in sorted(grouped):
        members = grouped[index]
        values = [m["value"] for m in members]
        # `members` is oldest-first, so `last` must read the END of the bucket.
        folded = fold(list(reversed(values)), aggregation)
        buckets.append({"i": index, "ts": members[-1]["ts"], "value": folded})
    return buckets


#: Aggregations for which folding ACROSS dimension series is defined. `sum` and
#: `avg` answer "what is the total / the mean at this moment" for any set of
#: series; `last` does not — the last value of two regions is not one number,
#: which is why `fold` resolves it to the newest series and why a `last` chart
#: says which series it is drawing instead of inventing a fold.
_FOLDABLE_ACROSS_SERIES = frozenset({"sum", "avg"})


def _fold_across_series(
    series: List[Dict[str, Any]], aggregation: Optional[str]
) -> List[Dict[str, Any]]:
    """One bucket list describing EVERY series, folded by bucket index.

    A bucket holds only the series that reported in it — the same rule the
    per-bucket fold already uses within one series, and the honest one: a
    region with no observation in a five-minute bucket has no value to add.
    """
    by_index: Dict[int, List[Dict[str, Any]]] = {}
    for entry in series:
        for bucket in entry.get("buckets") or []:
            by_index.setdefault(bucket.get("i", 0), []).append(bucket)

    folded = []
    for index in sorted(by_index):
        members = by_index[index]
        folded.append({
            "i": index,
            "ts": max(m["ts"] for m in members),
            "value": fold([m["value"] for m in members], aggregation),
        })
    return folded


def _chart(
    series: List[Dict[str, Any]],
    aggregation: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The ONE bucket list the sparkline draws and `stats` is computed from.

    The chart and the number above it must describe the same thing. `latest`
    is the fold across every dimension series, so for a foldable aggregation
    the chart is the fold too; for `last` a cross-series fold has no meaning,
    so the chart is the newest series and `basis: "series"` + `dims` SAY so —
    the tile labels it rather than letting a total's trend arrow be drawn from
    one region's history.
    """
    if not series:
        return None
    mode = (aggregation or "last").lower()
    if mode in _FOLDABLE_ACROSS_SERIES and len(series) > 1:
        return {
            "basis": "folded",
            "aggregation": mode,
            "series_count": len(series),
            "dims": None,
            "buckets": _fold_across_series(series, mode),
        }
    primary = series[0]
    return {
        "basis": "series",
        "aggregation": mode,
        "series_count": len(series),
        "dims": primary.get("dims"),
        "buckets": list(primary.get("buckets") or []),
    }


def _group_series(
    rows: List[Dict[str, Any]],
    aggregation: Optional[str],
) -> List[Dict[str, Any]]:
    """Group newest-first rows into per-dimension series, newest series first.

    Identity is `canonical_dims` — the same serialisation the write path hashes
    into the row key, so a series here is exactly a series there.
    """
    series: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        dims = _row_dims(row)
        key = canonical_dims(dims)
        entry = series.get(key)
        point = {"ts": row["ts"], "value": _row_value(row)}
        if entry is None:
            series[key] = {
                "dims": dims,
                "latest": {"value": point["value"], "ts": point["ts"],
                           "dims": dims},
                "points": [point],
            }
        else:
            entry["points"].append(point)
    ordered = sorted(
        series.values(),
        key=lambda s: s["latest"]["ts"],
        reverse=True,
    )
    for entry in ordered:
        entry["points"].reverse()  # oldest-first for charts
    return ordered


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _numeric(definition: Dict[str, Any]) -> bool:
    return (definition.get("type") or "") in _NUMERIC_TYPES


#: The copy a declared-but-never-recorded metric carries. It names BOTH
#: actions because this read cannot tell which the agent has: the playbook
#: catalog is a container probe (`GET /api/agents/{name}/playbooks`, 503 on a
#: stopped agent) and this route is deliberately store-only, while the
#: persisted `agent_skills` rows know only LIBRARY assignments — every bundled
#: template carries `/update-dashboard` in `.claude/commands/` instead, so a
#: conditional built on that table would tell exactly those agents they do not
#: have the playbook they ship with. A `has_playbook` flag no caller could
#: compute made the second half of this sentence unreachable dead copy, which
#: is worse than naming one action too many.
EMPTY_METRIC_MESSAGE = (
    "declared, no points yet — record points with `record_metrics` "
    "(or schedule `/update-dashboard` if the agent has that playbook)"
)


def _empty_message(definition: Dict[str, Any]) -> str:
    return EMPTY_METRIC_MESSAGE


def _definition_fields(definition: Dict[str, Any]) -> Dict[str, Any]:
    """The registry fields a consumer needs to RENDER a metric, spelled once."""
    return {
        "name": definition.get("name"),
        "type": definition.get("type"),
        "label": definition.get("label") or definition.get("name"),
        "description": definition.get("description"),
        "unit": definition.get("unit"),
        "direction": definition.get("direction"),
        "aggregation": definition.get("aggregation"),
        "cadence": definition.get("cadence"),
        "cadence_seconds": definition.get("cadence_seconds"),
        "warning_threshold": definition.get("warning_threshold"),
        "critical_threshold": definition.get("critical_threshold"),
        "values": definition.get("values"),
        "dimensions": definition.get("dimensions") or [],
        "status": definition.get("status"),
        "retired_at": definition.get("retired_at"),
        "type_conflict": definition.get("type_conflict"),
    }


def read_agent_metrics(
    agent_name: str,
    *,
    window: Optional[str] = "auto",
    since: Optional[str] = None,
    until: Optional[str] = None,
    metric: Optional[str] = None,
    include_retired: bool = False,
    series_limit: int = DEFAULT_SERIES_LIMIT,
    now: Optional[datetime] = None,
    findings: Optional[List[Dict[str, Any]]] = None,
    findings_evaluated_at: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
    retention_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Compose the agent's declared metrics with their recorded points.

    Store-only: definitions, the newest points per metric, and (for the
    single-metric path) the raw window. No container is contacted, so a
    stopped agent answers exactly like a running one.
    """
    from database import db  # deferred: `freshness` must import without it

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    all_definitions = db.list_metric_definitions(agent_name, include_retired=True)
    active = [d for d in all_definitions if d.get("status") == "active"]
    visible = all_definitions if include_retired else active

    if metric:
        match = next((d for d in visible if d.get("name") == metric), None)
        if match is None:
            retired = next(
                (d for d in all_definitions
                 if d.get("name") == metric
                 and d.get("status") != "active"),
                None,
            )
            if retired is not None:
                raise MetricReadError(
                    "metric_undeclared",
                    f"metric '{metric}' was retired at "
                    f"{retired.get('retired_at')} — pass include_retired=true "
                    "to read it",
                    retired_at=retired.get("retired_at"),
                )
            raise MetricReadError(
                "metric_undeclared",
                f"metric '{metric}' is not declared in template.yaml — declare "
                "it and POST .../metrics/definitions/refresh",
            )
        visible = [match]

    resolved = resolve_window(
        window, since, until,
        definitions=visible, now=now, retention_days=retention_days,
    )

    names = [d["name"] for d in visible if d.get("name")]
    rows_by_metric: Dict[str, List[Dict[str, Any]]] = {n: [] for n in names}
    for row in db.latest_metric_points(
            agent_name, names, LATEST_POINTS_PER_METRIC):
        rows_by_metric.setdefault(row["metric"], []).append(row)

    metrics: List[Dict[str, Any]] = []
    for definition in visible:
        metrics.append(_compose_metric(
            agent_name, definition, rows_by_metric.get(definition["name"], []),
            resolved, now,
            raw=bool(metric), series_limit=series_limit, db=db,
        ))

    return {
        "agent_name": agent_name,
        "declared": bool(active),
        "window": resolved,
        "generated_at": to_utc_iso(now),
        "metrics": metrics,
        "findings": findings or [],
        "findings_evaluated_at": findings_evaluated_at,
        "policy": policy,
        "stale_rule": STALE_RULE,
        "message": (
            None if active
            else "no metrics: block in template.yaml — declare one and pull, "
                 "restart the agent, or POST .../metrics/definitions/refresh"
        ),
    }


def _latest_entry(
    definition: Dict[str, Any],
    latest_rows: List[Dict[str, Any]],
    now: datetime,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Definition fields + the folded latest + freshness. Spelled ONCE.

    This is the tile: the newest point of every dimension series, folded by the
    declared `aggregation` into one number, stamped with the freshest `ts`
    across those series and judged by the one stale rule.

    `_compose_metric` (the route) and `latest_by_metric` (the ent#666 objective
    join) both call it, which is what makes "the objective's actual" and "the
    number on the tile" the same fact rather than two computations that happen
    to agree today. The grouped series are returned alongside because the route
    goes on to bucket them; the join never looks at them.
    """
    aggregation = definition.get("aggregation")
    grouped = _group_series(latest_rows, aggregation)
    entry = _definition_fields(definition)

    last_point_at = grouped[0]["latest"]["ts"] if grouped else None
    # The freshest point across EVERY dimension series, not the first group's:
    # one region reporting keeps the metric fresh, and the per-series `stale`
    # flags say which stopped.
    for series in grouped:
        if series["latest"]["ts"] > last_point_at:
            last_point_at = series["latest"]["ts"]

    fresh = freshness(definition.get("cadence_seconds"), last_point_at, now)
    entry.update({
        "last_point_at": last_point_at,
        "stale": fresh["stale"],
        "freshness": fresh["freshness"],
        "stale_after": fresh["stale_after"],
        "series_count": len(grouped),
    })

    if grouped:
        folded = fold([s["latest"]["value"] for s in grouped], aggregation)
        newest = max(grouped, key=lambda s: s["latest"]["ts"])
        entry["latest"] = {
            "value": folded,
            "ts": last_point_at,
            "dims": newest["dims"] if len(grouped) == 1 else None,
        }
    else:
        entry["latest"] = None
    return entry, grouped


def latest_by_metric(
    agent_name: str,
    *,
    definitions: Optional[List[Dict[str, Any]]] = None,
    names: Optional[List[str]] = None,
    now: Optional[datetime] = None,
    include_retired: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """`{metric_name: <the tile>}` — no series, no buckets, no stats.

    The cheap half of `read_agent_metrics` for a consumer that needs the
    current number and its freshness and nothing else: the ent#666 objective
    join asks "what is the actual against this target", not "draw me a chart".
    Running the full read and discarding the series would cost a per-metric
    window query and a 120-bucket fold per metric on every role-card poll.

    `definitions` lets a caller that already holds the registry rows pass them
    in rather than paying a second `list_metric_definitions`. `names` narrows
    both the registry and the point read; passing `[]` performs NO store read
    at all, which is the zero-config path.
    """
    from database import db  # deferred: `freshness` must import without it

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if names is not None and not names:
        return {}

    if definitions is None:
        definitions = db.list_metric_definitions(
            agent_name, include_retired=include_retired)
    if not include_retired:
        definitions = [d for d in definitions if d.get("status") == "active"]
    if names is not None:
        wanted = set(names)
        definitions = [d for d in definitions if d.get("name") in wanted]
    if not definitions:
        return {}

    metric_names = [d["name"] for d in definitions if d.get("name")]
    rows_by_metric: Dict[str, List[Dict[str, Any]]] = {
        n: [] for n in metric_names}
    for row in db.latest_metric_points(
            agent_name, metric_names, LATEST_POINTS_PER_METRIC):
        rows_by_metric.setdefault(row["metric"], []).append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for definition in definitions:
        entry, _grouped = _latest_entry(
            definition, rows_by_metric.get(definition["name"], []), now)
        out[definition["name"]] = entry
    return out


def _compose_metric(
    agent_name: str,
    definition: Dict[str, Any],
    latest_rows: List[Dict[str, Any]],
    window: Dict[str, Any],
    now: datetime,
    *,
    raw: bool,
    series_limit: int,
    db: Any,
) -> Dict[str, Any]:
    """One metric's entry: definition + latest + freshness + series + stats."""
    aggregation = definition.get("aggregation")
    entry, grouped = _latest_entry(definition, latest_rows, now)

    if grouped:
        entry["latest_by_series"] = [
            {"dims": s["dims"], "value": s["latest"]["value"],
             "ts": s["latest"]["ts"],
             **{k: v for k, v in freshness(
                 definition.get("cadence_seconds"), s["latest"]["ts"], now
             ).items() if k in ("stale", "freshness")}}
            for s in grouped[:MAX_SERIES_PER_METRIC]
        ]
        entry["message"] = None
    else:
        entry["latest_by_series"] = []
        entry["message"] = _empty_message(definition)

    entry["series"] = _build_series(
        agent_name, definition, grouped, window,
        raw=raw, series_limit=series_limit, db=db,
    )

    # `chart` is the ONE bucket list the sparkline and the trend arrow share,
    # so they cannot describe a different thing from the number above them
    # (`latest.value` is the cross-series fold; for `sum`/`avg` the chart is
    # that same fold, and for `last` it names the series it is drawing).
    entry["chart"] = _chart(entry["series"], aggregation)

    # Stats describe the folded sparkline of a NUMERIC metric only: a `status`
    # metric's min/max would be the alphabetical order of its labels.
    if _numeric(definition) and entry["chart"]:
        history = [{"t": b["ts"], "v": b["value"]}
                   for b in entry["chart"]["buckets"]
                   if isinstance(b.get("value"), (int, float))]
        entry["stats"] = db.calculate_widget_stats(history) if history else None
    else:
        entry["stats"] = None
    return entry


def _build_series(
    agent_name: str,
    definition: Dict[str, Any],
    grouped: List[Dict[str, Any]],
    window: Dict[str, Any],
    *,
    raw: bool,
    series_limit: int,
    db: Any,
) -> List[Dict[str, Any]]:
    """Per-dimension series for the window: bucketed always, raw only on request.

    The default (all-metrics) read never ships raw points — 50 metrics × 2 000
    points is a ten-megabyte "read". Raw points are the single-metric path's
    payload, capped at `series_limit`, keeping the NEWEST (a truncated series
    that dropped today's points would be worse than no series at all).
    """
    aggregation = definition.get("aggregation")
    name = definition.get("name")
    if not raw:
        return [
            {
                "dims": s["dims"],
                "buckets": _bucket(s["points"], window, aggregation),
                "truncated": False,
            }
            for s in grouped[:MAX_SERIES_PER_METRIC]
        ]

    limit = max(1, min(int(series_limit or DEFAULT_SERIES_LIMIT), MAX_SERIES_LIMIT))
    rows = db.metric_series_points(
        agent_name, name, window["since"], window["until"], limit)
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    windowed = _group_series(rows, aggregation)
    return [
        {
            "dims": s["dims"],
            "buckets": _bucket(s["points"], window, aggregation),
            "points": s["points"],
            "truncated": truncated,
            "oldest_ts": s["points"][0]["ts"] if s["points"] else None,
        }
        for s in windowed[:MAX_SERIES_PER_METRIC]
    ]


# ---------------------------------------------------------------------------
# The health block
# ---------------------------------------------------------------------------


def freshness_summary(
    agent_name: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """The informational `metrics` block on `get_agent_health` (ent#479 C5).

    Counts and name lists only — no values, no series. Deliberately NOT an
    input to `aggregate_status` or `issues`: a business metric going stale is
    the operator's news, not a platform health failure, and folding it into
    the status would make every dashboard red for a reason Trinity cannot fix.
    """
    from database import db

    now = now or datetime.now(timezone.utc)
    definitions = db.list_metric_definitions(agent_name, include_retired=True)
    active = [d for d in definitions if d.get("status") == "active"]
    retired = [d for d in definitions if d.get("status") != "active"]

    names = [d["name"] for d in definitions if d.get("name")]
    newest: Dict[str, str] = {}
    for row in db.latest_metric_points(agent_name, names, 1):
        current = newest.get(row["metric"])
        if current is None or row["ts"] > current:
            newest[row["metric"]] = row["ts"]

    stale, no_cadence, no_points = [], [], []
    for definition in active:
        verdict = freshness(
            definition.get("cadence_seconds"), newest.get(definition["name"]),
            now,
        )
        if verdict["freshness"] == "no_points":
            no_points.append(definition["name"])
        elif verdict["freshness"] == "no_cadence":
            no_cadence.append(definition["name"])
        elif verdict["stale"]:
            stale.append(definition["name"])

    last_point_at = max(newest.values()) if newest else None
    return {
        "declared": len(active),
        "with_points": sum(1 for d in active if d["name"] in newest),
        "stale": stale,
        "no_cadence": no_cadence,
        "no_points": no_points,
        "retired_with_points": [d["name"] for d in retired
                                if d.get("name") in newest],
        "last_point_at": last_point_at,
        "rule": STALE_RULE,
    }


# ---------------------------------------------------------------------------
# `dashboard.yaml` widget binding (ent#479 C7)
# ---------------------------------------------------------------------------


def _status_color(definition: Dict[str, Any], value: Any) -> Optional[str]:
    """The colour a bound `status` widget renders, from the registry.

    The author already chose colours per status value in `template.yaml`
    (ent#477 keeps them on the definition), so a bound widget must not make
    the author repeat them in `dashboard.yaml`.
    """
    for entry in definition.get("values") or []:
        if isinstance(entry, dict) and entry.get("value") == value:
            return entry.get("color")
    return None


def _threshold_color(definition: Dict[str, Any], value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    critical = definition.get("critical_threshold")
    warning = definition.get("warning_threshold")
    direction = (definition.get("direction") or "up").lower()
    worse = (lambda v, t: v >= t) if direction != "down" else (lambda v, t: v <= t)
    if critical is not None and worse(value, critical):
        return "red"
    if warning is not None and worse(value, warning):
        return "yellow"
    return None


def bind_dashboard_widgets(
    config: Dict[str, Any],
    agent_name: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Fill every widget that carries `metric: <name>` from the point store.

    Runs AFTER the dashboard cache write and the snapshot capture and BEFORE
    history enrichment, so a bound number is never written into
    `agent_dashboard_values` — the snapshot table must not become a second
    source of truth for a value the registry already owns (the whole point of
    ent#476).

    Degrades per widget: a store outage sets `binding_error` on the bound
    widgets and leaves every unbound widget untouched. A dashboard is never
    5xx'd because one widget named a metric.

    Three refusals, each with a machine `binding_error_code` beside the
    sentence (the route's `{reason, message}` pair, spelled for a widget):
    `metric_store_unavailable`, `metric_undeclared`, and `metric_retired` —
    the last is TD-10's rule applied to a reader who cannot pass
    `include_retired`, so a retired metric never reads as current here either.
    """
    if not isinstance(config, dict):
        return config
    widgets = _bound_widgets(config)
    if not widgets:
        return config

    from database import db  # deferred, as in read_agent_metrics above

    now = now or datetime.now(timezone.utc)
    try:
        # `include_retired=True` so a retired binding is DISTINGUISHABLE from a
        # name that was never declared — the two get different refusals below.
        payload = read_agent_metrics(
            agent_name, window="auto", include_retired=True, now=now)
        by_name = {m["name"]: m for m in payload.get("metrics", [])}
    except Exception as exc:  # noqa: BLE001 — a bind never breaks a dashboard
        logger.warning("[Metrics] Bind failed for %s: %s", agent_name, exc)
        for widget in widgets:
            widget["binding_error"] = "metric store unavailable"
            widget["binding_error_code"] = "metric_store_unavailable"
            widget["bound"] = False
        return config

    for widget in widgets:
        name = widget.get("metric")
        entry = by_name.get(name)
        if entry is None:
            widget["binding_error"] = (
                f"metric '{name}' is not declared in template.yaml")
            widget["binding_error_code"] = "metric_undeclared"
            widget["bound"] = False
            widget.pop("value", None)
            continue
        if (entry.get("status") or "active") != "active":
            # TD-10's rule, applied to the binding: `metric=<retired>` on the
            # route is a 422 rather than a 200 carrying the last value, and a
            # widget is the same read with no one there to pass
            # `include_retired`. Rendering the last value of a metric the
            # author retired is exactly the "silently reads as current" case
            # that refusal exists to prevent, so the widget refuses too — and
            # says why, rather than disappearing.
            widget["binding_error"] = (
                f"metric '{name}' was retired at {entry.get('retired_at')} — "
                "bind a declared metric or re-declare this one in template.yaml")
            widget["binding_error_code"] = "metric_retired"
            widget["retired_at"] = entry.get("retired_at")
            widget["bound"] = False
            widget.pop("value", None)
            widget.pop("history", None)
            continue
        latest = entry.get("latest")
        widget["bound"] = True
        widget.pop("binding_error", None)
        widget.pop("binding_error_code", None)
        widget["last_point_at"] = entry.get("last_point_at")
        widget["stale"] = entry.get("stale")
        widget["freshness"] = entry.get("freshness")
        widget["unit"] = widget.get("unit") or entry.get("unit")
        widget["label"] = widget.get("label") or entry.get("label")
        if latest is None:
            widget.pop("value", None)
            continue
        # Overwrite: an author keeping a placeholder `value:` for an older
        # base image must not see the placeholder once the binding works.
        widget["value"] = latest["value"]
        color = (_status_color(entry, latest["value"])
                 if entry.get("type") == "status"
                 else _threshold_color(entry, latest["value"]))
        if color:
            widget["color"] = color
        chart = entry.get("chart")
        if chart:
            # The SAME shape `_enrich_widgets_with_history` writes
            # (`{values, trend, trend_percent, min, max, avg}`), not a bare
            # list: `DashboardPanel.vue` reads `widget.history.values` and
            # `widget.history.trend`, so a bound widget handed a list would
            # silently lose its sparkline and its trend arrow while every
            # backend test still passed. One consumer, one shape.
            #
            # Drawn from `chart`, not `series[0]`: the widget's `value` above
            # is the cross-series fold, so its history must be the same fold
            # or the trend arrow would describe one region of a total.
            values = [
                {"t": b["ts"], "v": b["value"]}
                for b in chart["buckets"]
            ]
            stats = db.calculate_widget_stats(values) if values else None
            widget["history"] = {
                "values": values,
                "trend": (stats or {}).get("trend", "stable"),
                "trend_percent": (stats or {}).get("trend_percent", 0),
                "min": (stats or {}).get("min"),
                "max": (stats or {}).get("max"),
                "avg": (stats or {}).get("avg"),
            }
    return config


def _bound_widgets(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every widget in the config carrying a `metric:` binding."""
    found: List[Dict[str, Any]] = []
    for section in config.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for widget in section.get("widgets") or []:
            if isinstance(widget, dict) and widget.get("metric"):
                found.append(widget)
    return found


def is_bound(widget: Any) -> bool:
    """True when a widget's value comes from the registry, not a snapshot.

    Spelled once and imported by the snapshot writer and the history
    enrichment, so "skip bound widgets" cannot mean two different things in
    two files.
    """
    return isinstance(widget, dict) and bool(widget.get("metric"))
