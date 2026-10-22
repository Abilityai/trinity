"""
Nevermined card delegation (trinity-enterprise#965/#966).

The buyer side of Nevermined, built beside the provider-side paywall in
``nevermined_payment_service`` and sharing none of its gates. Nevermined holds
the card, holds the mandate and settles every purchase; Trinity keeps only the
organization config and the delegation id a person bound to an agent.

Upstream calls are plain ``httpx`` (payments-py has no widget-session, revoke
or transactions call), each inside :func:`delegation_slot` — its own gate, so a
panel can never starve the paid-chat verify/settle path. The API base is a
fixed two-entry map keyed by the configured environment, never built from
input.

Secrets (widget secret, agent API key, a minted session token) are never logged
and never returned except the session itself, to the person who asked for it.
Upstream failures are logged by status code only — never the response body.
"""

import asyncio
import contextlib
import logging
import os
import weakref
from typing import Any, Dict, List, Optional

import httpx

from database import db

logger = logging.getLogger(__name__)

#: Environment → Nevermined API base. A table lookup on a two-value enum, so
#: the outbound host can never be steered by a request.
NEVERMINED_API_BASES: Dict[str, str] = {
    "sandbox": "https://api.sandbox.nevermined.app",
    "live": "https://api.live.nevermined.app",
}

SECRET_FIELDS = ("widget_secret", "agent_api_key")

#: Delegation traffic gets its OWN gate (the module precedent is
#: `nevermined_payment_service.plan_lookup_slot`): a polling panel must never
#: hold a slot the paid-chat verify/settle path needs.
NEVERMINED_DELEGATION_MAX_INFLIGHT = int(
    os.getenv("NEVERMINED_DELEGATION_MAX_INFLIGHT", "4")
)
DELEGATION_SLOT_WAIT_SECONDS = 5.0
UPSTREAM_TIMEOUT_SECONDS = 10.0

_DELEGATION_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class DelegationError(Exception):
    """A named refusal: rendered as ``{"detail": {"code", "message"}}``."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra or {}

    def detail(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class DelegationBusy(Exception):
    """No delegation slot became free within the wait budget."""


def _delegation_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _DELEGATION_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(NEVERMINED_DELEGATION_MAX_INFLIGHT)
        _DELEGATION_GATES[loop] = gate
    return gate


@contextlib.asynccontextmanager
async def delegation_slot():
    """Hold one delegation slot, or raise `DelegationBusy`."""
    gate = _delegation_gate()
    try:
        await asyncio.wait_for(gate.acquire(), timeout=DELEGATION_SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise DelegationBusy("delegation concurrency limit reached") from None
    try:
        yield
    finally:
        gate.release()


def _not_configured() -> DelegationError:
    return DelegationError(
        409,
        "nevermined_not_configured",
        "No Nevermined organization is configured on this instance. "
        "An admin sets it under Settings before an agent can be funded through Nevermined.",
    )


def _delegations_active(count: int, what: str) -> DelegationError:
    return DelegationError(
        409,
        "delegations_active",
        f"{count} agent(s) hold an active Nevermined mandate; {what}. "
        "Revoke those mandates first.",
        extra={"active_delegations": count},
    )


# ---------------------------------------------------------------------------
# Organization (admin)
# ---------------------------------------------------------------------------

def organization_state() -> Dict[str, Any]:
    """The GET body: always a state, never a 404 and never a secret."""
    org = db.get_nevermined_organization()
    active = db.count_active_agent_delegations()
    if not org:
        return {
            "configured": False,
            "org_id": None,
            "environment": None,
            "updated_at": None,
            "updated_by": None,
            "active_delegations": active,
        }
    return {
        "configured": True,
        "org_id": org["org_id"],
        "environment": org["environment"],
        "updated_at": org["updated_at"],
        "updated_by": org["updated_by"],
        "active_delegations": active,
    }


def _key_environment_prefix(api_key: str) -> Optional[str]:
    """`sandbox:` / `live:` prefix the Nevermined SDK understands, if present."""
    head, sep, _ = api_key.partition(":")
    if sep and head in NEVERMINED_API_BASES:
        return head
    return None


def save_organization(
    org_id: str,
    environment: str,
    widget_secret: Optional[str],
    agent_api_key: Optional[str],
    updated_by: Optional[str],
) -> Dict[str, Any]:
    """Create or update the organization. Returns ``{state, rotated, created}``.

    A secret left out keeps the stored one; on first create both are required.
    """
    if agent_api_key:
        prefix = _key_environment_prefix(agent_api_key)
        if prefix and prefix != environment:
            raise DelegationError(
                422,
                "environment_mismatch",
                f"The Nevermined agent API key is a '{prefix}' key but the "
                f"environment is '{environment}'. Use a key for the same Nevermined environment.",
            )

    current = db.get_nevermined_organization()
    stored = db.get_nevermined_organization_secrets() if current else None
    if not current or not stored:
        missing = [
            name for name, value in (("widget_secret", widget_secret), ("agent_api_key", agent_api_key))
            if not value
        ]
        if missing:
            raise DelegationError(
                422,
                "secret_required",
                "The first Nevermined organization setup needs both secrets: "
                + ", ".join(missing) + " missing.",
                extra={"missing": missing},
            )
        stored = {}

    rotated: List[str] = [
        name for name, value in (("widget_secret", widget_secret), ("agent_api_key", agent_api_key))
        if value and value != stored.get(name)
    ]

    if current:
        changes_env = environment != current["environment"]
        rotates_key = "agent_api_key" in rotated
        if changes_env or rotates_key:
            active = db.count_active_agent_delegations()
            if active:
                what = (
                    "they hold keys for the current Nevermined environment"
                    if changes_env
                    else "they hold the current Nevermined agent API key"
                )
                raise _delegations_active(active, what)

    secrets = {
        "widget_secret": widget_secret or stored.get("widget_secret"),
        "agent_api_key": agent_api_key or stored.get("agent_api_key"),
    }
    db.save_nevermined_organization(org_id, environment, secrets, updated_by)
    return {"state": organization_state(), "rotated": rotated, "created": not current}


def delete_organization() -> bool:
    """Remove the organization; refused while any live agent holds a mandate."""
    active = db.count_active_agent_delegations()
    if active:
        raise _delegations_active(active, "deleting the organization would strand them")
    return db.delete_nevermined_organization()
