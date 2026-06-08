"""Entitlement Service — gates enterprise features at runtime (#847 seam).

Phase 0 stub. Answers ``is_entitled(feature_id)`` for the
``requires_entitlement`` FastAPI dependency in ``dependencies.py``.

This stub returns True for every feature_id. The seam exists so
enterprise routers can be wired into the app today (via the private
submodule at ``src/backend/enterprise/``) without each call site needing
a license-check conditional bolted on later. When the real license
mechanism lands (Phase 1 — Ed25519-signed token + offline verify), this
class gets the verification logic and every gated endpoint picks it up
with zero diff at the call site.

Why a class and not a module function:

* Phase 1 needs cached state (license blob, parsed claims, expiry,
  grace window). A class is the natural home.
* Tests can swap the singleton via ``_set_for_testing(service)`` without
  touching ``sys.modules``.

Public API
----------
* ``entitlement_service`` — module-level singleton, imported by the
  ``requires_entitlement`` dependency
* ``EntitlementService.is_entitled(feature_id: str) -> bool``
* ``EntitlementService.list_entitled_features() -> list[str]``

Read by:
* ``dependencies.requires_entitlement(...)`` — per-request gate
* ``routers/settings.get_feature_flags`` — UI hide/show signal
* (future) MCP server via ``GET /api/internal/entitlements``
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class EntitlementService:
    """In-process entitlement check + module registry.

    Backed by an internal set of *registered* enterprise modules
    populated when ``enterprise.backend.register_enterprise(app)``
    runs in ``main.py``. OSS-only builds never call
    ``register_module()`` → the registry stays empty → both
    ``is_entitled()`` and ``list_entitled_features()`` deny everything
    → the OSS frontend's `enterprise_features`-driven UI hides every
    enterprise nav entry / login button / view automatically (same
    pattern as `session_tab_enabled` and `voice_available`).

    ``TRINITY_OSS_ONLY=1`` is a hard override that empties the
    registry-derived list even when the enterprise submodule IS
    mounted — useful for operators who want a compliance lockdown
    or for CI builds exercising the deny path.

    Phase 1 will layer a license-claim check on top of the registry:
    ``is_entitled(f) = f in registered AND f in license_claims``.
    """

    def __init__(self) -> None:
        # When TRINITY_OSS_ONLY=1, deny every feature regardless of
        # what's registered. Use cases: operators running with the
        # submodule present but wanting compliance lockdown; CI
        # builds testing the deny path.
        self._oss_only = os.getenv("TRINITY_OSS_ONLY", "0").lower() in {"1", "true", "yes"}
        # Module registry. Populated by `register_module()` from
        # `enterprise.backend.register_enterprise(app)`. OSS-only
        # builds never reach that code → stays empty → returns False
        # from `is_entitled()` for every feature.
        self._registered_modules: set[str] = set()
        if self._oss_only:
            logger.info(
                "[EntitlementService] TRINITY_OSS_ONLY=1 — all enterprise "
                "features will report as not-entitled"
            )

    def register_module(self, feature_id: str) -> None:
        """Register an enterprise module by its feature_id.

        Called from the private repo's
        ``enterprise.backend.register_enterprise(app)`` for each
        module it mounts. The registry drives
        ``list_entitled_features()`` so the OSS frontend hides
        surfaces for features that aren't actually present in this
        build.

        Idempotent — safe to call twice with the same feature_id.
        """
        if feature_id in self._registered_modules:
            return
        self._registered_modules.add(feature_id)
        logger.info(
            f"[EntitlementService] registered enterprise module: {feature_id!r} "
            f"(total: {len(self._registered_modules)})"
        )

    def is_entitled(self, feature_id: str) -> bool:
        """Return True if the named feature is licensed AND registered.

        Phase 0: True iff the feature is in the registry (registered
        by the private submodule on boot) AND OSS-only mode is not
        set. Phase 1: also cross-checks the license claim set.
        """
        if self._oss_only:
            return False
        return feature_id in self._registered_modules

    def list_entitled_features(self) -> list[str]:
        """Return the set of feature IDs this instance is entitled to.

        Used by ``GET /api/settings/feature-flags`` to drive UI tab
        visibility. Returns the registered modules in sorted order
        (deterministic for tests + UI ordering). OSS-only builds
        return ``[]`` because nothing ever registered.
        """
        if self._oss_only:
            return []
        return sorted(self._registered_modules)


# Module-level singleton — what `dependencies.requires_entitlement` calls.
entitlement_service = EntitlementService()


def _set_for_testing(service: Optional[EntitlementService]) -> None:
    """Replace the singleton. Test-only.

    Pass an instance with custom behaviour (e.g. ``is_entitled`` mocked
    to return False) to exercise the deny path without monkeypatching
    ``sys.modules`` or environment variables.

    Pass None to restore the default singleton.
    """
    global entitlement_service
    if service is None:
        entitlement_service = EntitlementService()
    else:
        entitlement_service = service
