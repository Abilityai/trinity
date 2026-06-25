"""
Agent compatibility validation service (#668).

Public API:
  * ``build_report(agent_name, include_ai)`` — collect a workspace snapshot, run
    the STATIC checks live, merge AI verdicts (freshly evaluated when
    ``include_ai`` else the last persisted ones), persist the latest snapshot,
    and return a report dict.
  * ``apply_fix(agent_name, check_id)`` — apply a gitignore auto-fix.

Design (per the /autoplan plan):
  * STATIC checks always recompute live (cheap, deterministic, free).
  * AI verdicts are PERSISTED (db/compatibility.py) so they show on every load
    without re-spending tokens; ``include_ai`` (and the "re-run" affordance)
    forces fresh AI evaluation. The two-phase frontend fetches STATIC-only first
    for instant paint, then AI.
  * Claude-only checks (CLAUDE.md, .claude/skills) are omitted for non-Claude
    runtimes (#1187) so a Codex/Gemini agent isn't flagged with false HARDs.
  * AI severity is capped at SOFT — an LLM verdict never drives the HARD count.

This service creates NO execution, so the Idempotency-Key invariant (#18) does
not apply; the fix endpoint is naturally idempotent + per-agent locked.
"""

import logging
from typing import Any, Dict, List, Optional

from utils.helpers import utc_now_iso
from database import db
from . import spec
from .collector import collect
from .static_checks import run_static
from .ai_checks import run_ai
from .fixes import apply_fix as _apply_fix, FixError, FixBusy  # noqa: F401 re-export

logger = logging.getLogger(__name__)


def _check_dict(c: spec.CheckDef, status: str, message: str,
                detail: Optional[Dict[str, Any]],
                explanation: Optional[str] = None,
                confidence: Optional[float] = None,
                skip_reason: Optional[str] = None) -> Dict[str, Any]:
    d = detail or {}
    if skip_reason is None:
        skip_reason = d.get("skip_reason")
    # Don't leak the internal skip_reason key into the public detail blob twice.
    public_detail = {k: v for k, v in d.items() if k != "skip_reason"} or None
    return {
        "check_id": c.id,
        "category": c.category_name,
        "severity": spec.effective_severity(c),
        "type": c.type,
        "status": status,
        "message": message,
        "auto_fixable": c.auto_fixable,
        "explanation": explanation,
        "confidence": confidence,
        "detail": public_detail,
        "skip_reason": skip_reason,
    }


def _counts(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    hard = sum(1 for c in checks if c["status"] == "fail" and c["severity"] == "hard")
    soft = sum(1 for c in checks if c["status"] == "fail" and c["severity"] == "soft")
    info = sum(1 for c in checks if c["status"] == "fail" and c["severity"] == "info")
    return {"hard_count": hard, "soft_count": soft, "info_count": info}


def _report_from_persisted(agent_name: str, persisted: Optional[Dict[str, Any]],
                           container_running: bool, status: str, message: str,
                           runtime: Optional[str]) -> Dict[str, Any]:
    """Build a degraded report (stopped / unavailable) from the last snapshot."""
    checks = persisted.get("checks", []) if persisted else []
    counts = _counts(checks) if checks else {"hard_count": 0, "soft_count": 0, "info_count": 0}
    return {
        "agent_name": agent_name,
        "container_running": container_running,
        "overall_status": status,
        "runtime": runtime,
        "checks": checks,
        **counts,
        "ai_ran_at": persisted.get("ai_ran_at") if persisted else None,
        "static_ran_at": persisted.get("static_ran_at") if persisted else None,
        "message": message,
    }


async def build_report(agent_name: str, include_ai: bool = False) -> Dict[str, Any]:
    """Build (and persist) the compatibility report for an agent."""
    collected = await collect(agent_name)
    status = collected["status"]
    runtime = collected.get("runtime")

    if status != "ok":
        persisted = db.get_compatibility_result(agent_name)
        if status == "not_running":
            msg = "Agent is stopped — compatibility checks require a running agent."
            if persisted:
                msg = (f"Agent is stopped — showing the last validation"
                       f" from {persisted.get('static_ran_at') or persisted.get('updated_at')}.")
            return _report_from_persisted(agent_name, persisted, False, "unavailable", msg, runtime)
        # unavailable
        msg = "Could not read the agent workspace — try again shortly."
        return _report_from_persisted(agent_name, persisted, True, "unavailable", msg, runtime)

    snapshot = collected["snapshot"]

    # Which checks apply to this runtime?
    applicable = [c for c in spec.CHECKS if spec.applies_to_runtime(c, runtime)]
    static_ids = [c.id for c in applicable if c.type == "static"]
    ai_ids = [c.id for c in applicable if c.type == "ai"]

    static_results = run_static(snapshot, static_ids)

    now = utc_now_iso()
    persisted = db.get_compatibility_result(agent_name)

    if include_ai:
        ai_verdicts = await run_ai(snapshot, ai_ids)
        ai_ran_at = now
    else:
        # Reuse the last persisted AI verdicts so findings show without re-spend.
        ai_verdicts = {}
        if persisted:
            for c in persisted.get("checks", []):
                if c.get("type") == "ai":
                    ai_verdicts[c["check_id"]] = {
                        "status": c.get("status", "skipped"),
                        "explanation": c.get("explanation"),
                        "confidence": c.get("confidence"),
                        "skip_reason": c.get("skip_reason"),
                    }
        ai_ran_at = persisted.get("ai_ran_at") if persisted else None

    # Assemble checks in canonical spec order.
    checks: List[Dict[str, Any]] = []
    for c in applicable:
        if c.type == "static":
            st, msg, detail = static_results.get(c.id, ("skipped", "not run", {"skip_reason": "not_run"}))
            checks.append(_check_dict(c, st, msg, detail))
        else:
            v = ai_verdicts.get(c.id)
            if not v:
                checks.append(_check_dict(c, "skipped", "AI check not yet run", None,
                                          skip_reason="ai_not_run"))
                continue
            st = v.get("status", "skipped")
            if st == "fail":
                msg = "Did not meet the best-practice bar (see explanation)"
            elif st == "pass":
                msg = "Meets the best-practice bar"
            else:
                reason = v.get("skip_reason") or "skipped"
                msg = "AI check skipped (no API key configured)" if reason == "no_api_key" \
                    else "AI check skipped"
            checks.append(_check_dict(
                c, st, msg, None,
                explanation=v.get("explanation"),
                confidence=v.get("confidence"),
                skip_reason=v.get("skip_reason"),
            ))

    counts = _counts(checks)
    overall = "issues" if (counts["hard_count"] + counts["soft_count"]) > 0 else "compatible"

    # Persist the latest snapshot (upsert). Preserve a prior ai_ran_at when AI
    # wasn't run this pass.
    try:
        db.upsert_compatibility_result(
            agent_name,
            overall_status=overall,
            checks=checks,
            hard_count=counts["hard_count"],
            soft_count=counts["soft_count"],
            info_count=counts["info_count"],
            container_running=True,
            ai_ran_at=ai_ran_at,
            static_ran_at=now,
        )
    except Exception as e:  # noqa: BLE001 — persistence is best-effort, never fail the read
        logger.warning("[compatibility] failed to persist result for %s: %s", agent_name, e)

    return {
        "agent_name": agent_name,
        "container_running": True,
        "overall_status": overall,
        "runtime": runtime,
        "checks": checks,
        **counts,
        "ai_ran_at": ai_ran_at,
        "static_ran_at": now,
        "message": None,
    }


async def apply_fix(agent_name: str, check_id: str) -> Dict[str, Any]:
    """Apply a gitignore auto-fix. Returns the fix-response dict."""
    fixed, message = await _apply_fix(agent_name, check_id)
    return {"check_id": check_id, "fixed": fixed, "message": message, "uncommitted": True}
