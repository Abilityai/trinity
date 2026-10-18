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
`tier='gate'`; signals land in A2 with `tier='signal'`. Every check here is
graded from platform records (`source='platform'`).
"""
from __future__ import annotations

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


def _scrubbed_excerpt(text: str, start: int, end: int, staged: List[str]) -> str:
    """≤200 chars around [start, end), credential patterns and staged runtime
    secrets redacted. Scrubbed BEFORE it is cut, so a secret straddling the
    window edge cannot leave half of itself behind."""
    lo = max(0, start - 80)
    window = text[lo:end + 80 + 64]
    window = sanitize_text(window)
    for value in sorted((v for v in staged if v), key=len, reverse=True):
        window = window.replace(value, REDACTION_PLACEHOLDER)
    return window[:MAX_EXCERPT_CHARS]


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

    - explicit no-op wording → `skip: no_claim` (an honest "nothing to do")
    - no claim → `skip: no_claim`
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
    scan = _FOOTER_LINE_RE.sub("", text)
    if _NO_OP_RE.search(scan):
        return _skip("no_claim", **evidence)
    claims = _claims(scan)
    if not claims:
        return _skip("no_claim", **evidence)
    first = claims[0]
    evidence["claim_excerpt"] = _scrubbed_excerpt(
        scan, first.start(), first.end(), ctx.get("staged_values") or [])
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


CHECKS: List[Check] = [
    Check("execution_exists", "gate", "platform", check_execution_exists),
    Check("credential_echo", "gate", "platform", check_credential_echo),
    Check("validate_001", "gate", "platform", check_validate_001),
    Check("deliverable_landed", "gate", "platform", check_deliverable_landed),
]


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
    }
    intent = resolve_intent(row.message, response=row.response)
    try:
        receipts = db.get_execution_receipts(
            row.id, row.agent_name, _iso(row.started_at), _iso(row.completed_at))
    except Exception as e:  # noqa: BLE001 — absent receipts read as zero, never a crash
        logger.warning("[tier0] receipts read failed for %s: %s", execution_id, type(e).__name__)
        receipts = None
    ctx = {"execution": execution, "intent": intent, "receipts": receipts,
           "staged_values": _staged_values()}
    if receipts is None:
        ctx["receipts"] = {}

    results = [_run_check(c, ctx) for c in CHECKS]
    if receipts is None:
        for r in results:
            if r["id"] == "deliverable_landed" and r["status"] != "skip":
                r.update(status="skip", reason="receipts_unreadable")
    checks = {
        "schema": SCHEMA_VERSION,
        "intent_ref": {"kind": intent["kind"], "ref": intent["ref"],
                       "unresolved": intent["unresolved"]},
        "evaluated_at": utc_now_iso(),
        "checks": results,
    }
    try:
        return db.upsert_platform_evaluation(
            row.agent_name, execution_id=row.id, completion=(status == "success"),
            archetype=intent["kind"], checks=checks)
    except Exception as e:  # noqa: BLE001 — the run already happened; log, never raise
        logger.warning("[tier0] write failed for %s: %s", execution_id, type(e).__name__)
        return None
