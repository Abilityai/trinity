"""Workspace capability seam: per-person capability answers from a module.

The Workspace roster is the portal's only capability channel (#2128). Some
capabilities depend on data the OSS core does not hold — whether a given email
was invited to something a module owns. A module registers one provider per
capability name; the roster asks `has(name, email)`.

* OSS-only build → no provider → `has` returns False.
* Fails CLOSED: a provider that raises, or answers anything but True, is False.
  A capability bit that errs toward False hides an affordance; one that errs
  toward True advertises routes that would refuse the caller.

A seam file — its comments describe the mechanism only and are grepped by
`enterprise-docs-guard.yml` (#1461 class).
"""
from __future__ import annotations

import logging
from typing import Callable, Dict

logger = logging.getLogger(__name__)

CapabilityProvider = Callable[[str], bool]

_providers: Dict[str, CapabilityProvider] = {}


def register_provider(name: str, provider: CapabilityProvider) -> None:
    """Register the provider for one capability. Idempotent (last wins)."""
    _providers[name] = provider
    logger.info("[portal_capabilities] provider registered for %s", name)


def clear_providers() -> None:
    """Drop every provider — used by tests to restore the OSS path."""
    _providers.clear()


def has(name: str, email: str) -> bool:
    provider = _providers.get(name)
    if provider is None or not email:
        return False
    try:
        return provider(email) is True
    except Exception:  # noqa: BLE001 — fail closed
        logger.warning("[portal_capabilities] provider for %s failed", name, exc_info=True)
        return False
