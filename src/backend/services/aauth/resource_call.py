"""The route's service seam for an AAuth resource call (ent#879).

``resource_client`` is the ladder — pure protocol, no platform concerns. This
module is what the route calls: the ladder under the #1084 **effect guard**,
the **audit** row and the **activity** row. Nothing here signs, dials or
parses; nothing here is FastAPI (Invariant #1).

Effect guard (review H1, decisions #22 / #29). Identity is
``{resource, method, path}`` plus the caller's REQUIRED ``dedup_label`` — the
body is deliberately absent (an LLM body is non-deterministic across a re-run
and would defeat re-delivery dedup, the #1084 rule; the body lives in the
pending record for resume). Only a **terminal** outcome — ``result`` or
``denied`` — completes the claim: a pending handle, an interaction the person
has not acted on yet, an expiry or a transient refusal leaves the guard by
raising ``_NotTerminal`` so ``effect_guard`` releases the claim and the
post-consent retry runs for real instead of replaying a dead pending. A
completed replay returns the stored snapshot with ``replayed=True`` and makes
no network call; a resume never enters the guard (it is a continuation, not a
second effect).

Audit (plan §Audit row, T10). One row per call: ``EXECUTION`` /
``aauth_resource_call`` once the ladder ran, or ``AUTHORIZATION`` /
``aauth_resource_refused`` when a refusal happened before any egress. The
details carry the agent, the resource, the method and path, the token kind
presented, the outcome, the resource status, the budget cost and the ordered
challenge list — **never** a token, the body, the justification (agent-asserted
Markdown rendered at the PS; INFO-logged through ``sanitize_text`` by the
ladder), a handle, an interaction url or a code.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from services.aauth import config, resource_client as rc, resource_meta
from services.idempotency_service import EffectInProgressError, EffectUnguardedError, effect_guard

logger = logging.getLogger(__name__)

EFFECT_TYPE = "aauth_resource"

#: Refusals the ladder raises before any request leaves the backend — audited
#: as an AUTHORIZATION decision, not as an execution. `resource_unreachable`
#: and friends are execution outcomes (a dial happened).
LOCAL_REFUSALS = frozenset({
    "invalid_resource", "invalid_method", "invalid_path", "resource_not_allowed",
    "unsupported_signature_components", "person_server_unconfigured", "state_unavailable",
})


class _NotTerminal(Exception):
    """Leaves `effect_guard` with the claim RELEASED (its `except BaseException` arm)."""

    def __init__(self, outcome: rc.Outcome):
        super().__init__(outcome.outcome)
        self.outcome = outcome


def snapshot_of(outcome: rc.Outcome) -> Dict[str, Any]:
    """The replayable form of a TERMINAL outcome — no handle, no token."""
    base = {"outcome": outcome.outcome, "resource": outcome.resource, "challenges": list(outcome.challenges),
            "budget": outcome.budget.as_dict() if outcome.budget else None}
    if isinstance(outcome, rc.ResourceResult):
        base.update(status=outcome.status, body=outcome.body, truncated=outcome.truncated,
                    token_kind=outcome.token_kind, method=outcome.method, path=outcome.path,
                    content_type=outcome.content_type, access_server=outcome.access_server)
    else:  # Denied
        base.update(reason=outcome.reason, detail=outcome.detail)
    return base


def from_snapshot(snapshot: Any, *, resource: str, method: str, path: str) -> rc.Outcome:
    """Rebuild a terminal outcome from a stored snapshot (tolerant of an older shape)."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    budget = snap.get("budget")
    budget_obj = resource_meta.Budget(**{k: budget.get(k) for k in ("cost", "balance", "unit")}) \
        if isinstance(budget, dict) else None
    challenges = [str(c) for c in (snap.get("challenges") or [])]
    if snap.get("outcome") == "denied":
        return rc.Denied(str(snap.get("reason") or "denied"), str(snap.get("detail") or ""), challenges,
                         str(snap.get("resource") or resource), budget=budget_obj)
    return rc.ResourceResult(status=int(snap.get("status") or 0), body=str(snap.get("body") or ""),
                             truncated=bool(snap.get("truncated")), token_kind=str(snap.get("token_kind") or "agent"),
                             challenges=challenges, resource=str(snap.get("resource") or resource),
                             method=str(snap.get("method") or method), path=str(snap.get("path") or path),
                             budget=budget_obj, content_type=snap.get("content_type"),
                             access_server=snap.get("access_server") or None)


async def guarded_call(
    agent_name: str,
    *,
    resource: str,
    method: str,
    path: str,
    body: Optional[bytes],
    justification: Optional[str],
    dedup_label: str,
    execution_id: Optional[str],
    now: Optional[int] = None,
    client_factory=None,
    budget_seconds: float = rc.ROUTE_BUDGET,
) -> Tuple[rc.Outcome, bool]:
    """`resource_client.call_resource` under the effect guard → `(outcome, replayed)`.

    Raises `EffectInProgressError` (a concurrent attempt holds the claim),
    `EffectUnguardedError` (#2392: a pull-mode agent without an execution id)
    and `signer.SigningUnavailable` (AAuth inert) for the route to map.
    """
    identity = {"resource": config.normalize_issuer(resource) or resource, "method": (method or "").upper(),
                "path": path}
    try:
        async with effect_guard(EFFECT_TYPE, identity, execution_id=execution_id, agent_name=agent_name,
                                dedup_label=dedup_label) as guard:
            if guard.replay:
                return from_snapshot(guard.snapshot, resource=identity["resource"], method=identity["method"],
                                     path=path), True
            outcome = await rc.call_resource(agent_name, resource=resource, method=method, path=path, body=body,
                                             justification=justification, now=now, client_factory=client_factory,
                                             budget_seconds=budget_seconds)
            if isinstance(outcome, rc.TERMINAL_OUTCOMES):
                guard.snapshot = snapshot_of(outcome)
                return outcome, False
            raise _NotTerminal(outcome)
    except _NotTerminal as exc:
        return exc.outcome, False


# --------------------------------------------------------------------------- #
# Audit + activity
# --------------------------------------------------------------------------- #
def audit_action(outcome: rc.Outcome) -> Tuple[str, str]:
    """`(event_type name, event_action)` — AUTHORIZATION for a pre-egress refusal, else EXECUTION."""
    if isinstance(outcome, rc.Refusal) and outcome.code in LOCAL_REFUSALS:
        return "AUTHORIZATION", "aauth_resource_refused"
    return "EXECUTION", "aauth_resource_call"


def audit_details(agent_name: str, outcome: rc.Outcome, *, method: Optional[str] = None, path: Optional[str] = None,
                  replayed: bool = False, resumed: bool = False, execution_id: Optional[str] = None,
                  trigger_source: Optional[str] = None) -> Dict[str, Any]:
    """The allow-listed audit payload — never a token, body, justification, handle, url or code."""
    details: Dict[str, Any] = {
        "auth": "aauth", "agent": agent_name, "outcome": outcome.outcome,
        "resource": outcome.resource or None, "challenges": list(outcome.challenges),
        "replayed": replayed, "resumed": resumed,
    }
    if isinstance(outcome, rc.ResourceResult):
        details.update(method=outcome.method, path=outcome.path, token_kind=outcome.token_kind, status=outcome.status)
        if outcome.token_kind == "auth":
            # ent#928: which server minted the auth token — our PS (three-party)
            # or the access server the resource named (four-party). A URL, never a token.
            details["auth_mode"] = "four-party" if outcome.access_server else "three-party"
            if outcome.access_server:
                details["access_server"] = outcome.access_server
    else:
        if method:
            details["method"] = method
        if path:
            details["path"] = path
    if isinstance(outcome, (rc.InteractionRequired, rc.ResourcePending)):
        details.update(pending=True, at=outcome.at,
                       requirement="interaction" if isinstance(outcome, rc.InteractionRequired) else outcome.requirement,
                       expires_at=outcome.expires_at)
    if isinstance(outcome, (rc.Denied, rc.Expired)):
        details["reason"] = outcome.reason
    if isinstance(outcome, rc.Refusal):
        details["reason"] = outcome.code
        if outcome.status is not None:
            details["status"] = outcome.status
        if outcome.possibly_executed:
            details["possibly_executed"] = True
    budget = getattr(outcome, "budget", None)
    if budget is not None:
        details.update(budget_cost=budget.cost, budget_balance=budget.balance, budget_unit=budget.unit)
    if execution_id:
        details["execution_id"] = execution_id
    if trigger_source:
        details["trigger_source"] = trigger_source
    return details


async def record_activity(agent_name: str, outcome: rc.Outcome) -> None:
    """One `agent_activities` row per call (the #736 F12 shape, written already closed).

    Fail-open: an observability write never turns a completed call into an
    error. `details` carries the resource host, the outcome and the token
    kind — never the path's body, a token or a handle.
    """
    try:
        from urllib.parse import urlsplit

        from models import ActivityState, ActivityType
        from services.activity_service import activity_service

        host = urlsplit(outcome.resource).hostname if outcome.resource else None
        error = None
        if isinstance(outcome, rc.Refusal):
            error = outcome.code
        elif isinstance(outcome, (rc.Denied, rc.Expired)):
            error = outcome.reason
        activity_id = await activity_service.track_activity(
            agent_name=agent_name,
            activity_type=ActivityType.AGENT_COLLABORATION,
            triggered_by="agent",
            details={
                "direction": "aauth_resource",
                "auth": "aauth",
                "host": host,
                "state": outcome.outcome,
                **({"token_kind": outcome.token_kind} if isinstance(outcome, rc.ResourceResult) else {}),
            },
        )
        await activity_service.complete_activity(
            activity_id,
            status=ActivityState.COMPLETED if error is None else ActivityState.FAILED,
            error=error,
        )
    except Exception:  # noqa: BLE001 — observability never breaks the call
        logger.warning("[aauth] activity write failed for %s", agent_name, exc_info=True)


__all__ = ["EFFECT_TYPE", "LOCAL_REFUSALS", "EffectInProgressError", "EffectUnguardedError", "audit_action", "audit_details",
           "from_snapshot", "guarded_call", "record_activity", "snapshot_of"]
