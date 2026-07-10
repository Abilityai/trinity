"""Tests for #847 Phase 0 enterprise seam.

Verifies:

1. ``EntitlementService.is_entitled`` returns True for every feature
   in the default (stub) configuration.
2. ``TRINITY_OSS_ONLY=1`` flips every check to False — the
   ``oss_only`` deny path.
3. ``requires_entitlement(feature_id)`` raises HTTP 403 when the
   service denies and returns None on allow.
4. ``list_entitled_features()`` reports the known feature IDs in
   the OSS build.
5. ``_set_for_testing`` cleanly swaps the singleton.

The conditional ``register_enterprise`` import in ``main.py`` is
covered by an integration test (live container) rather than a unit
test — the unit path can't faithfully simulate the submodule's
absence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# -----------------------------------------------------------------------------
# EntitlementService default behaviour
# -----------------------------------------------------------------------------


def test_empty_registry_denies_every_feature(monkeypatch):
    """Default (no `register_module` calls): everything denied.
    This is the OSS-only build state — no enterprise submodule was
    mounted, so `register_enterprise(app)` never ran, so the
    registry stays empty."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    assert svc.is_entitled("sso") is False
    assert svc.is_entitled("scim") is False
    assert svc.is_entitled("siem") is False
    assert svc.list_entitled_features() == []


def test_register_module_then_entitled(monkeypatch):
    """After `register_module("audit")`, "audit" is entitled and listed."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    svc.register_module("audit")
    svc.register_module("scim")

    assert svc.is_entitled("audit") is True
    assert svc.is_entitled("scim") is True
    assert svc.is_entitled("siem") is False  # not registered
    assert svc.list_entitled_features() == ["audit", "scim"]  # sorted


def test_register_module_is_idempotent(monkeypatch):
    """Calling register_module twice with the same id doesn't grow
    the list (idempotency contract from the docstring)."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    svc.register_module("audit")
    svc.register_module("audit")  # second call should be a no-op
    assert svc.list_entitled_features() == ["audit"]


# -----------------------------------------------------------------------------
# TRINITY_OSS_ONLY=1 deny path
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_oss_only_denies_every_feature_even_when_registered(monkeypatch, value):
    """TRINITY_OSS_ONLY hard-overrides the registry. Even after
    `register_module("audit")`, the deny path fires."""
    monkeypatch.setenv("TRINITY_OSS_ONLY", value)
    monkeypatch.delitem(sys.modules, "services.entitlement_service", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    svc.register_module("audit")  # the override wins regardless
    assert svc.is_entitled("audit") is False
    assert svc.list_entitled_features() == []


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_oss_only_falsy_keeps_registry_behaviour(monkeypatch, value):
    """Falsy spellings leave the registry behaviour intact."""
    monkeypatch.setenv("TRINITY_OSS_ONLY", value)
    monkeypatch.delitem(sys.modules, "services.entitlement_service", raising=False)
    from services.entitlement_service import EntitlementService

    svc = EntitlementService()
    assert svc.is_entitled("audit") is False  # nothing registered yet
    svc.register_module("audit")
    assert svc.is_entitled("audit") is True


# -----------------------------------------------------------------------------
# `requires_entitlement` dependency factory
# -----------------------------------------------------------------------------


def _import_requires_entitlement_or_skip(monkeypatch):
    """Import ``dependencies.requires_entitlement`` or skip if backend
    venv isn't available locally (e.g. ``passlib`` missing in a stub
    dev environment). CI installs the full backend deps so this
    skip never fires there.

    Caller passes the test's monkeypatch fixture so the stale
    `dependencies` sys.modules entry (left by another test in the
    same session) is removed via the auto-restoring API rather than
    a bare `del sys.modules[...]` (which the lint check forbids)."""
    monkeypatch.delitem(sys.modules, "dependencies", raising=False)
    try:
        from dependencies import requires_entitlement
        return requires_entitlement
    except ImportError as e:
        pytest.skip(f"backend venv required (no `dependencies` import: {e})")


def test_requires_entitlement_allows_when_entitled(monkeypatch):
    """The Depends() callable returns None on allow. Allow path
    requires registering the module first — empty registry denies."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    monkeypatch.delitem(sys.modules, "services.entitlement_service", raising=False)
    requires_entitlement = _import_requires_entitlement_or_skip(monkeypatch)

    # Register "audit" so the dependency allows the call.
    from services.entitlement_service import entitlement_service
    entitlement_service.register_module("audit")

    inner = requires_entitlement("audit")
    assert inner() is None


def test_requires_entitlement_raises_403_when_denied(monkeypatch):
    """Deny path raises HTTPException(403) with the feature_id in detail."""
    from fastapi import HTTPException

    monkeypatch.setenv("TRINITY_OSS_ONLY", "1")
    monkeypatch.delitem(sys.modules, "services.entitlement_service", raising=False)
    requires_entitlement = _import_requires_entitlement_or_skip(monkeypatch)

    inner = requires_entitlement("audit")
    with pytest.raises(HTTPException) as exc:
        inner()
    assert exc.value.status_code == 403
    assert "audit" in exc.value.detail


# -----------------------------------------------------------------------------
# _set_for_testing
# -----------------------------------------------------------------------------


def test_set_for_testing_swaps_singleton(monkeypatch):
    """Replacing the singleton lets tests force specific behaviour
    without monkeypatching the env or sys.modules."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    monkeypatch.delitem(sys.modules, "services.entitlement_service", raising=False)
    import services.entitlement_service as ent_mod

    class _StubFalse:
        def is_entitled(self, _feature_id):
            return False

        def list_entitled_features(self):
            return []

    ent_mod._set_for_testing(_StubFalse())
    try:
        assert ent_mod.entitlement_service.is_entitled("audit") is False
        assert ent_mod.entitlement_service.list_entitled_features() == []
    finally:
        ent_mod._set_for_testing(None)  # restore default
    # Restored — fresh default singleton has empty registry, so
    # still False until something calls register_module().
    assert ent_mod.entitlement_service.is_entitled("audit") is False
    ent_mod.entitlement_service.register_module("audit")
    assert ent_mod.entitlement_service.is_entitled("audit") is True


# -----------------------------------------------------------------------------
# Static check: main.py must import enterprise conditionally
# -----------------------------------------------------------------------------


def test_main_py_uses_conditional_enterprise_import():
    """The enterprise loader must be in a try/except ImportError so
    OSS-only builds (no submodule) start cleanly."""
    src = (_BACKEND / "main.py").read_text(encoding="utf-8")
    # Find the import line
    idx = src.find("from enterprise.backend import register_enterprise")
    assert idx != -1, (
        "main.py must import register_enterprise from `enterprise.backend` "
        "(the private repo is dual-mounted at src/backend/enterprise/ and "
        "src/frontend/src/enterprise/; backend Python imports the `backend/` "
        "subdir)"
    )
    # The preceding ~150 chars should contain `try:`
    window = src[max(0, idx - 200) : idx]
    assert "try:" in window, (
        "enterprise import must be inside a try/except ImportError block "
        "so OSS-only builds (no submodule) boot cleanly"
    )
    # And the trailing block should catch ImportError. Window is
    # generous (~1500 chars) because the success-branch body has
    # comment lines explaining the `print(..., flush=True)` rationale
    # — easy for a future tweak to push the `except` past a tight
    # search window without changing the contract this test is
    # actually guarding.
    tail = src[idx : idx + 1500]
    assert "except ImportError" in tail, (
        "enterprise import must be guarded by `except ImportError`"
    )


# -----------------------------------------------------------------------------
# Static check: enterprise submodule registers `audit`, not the old SSO stub
# -----------------------------------------------------------------------------


def test_submodule_registers_audit_and_sso():
    """The enterprise submodule registers `audit` (UI-gating only, via a direct
    `register_module("audit")`) and — since SSO-OIDC (#32) landed via #1303 — a
    real SSO implementation, wired the same way as the other real modules
    (`from .sso import register as register_sso` + `register_sso(app)`, which
    self-registers the `sso` feature_id inside `.sso.register`).

    This replaces the earlier `not_sso` pin from the #910/#941 era, when only
    the removed SSO PoC stub existed and the `.sso` package was asserted absent.

    Static check on `src/backend/enterprise/backend/__init__.py`. The
    submodule may not be checked out in every CI job (OSS-only build),
    so this test skips cleanly when the file is absent rather than
    failing in those jobs.
    """
    init_path = _BACKEND / "enterprise" / "backend" / "__init__.py"
    if not init_path.exists():
        pytest.skip("enterprise submodule not checked out (OSS-only build)")

    src = init_path.read_text(encoding="utf-8")

    assert 'register_module("audit")' in src, (
        "enterprise submodule must call register_module(\"audit\") so the "
        "/enterprise/audit dashboard route is entitled in licensed deploys"
    )
    # SSO-OIDC (#32) landed in the submodule via #1303. It self-registers the
    # `sso` feature_id inside `.sso.register` (mirroring the other real modules),
    # so `register_module("sso")` is NOT called here in __init__.py — the pin is
    # on the import + registration call instead.
    assert "from .sso import register as register_sso" in src, (
        "SSO-OIDC (#32) is implemented — __init__.py must import the .sso package"
    )
    assert "register_sso(app)" in src, (
        "SSO-OIDC must be registered on the enterprise app via register_sso(app)"
    )
