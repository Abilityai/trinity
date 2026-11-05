"""Terms-acceptance gate — the OSS seam, and the door an optional module adds
(abilityai/trinity-enterprise#1054, #1107).

The shape of ``mfa_gate``: the one resolver every door shares
(``dependencies.resolve_platform_user_unfloored``) consults this module exactly
once, after every scope fence and the suspension check. That placement is the
whole point — the Workspace (``get_portal_principal``), the rooms principal,
the Workspace voice start, every operator route through ``get_current_user``
and a person's own MCP key all resolve through that function, so nothing a
person can call answers before acceptance, on whichever door they arrive.

Since ent#1107 the decision is the OSS's own (``services/terms_service``): the
terms are an instance setting with a shipped default, and every signed-in
PERSON is gated — an administrator arriving through the panel included. A
provider an optional module registers (``register_provider``) answers INSTEAD
of the OSS decision: it adds the door (an invitation that is no longer live,
``invite_expired`` / ``invite_revoked``) on top of the same OSS decision it
delegates to. With none registered, the OSS decision applies on its own.

Who is gated, and how it fails:

* **Persons on a VISIT, whatever the role.** A visit is a session the SPA
  declared interactive at sign-in (ruling A on ent#1107): the SPA sends
  ``client=web`` on ``POST /api/token`` and ``"client": "web"`` on the email
  verify, the token is minted with an ``interactive: true`` claim
  (``routers/auth.py::_session_claims``), the resolver reads it into
  ``User.interactive_session``, and this gate asks exactly those sessions. A
  JWT WITHOUT the claim — the CLI, curl, a CI harness, a test fixture, a
  session minted before the claim existed — is never gated, whatever the role.
  A Workspace-only person's own ``user``-scoped key is gated too (ent#1054:
  the key is the person); a machine credential of operator-and-above — a
  ``user``-scoped key, the system key — is NOT: an ops key failing at 03:00
  because the admin has not signed in since an upgrade is an outage, not a
  terms screen.

  **Why a claim the client declares is enough.** The gate is a UX/record
  contract — "every new person sees the terms once and the acceptance is
  recorded" — not a security boundary: nothing is authorized by accepting,
  and a caller who strips the claim gains only the right not to be shown a
  screen that a CLI could not render anyway. So the honest signal is the
  client's own word for what it is, and a header/claim the SPA sets is the
  right mechanism; deriving "interactive" from the credential type instead
  (the ent#1054 shape, every JWT) gated every headless caller that holds a
  session token and turned the harness red (327 fixtures).
* **Never an agent's credential.** An agent-scoped key, a connector key, a
  delegated portal key and a loopback token are the owner's agents and
  automations, not the person; an agent whose owner has not yet accepted keeps
  its own runtime (heartbeat, reports, asks).
* **Fail CLOSED on the OSS decision**: a fault reading it refuses — it is the
  same database the resolver just read the account from.
* **A provider fault is fail-closed for a Workspace-only person** (the
  invitees the door is for) **and ignored for operator-and-above**, who then
  face the OSS decision alone: a broken optional module must not lock an
  administrator out of the instance that could fix it.
* **The switch is on by default**; ``terms_required=false`` (through
  ``PUT /api/terms``) turns the gate off instance-wide.

A route the person must reach BEFORE accepting — signing out, the identity
read the SPA lands on, and the read and accept routes themselves — is marked
with ``dependencies.terms_route(reason)``; the gate skips a marked route.
``/api/setup/*`` resolves no principal at all, so the first admin's claim
always precedes their acceptance.

The refusal is ``403 {"code": <reason>, "version": <terms version>, "message"}``
where ``code`` is one of ``REFUSAL_CODES``: ``terms_required`` (accept the
current version), ``invite_expired`` or ``invite_revoked`` (the invitation that
opened the door is no longer live). The SPA switches on ``code``.

Protocol::

    provider.decision(user: models.User) -> {
        "required": bool, "version": str | None, "reason": str
    }
    provider.door_for(user) -> str | None     # optional: the invitation id an
                                              # acceptance is bound to

The accept route (``routers/terms.py``) calls ``assert_door_open(user)``
first: a decision naming a closed door refuses the acceptance with that code.
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

#: What a provider "answered" when it raised — distinct from `None`, which is
#: a (refused) answer of its own.
_FAULT = object()


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


def provider_door(user: Any) -> Optional[str]:
    """The invitation id the registered provider binds this person's acceptance
    to (``door_for``), or ``None`` — the OSS accept route stamps it on the row."""
    provider = _provider
    door_for = getattr(provider, "door_for", None) if provider is not None else None
    if not callable(door_for):
        return None
    try:
        door = door_for(user)
    except Exception:  # noqa: BLE001 — the door is a label; never fail the accept over it
        logger.warning("[terms_gate] provider.door_for failed for user %s",
                       getattr(user, "id", None), exc_info=True)
        return None
    return str(door) if door else None


def _is_visit(user: Any) -> bool:
    """A session the gate asks: a JWT carrying the SPA's ``interactive`` claim
    (``User.interactive_session``), or a Workspace-only person's own key
    (ent#1054: the key is the person). A bare token or a machine credential of
    operator-and-above is not a visit. Read with a "not a visit" default: an
    absent attribute is a principal that never declared itself, and the gate
    is a UX contract (module doc), so it is waved through rather than refused."""
    from dependencies import is_workspace_only_role

    if getattr(user, "interactive_session", False) is True:
        return True
    scope = getattr(user, "mcp_scope", None)
    return scope is not None and is_workspace_only_role(getattr(user, "role", None))


def _provider_decision(user: Any) -> Optional[dict]:
    """The registered provider's answer under the gate's fault policy, or
    ``None`` when there is no provider or (operator-and-above) its fault is
    ignored. A Workspace-only person is REFUSED on a fault — fail closed: a
    provider that raised, or answered nothing, has not admitted the invitee
    it exists for. Shared by ``enforce`` and ``assert_door_open`` so the
    accept route and the gate agree on what a broken module means."""
    from dependencies import is_workspace_only_role

    provider = _provider
    if provider is None:
        return None
    workspace_only = is_workspace_only_role(getattr(user, "role", None))
    answer: Any = _FAULT
    try:
        answer = provider.decision(user)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[terms_gate] provider.decision failed for user %s",
            getattr(user, "id", None),
        )
    if isinstance(answer, dict):
        return answer
    if workspace_only:
        logger.error(
            "[terms_gate] provider answered %s for user %s; refusing (fail-closed)",
            "a fault" if answer is _FAULT else repr(type(answer).__name__),
            getattr(user, "id", None),
        )
        raise refusal(TERMS_REQUIRED, None)
    # Operator and above: the door layer is ignored, the instance decision
    # stands, so a broken module cannot lock them out.
    logger.error(
        "[terms_gate] provider answered %s for user %s; falling back to the "
        "instance decision so an operator is not locked out",
        "a fault" if answer is _FAULT else repr(type(answer).__name__),
        getattr(user, "id", None),
    )
    return None


def assert_door_open(user: Any) -> None:
    """The accept route's check (W2 on ent#1107): a registered provider whose
    decision names a CLOSED door — ``invite_expired`` / ``invite_revoked`` —
    refuses the acceptance with that code, so a person whose invitation is no
    longer live cannot accept through the OSS route and be admitted. A plain
    ``terms_required`` is exactly what accepting answers, so it passes; no
    provider passes. The provider's fault policy is ``_provider_decision``'s.
    """
    decision = _provider_decision(user)
    if not decision or not decision.get("required"):
        return
    reason = str(decision.get("reason") or TERMS_REQUIRED)
    if reason in REFUSAL_CODES and reason != TERMS_REQUIRED:
        raise refusal(reason, decision.get("version"))


def enforce(request: Any, user: Any) -> None:
    """Refuse ``user`` (403) while the terms are outstanding.

    Called once, by ``resolve_platform_user_unfloored``, after the principal is
    fully resolved. No-op for an agent's credential, for anything that is not
    a visit (``_is_visit``: a token without the ``interactive`` claim, a
    machine credential of operator-and-above), and on a route marked
    ``@terms_route``.
    """
    # Late imports: `dependencies` imports this module.
    from dependencies import TERMS_ROUTE_ATTR
    from services import terms_service

    if not _is_person(user):
        return
    if not _is_visit(user):
        return
    route = request.scope.get("route") if getattr(request, "scope", None) else None
    if getattr(getattr(route, "endpoint", None), TERMS_ROUTE_ATTR, None):
        return

    decision = _provider_decision(user)
    if decision is None:
        try:
            decision = terms_service.decision(user)
        except Exception:  # noqa: BLE001 — fail CLOSED
            logger.exception(
                "[terms_gate] the instance decision failed for user %s; refusing (fail-closed)",
                getattr(user, "id", None),
            )
            raise refusal(TERMS_REQUIRED, None)
    if not decision.get("required"):
        return
    raise refusal(str(decision.get("reason") or TERMS_REQUIRED), decision.get("version"))
