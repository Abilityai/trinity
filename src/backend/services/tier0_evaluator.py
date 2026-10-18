"""Tier-0 deterministic evaluator (trinity-enterprise#935).

Grades a finished run against the platform's own records — no LLM call, no
agent-supplied verdict. One `agent_evaluations` row per execution, written by
`evaluate_execution(execution_id)` under `evaluator='platform:tier0'`.

The write fence (plan decision 18): this module takes an execution id and
nothing else — no principal, no request. It imports no FastAPI, no
`dependencies`, no `models.User`, so no route can hand it a caller to trust;
the graded agent never reaches the writer.

Each check is a plain function over the inputs gathered ONCE per run and
returns `{status: pass|fail|skip, reason?, evidence}`. A check that raises is
recorded as `skip: evaluator_error` with the exception's class name only — one
broken check never costs the row. Evidence is counts / ids / hashes; the one
text field, a claim excerpt, is ≤200 chars and scrubbed (decision 17), because
the graded agent can read its own row.

Gate checks (counted in the "Checks passed" headline, TD-7) carry
`tier='gate'`; signals carry `tier='signal'` — recorded, listed, and never
counted in `headline()`. Every check here is graded from platform records
(`source='platform'`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from database import db
from db.evaluations import TIER0_TERMINAL_STATUSES, TIER0_TRIGGERS
from services.execution_classification import detect_unresolved_slash_command
from utils.credential_sanitizer import REDACTION_PLACEHOLDER, count_hits, sanitize_text
from utils.helpers import to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# Regexes run over at most this many characters of agent output (untrusted).
MAX_SCAN_CHARS = 20_000
MAX_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class Check:
    id: str
    tier: str      # gate | signal
    source: str    # platform | agent_reported
    fn: Callable[[Dict[str, Any]], Dict[str, Any]]


def _pass(**evidence) -> Dict[str, Any]:
    return {"status": "pass", "evidence": evidence}


def _fail(reason: str, **evidence) -> Dict[str, Any]:
    return {"status": "fail", "reason": reason, "evidence": evidence}


def _skip(reason: str, **evidence) -> Dict[str, Any]:
    return {"status": "skip", "reason": reason, "evidence": evidence}


# --- intent -----------------------------------------------------------------

# A skill name: `name` or `namespace:name`, no `/` (so a leading path is prose).
_SKILL_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,63})?")


def resolve_intent(message: Optional[str], response: Optional[str] = None) -> Dict[str, Any]:
    """What the run was asked to do: a `/playbook args` invocation or a message.

    `unresolved` is #1410's shape — a slash command answered by the runtime's
    "Unknown command" reply — read through #1410's own detector, not a copy.
    """
    text = (message or "").lstrip()
    if text.startswith("/"):
        token, _, rest = text[1:].partition(" ")
        token, nl, after = token.partition("\n")
        if token and _SKILL_REF_RE.fullmatch(token):
            unresolved = bool(detect_unresolved_slash_command(text, response))
            return {"kind": "playbook", "ref": token,
                    "has_args": bool((rest or after).strip()), "unresolved": unresolved}
    return {"kind": "message", "ref": None, "has_args": False, "unresolved": False}


# --- shared text helpers ------------------------------------------------------

def _bounded(text: Optional[str]) -> tuple:
    text = text or ""
    return text[:MAX_SCAN_CHARS], len(text) > MAX_SCAN_CHARS


def _scrub(text: str, staged: List[str]) -> str:
    """Credential patterns and staged runtime secrets redacted from the WHOLE
    (already bounded) text. Done once, before any position is taken or any
    window is cut: a secret sliced first would leave a fragment that matches
    neither its pattern nor its staged value, and that fragment would survive."""
    text = sanitize_text(text)
    for value in sorted((v for v in staged if v), key=len, reverse=True):
        text = text.replace(value, REDACTION_PLACEHOLDER)
    return text


def _scrubbed_excerpt(scrubbed: str, start: int, end: int) -> str:
    """≤200 chars around [start, end) of text that `_scrub` already redacted
    whole — so cutting the window can never split a secret into a fragment
    that escapes redaction. Positions are offsets into that scrubbed text."""
    lo = max(0, start - 80)
    return scrubbed[lo:end + 80 + 64][:MAX_EXCERPT_CHARS]


def _staged_hits(staged: List[str], text: str) -> int:
    if not text or not staged:
        return 0
    from services.runtime_secret_scrub import scrub_text
    before = text.count(REDACTION_PLACEHOLDER)
    return max(0, scrub_text(staged, text).count(REDACTION_PLACEHOLDER) - before)


# --- checks -------------------------------------------------------------------

def check_execution_exists(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """The run's own integrity record (#2467). NULL ≡ no evidence, never
    verified healthy, so the commonest outcome is a skip, not a pass."""
    raw = ctx["execution"].get("turn_integrity")
    if not raw:
        return _skip("no_integrity_record")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (TypeError, ValueError):
        return _skip("integrity_unreadable")
    killed = data.get("background_tasks_killed") or []
    killed_n = len(killed) if isinstance(killed, list) else 0
    pending = data.get("background_tasks_pending_at_exit") or 0
    pending_n = pending if isinstance(pending, int) and not isinstance(pending, bool) else 0
    evidence = {"background_tasks_killed": killed_n, "background_tasks_pending_at_exit": pending_n}
    if killed_n or pending_n:
        return _fail("background_work_lost", **evidence)
    return _pass(**evidence)


def check_credential_echo(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Did the run put a credential in its reply or error? The stored reply is
    sanitized before persistence, so a redaction marker is the trace of an echo
    that already happened; a pattern hit is one that got past it; a staged
    runtime secret is compared in memory and never written."""
    ex = ctx["execution"]
    response, _ = _bounded(ex.get("response"))
    error, _ = _bounded(ex.get("error"))
    if not response and not error:
        return _skip("no_output")
    staged = ctx.get("staged_values") or []
    evidence = {
        "pattern_hits": count_hits(response) + count_hits(error),
        "redaction_markers": response.count(REDACTION_PLACEHOLDER) + error.count(REDACTION_PLACEHOLDER),
        "staged_hits": _staged_hits(staged, response) + _staged_hits(staged, error),
    }
    if any(evidence.values()):
        return _fail("credential_in_output", **evidence)
    return _pass(**evidence)


_VALIDATION_SKIPS = {
    None: "not_run_yet",
    "pending_validation": "not_run_yet",
    "validation_unavailable": "validation_unavailable",
    "skipped": "validation_skipped",
}


def check_validate_001(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """VALIDATE-001's verdict, read off `business_status`. It usually runs
    after the terminal, so at sweep time this is most often `not_run_yet`
    (re-stamping when it lands is deferred, D-3)."""
    ex = ctx["execution"]
    status = ex.get("business_status")
    evidence = {"validation_execution_id": ex.get("validation_execution_id")}
    if status == "validated":
        return _pass(**evidence)
    if status == "failed_validation":
        return _fail("failed_validation", **evidence)
    if status in _VALIDATION_SKIPS:
        return _skip(_VALIDATION_SKIPS[status], **evidence)
    return _skip("unknown_business_status", **evidence)


# Linear word lists (no nested quantifiers) over bounded, untrusted text.
_FOOTER_LINE_RE = re.compile(r"^[ \t]*(?:not done|deviations)[ \t]*:.*$", re.IGNORECASE | re.MULTILINE)
_NO_OP_RE = re.compile(
    r"\b(?:nothing (?:new|to do|to report|to update|changed)|no new (?:items?|entries|messages|leads|results|data)"
    r"|no changes?(?: needed| required| made)?|no updates?|no action (?:needed|required)"
    r"|already up[ -]to[ -]date|no-op)\b",
    re.IGNORECASE,
)
_COMMIT_CLAIMS = frozenset({"committed", "pushed", "merged"})
# The only claim words a no-op phrase may excuse ("Done — nothing to do").
# A deliverable verb (sent, published, filed, …) next to a sign-off such as
# "No action needed from you" is still a claim that must leave a receipt.
_GENERIC_CLAIMS = frozenset({"done", "complete", "completed", "finished"})
_CLAIM_RE = re.compile(
    r"\b(done|completed?|finished|sent|published|posted|created|filed|delivered|shared"
    r"|uploaded|reported|committed|pushed|merged)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:\bnot|\bnever|n't|\bunable to|\bfailed to|\bcannot)\s+$", re.IGNORECASE)


def _claims(text: str) -> List[re.Match]:
    out = []
    for m in _CLAIM_RE.finditer(text):
        if _NEGATION_RE.search(text[max(0, m.start() - 16):m.start()]):
            continue
        out.append(m)
    return out


def check_deliverable_landed(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """A claimed deliverable must have left a receipt (TD-6, the null test).

    - no claim → `skip: no_claim`
    - explicit no-op wording and only generic completion words (done /
      completed / finished) → `skip: no_claim` (an honest "nothing to do");
      a deliverable verb is never excused by a no-op sign-off
    - claim + an attributable receipt (ask / completed effect / metric) → pass
    - claim, none attributable, a report published in the run's window →
      `skip: unattributable` — a window match is never a pass (TD-3)
    - only a commit/push claim → `skip: no_receipt_source` (no OSS push record)
    - otherwise — a bare "Done" with zero receipts → fail
    """
    ex = ctx["execution"]
    raw = ex.get("response") or ""
    text, truncated = _bounded(raw)
    receipts = ctx.get("receipts") or {}
    counts = {k: int((receipts.get(k) or {}).get("count") or 0)
              for k in ("asks", "effects", "metrics", "reports_in_window")}
    evidence: Dict[str, Any] = {"receipts": counts, "truncated": truncated}
    scan = _scrub(_FOOTER_LINE_RE.sub("", text), ctx.get("staged_values") or [])
    claims = _claims(scan)
    if not claims:
        return _skip("no_claim", **evidence)
    if _NO_OP_RE.search(scan) and all(m.group(1).lower() in _GENERIC_CLAIMS for m in claims):
        return _skip("no_claim", **evidence)
    first = claims[0]
    evidence["claim_excerpt"] = _scrubbed_excerpt(scan, first.start(), first.end())
    attributable = counts["asks"] + counts["effects"] + counts["metrics"]
    if attributable:
        evidence["ask_ids"] = list((receipts.get("asks") or {}).get("ids") or [])
        return _pass(**evidence)
    if counts["reports_in_window"]:
        evidence["report_ids"] = list((receipts.get("reports_in_window") or {}).get("ids") or [])
        return _skip("unattributable", **evidence)
    if all(m.group(1).lower() in _COMMIT_CLAIMS for m in claims):
        return _skip("no_receipt_source", **evidence)
    return _fail("claim_without_receipt", **evidence)


# --- playbook_outputs ------------------------------------------------------------

# What a declared output word means as a receipt. `report` can only ever be
# window-matched (reports carry no execution id, TD-3); a word outside this map
# (file, canvas, commit, …) has no receipt source and is counted, not guessed.
_OUTPUT_KINDS = {
    "report": "report", "reports": "report",
    "ask": "ask", "asks": "ask", "approval": "ask", "approvals": "ask",
    "question": "ask", "alert": "ask", "alerts": "ask", "operator": "ask",
    "metric": "metric", "metrics": "metric",
    "effect": "effect", "message": "effect", "messages": "effect", "email": "effect",
    "emails": "effect", "slack": "effect", "notification": "effect",
    "notifications": "effect",
}
_RECEIPT_FOR_KIND = {"ask": "asks", "effect": "effects", "metric": "metrics"}
_PROSE_OUTPUT_HEADING_RE = re.compile(r"^##[ \t]+outputs?\b[^\n]*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#{1,2}[ \t]", re.MULTILINE)
_PROSE_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_OUTPUT_KINDS, key=len, reverse=True)) + r")\b",
                            re.IGNORECASE)
_NOT_DONE_RE = re.compile(r"^[ \t]*not done[ \t]*:[ \t]*([^\n]*)$", re.IGNORECASE | re.MULTILINE)
_NOT_DONE_NONE = frozenset({"", "none", "none.", "nothing", "n/a", "-"})


def _frontmatter_outputs(content: str):
    """The `outputs:` key, read through the ent#183 hardened frontmatter
    parser with the contract's precedence (a `trinity:` block wins over the
    flat key). None when absent or the frontmatter is refused."""
    from services.skill_packaging import parse_frontmatter
    fm, _warning = parse_frontmatter(content)
    if not isinstance(fm, dict):
        return None
    block = fm.get("trinity")
    if isinstance(block, dict) and block.get("outputs") is not None:
        raw = block.get("outputs")
    else:
        raw = fm.get("outputs")
    if raw is None:
        return None
    return raw if isinstance(raw, list) else [raw]


def declared_outputs(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """`{source: frontmatter|prose, kinds: [...], unmapped: n}` or None.

    Frontmatter first (TD-5); the `## Output` prose section only when the
    frontmatter declares nothing. Kinds are the receipt vocabulary above,
    sorted and de-duplicated; never raises on hostile input."""
    text, _ = _bounded(content)
    if not text:
        return None
    entries = _frontmatter_outputs(text)
    if entries:
        kinds, unmapped = set(), 0
        for entry in entries:
            word = entry.get("kind") or entry.get("type") if isinstance(entry, dict) else entry
            kind = _OUTPUT_KINDS.get(word.strip().lower()) if isinstance(word, str) else None
            if kind:
                kinds.add(kind)
            else:
                unmapped += 1
        return {"source": "frontmatter", "kinds": sorted(kinds), "unmapped": unmapped}
    heading = _PROSE_OUTPUT_HEADING_RE.search(text)
    if not heading:
        return None
    body = text[heading.end():]
    nxt = _NEXT_HEADING_RE.search(body)
    body = body[:nxt.start()] if nxt else body
    kinds = {_OUTPUT_KINDS[m.group(1).lower()] for m in _PROSE_WORD_RE.finditer(body)}
    if not kinds:
        return None
    return {"source": "prose", "kinds": sorted(kinds), "unmapped": 0}


def _declared_not_done(text: str) -> bool:
    """The fleet's honest-abort footer: `Not done: <something other than none>`."""
    return any(m.group(1).strip().lower() not in _NOT_DONE_NONE for m in _NOT_DONE_RE.finditer(text))


def check_playbook_outputs(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Every output the invoked skill declares must have left a receipt.

    - not a `/playbook` run → `skip: not_a_playbook`
    - the runtime answered "Unknown command" (#1410) → fail `playbook_unresolved`
    - skill not found in the library → `skip: contract_unresolved`; the
      lookup failing → `skip: skill_unreadable`
    - nothing declared → `skip: no_declared_outputs`; declared but none with a
      receipt source → `skip: no_checkable_outputs`
    - the reply's `Not done:` footer names missing work → `skip:
      declared_not_done` (an honest abort is not graded as a silent miss)
    - a declared ask / effect / metric with zero receipts, or a declared report
      with no report at all in the run's window → fail `declared_output_missing`
    - otherwise a report only window-matched → `skip: unattributable` (TD-3)
    """
    intent = ctx["intent"]
    if intent["kind"] != "playbook":
        return _skip("not_a_playbook")
    if intent["unresolved"]:
        return _fail("playbook_unresolved")
    skill = ctx.get("skill") or {}
    if skill.get("status") == "unreadable":
        return _skip("skill_unreadable", error=skill.get("error"))
    if skill.get("status") != "found":
        return _skip("contract_unresolved")
    declared = declared_outputs(skill.get("content"))
    provenance = {"skill_source_id": skill.get("source_id"), "skill_version": skill.get("version")}
    if declared is None:
        return _skip("no_declared_outputs", **provenance)
    evidence: Dict[str, Any] = {"declared": declared["kinds"], "contract_source": declared["source"],
                                "unmapped": declared["unmapped"], **provenance}
    if not declared["kinds"]:
        return _skip("no_checkable_outputs", **evidence)
    text, _ = _bounded(ctx["execution"].get("response"))
    if _declared_not_done(text):
        return _skip("declared_not_done", **evidence)
    receipts = ctx.get("receipts") or {}

    def _count(key):
        return int((receipts.get(key) or {}).get("count") or 0)

    missing, landed, unattributable = [], [], []
    for kind in declared["kinds"]:
        if kind == "report":
            (unattributable if _count("reports_in_window") else missing).append(kind)
        else:
            (landed if _count(_RECEIPT_FOR_KIND[kind]) else missing).append(kind)
    evidence.update(missing=missing, landed=landed, unattributable=unattributable)
    if missing:
        return _fail("declared_output_missing", **evidence)
    if unattributable:
        return _skip("unattributable", **evidence)
    return _pass(**evidence)


# --- budget_vs_baseline (signal) ---------------------------------------------------

BASELINE_MIN_RUNS = 5
BASELINE_METRICS = ("cost", "duration_ms", "tool_call_count", "context_used")


def _percentile(sorted_values: List[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted, non-empty list."""
    rank = max(1, -(-len(sorted_values) * pct // 100))   # ceil without float drift
    return sorted_values[int(rank) - 1]


def check_budget_vs_baseline(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Cost / duration / tool calls / context against the p5–p95 band of the
    same series' last successful runs (at most 20, at least 5). A metric the
    run did not record, or with fewer than 5 recorded history values, is left
    out — NULL is never read as zero. Outside the band on either side fails:
    far cheaper than usual is as much a signal as far dearer."""
    series = ctx.get("series")
    if series is None:
        return _skip("history_unreadable")
    if len(series) < BASELINE_MIN_RUNS:
        return _skip("insufficient_history", n=len(series))
    ex = ctx["execution"]
    metrics: Dict[str, Any] = {}
    outside: List[str] = []
    for name in BASELINE_METRICS:
        observed = ex.get(name)
        history = sorted(h[name] for h in series if h.get(name) is not None)
        if observed is None or len(history) < BASELINE_MIN_RUNS:
            continue
        p5, p95 = _percentile(history, 5), _percentile(history, 95)
        metrics[name] = {"p5": p5, "p95": p95, "observed": observed}
        if observed < p5 or observed > p95:
            outside.append(name)
    if not metrics:
        return _skip("no_comparable_metrics", n=len(series))
    if outside:
        return _fail("outside_baseline", n=len(series), outside=outside, metrics=metrics)
    return _pass(n=len(series), metrics=metrics)


CHECKS: List[Check] = [
    Check("execution_exists", "gate", "platform", check_execution_exists),
    Check("credential_echo", "gate", "platform", check_credential_echo),
    Check("validate_001", "gate", "platform", check_validate_001),
    Check("deliverable_landed", "gate", "platform", check_deliverable_landed),
    Check("playbook_outputs", "gate", "platform", check_playbook_outputs),
    Check("budget_vs_baseline", "signal", "platform", check_budget_vs_baseline),
]


def headline(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The run's headline, from GATE checks only (TD-7): it passes when no gate
    failed. Skips never count against it; signals are never read here."""
    gates = [r for r in results if r.get("tier") == "gate"]
    counts = {s: sum(1 for r in gates if r.get("status") == s) for s in ("pass", "fail", "skip")}
    return {"passed": counts["fail"] == 0, "gate_pass": counts["pass"],
            "gate_fail": counts["fail"], "gate_skip": counts["skip"]}


# --- the run ------------------------------------------------------------------

def _staged_values() -> List[str]:
    """Staged runtime secrets, compared in memory only. Fail-open: the store
    being down costs the staged comparison, never the evaluation."""
    try:
        from services.runtime_secret_scrub import get_staged_values
        return list(get_staged_values() or [])
    except Exception as e:  # noqa: BLE001
        logger.debug("[tier0] staged values unavailable: %s", type(e).__name__)
        return []


def _load_skill(ref: str) -> Optional[Dict[str, Any]]:
    """The invoked skill from the library (the contract the run is graded
    against — the library's copy, recorded with its source and tree sha, since
    the agent's workspace is not readable from here). `not_found` for a
    namespaced plugin ref or a skill the library does not hold; a failing
    lookup is `unreadable` with the exception class only."""
    if ":" in ref:
        return {"status": "not_found"}
    try:
        from services.skill_service import skill_service
        info = skill_service.get_skill(ref)
    except Exception as e:  # noqa: BLE001 — a dead clone costs the check, never the row
        logger.debug("[tier0] skill lookup failed for %s: %s", ref, type(e).__name__)
        return {"status": "unreadable", "error": type(e).__name__}
    if not info or not info.get("content"):
        return {"status": "not_found"}
    return {"status": "found", "content": info["content"],
            "source_id": info.get("source_id"), "version": info.get("version")}


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else to_utc_iso(value)


def _run_check(check: Check, ctx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = check.fn(ctx)
    except Exception as e:  # noqa: BLE001 — one check never costs the row
        logger.warning("[tier0] check %s raised %s", check.id, type(e).__name__)
        result = _skip("evaluator_error", error=type(e).__name__)
    return {"id": check.id, "tier": check.tier, "source": check.source, **result}


def evaluate_execution(execution_id: str) -> Optional[dict]:
    """Grade one finished run and write its Tier-0 row. Returns the row, or
    None when the run is not eligible (missing, not terminal, trigger outside
    {schedule, agent}) or the write failed — never raises."""
    try:
        row = db.get_execution(execution_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[tier0] execution read failed for %s: %s", execution_id, type(e).__name__)
        return None
    if row is None:
        return None
    status = getattr(row.status, "value", row.status)
    if status not in TIER0_TERMINAL_STATUSES or row.triggered_by not in TIER0_TRIGGERS:
        return None

    execution = {
        "id": row.id, "agent_name": row.agent_name, "status": status,
        "triggered_by": row.triggered_by, "message": row.message,
        "response": row.response, "error": row.error,
        "turn_integrity": row.turn_integrity, "business_status": row.business_status,
        "validation_execution_id": getattr(row, "validation_execution_id", None),
        "schedule_id": row.schedule_id, "cost": row.cost, "duration_ms": row.duration_ms,
        "context_used": row.context_used,
        "tool_call_count": db.tool_call_count(row.tool_calls),
    }
    intent = resolve_intent(row.message, response=row.response)
    try:
        receipts = db.get_execution_receipts(
            row.id, row.agent_name, _iso(row.started_at), _iso(row.completed_at))
    except Exception as e:  # noqa: BLE001 — absent receipts read as zero, never a crash
        logger.warning("[tier0] receipts read failed for %s: %s", execution_id, type(e).__name__)
        receipts = None
    try:
        series = db.recent_series_metrics(row.id)
    except Exception as e:  # noqa: BLE001 — no history reads as a skip, never a crash
        logger.warning("[tier0] series read failed for %s: %s", execution_id, type(e).__name__)
        series = None
    skill = _load_skill(intent["ref"]) if intent["kind"] == "playbook" and not intent["unresolved"] else None
    ctx = {"execution": execution, "intent": intent, "receipts": receipts,
           "skill": skill, "series": series, "staged_values": _staged_values()}
    if receipts is None:
        ctx["receipts"] = {}

    results = [_run_check(c, ctx) for c in CHECKS]
    if receipts is None:
        for r in results:
            if r["id"] in ("deliverable_landed", "playbook_outputs") and r["status"] != "skip":
                r.update(status="skip", reason="receipts_unreadable")
    checks = {
        "schema": SCHEMA_VERSION,
        "intent_ref": {"kind": intent["kind"], "ref": intent["ref"],
                       "unresolved": intent["unresolved"]},
        "evaluated_at": utc_now_iso(),
        "headline": headline(results),
        "checks": results,
    }
    try:
        return db.upsert_platform_evaluation(
            row.agent_name, execution_id=row.id, completion=(status == "success"),
            archetype=intent["kind"], checks=checks)
    except Exception as e:  # noqa: BLE001 — the run already happened; log, never raise
        logger.warning("[tier0] write failed for %s: %s", execution_id, type(e).__name__)
        return None


# --- the sweep (checkpoint B) -----------------------------------------------------

# TD-8 bounds. The sweep is the ONLY writer path (TD-1): no terminal hook, no
# fan-out consumer — so a lost lease, a slow tick or a crashed evaluation can
# never delay a terminal execution write; the two never meet.
SWEEP_LIMIT = 25
SWEEP_SETTLE_SECONDS = 30
SWEEP_LOOKBACK_HOURS = 24
EVALUATION_TIMEOUT_SECONDS = 10
EVALUATION_CONCURRENCY = 4


def _timeout_checks() -> Dict[str, Any]:
    """The row a hung evaluation gets: every registered check skipped as
    `evaluator_timeout`. Same vocabulary as a graded row (FR-8), so a reader
    needs no third shape; the headline is all-skip, never a pass count."""
    results = [{"id": c.id, "tier": c.tier, "source": c.source, "status": "skip",
                "reason": "evaluator_timeout", "evidence": {}} for c in CHECKS]
    return {"schema": SCHEMA_VERSION, "intent_ref": None, "evaluated_at": utc_now_iso(),
            "headline": headline(results), "checks": results}


def record_timeout(execution_id: str) -> Optional[dict]:
    """Leave a Tier-0 row for a run whose evaluation outlived its bound.

    `wait_for` cannot stop a thread, so the bound is enforced by recording the
    outcome: with a row in place the run is not re-picked every tick for as
    long as the evaluation keeps hanging. The write is INSERT-only — the slow
    thread may land its real grade at any moment after the bound fires,
    including between a "no row yet" read and this write, and a grade must
    never be replaced by a skip; a row already on the index makes this a
    no-op (None). The reverse order (this row, then the late grade)
    re-grades in place through the TD-2 update path, which is the right
    outcome. Never raises."""
    try:
        if db.latest_tier0_for_execution(execution_id) is not None:
            return None
        row = db.get_execution(execution_id)
        if row is None:
            return None
        status = getattr(row.status, "value", row.status)
        return db.insert_platform_evaluation_if_absent(
            row.agent_name, execution_id=row.id, completion=(status == "success"),
            archetype=resolve_intent(row.message, response=row.response)["kind"],
            checks=_timeout_checks())
    except Exception as e:  # noqa: BLE001 — a bookkeeping row, never worth a raise
        logger.warning("[tier0] timeout row failed for %s: %s", execution_id, type(e).__name__)
        return None


async def sweep(*, limit: int = SWEEP_LIMIT, settle_seconds: int = SWEEP_SETTLE_SECONDS,
                lookback_hours: int = SWEEP_LOOKBACK_HOURS,
                timeout_seconds: float = EVALUATION_TIMEOUT_SECONDS,
                concurrency: int = EVALUATION_CONCURRENCY) -> Dict[str, int]:
    """One tick: grade up to `limit` settled, unevaluated terminal runs.

    Each evaluation is sync DB + regex work, so it runs in a thread
    (`to_thread`) behind a semaphore and under `wait_for(timeout_seconds)`;
    one raising or hanging evaluation costs itself only, never the batch.
    Idempotent and concurrent-safe: two ticks that pick the same run both
    write through `upsert_platform_evaluation`, and the index makes the
    second write an UPDATE (TD-2) — which is why the lease may fail open.
    Returns counts only; never raises.
    """
    try:
        ids = await asyncio.to_thread(
            db.unevaluated_execution_ids, limit=limit, settle_seconds=settle_seconds,
            lookback_hours=lookback_hours)
    except Exception as e:  # noqa: BLE001 — an unreadable candidate list is an empty tick
        logger.warning("[tier0] candidate read failed: %s", type(e).__name__)
        ids = []
    result = {"candidates": len(ids), "evaluated": 0, "timed_out": 0, "failed": 0}
    if not ids:
        return result
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(execution_id: str) -> str:
        async with sem:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(evaluate_execution, execution_id), timeout_seconds)
                return "evaluated"
            except asyncio.TimeoutError:
                logger.warning("[tier0] evaluation of %s exceeded %ss", execution_id,
                               timeout_seconds)
                await asyncio.to_thread(record_timeout, execution_id)
                return "timed_out"
            except Exception as e:  # noqa: BLE001 — evaluate_execution never raises; belt
                logger.warning("[tier0] evaluation of %s failed: %s", execution_id,
                               type(e).__name__)
                return "failed"

    for outcome in await asyncio.gather(*(one(i) for i in ids)):
        result[outcome] += 1
    return result
