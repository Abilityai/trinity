"""Per-agent behavioral indicators — the pure half (trinity-enterprise#939).

No SQL and no HTTP (Invariant #1). The rows are the ent#937 window rows
(`db/evaluation_aggregates.py`), so an indicator is read-only over the
evaluations that already exist: nothing here writes a grade.

* `compute` — window rows → six named indicators and the fix paths.
* `failing_runs` — the runs behind one indicator, grouped by where they are
  fixed (the playbook that ran, the schedule, or chat).
* `plain_lines` — the Workspace projection: a tone per indicator, no numbers.

The rules this module carries:

* **No blended score.** Each indicator is its own measure and is returned on
  its own; there is no total, no weighting, no rank.
* **A thin sample is not a zero.** Fewer than `min_runs` reads `insufficient`
  with the counts it has; an empty window reads `no_runs`. `value` is None in
  both, so no renderer can draw a 0% from it.
* **Judge dimensions are read off the stored verdict** (`judge.dimensions`,
  validated by `db.evaluations.validate_judge_json`). `n/a` is not a sample.
"""
from __future__ import annotations

from typing import Any, Optional

from db.evaluations import JUDGE_DIMENSIONS as JUDGE_DIMENSION_NAMES, tier0_gate_outcome
from services.evaluation_read_service import FAILED_LIMIT, playbook_of

INDICATORS = ("completion", "checks_passed", "scope_fidelity",
              "escalation_correctness", "quality", "cost_vs_baseline")
JUDGE_INDICATORS = ("scope_fidelity", "escalation_correctness")
# The indicators with runs behind them to list (cost has no failing run).
DRILLABLE = ("completion", "checks_passed", "scope_fidelity",
             "escalation_correctness", "quality")
# Declared defaults (AC 1); the router may read an override per install.
DEFAULT_WINDOW_DAYS = 14
WINDOW_DAYS = (7, 14, 30)
DEFAULT_MIN_RUNS = 5
BASELINE_DAYS = 30
# A judged run below this quality is a "low quality" run in the drill-down.
LOW_QUALITY = 0.5
# Workspace tone: a rate at or above this reads "good", below it "watch".
GOOD_AT = 0.85
ITEMS_PER_GROUP = 50
# The schedule_id a chat / API run carries (`db/schedules/executions.py`).
NO_SCHEDULE_ID = "__manual__"


class IndicatorError(ValueError):
    code = "invalid_indicator"


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _completed(row: dict) -> bool:
    if row.get("completion") is not None:
        return bool(row["completion"])
    return row.get("status") == "success"


def _num(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _failed_checks(row: dict) -> list[tuple[str, Optional[str]]]:
    checks = row.get("checks")
    if tier0_gate_outcome(checks) is None:
        return []
    items = checks.get("checks") if isinstance(checks, dict) else None
    out = []
    for c in items if isinstance(items, list) else []:
        if isinstance(c, dict) and c.get("status") == "fail" and isinstance(c.get("id"), str):
            reason = c.get("reason")
            out.append((c["id"], reason if isinstance(reason, str) else None))
    return out


def _verdict(row: dict, dimension: str) -> Optional[str]:
    judge = row.get("judge")
    dims = judge.get("dimensions") if isinstance(judge, dict) else None
    dim = dims.get(dimension) if isinstance(dims, dict) else None
    verdict = dim.get("verdict") if isinstance(dim, dict) else None
    return verdict if verdict in ("pass", "fail") else None


def source_of(row: dict) -> dict:
    """Where a run's failure is fixed: the playbook it invoked, else the
    schedule that fired it, else chat."""
    playbook = playbook_of(row)
    if playbook:
        return {"kind": "playbook", "ref": playbook}
    if row.get("schedule_id") and row["schedule_id"] != NO_SCHEDULE_ID:
        return {"kind": "schedule", "ref": row["schedule_id"]}
    return {"kind": "chat", "ref": None}


def _skey(source: dict) -> tuple:
    return (source["kind"], source["ref"] or "")


# --- the fold -----------------------------------------------------------------

def _counts(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """`{indicator: (num, n)}` for the rate indicators."""
    out = {"completion": [0, 0], "checks_passed": [0, 0]}
    for d in JUDGE_INDICATORS:
        out[d] = [0, 0]
    for r in rows:
        out["completion"][1] += 1
        out["completion"][0] += _completed(r)
        outcome = tier0_gate_outcome(r.get("checks"))
        if outcome is not None:
            out["checks_passed"][1] += 1
            out["checks_passed"][0] += outcome is True
        for d in JUDGE_INDICATORS:
            v = _verdict(r, d)
            if v is not None:
                out[d][1] += 1
                out[d][0] += v == "pass"
    return {k: (v[0], v[1]) for k, v in out.items()}


def _state(runs: int, n: int, min_runs: int) -> str:
    if runs == 0:
        return "no_runs"
    return "ok" if n >= min_runs else "insufficient"


def compute(rows: list[dict], prior_rows: list[dict], human_labels: dict[str, list[float]],
            baseline_costs: list[float], *, min_runs: int = DEFAULT_MIN_RUNS) -> dict:
    """The six indicators over `rows`, each with its sample and a delta in
    points against `prior_rows` (the same-length window before) when both
    windows carry `min_runs`. `baseline_costs` are the agent's run costs in
    the `BASELINE_DAYS` before the window."""
    runs = len(rows)
    now, before = _counts(rows), _counts(prior_rows)
    indicators = []
    for iid in ("completion", "checks_passed", *JUDGE_INDICATORS):
        num, n = now[iid]
        pnum, pn = before[iid]
        state = _state(runs, n, min_runs)
        value = _rate(num, n) if state == "ok" else None
        delta = None
        if value is not None and pn >= min_runs:
            delta = int(round((num / n - pnum / pn) * 100))
        indicators.append({"id": iid, "state": state, "value": value, "num": num, "n": n,
                           "delta": delta})

    # quality — judged runs only, with the judge↔person agreement beside it
    qs, agree, agreement_n = [], 0, 0
    for r in rows:
        q = _num(r.get("judge_quality"))
        if q is None:
            continue
        qs.append(q)
        labels = human_labels.get(r.get("execution_id")) or []
        if labels:
            agreement_n += 1
            human_up = sum(1 for v in labels if v >= 0.5) * 2 >= len(labels)
            agree += (q >= 0.5) == human_up
    state = _state(runs, len(qs), min_runs)
    indicators.append({"id": "quality", "state": state, "n": len(qs),
                       "value": round(sum(qs) / len(qs), 4) if state == "ok" else None,
                       "agreement": _rate(agree, agreement_n), "agreement_n": agreement_n,
                       "delta": None})

    # cost — this window's median run cost over the agent's prior baseline
    costs = [c for c in (_num(r.get("cost")) for r in rows) if c is not None]
    base = [c for c in (_num(c) for c in baseline_costs) if c is not None]
    med, bmed = _median(costs), _median(base)
    state = _state(runs, min(len(costs), len(base)), min_runs)
    if state == "ok" and not bmed:
        state = "insufficient"  # a $0 baseline is no baseline to divide by
    ok = state == "ok"
    indicators.append({"id": "cost_vs_baseline", "state": state,
                       "n": len(costs), "baseline_n": len(base),
                       "median": round(med, 4) if med is not None else None,
                       "baseline": round(bmed, 4) if bmed is not None else None,
                       "value": round(med / bmed, 2) if ok else None, "delta": None})

    return {"runs": runs, "indicators": indicators, "fix_paths": fix_paths(rows)}


def fix_paths(rows: list[dict]) -> list[dict]:
    """Failed checks and judge-failed dimensions, counted per (check, source),
    most failures first."""
    counts: dict[tuple, dict] = {}
    for r in rows:
        src = source_of(r)
        hits = [(cid, False) for cid in dict.fromkeys(c for c, _ in _failed_checks(r))]
        hits += [(d, True) for d in JUDGE_INDICATORS if _verdict(r, d) == "fail"]
        for cid, judge in hits:
            key = (cid, judge, *_skey(src))
            entry = counts.setdefault(key, {"check": cid, "judge": judge, "source": src,
                                            "failed": 0})
            entry["failed"] += 1
    return sorted(counts.values(), key=lambda e: (-e["failed"], e["check"], _skey(e["source"])))


def judge_failures(rows: list[dict], *, limit: int = FAILED_LIMIT) -> list[dict]:
    """Runs a judge failed on a dimension, newest first — what the agent's
    own summary lists beside its failed checks. Dimension NAMES only: the
    judge's reason, its score and its model never cross (the Goodhart rule,
    `evaluation_read_service`)."""
    out = []
    for r in rows:
        dims = [d for d in JUDGE_DIMENSION_NAMES if _verdict(r, d) == "fail"]
        if not dims:
            continue
        out.append({"execution_id": r.get("execution_id"),
                    "completed_at": r.get("completed_at"),
                    "schedule_id": (r.get("schedule_id")
                                    if r.get("schedule_id") != NO_SCHEDULE_ID else None),
                    "playbook": playbook_of(r), "dimensions": dims})
        if len(out) >= limit:
            break
    return out


# --- the drill-down -----------------------------------------------------------

def _reasons(row: dict, indicator: str, check: Optional[str]) -> Optional[list[str]]:
    """Why `row` counts against `indicator`, as closed-vocabulary codes; None
    when it does not."""
    if indicator == "completion":
        return None if _completed(row) else ["not_completed"]
    if indicator == "checks_passed":
        fails = [(c, r) for c, r in _failed_checks(row) if check is None or c == check]
        return [f"{c}:{r}" if r else c for c, r in fails] or None
    if indicator in JUDGE_INDICATORS:
        return [indicator] if _verdict(row, indicator) == "fail" else None
    if indicator == "quality":
        q = _num(row.get("judge_quality"))
        return ["low_quality"] if q is not None and q < LOW_QUALITY else None
    raise IndicatorError(f"indicator must be one of {', '.join(DRILLABLE)}")


def failing_runs(rows: list[dict], indicator: str, *, check: Optional[str]) -> list[dict]:
    """The runs that count against `indicator`, grouped by source, most
    failures first. Each group says how many of that source's runs in the
    window failed, so "9 of 14" is answerable without a second read."""
    if indicator not in DRILLABLE:
        raise IndicatorError(f"indicator must be one of {', '.join(DRILLABLE)}")
    groups: dict[tuple, dict] = {}
    for r in rows:
        src = source_of(r)
        g = groups.setdefault(_skey(src), {"source": src, "runs": 0, "failed": 0, "items": []})
        g["runs"] += 1
        reasons = _reasons(r, indicator, check)
        if not reasons:
            continue
        g["failed"] += 1
        if len(g["items"]) < ITEMS_PER_GROUP:
            g["items"].append({"execution_id": r.get("execution_id"),
                               "completed_at": r.get("completed_at"),
                               "reasons": reasons, "cost": _num(r.get("cost"))})
    out = [g for g in groups.values() if g["failed"]]
    return sorted(out, key=lambda g: (-g["failed"], _skey(g["source"])))


# --- the Workspace projection -----------------------------------------------

PLAIN_INDICATORS = ("completion", "checks_passed", "scope_fidelity", "escalation_correctness")


def plain_lines(computed: dict) -> list[dict]:
    """A tone per indicator for the people the agent serves: `good`, `watch`
    or `unknown` (not enough runs). No rate, count or score crosses — the
    words are the frontend's, keyed on `indicator` + `tone`."""
    by = {i["id"]: i for i in computed.get("indicators", [])}
    out = []
    for iid in PLAIN_INDICATORS:
        ind = by.get(iid) or {}
        value = ind.get("value")
        tone = "unknown" if value is None else ("good" if value >= GOOD_AT else "watch")
        out.append({"indicator": iid, "tone": tone})
    return out
