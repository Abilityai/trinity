"""Terms-acceptance gate — the OSS seam an enterprise module fills (abilityai/trinity-enterprise#1054).

The shape of ``mfa_gate``: a provider is registered at startup, and the one
resolver every door shares (``dependencies.resolve_platform_user_unfloored``)
consults it exactly once, after every scope fence and the suspension check.
That placement is the whole point — the Workspace (``get_portal_principal``),
the rooms principal, the Workspace voice start, every ``@workspace_route``
operator route (the ``/ws`` ticket mint included) and a person's own MCP key
all resolve through that function, so nothing a Workspace-only person can call
answers before acceptance.

Who is gated, and how it fails:

* **Workspace-only roles only** (``user`` and any role outside the ladder).
  Operator and above are never gated — the provider is not even consulted —
  so a gate fault can never lock an administrator out.
* **Fail CLOSED for the gated principals.** ``mfa_gate`` fails open because a
  2FA bug must not become a platform-wide login outage; here the gated
  principals are exactly the people the terms are for, and the people who can
  repair the gate are never behind it. A provider that raises, or answers
  nothing, refuses with ``terms_required``.
* **Persons, not agents.** An agent-scoped key, a connector key, a delegated
  portal key and a loopback token are the owner's agents and automations, not
  the person; they are untouched, so an agent whose owner has not yet accepted
  keeps its own runtime (heartbeat, reports, asks).
* **Off by default.** With no provider registered — every OSS-only build —
  behaviour is byte-identical to before. The provider itself decides whether a
  gate is live at all (a settings switch on the enterprise side).

A route the person must reach BEFORE accepting — signing out, the identity
read the SPA lands on, and the acceptance routes themselves — is marked with
``dependencies.terms_route(reason)``; the gate skips a marked route.

The refusal is ``403 {"code": <reason>, "version": <terms version>, "message"}``
where ``code`` is one of ``REFUSAL_CODES``: ``terms_required`` (accept the
current version), ``invite_expired`` or ``invite_revoked`` (the invitation that
opened the door is no longer live). The SPA switches on ``code``.

Protocol::

    provider.decision(user: models.User) -> {
        "required": bool, "version": str | None, "reason": str
    }
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

TERMS_REQUIRED = "terms_required"
INVITE_EXPIRED = "invite_expired"
INVITE_REVOKED = "invite_revoked"

#: The closed set of refusal codes; anything else a provider says collapses to
#: ``terms_required`` so the SPA only ever sees codes it knows.
REFUSAL_CODES = frozenset({TERMS_REQUIRED, INVITE_EXPIRED, INVITE_REVOKED})

_MESSAGES = {
    TERMS_REQUIRED: "Accept the current terms to continue.",
    INVITE_EXPIRED: "Your invitation has expired. Ask your administrator for a new one.",
    INVITE_REVOKED: "Your invitation was revoked.",
}


class TermsProvider(Protocol):
    def decision(self, user: Any) -> dict:  # {"required": bool, "version": str|None, "reason": str}
        ...


_provider: Optional[TermsProvider] = None


def register_provider(provider: TermsProvider) -> None:
    """Register the enterprise terms provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[terms_gate] provider registered: %s", type(provider).__name__)


def get_provider() -> Optional[TermsProvider]:
    return _provider


def clear_provider() -> None:
    """Drop the provider — used by tests to restore the OSS no-op path."""
    global _provider
    _provider = None


def refusal(code: str, version: Optional[str]) -> HTTPException:
    """The 403 a gated person receives; ``code`` is normalised to the closed set."""
    if code not in REFUSAL_CODES:
        code = TERMS_REQUIRED
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code, "version": version, "message": _MESSAGES[code]},
    )


def _is_person(user: Any) -> bool:
    """An agent's credential is never gated. Each identity field is read with a
    fail-closed default: an absent attribute reads as "not an agent", so the
    principal is gated rather than waved through (the #2323 getattr trap, in
    the safe direction)."""
    return not (
        getattr(user, "agent_name", None)
        or getattr(user, "connector_agent", None)
        or getattr(user, "portal_delegate", False)
        or getattr(user, "is_event_loopback", False)
    )


def enforce(request: Any, user: Any) -> None:
    """Refuse ``user`` (403) while the provider says the terms are outstanding.

    Called once, by ``resolve_platform_user_unfloored``, after the principal is
    fully resolved. No-op without a provider, for operator and above, for an
    agent's credential, and on a route marked ``@terms_route``.
    """
    # Late import: `dependencies` imports this module.
    from dependencies import TERMS_ROUTE_ATTR, is_workspace_only_role

    if not is_workspace_only_role(getattr(user, "role", None)):
        return
    provider = _provider
    if provider is None:
        return
    if not _is_person(user):
        return
    route = request.scope.get("route") if getattr(request, "scope", None) else None
    if getattr(getattr(route, "endpoint", None), TERMS_ROUTE_ATTR, None):
        return

    try:
        decision = provider.decision(user)
    except Exception:  # noqa: BLE001 — fail CLOSED: the gated person is refused
        logger.exception(
            "[terms_gate] provider.decision failed for user %s; refusing (fail-closed)",
            getattr(user, "id", None),
        )
        raise refusal(TERMS_REQUIRED, None)
    if not isinstance(decision, dict):
        logger.error(
            "[terms_gate] provider.decision answered %r for user %s; refusing (fail-closed)",
            type(decision).__name__, getattr(user, "id", None),
        )
        raise refusal(TERMS_REQUIRED, None)
    if not decision.get("required"):
        return
    raise refusal(str(decision.get("reason") or TERMS_REQUIRED), decision.get("version"))
