"""Evaluation read API — the pure half (trinity-enterprise#937).

No SQL and no HTTP (Invariant #1). The rows come from
`db/evaluation_aggregates.py`; this module decides what they add up to and
what each kind of reader may see of them:

* `parse_period` — the UTC window a request names (≤ 92 days).
* `fold` — window rows → evaluated / completion / checks passed / per-check
  fails / judged / quality / agreement, plus a bounded failed-run list.
* `redact_evaluator` — the grader-identity ALLOWLIST (ent#781).
* `project_row` — one evaluation row for one reader; the Tier-1 read guard
  (ent#936 TD-8) lives here and nowhere else.
* `project_summary` — an aggregate for one reader.

The Goodhart rule this enforces: a judge's `quality`, the grader, the model
name and the judge's prose never reach an agent principal. What an agent gets
instead is structural — check ids, reason codes, judge dimension verdicts.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.evaluations import TIER0_EVALUATOR, tier0_gate_outcome

TIER1_EVALUATOR_PREFIX = "platform:tier1:"
TIER1_KIND = "platform:tier1"
MAX_WINDOW_DAYS = 92
FAILED_LIMIT = 50
FINDINGS_CAP = 32

# Known non-person evaluator names: shown as stored. Everything else is
# reduced to its kind — a fourth kind is anonymised on the day it appears.
NON_PERSON_EVALUATORS = frozenset({TIER0_EVALUATOR})
_PERIODS = {"day": timedelta(days=1), "week": timedelta(days=7)}
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
# A closed-vocabulary token: a check id, a reason code, a judge dimension.
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERDICTS = ("pass", "fail", "n/a")
# Aggregate keys that carry a judge's score — absent from an agent's view.
QUALITY_KEYS = ("quality_mean", "judge_human_agreement", "agreement_n")


class PeriodError(ValueError):
    code = "invalid_period"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(value: Optional[str]) -> datetime:
    if not value or not _RFC3339_RE.match(value):
        raise PeriodError("since/until must be RFC 3339 timestamps")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeriodError("since/until must be RFC 3339 timestamps") from exc


def parse_period(period: str, *, since: Optional[str] = None, until: Optional[str] = None,
                 now: Optional[datetime] = None) -> tuple[str, str]:
    """`[since, until)` as ISO-Z strings for `day` (last 24 h), `week` (last
    7 days) or `custom` (both bounds, at most 92 days apart)."""
    now = now or datetime.now(timezone.utc)
    if period in _PERIODS:
        # `until` is just past now, so a run completed this microsecond counts.
        return _iso(now - _PERIODS[period]), _iso(now + timedelta(microseconds=1))
    if period != "custom":
        raise PeriodError("period must be day, week or custom")
    start, end = _parse_ts(since), _parse_ts(until)
    if start >= end:
        raise PeriodError("since must be before until")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise PeriodError(f"the window is at most {MAX_WINDOW_DAYS} days")
    return _iso(start), _iso(end)


# --- the fold -----------------------------------------------------------------

def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def _checks_of(row: dict) -> list:
    checks = row.get("checks")
    items = checks.get("checks") if isinstance(checks, dict) else None
    return items if isinstance(items, list) else []


def _failed_checks(row: dict) -> list[dict]:
    """`[{id, reason}]` of the run's failed checks — closed vocabulary only.
    `evidence` (which may quote the run) is never carried."""
    out = []
    for c in _checks_of(row):
        if not isinstance(c, dict) or c.get("status") != "fail":
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not _TOKEN_RE.match(cid):
            continue
        reason = c.get("reason")
        out.append({"id": cid,
                    "reason": reason if isinstance(reason, str) and _TOKEN_RE.match(reason) else None})
    return out


def _completed(row: dict) -> bool:
    if row.get("completion") is not None:
        return bool(row["completion"])
    return row.get("status") == "success"


def fold(rows: list[dict], human_labels: dict[str, list[float]], *,
         failed_limit: int = FAILED_LIMIT) -> dict:
    """The aggregate over window rows. Never raises on a malformed row: a
    headline that cannot be read is "not graded", a wrong-typed check is
    skipped. Zero rows → zero counts and null rates."""
    evaluated = completed = graded = passed = judged = 0
    quality_sum = 0.0
    agree = agreement_n = 0
    per_check: dict[str, int] = {}
    failed: list[dict] = []
    failed_total = 0
    for r in rows:
        evaluated += 1
        done = _completed(r)
        completed += done
        outcome = tier0_gate_outcome(r.get("checks"))
        if outcome is not None:
            graded += 1
            passed += outcome is True
        fails = _failed_checks(r) if outcome is not None else []
        for c in fails:
            per_check[c["id"]] = per_check.get(c["id"], 0) + 1
        q = r.get("judge_quality")
        if isinstance(q, (int, float)) and not isinstance(q, bool):
            judged += 1
            quality_sum += float(q)
            labels = human_labels.get(r.get("execution_id")) or []
            if labels:
                agreement_n += 1
                human_up = sum(1 for v in labels if v >= 0.5) * 2 >= len(labels)
                agree += (q >= 0.5) == human_up
        if outcome is False or not done:
            failed_total += 1
            if len(failed) < failed_limit:
                failed.append({
                    "execution_id": r.get("execution_id"),
                    "agent_name": r.get("agent_name"),
                    "caller_agent": r.get("caller_agent"),
                    "schedule_id": r.get("schedule_id"),
                    "completed_at": r.get("completed_at"),
                    "completion": done,
                    "failed_checks": fails,
                })
    return {
        "evaluated": evaluated,
        "completed": completed,
        "completion_rate": _rate(completed, evaluated),
        "graded": graded,
        "passed": passed,
        "checks_passed_rate": _rate(passed, graded),
        "per_check_fail": per_check,
        "judged": judged,
        "quality_mean": round(quality_sum / judged, 4) if judged else None,
        "judge_human_agreement": _rate(agree, agreement_n),
        "agreement_n": agreement_n,
        "failed": failed,
        "failed_truncated": failed_total > len(failed),
    }


# --- projection -----------------------------------------------------------------

def redact_evaluator(evaluator: Any) -> str:
    """The grader as a non-operator may see it (ent#781).

    An ALLOWLIST: a known non-person name passes through; a Tier-1 row reads
    `platform:tier1` (the model name is withheld too, TD-8); any other
    `kind:rest` reads `kind`; anything without a kind reads `unknown`.
    """
    name = evaluator if isinstance(evaluator, str) else ""
    if name in NON_PERSON_EVALUATORS:
        return name
    if name.startswith(TIER1_EVALUATOR_PREFIX):
        return TIER1_KIND
    kind, sep, _ = name.partition(":")
    if sep and kind:
        return kind
    return "unknown"


def judge_findings(judge: Any) -> list[dict]:
    """A judge verdict's structural findings: `{dimension, verdict, reason}`
    with every field from a closed vocabulary. Scores, details, quotes and
    anything unrecognised are dropped, not escaped."""
    items = judge.get("findings") if isinstance(judge, dict) else None
    out = []
    for f in items if isinstance(items, list) else []:
        if not isinstance(f, dict):
            continue
        dim, verdict, reason = f.get("dimension"), f.get("verdict"), f.get("reason")
        if not (isinstance(dim, str) and _TOKEN_RE.match(dim)) or verdict not in _VERDICTS:
            continue
        out.append({"dimension": dim, "verdict": verdict,
                    "reason": reason if isinstance(reason, str) and _TOKEN_RE.match(reason) else None})
        if len(out) >= FINDINGS_CAP:
            break
    return out


def is_tier1(row: dict) -> bool:
    return str(row.get("evaluator") or "").startswith(TIER1_EVALUATOR_PREFIX)


def project_row(row: dict, *, machine: bool, operator: bool) -> dict:
    """One evaluation row for one reader; never mutates `row`.

    An operator (owner/admin, a person) reads the row as stored. Anyone else
    reads the grader's kind only. A machine reading a Tier-1 row additionally
    loses `quality` and the judge document, and gets `judge_findings` — the
    one read guard for the judge's verdict (ent#936 TD-8)."""
    if operator and not machine:
        return row
    out = dict(row)
    out["evaluator"] = redact_evaluator(row.get("evaluator"))
    if machine and is_tier1(row):
        out["quality"] = None
        out["judge"] = None
        out["judge_findings"] = judge_findings(row.get("judge"))
    return out


def project_summary(summary: dict, *, machine: bool) -> dict:
    """An aggregate for one reader: a machine never reads a judge's score."""
    if not machine:
        return summary
    return {k: v for k, v in summary.items() if k not in QUALITY_KEYS}
