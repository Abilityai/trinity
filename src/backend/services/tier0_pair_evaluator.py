"""Tier-0 pair evaluator (trinity-enterprise#940, Layer 1).

Grades the EDGE of a delegated run — the caller→callee hand-off — from the
platform's own records: no LLM call, no agent-supplied verdict. One
`agent_evaluations` row per delegated callee run, under
`evaluator='platform:tier0:pair'`, keyed on the callee execution id beside
(never inside) its Tier-0 row, so a Tier-0 re-grade can never clobber a pair
result.

The write fence is ent#935's, unchanged in shape: this module takes an
execution id and nothing else — no principal, no request — and imports no
FastAPI, no `dependencies`, no `models.User`. The writer it calls refuses any
evaluator outside `PLATFORM_EVALUATORS`.

A run is a candidate only once Tier-0 graded it AND that row names a caller
(`caller_agent`); the delegation columns are re-derived by the writer from the
execution row, exactly as for Tier-0. Caller-level facts (duplicate briefs,
re-sends) are computed over the caller run's sibling set and stamped on every
sibling's row, with the sibling ids in the evidence.

Checks reuse ent#935's vocabulary (`{status: pass|fail|skip, reason?,
evidence}`, gate vs signal, `headline()` over gates only). Evidence is ids,
counts, timestamps and scores — never response text.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from database import db
from db.evaluations import TIER0_PAIR_EVALUATOR
from services.tier0_evaluator import (
    BASELINE_MIN_RUNS, EVALUATION_CONCURRENCY, EVALUATION_TIMEOUT_SECONDS, MAX_SCAN_CHARS,
    SWEEP_LIMIT, SWEEP_LOOKBACK_HOURS, Check, _fail, _pass, _percentile, _skip, headline,
    _run_check,
)
from utils.helpers import parse_iso_timestamp, utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Fan-out storm bound: similarity is O(k²) over the siblings, so above this the
# caller-level checks skip with `sibling_cap` instead of running unbounded.
SIBLING_CAP = 200
# TD-3 (ruled): "near-identical brief" = same series key AND token-set Jaccard
# at or above this over the normalised text. A module constant, deliberately
# not a setting — a threshold an operator can move is a Goodhart knob.
DUPLICATE_JACCARD = 0.9
# The caller's reply is scanned (bounded) for execution ids it names — the
# secondary claim source behind its own activity rows.
MAX_CLAIMED_TOKENS = 20
TERMINAL = ("success", "failed", "cancelled")
# The activity `details.action` a first read of a delegated result writes
# (TD-1, CP-D). Before the first such row exists anywhere the relay checks
# cannot grade — `skip: no_readback_record`, never a fail.
READ_RESULT_ACTION = "read_result"
# The pair series' history read for rework_bounds, in callee runs (grouped
# into caller turns afterwards); the db read is itself capped at its scan bound.
REWORK_SERIES_LIMIT = 100
NON_ACTIVITY_MODES = ("fan_out", "loop", "reminder")


# --- text helpers --------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+")


def normalise_brief(message: Optional[str]) -> str:
    return _WS_RE.sub(" ", (message or "")[:MAX_SCAN_CHARS]).strip().lower()


def brief_similarity(a: Optional[str], b: Optional[str]) -> Dict[str, Any]:
    """`{method: exact|jaccard, score}` for two briefs, normalised once."""
    na, nb = normalise_brief(a), normalise_brief(b)
    if na == nb:
        return {"method": "exact", "score": 1.0}
    ta, tb = set(_TOKEN_RE.findall(na)), set(_TOKEN_RE.findall(nb))
    union = ta | tb
    score = (len(ta & tb) / len(union)) if union else 1.0
    return {"method": "jaccard", "score": round(score, 4)}


def _series_key(message: Optional[str]) -> str:
    """ent#935's series key (playbook name, or "message"), read through the
    db layer's own definition rather than a copy."""
    from db.schedules.executions import ScheduleExecutionsMixin
    return ScheduleExecutionsMixin._series_key(message)


# A platform execution id is `secrets.token_urlsafe(16)` — 22 url-safe chars.
# Only a token that names "exec…" just before it AND looks random (a digit,
# an upper and a lower) counts, so a long English word never becomes a claim.
_EXEC_TOKEN_RE = re.compile(
    r"exec\w*\W{1,20}?(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])", re.IGNORECASE)
_FAN_OUT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])(fo_[A-Za-z0-9_-]{16})(?![A-Za-z0-9_-])")


def _looks_random(token: str) -> bool:
    return (any(c.isdigit() for c in token) and any(c.isupper() for c in token)
            and any(c.islower() for c in token))


def claimed_tokens(text: Optional[str]) -> Dict[str, List[str]]:
    """Execution / fan-out ids a caller's reply names, bounded."""
    text = (text or "")[:MAX_SCAN_CHARS]
    execs = [m.group(1) for m in _EXEC_TOKEN_RE.finditer(text) if _looks_random(m.group(1))]
    fans = [m.group(1) for m in _FAN_OUT_TOKEN_RE.finditer(text)]
    return {"executions": list(dict.fromkeys(execs))[:MAX_CLAIMED_TOKENS],
            "fan_outs": list(dict.fromkeys(fans))[:MAX_CLAIMED_TOKENS]}


def _ts(value):
    """An aware UTC datetime from an ISO string or a datetime (the execution
    model hands back parsed values, the sibling read strings); None if neither."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return parse_iso_timestamp(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _admitted_at(row: Dict[str, Any]):
    return _ts(row.get("queued_at")) or _ts(row.get("started_at"))


# --- checks -------------------------------------------------------------------

def check_dispatch_exists(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Every id the caller claims for this callee resolves to a run the caller
    actually spawned. Primary claims are the caller's own activity rows
    (`related_execution_id` / `details.execution_id`); its reply is secondary
    and fails only for an id that exists nowhere at all."""
    if ctx.get("caller") is None:
        return _skip("caller_missing")
    if ctx.get("activities") is None:
        return _skip("activities_unreadable")
    sibling_ids = {s["id"] for s in ctx["siblings"]}
    claimed = set()
    for a in ctx["activities"]:
        if a.get("agent_name") != ctx["caller_agent"]:
            continue
        details = a.get("details") if isinstance(a.get("details"), dict) else {}
        if details.get("target_agent") not in (None, ctx["callee"]["agent_name"]):
            continue
        for eid in (a.get("related_execution_id"), details.get("execution_id")):
            if eid:
                claimed.add(eid)
    unresolved = sorted(i for i in claimed if i not in sibling_ids)
    prose = ctx.get("claimed_in_reply") or {"executions": [], "fan_outs": []}
    fan_out_ids = {s.get("fan_out_id") for s in ctx["siblings"] if s.get("fan_out_id")}
    exists = ctx.get("exists") or (lambda kind, token: True)
    prose_missing = sorted(
        [t for t in prose["executions"] if t not in sibling_ids and not exists("execution", t)]
        + [t for t in prose["fan_outs"] if t not in fan_out_ids and not exists("fan_out", t)])
    n_prose = len(prose["executions"]) + len(prose["fan_outs"])
    if not claimed and not n_prose:
        return _skip("no_claimed_ids")
    if unresolved or prose_missing:
        return _fail("unresolved_id", unresolved=unresolved + prose_missing,
                     claimed=len(claimed) + n_prose)
    return _pass(claimed=len(claimed) + n_prose)


def check_one_sided_record(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A `chat_with_agent` delegation needs the caller's own record of it:
    an activity row on the caller pointing at this callee run. fan_out / loop
    / reminder children carry their link on the row itself (`fan_out_id`,
    `loop_id`), so there is no activity channel to compare."""
    if ctx.get("delegation_mode") in NON_ACTIVITY_MODES:
        return _skip("no_activity_channel", mode=ctx["delegation_mode"])
    if ctx.get("caller") is None:
        return _skip("caller_missing")
    if ctx.get("activities") is None:
        return _skip("activities_unreadable")
    callee_id = ctx["callee"]["id"]
    rows = [a["id"] for a in ctx["activities"]
            if a.get("agent_name") == ctx["caller_agent"]
            and a.get("related_execution_id") == callee_id]
    if not rows:
        return _fail("no_caller_record", callee_execution_id=callee_id)
    return _pass(activity_ids=rows[:5])


def _caller_level_skip(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ctx.get("caller") is None:
        return _skip("caller_missing")
    if ctx["caller"].get("status") not in TERMINAL:
        return _skip("caller_running")
    if ctx.get("siblings_truncated"):
        return _skip("sibling_cap", siblings=len(ctx["siblings"]), cap=SIBLING_CAP)
    callee = ctx["callee"]
    if callee.get("loop_id"):
        return _skip("loop_mode")
    if callee.get("fan_out_id"):
        return _skip("fan_out_task")
    return None


def duplicates_of_callee(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Siblings that repeat this callee run's brief to the same callee (TD-3),
    each `{id, method, score}`, cached on the ctx. Loop iterations (same brief
    by design) and fan-out tasks (distinct `fan_out_task_id`) are never
    compared; a retry chain (`retry_of_execution_id`) is ONE dispatch — the
    platform's re-run, not the caller's re-send."""
    if "_duplicates" in ctx:
        return ctx["_duplicates"]
    callee = ctx["callee"]
    by_id = {s["id"]: s for s in ctx["siblings"]}

    def root(row):
        seen = set()
        while row.get("retry_of_execution_id") in by_id and row["id"] not in seen:
            seen.add(row["id"])
            row = by_id[row["retry_of_execution_id"]]
        return row["id"]

    mine = root(callee)
    key = _series_key(callee.get("message"))
    out = []
    for s in ctx["siblings"]:
        if (s["id"] == callee["id"] or s.get("agent_name") != callee.get("agent_name")
                or s.get("loop_id") or s.get("fan_out_id") or root(s) == mine):
            continue
        if _series_key(s.get("message")) != key:
            continue
        sim = brief_similarity(callee.get("message"), s.get("message"))
        if sim["score"] >= DUPLICATE_JACCARD:
            out.append({"id": s["id"], **sim})
    ctx["_duplicates"] = out
    return out


def _eligible_peers(ctx: Dict[str, Any]) -> int:
    callee = ctx["callee"]
    return sum(1 for s in ctx["siblings"]
               if s["id"] != callee["id"] and s.get("agent_name") == callee.get("agent_name")
               and not s.get("loop_id") and not s.get("fan_out_id"))


def check_duplicate_dispatch(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The caller sent this callee the same (or a near-identical) brief more
    than once in one turn."""
    skip = _caller_level_skip(ctx)
    if skip:
        return skip
    if not _eligible_peers(ctx):
        return _skip("single_dispatch")
    dups = duplicates_of_callee(ctx)
    if dups:
        return _fail("duplicate_brief", siblings=dups, threshold=DUPLICATE_JACCARD)
    return _pass(compared=_eligible_peers(ctx), threshold=DUPLICATE_JACCARD)


def check_resend_after_receipt(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A duplicate sent AFTER the earlier copy was already admitted — the
    caller held a receipt and re-sent anyway. Two copies admitted at the same
    instant are a parallel dispatch, which `duplicate_dispatch` alone covers."""
    skip = _caller_level_skip(ctx)
    if skip:
        return skip
    dups = duplicates_of_callee(ctx)
    if not dups:
        return _skip("no_duplicate")
    by_id = {s["id"]: s for s in ctx["siblings"]}
    me = _admitted_at(ctx["callee"])
    resends = []
    for d in dups:
        other = _admitted_at(by_id.get(d["id"], {}))
        if me is None or other is None:
            continue
        first, second = (d["id"], ctx["callee"]["id"]) if other < me else (ctx["callee"]["id"], d["id"])
        if other != me:
            resends.append({"first": first, "resent": second})
    if not resends:
        if me is None:
            return _skip("no_admission_time")
        return _pass(duplicates=len(dups), parallel=True)
    return _fail("resent_after_receipt", resends=resends)


def check_wrong_route(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The brief invoked a playbook the callee does not serve: the callee's
    runtime answered "Unknown command" (#1410, recorded on its Tier-0 row), or
    the peer-grant policy refused the call (ent#662 audit row)."""
    intent = ctx.get("intent_ref") or {}
    if intent.get("kind") != "playbook":
        return _skip("not_a_playbook")
    refusals = ctx.get("refusals")
    if refusals:
        return _fail("skill_not_granted", ref=intent.get("ref"), audit_ids=refusals[:5])
    if intent.get("unresolved"):
        return _fail("unknown_command", ref=intent.get("ref"))
    if refusals is None:
        return _skip("refusals_unreadable", ref=intent.get("ref"))
    catalog = ctx.get("catalog")
    return _pass(ref=intent.get("ref"),
                 in_catalog=None if catalog is None else intent.get("ref") in catalog)


def _caller_ended_skip(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ctx.get("caller") is None:
        return _skip("caller_missing")
    if ctx["caller"].get("status") not in TERMINAL:
        return _skip("caller_running")
    return None


def relays_callee(ctx: Dict[str, Any]) -> bool:
    """The caller passed this callee's result upward: its own reply names the
    callee run's id. (Reports carry no execution id and asks carry no cited
    id — D-1 — so the reply is the one attributable relay channel.)"""
    reply = ((ctx.get("caller") or {}).get("response") or "")[:MAX_SCAN_CHARS]
    return bool(ctx["callee"]["id"]) and ctx["callee"]["id"] in reply


def first_read(ctx: Dict[str, Any]):
    """The caller's first `read_result` receipt for this callee run dated
    after the callee ended, as `(activity_id, datetime)`, or None."""
    callee_end = _ts(ctx["callee"].get("completed_at"))
    reads = []
    for a in ctx.get("activities") or []:
        details = a.get("details") if isinstance(a.get("details"), dict) else {}
        at = _ts(a.get("created_at"))
        if (a.get("agent_name") == ctx["caller_agent"]
                and a.get("related_execution_id") == ctx["callee"]["id"]
                and details.get("action") == READ_RESULT_ACTION and at is not None
                and (callee_end is None or at >= callee_end)):
            reads.append((at, a.get("id")))
    if not reads:
        return None
    at, act_id = min(reads)
    return act_id, at


def check_relay_unverified(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The caller relayed the callee's result without ever reading it: a reply
    that names the callee run, with no `read_result` receipt between the
    callee's end and the caller's."""
    skip = _caller_ended_skip(ctx)
    if skip:
        return skip
    if not relays_callee(ctx):
        return _skip("no_relay")
    if ctx.get("activities") is None:
        return _skip("activities_unreadable")
    caller_end = _ts(ctx["caller"].get("completed_at"))
    live_since = _ts(ctx.get("readback_live_since"))
    if live_since is None or (caller_end is not None and live_since > caller_end):
        return _skip("no_readback_record")
    read = first_read(ctx)
    if read is None or (caller_end is not None and read[1] > caller_end):
        return _fail("unread_relay", callee_execution_id=ctx["callee"]["id"])
    return _pass(read_activity_id=read[0])


def check_relayed_failed_work(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The caller relayed a result that Tier-0 graded as failed."""
    skip = _caller_ended_skip(ctx)
    if skip:
        return skip
    if not relays_callee(ctx):
        return _skip("no_relay")
    callee_headline = (ctx.get("tier0") or {}).get("checks")
    callee_headline = callee_headline.get("headline") if isinstance(callee_headline, dict) else None
    if not isinstance(callee_headline, dict) or "passed" not in callee_headline:
        return _skip("callee_not_graded")
    if callee_headline["passed"] is False:
        return _fail("relayed_failed", callee_execution_id=ctx["callee"]["id"],
                     gate_fail=callee_headline.get("gate_fail"))
    return _pass()


def check_failed_with_side_effects(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A failed callee run that still left attributable side effects (asks,
    completed outbound effects, metric points). Recorded, not counted. Window-
    matched reports are evidence only (D-1)."""
    if ctx["callee"].get("status") != "failed":
        return _skip("not_failed")
    receipts = ctx.get("receipts")
    if receipts is None:
        return _skip("receipts_unreadable")
    asks = (receipts.get("asks") or {}).get("count", 0)
    effects = (receipts.get("effects") or {}).get("count", 0)
    metrics = (receipts.get("metrics") or {}).get("count", 0)
    evidence = {"asks": asks, "effects": effects, "metrics": metrics,
                "reports_in_window": (receipts.get("reports_in_window") or {}).get("count", 0)}
    if asks + effects + metrics:
        return _fail("side_effects", **evidence)
    return _pass(**evidence)


def round_trips(ctx: Dict[str, Any]) -> int:
    """Dispatches from this caller turn to this callee, a retry chain counted
    once (the platform's re-run, not the caller's)."""
    callee = ctx["callee"]
    same = {s["id"] for s in ctx["siblings"] if s.get("agent_name") == callee.get("agent_name")}
    return sum(1 for s in ctx["siblings"]
               if s["id"] in same and s.get("retry_of_execution_id") not in same)


def check_rework_bounds(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Round-trips this caller turn needed with this callee, against the p95
    of the same pair's past turns for the same kind of brief. Only the upper
    side fails — needing fewer round-trips than usual is not rework."""
    skip = _caller_level_skip(ctx)
    if skip:
        return skip
    series = ctx.get("rework_series")
    if series is None:
        return _skip("history_unreadable")
    per_turn: Dict[str, int] = {}
    for r in series:
        parent = r.get("parent_execution_id")
        if parent and parent != ctx["parent_execution_id"]:
            per_turn[parent] = per_turn.get(parent, 0) + 1
    if len(per_turn) < BASELINE_MIN_RUNS:
        return _skip("insufficient_history", n=len(per_turn))
    p95 = _percentile(sorted(per_turn.values()), 95)
    observed = round_trips(ctx)
    if observed > p95:
        return _fail("above_baseline", round_trips=observed, p95=p95, n=len(per_turn))
    return _pass(round_trips=observed, p95=p95, n=len(per_turn))


CHECKS: List[Check] = [
    Check("dispatch_exists", "gate", "platform", check_dispatch_exists),
    Check("one_sided_record", "gate", "platform", check_one_sided_record),
    Check("relay_unverified", "gate", "platform", check_relay_unverified),
    Check("relayed_failed_work", "gate", "platform", check_relayed_failed_work),
    Check("duplicate_dispatch", "gate", "platform", check_duplicate_dispatch),
    Check("resend_after_receipt", "gate", "platform", check_resend_after_receipt),
    Check("wrong_route", "gate", "platform", check_wrong_route),
    Check("failed_with_side_effects", "signal", "platform", check_failed_with_side_effects),
    Check("rework_bounds", "signal", "platform", check_rework_bounds),
]


# --- metrics ------------------------------------------------------------------

def _ms(start, end) -> Optional[int]:
    if start is None or end is None:
        return None
    return int(round((end - start).total_seconds() * 1000))


def pair_metrics(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence beside the checks, never pass/fail: dispatch (the callee's
    admission) → start → end → the caller's first read, the callee run's
    cost, and `verified` = the callee's own Tier-0 headline passed. Anything
    the records do not hold is None, never 0 — a zero would pull a mean."""
    callee = ctx["callee"]
    started, ended = _ts(callee.get("started_at")), _ts(callee.get("completed_at"))
    read = first_read(ctx) if ctx.get("activities") is not None else None
    headline_ = (ctx.get("tier0") or {}).get("checks")
    headline_ = headline_.get("headline") if isinstance(headline_, dict) else None
    passed = headline_.get("passed") if isinstance(headline_, dict) else None
    return {
        "latency_ms": {
            "dispatch_to_start": _ms(_ts(callee.get("queued_at")), started),
            "start_to_end": _ms(started, ended),
            "end_to_first_read": _ms(ended, read[1]) if read else None,
        },
        "cost": callee.get("cost"),
        "verified": passed if isinstance(passed, bool) else None,
    }


# --- context ------------------------------------------------------------------

def _guarded(label: str, execution_id: str, fn, *args, **kw):
    """`fn(...)`, or None when it raises — the caller records a named skip."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 — one unreadable source costs its checks only
        logger.warning("[tier0-pair] %s read failed for %s: %s", label, execution_id,
                       type(e).__name__)
        return None


def _exec_dict(row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    status = getattr(row.status, "value", row.status)
    return {"id": row.id, "agent_name": row.agent_name, "status": status,
            "triggered_by": row.triggered_by, "message": row.message,
            "response": row.response, "source_agent_name": row.source_agent_name,
            "fan_out_id": getattr(row, "fan_out_id", None),
            "loop_id": getattr(row, "loop_id", None),
            "retry_of_execution_id": getattr(row, "retry_of_execution_id", None),
            "queued_at": getattr(row, "queued_at", None), "started_at": row.started_at,
            "completed_at": row.completed_at, "cost": getattr(row, "cost", None)}


def _refusal_ids(callee: Dict[str, Any]) -> List[Any]:
    """Peer-grant refusal audit rows for this callee run (ent#662 writes one
    `authorization/peer_skill_refused` row carrying the execution id)."""
    rows = db.get_audit_entries(event_type="authorization", target_type="agent",
                                target_id=callee["agent_name"], limit=200)
    out = []
    for r in rows:
        details = r.get("details") if isinstance(r.get("details"), dict) else {}
        if (r.get("event_action") == "peer_skill_refused"
                and details.get("execution_id") == callee["id"]):
            out.append(r.get("event_id") or r.get("id"))
    return out


def _exists(kind: str, token: str) -> bool:
    """Does a token the caller's reply names exist anywhere? A fan-out id is
    looked up per target agent, which the reply does not name — so outside the
    sibling set it is unknown, and unknown is never "nowhere"."""
    if kind != "execution":
        return True
    try:
        return db.get_execution(token) is not None
    except Exception:  # noqa: BLE001 — unknown is not "nowhere": never a false fail
        return True


def build_context(execution_id: str) -> Optional[Dict[str, Any]]:
    """Everything the pair checks read, gathered once; None when the run is
    not a pair candidate (no Tier-0 row, or one that names no caller)."""
    tier0 = db.latest_tier0_for_execution(execution_id)
    if not tier0 or not tier0.get("caller_agent"):
        return None
    callee = _exec_dict(db.get_execution(execution_id))
    if callee is None:
        return None
    parent_id = tier0.get("parent_execution_id")
    caller = _exec_dict(_guarded("caller", execution_id, db.get_execution, parent_id)) \
        if parent_id else None
    siblings = (_guarded("siblings", execution_id, db.sibling_executions, parent_id,
                         SIBLING_CAP) or []) if parent_id else []
    truncated = len(siblings) > SIBLING_CAP
    siblings = siblings[:SIBLING_CAP]
    # The sibling read carries the columns `get_execution`'s model lacks
    # (`fan_out_task_id`, `parent_execution_id`) — prefer it for the callee.
    for s in siblings:
        if s["id"] == execution_id:
            callee = {**callee, **{k: v for k, v in s.items() if k != "response"}}
    if not any(s["id"] == execution_id for s in siblings):
        siblings = siblings + [callee]
    ids = [s["id"] for s in siblings]
    activities = _guarded("activities", execution_id, db.collaboration_rows_for_executions, ids)
    checks = tier0.get("checks") if isinstance(tier0.get("checks"), dict) else {}
    catalog = _guarded("catalog", execution_id, db.get_agent_skill_names, callee["agent_name"])
    return {
        "callee": callee,
        "caller": caller,
        "caller_agent": tier0["caller_agent"],
        "parent_execution_id": parent_id,
        "delegation_mode": tier0.get("delegation_mode"),
        "tier0": tier0,
        "intent_ref": checks.get("intent_ref"),
        "siblings": siblings,
        "siblings_truncated": truncated,
        "activities": activities,
        "claimed_in_reply": claimed_tokens(caller.get("response")) if caller else None,
        "exists": _exists,
        "catalog": set(catalog) if catalog is not None else None,
        "refusals": _guarded("refusals", execution_id, _refusal_ids, callee),
        "readback_live_since": _guarded("readback", execution_id,
                                        db.earliest_collaboration_action, READ_RESULT_ACTION),
        "receipts": _guarded("receipts", execution_id, db.get_execution_receipts,
                             callee["id"], callee["agent_name"], callee.get("started_at"),
                             callee.get("completed_at")),
        "rework_series": _guarded("rework", execution_id, db.pair_rework_series,
                                  tier0["caller_agent"], callee["agent_name"],
                                  _series_key(callee.get("message")), REWORK_SERIES_LIMIT),
    }


# --- the run ------------------------------------------------------------------

def evaluate_pair(execution_id: str) -> Optional[dict]:
    """Grade one delegated run's edge and write its pair row. Returns the row,
    or None when the run is not a candidate or the write failed — never
    raises."""
    try:
        ctx = build_context(execution_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[tier0-pair] context failed for %s: %s", execution_id, type(e).__name__)
        return None
    if ctx is None:
        return None
    results = [_run_check(c, ctx) for c in CHECKS]
    caller = ctx["caller"] or {}
    checks = {
        "schema": SCHEMA_VERSION,
        "edge": {"caller": ctx["caller_agent"], "callee": ctx["callee"]["agent_name"],
                 "mode": ctx["delegation_mode"],
                 "parent_execution_id": ctx["parent_execution_id"]},
        "caller_status": caller.get("status"),
        "evaluated_at": utc_now_iso(),
        "headline": headline(results),
        "checks": results,
        "metrics": _guarded("metrics", execution_id, pair_metrics, ctx),
    }
    try:
        return db.upsert_platform_evaluation(
            ctx["callee"]["agent_name"], execution_id=execution_id,
            completion=(ctx["callee"]["status"] == "success"),
            archetype=ctx["tier0"].get("archetype"), checks=checks,
            evaluator=TIER0_PAIR_EVALUATOR)
    except Exception as e:  # noqa: BLE001 — log, never raise
        logger.warning("[tier0-pair] write failed for %s: %s", execution_id, type(e).__name__)
        return None


# --- the sweep (phase 2 of tier0_sweep_service.run_cycle, TD-5) -----------------

def _timeout_checks() -> Dict[str, Any]:
    """Every pair check skipped as `evaluator_timeout` — the graded-row
    vocabulary, so a reader needs no third shape; no metrics, none measured."""
    results = [{"id": c.id, "tier": c.tier, "source": c.source, "status": "skip",
                "reason": "evaluator_timeout", "evidence": {}} for c in CHECKS]
    return {"schema": SCHEMA_VERSION, "evaluated_at": utc_now_iso(),
            "headline": headline(results), "checks": results}


def record_timeout(execution_id: str) -> Optional[dict]:
    """Leave a pair row for a run whose pair evaluation outlived its bound,
    so it is not re-picked every tick for as long as it keeps hanging.

    INSERT-only (`insert_platform_evaluation_if_absent`): the slow thread may
    land its grade at any moment, and a grade is never replaced by a skip; a
    timeout row first and the late grade after re-grades in place through the
    upsert. Only a pair candidate (a Tier-0 row naming a caller) gets one.
    Never raises."""
    try:
        if db.latest_tier0_for_execution(execution_id, evaluator=TIER0_PAIR_EVALUATOR):
            return None
        tier0 = db.latest_tier0_for_execution(execution_id)
        if not tier0 or not tier0.get("caller_agent"):
            return None
        return db.insert_platform_evaluation_if_absent(
            tier0["agent_name"], execution_id=execution_id,
            completion=tier0.get("completion"), archetype=tier0.get("archetype"),
            checks=_timeout_checks(), evaluator=TIER0_PAIR_EVALUATOR)
    except Exception as e:  # noqa: BLE001 — a bookkeeping row, never worth a raise
        logger.warning("[tier0-pair] timeout row failed for %s: %s", execution_id,
                       type(e).__name__)
        return None


async def sweep(*, limit: int = SWEEP_LIMIT, lookback_hours: int = SWEEP_LOOKBACK_HOURS,
                timeout_seconds: float = EVALUATION_TIMEOUT_SECONDS,
                concurrency: int = EVALUATION_CONCURRENCY) -> Dict[str, int]:
    """One pair tick: grade up to `limit` pair candidates, each in a thread
    behind a semaphore and under `wait_for(timeout_seconds)` — Tier-0's
    bounds, so the phase delays the next tick by at most
    `timeout_seconds × ceil(limit / concurrency)`. The Tier-0 sweep's settle
    window already applies (a candidate hangs off a Tier-0 row). Runs inside
    the Tier-0 cycle, under its lease; idempotent on the one-row index, so a
    fail-open double leader leaves one row. Counts only; never raises."""
    try:
        ids = await asyncio.to_thread(db.unevaluated_pair_execution_ids, limit=limit,
                                      lookback_hours=lookback_hours)
    except Exception as e:  # noqa: BLE001 — an unreadable candidate list is an empty tick
        logger.warning("[tier0-pair] candidate read failed: %s", type(e).__name__)
        ids = []
    result = {"candidates": len(ids), "evaluated": 0, "timed_out": 0, "failed": 0}
    if not ids:
        return result
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(execution_id: str) -> str:
        async with sem:
            try:
                row = await asyncio.wait_for(
                    asyncio.to_thread(evaluate_pair, execution_id), timeout_seconds)
                return "evaluated" if row is not None else "failed"
            except asyncio.TimeoutError:
                logger.warning("[tier0-pair] evaluation of %s exceeded %ss", execution_id,
                               timeout_seconds)
                await asyncio.to_thread(record_timeout, execution_id)
                return "timed_out"
            except Exception as e:  # noqa: BLE001 — evaluate_pair never raises; belt
                logger.warning("[tier0-pair] evaluation of %s failed: %s", execution_id,
                               type(e).__name__)
                return "failed"

    for outcome in await asyncio.gather(*(one(i) for i in ids)):
        result[outcome] += 1
    return result
