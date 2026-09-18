"""Inbound bookkeeping for AAuth callers on the A2A server (ent#623).

Everything the router needs beyond verification, kept out of the router
(Invariant #1):

* the audit ``details`` and the ``agent_activities`` row that make the callee's
  records say WHO called — identity, verification result, allow-list decision;
* the execution → identity map that scopes ``tasks/get`` / ``tasks/cancel`` to
  the identity that started the task. A bearer caller is unaffected.

The map is Redis-backed (both workers must agree) and fails CLOSED: if the
owner of an execution cannot be established, an AAuth caller is told the task
does not exist.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.aauth.verifier import VerifiedAgent, normalize_identity

logger = logging.getLogger(__name__)

EXECUTION_OWNER_TTL = 24 * 3600
_EXEC_KEY = "aauth:exec:{}"


def is_listed(identity: str, identities: Optional[list]) -> bool:
    """Exact membership (host part case-insensitive). ``None`` → nobody."""
    if not identities:
        return False
    wanted = normalize_identity(identity)
    return wanted is not None and wanted in {normalize_identity(i) for i in identities}


def audit_details(verified: Optional[VerifiedAgent], *, allowlist: str,
                  identity: Optional[str] = None, issuer: Optional[str] = None,
                  verification: str = "verified", error: Optional[str] = None) -> Dict[str, Any]:
    """Audit ``details`` for an AAuth caller. Never carries the token."""
    details: Dict[str, Any] = {
        "auth": "aauth",
        "caller_identity": verified.identity if verified else identity,
        "issuer": verified.issuer if verified else issuer,
        "verification": verification,
        "allowlist": allowlist,
    }
    if verified is not None:
        details["key_thumbprint"] = verified.key_thumbprint
        details["token_id"] = verified.token_id
    if error:
        details["error"] = error
    return details


async def record_activity(agent_name: str, verified: VerifiedAgent, *, allowlist: str,
                          execution_id: Optional[str] = None, state: Optional[str] = None,
                          error: Optional[str] = None) -> None:
    """One already-closed ``agent_activities`` row per inbound AAuth call.

    Mirrors the outbound row (`a2a_outbound_service._record_activity`): not an
    execution terminal, so written closed. Identifiers only in ``details``.
    Fail-open — observability never changes the answer the caller gets.
    """
    try:
        from models import ActivityState, ActivityType
        from services.activity_service import activity_service

        details: Dict[str, Any] = {
            "direction": "a2a_inbound",
            "auth": "aauth",
            "caller": verified.identity,
            "verification": "verified",
            "allowlist": allowlist,
        }
        if execution_id:
            details["execution_id"] = execution_id
        if state:
            details["state"] = state
        activity_id = await activity_service.track_activity(
            agent_name=agent_name,
            activity_type=ActivityType.AGENT_COLLABORATION,
            triggered_by="a2a",
            related_execution_id=execution_id,
            details=details,
        )
        await activity_service.complete_activity(
            activity_id,
            status=ActivityState.COMPLETED if error is None else ActivityState.FAILED,
            error=error,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[aauth] inbound activity write failed for %s", agent_name, exc_info=True)


def remember_execution(execution_id: str, identity: str) -> None:
    try:
        from services.rate_limiter import _get_redis

        client = _get_redis()
        if client is not None:
            client.set(_EXEC_KEY.format(execution_id), identity, ex=EXECUTION_OWNER_TTL)
    except Exception:  # noqa: BLE001 — the caller then simply cannot poll it
        logger.warning("[aauth] could not record execution owner for %s", execution_id, exc_info=True)


def execution_owned_by(execution_id: str, identity: str) -> bool:
    try:
        from services.rate_limiter import _get_redis

        client = _get_redis()
        if client is None:
            return False
        owner = client.get(_EXEC_KEY.format(execution_id))
    except Exception:  # noqa: BLE001 — unknown owner ⇒ not yours
        return False
    if isinstance(owner, bytes):
        owner = owner.decode("utf-8", "replace")
    return owner == identity
