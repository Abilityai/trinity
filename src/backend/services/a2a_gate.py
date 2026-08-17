"""A2A open-core seam — the hooks the enterprise A2A module plugs into.

Two independent providers, same contract shape:

* **inbound allow-list** (ent#157) — *which callers* may task an exposed agent.
* **exposed skills** (ent#180) — *which skills* an exposed agent advertises on
  its card. A disclosure control only: `message/send` dispatches free-form text,
  so advertising fewer skills never narrows what a caller may ask for.

Both fail open, and both are no-ops in an OSS build (no provider registered).

The public A2A JSON-RPC task endpoint (``routers/a2a.py``) must stay
edition-agnostic. Authenticated owner/shared access is the OSS baseline; an
enterprise module can additionally restrict *which* caller identities may task
an exposed agent via a per-agent inbound allow-list.

* OSS-only build → no provider registered → :func:`check_inbound_allowed`
  returns ``True`` for any already-authenticated owner/shared caller. Zero
  behavioural change (the exposure flag can't even be turned on without the
  entitled enterprise setter, so this path is only reachable in enterprise
  builds anyway).
* Enterprise build → a registered provider consults its own private policy
  store: an empty allow-list means "no extra restriction" (owner/shared is
  enough); a non-empty allow-list means the caller identity MUST be on it.

The provider holds the policy + the private table; this module knows only the
protocol:

    provider.is_inbound_allowed(agent_name: str, caller_identity: str) -> bool

Fail-open (Trinity's availability bias): a provider error never blocks an
already-authenticated owner/shared caller.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


class A2AAllowlistProvider(Protocol):
    def is_inbound_allowed(self, agent_name: str, caller_identity: str) -> bool:
        ...


class A2ASkillsProvider(Protocol):
    """Answers "which skills may this agent advertise?" (ent#180).

    Returns the allowed skill ids, or ``None`` for "no opinion" — which means
    advertise everything, the unconfigured default. ``[]`` is NOT None: it is
    an explicit "advertise nothing".
    """

    def exposed_skills(self, agent_name: str) -> Optional[List[str]]:
        ...


_provider: Optional[A2AAllowlistProvider] = None


def register_provider(provider: A2AAllowlistProvider) -> None:
    """Register the enterprise A2A allow-list provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[a2a_gate] provider registered: %s", type(provider).__name__)


def get_provider() -> Optional[A2AAllowlistProvider]:
    return _provider


def clear_provider() -> None:
    """Drop the provider — used by tests to restore the OSS no-op path."""
    global _provider
    _provider = None


_skills_provider: Optional[A2ASkillsProvider] = None


def register_skills_provider(provider: A2ASkillsProvider) -> None:
    """Register the enterprise exposed-skills provider. Idempotent (last wins)."""
    global _skills_provider
    _skills_provider = provider
    logger.info("[a2a_gate] skills provider registered: %s", type(provider).__name__)


def get_skills_provider() -> Optional[A2ASkillsProvider]:
    return _skills_provider


def clear_skills_provider() -> None:
    """Drop the skills provider — used by tests to restore the OSS no-op path."""
    global _skills_provider
    _skills_provider = None


def filter_exposed_skills(
    agent_name: str, skills: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Narrow a card's ``skills[]`` to the ones this agent may advertise (ent#180).

    A **disclosure** control, not an invocation boundary: A2A `message/send`
    dispatches free-form text (`execute_task`), so what is advertised has no
    bearing on what a caller may ask for. Filtering here changes what an
    orchestrator *sees* — worth doing because the well-known card is
    unauthenticated — and nothing else. Anyone reading this looking for the
    thing that stops an external caller reaching a capability: it isn't here,
    and it isn't anywhere yet (see requirements §32.4 FR-1).

    - No provider (OSS) → returned unchanged. OSS behaviour is identical by
      construction.
    - Provider returns ``None`` → no opinion → unchanged (the unconfigured
      default: an exposed agent advertises everything, as it always has).
    - Provider returns a list → keep only skills whose ``id`` is in it, in the
      card's original order. ``[]`` therefore advertises nothing — an explicit
      operator choice, distinct from ``None``. A stored id the template no
      longer declares simply matches nothing: the selection only subtracts, the
      template stays the source of truth for what exists (FR-4).
    - Provider error → unchanged + WARNING (fail-open, FR-5). Consistent with
      this module's availability bias and with "advertise all by default"; the
      honest trade is that an error over-discloses rather than silently
      emptying a card and breaking discovery invisibly. Acceptable *because*
      this is not a security boundary — it would not be if it were.
    """
    provider = _skills_provider
    if provider is None or not skills:
        return skills
    try:
        allowed = provider.exposed_skills(agent_name)
    except Exception:  # noqa: BLE001 — a policy error must not empty a card
        logger.warning(
            "[a2a_gate] skills provider error; advertising unfiltered for %s",
            agent_name,
            exc_info=True,
        )
        return skills
    if allowed is None:
        return skills
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        # A malformed return is a provider defect, so it takes the SAME
        # fail-open path as a raised error. Without this, a str would iterate
        # into single characters, match no skill id, and silently empty the
        # card — fail-closed, i.e. the opposite of the documented contract, and
        # invisible (an empty card looks like "no capabilities", not "bug").
        logger.warning(
            "[a2a_gate] skills provider returned %s (expected list/None); "
            "advertising unfiltered for %s",
            type(allowed).__name__,
            agent_name,
        )
        return skills
    allowed_ids = {str(s) for s in allowed}
    return [s for s in skills if str(s.get("id")) in allowed_ids]


def check_inbound_allowed(agent_name: str, caller_identity: str) -> bool:
    """Whether ``caller_identity`` may task ``agent_name`` over A2A inbound.

    The caller is ALREADY authenticated as an owner/shared identity for the
    agent by the router; this is the additional per-agent allow-list layer.
    No provider (OSS) → allow. Provider error → allow (fail-open).
    """
    provider = _provider
    if provider is None:
        return True
    try:
        return bool(provider.is_inbound_allowed(agent_name, caller_identity))
    except Exception:  # noqa: BLE001 — never block an authenticated caller on a policy error
        logger.warning("[a2a_gate] provider error; failing open for %s", agent_name, exc_info=True)
        return True
