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

from db.evaluations import TIER0_EVALUATOR, TIER1_EVALUATOR_PREFIX, tier0_gate_outcome

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
    reads the grader's kind only. A machine never reads a judge document —
    on any row: an `admin:` row's `judge` is a person's free-form words, held
    back like the comment — and gets `judge_findings` in its place; on a
    Tier-1 row it also loses `quality`. The one read guard for the judge's
    verdict (ent#936 TD-8)."""
    if operator and not machine:
        return row
    out = dict(row)
    out["evaluator"] = redact_evaluator(row.get("evaluator"))
    if machine and (is_tier1(row) or row.get("judge") is not None):
        if is_tier1(row):
            out["quality"] = None
        out["judge"] = None
        out["judge_findings"] = judge_findings(row.get("judge"))
    return out


def project_summary(summary: dict, *, machine: bool) -> dict:
    """An aggregate for one reader: a machine never reads a judge's score."""
    if not machine:
        return summary
    return {k: v for k, v in summary.items() if k not in QUALITY_KEYS}


# --- who reads what (ent#937 T1) ---------------------------------------------

class ReadScope:
    """The rows one principal may read, decided once per request.

    `names` is the accessible-set tri-state (None = every agent, an empty set =
    none). `self_agent` is set for an agent-scoped key: it reads its OWN rows
    and the pairs it is a side of — never its owner's other agents, which is
    what `accessible_agent_names` alone would hand it (ent#629 class)."""

    __slots__ = ("names", "self_agent", "machine")

    def __init__(self, names: Optional[set], self_agent: Optional[str], machine: bool):
        self.names = names
        self.self_agent = self_agent
        self.machine = machine

    def sees(self, agent: Optional[str]) -> bool:
        return self.names is None or agent in self.names

    def sees_row(self, row: dict) -> bool:
        """One row on a per-agent read the path gate already let through. A
        person reads every row there; an agent key only its own rows and the
        pairs it is a side of — what the fleet reads would show it."""
        if not self.self_agent:
            return True
        return self.self_agent in (row.get("agent_name"), row.get("caller_agent"))

    def caller_for(self, agent_name: str, caller: Optional[str]) -> tuple[bool, Optional[str]]:
        """`(empty, caller)` for a per-agent list read of `agent_name`. An
        agent key reading another agent's rows is held to the runs it
        delegated (`caller` = itself); naming a different caller reads empty."""
        if not self.self_agent or agent_name == self.self_agent:
            return False, caller
        if caller not in (None, self.self_agent):
            return True, None
        return False, self.self_agent


def scope_for(current_user, *, machine: bool) -> ReadScope:
    """The one scope helper every evaluation read goes through."""
    agent = getattr(current_user, "agent_name", None)
    if agent:
        return ReadScope({agent}, agent, machine)
    from services.agent_service.helpers import accessible_agent_names
    names = accessible_agent_names(current_user)
    return ReadScope(None if names is None else set(names), None, machine)


def narrow(scope: ReadScope, agent: Optional[str]) -> Optional[set]:
    """`scope.names` narrowed by an `agent=` filter; an agent outside the
    scope narrows to nothing (zeros, never a 403 — `narrow_to_agent` parity)."""
    if agent is None:
        return scope.names
    return {agent} if scope.sees(agent) else set()


def window_filters(scope: ReadScope, *, by: str, agent: Optional[str] = None,
                   caller: Optional[str] = None, callee: Optional[str] = None,
                   schedule_id: Optional[str] = None) -> dict:
    """`db.evaluation_window_rows` keyword filters for one scope.

    A person reads a pair only when BOTH sides are visible; an agent key reads
    its own runs, and with `by=pair` the pairs it is caller or callee of."""
    kw: dict = {"caller": caller, "callee": callee, "schedule_id": schedule_id}
    if by != "pair":
        kw["agents"] = narrow(scope, agent)
        return kw
    kw["delegated_only"] = True
    if scope.self_agent:
        kw["either"] = scope.self_agent
    else:
        kw["agents"] = scope.names
        kw["callers"] = scope.names
    return kw


def group_key(by: str, row: dict) -> tuple:
    if by == "schedule":
        return (row.get("agent_name"), row.get("schedule_id"))
    if by == "pair":
        return (row.get("caller_agent"), row.get("agent_name"))
    return (row.get("agent_name"),)


def group_fields(by: str, key: tuple) -> dict:
    if by == "schedule":
        return {"agent": key[0], "schedule_id": key[1]}
    if by == "pair":
        return {"caller": key[0], "callee": key[1]}
    return {"agent": key[0]}


def aggregate(by: str, rows: list[dict], human_labels: dict, *, machine: bool) -> dict:
    """`{total, groups}` for `by` — each group is a `fold`, projected for the
    reader. The failed lists stay on the failed route; groups carry counts."""
    buckets: dict[tuple, list] = {}
    for r in rows:
        buckets.setdefault(group_key(by, r), []).append(r)

    def _summary(rs):
        s = fold(rs, human_labels, failed_limit=0)
        s.pop("failed", None)
        s.pop("failed_truncated", None)
        return project_summary(s, machine=machine)

    groups = [{"key": group_fields(by, k), **_summary(rs)}
              for k, rs in sorted(buckets.items(), key=lambda kv: tuple(str(x) for x in kv[0]))]
    return {"total": _summary(rows), "groups": groups}


# --- the stored scorecard (trend reads; the writer is ent#937 C4) -------------

# The reserved owner the platform writes `eval.*` points under. `:` is not
# legal in an agent name, so no agent can hold it, rename into it or cascade it.
PLATFORM_EVAL_OWNER = "platform:eval"
EVAL_RATE_METRICS = ("eval.checks_passed_rate", "eval.completion_rate",
                     "eval.quality_mean", "eval.judge_human_agreement")
EVAL_COUNT_METRICS = ("eval.evaluated", "eval.completed", "eval.graded",
                      "eval.passed", "eval.judged")
EVAL_METRICS = EVAL_RATE_METRICS + EVAL_COUNT_METRICS
# A judge's score as a series — never served to a machine.
QUALITY_METRICS = frozenset({"eval.quality_mean", "eval.judge_human_agreement"})
MAX_TREND_DAYS = 92


class MetricError(ValueError):
    code = "unknown_metric"


def trend_metric(metric: str, *, machine: bool) -> str:
    """The metric if this reader may read it; a closed enum, never echoed."""
    if metric not in EVAL_METRICS or (machine and metric in QUALITY_METRICS):
        raise MetricError("metric is not an evaluation scorecard metric")
    return metric


# --- the delegation chain ------------------------------------------------------

CHAIN_DEPTH_KEY = "inter_agent_max_chain_depth"
CHAIN_DEPTH_HARD_CAP = 32


def chain_max_depth() -> int:
    """The platform's own stated chain maximum, clamped to 1..32; a bad
    setting falls back to the code default rather than failing the read."""
    from config import OPS_SETTINGS_DEFAULTS
    default = int(OPS_SETTINGS_DEFAULTS[CHAIN_DEPTH_KEY])
    try:
        from services import settings_service
        value = int(settings_service.get_ops_setting(CHAIN_DEPTH_KEY, int))
    except Exception:  # noqa: BLE001
        return default
    return max(1, min(CHAIN_DEPTH_HARD_CAP, value))


def chain_node_view(node: dict) -> dict:
    """A chain node as served: ids, agents, status and times — no message,
    no response text."""
    return {
        "execution_id": node.get("id"),
        "agent_name": node.get("agent_name"),
        "caller_agent": node.get("source_agent_name"),
        "parent_execution_id": node.get("parent_execution_id"),
        "status": node.get("status"),
        "triggered_by": node.get("triggered_by"),
        "chain_depth": node.get("chain_depth"),
        "started_at": node.get("started_at"),
        "completed_at": node.get("completed_at"),
    }
