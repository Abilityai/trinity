"""A2A inbound allow-list gate — the OSS seam the enterprise A2A module plugs into.

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
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class A2AAllowlistProvider(Protocol):
    def is_inbound_allowed(self, agent_name: str, caller_identity: str) -> bool:
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
