"""
Regression test for #1069 — VoIP outbound call always returned HTTP 422.

Bug: ``POST /api/agents/{agent_name}/voip/call`` declared its agent path
parameter via the ``AuthorizedAgent`` dependency, whose ``Path()`` parameter
is named ``name``. The route template, however, exposes the segment as
``{agent_name}`` (matching the sibling GET/PUT/DELETE ``/voip`` handlers,
which use the ``*_by_name`` dependency family). FastAPI therefore derived a
required path parameter ``name`` that no URL could ever populate, so every
call — REST and the ``call_user`` MCP tool that proxies to it — failed with::

    {"detail":[{"type":"missing","loc":["path","name"],"msg":"Field required"}]}

Fix: the handler uses ``AuthorizedAgentByName`` (``Path()`` param
``agent_name``) so the dependency's path parameter matches the template.

The first test loads the *real* ``routers/voip.py`` with the *real*
``dependencies`` module (only leaf services are stubbed) and asserts FastAPI's
flattened dependant for the POST route carries a path parameter ``agent_name``
and NOT ``name``. Stubbing the dependency aliases — as other router-signature
tests do — would mask the bug, because the defect lives in the ``Path()``
parameter name those aliases carry. A revert to ``AuthorizedAgent``
reintroduces the ``name`` path parameter and fails this test.

The second test pins the dependency-layer contract that makes the fix work
(``*_by_name`` → ``agent_name`` Path param; the bare aliases → ``name``).

Module under test: src/backend/routers/voip.py
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
import types
from pathlib import Path as _FsPath
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ── Environment / backend resolution ─────────────────────────────────────────
# database.py instantiates `db = DatabaseManager()` on import and tries to
# mkdir(/data); config.py raises if REDIS_URL lacks credentials. The unit
# conftest sets these too, but pin them here so this module is import-order
# self-sufficient (it sorts very early in the collection order).
os.environ.setdefault(
    "TRINITY_DB_PATH",
    str(_FsPath(tempfile.gettempdir()) / "trinity_test_voip_router_1069.db"),
)
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _find_backend_root() -> _FsPath:
    """Locate the backend source tree across host and in-container layouts."""
    candidates = [
        _FsPath(__file__).resolve().parent.parent.parent / "src" / "backend",  # host
        _FsPath("/app"),  # trinity-backend container
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, _FsPath(env_override))
    for c in candidates:
        if (c / "routers" / "voip.py").exists():
            return c
    raise RuntimeError("Cannot locate backend source tree (set TRINITY_BACKEND_PATH)")


_BACKEND = _find_backend_root()
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Modules this test stubs into sys.modules during the import-time router load
# below — restored synchronously inside `_load_voip_router()` and, as a
# belt-and-suspenders guard, snapshot/restored around every test by the autouse
# fixture. This named-helper pair is the sanctioned exemption from the
# tests/lint_sys_modules.py ban on bare sys.modules mutation (Issue #762),
# matching the precedent in tests/unit/test_telegram_webhook_backfill.py.
_STUBBED_MODULE_NAMES = [
    "services",
    "services.voip_service",
    "services.idempotency_service",
    "services.settings_service",
    "services.platform_audit_service",
    "routers.voip",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    The leaf-service stubs are installed and removed inside
    `_load_voip_router()` at import time (leaving real `dependencies`/`jose`
    untouched); this fixture guards against any per-test re-stub leaking into
    sibling test files in the same session.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ── Load routers/voip.py with leaf services stubbed but dependencies REAL ─────

def _load_voip_router() -> types.ModuleType:
    """Exec routers/voip.py directly, stubbing only its heavy leaf services.

    `dependencies` is deliberately left REAL: the bug is in the Path()
    parameter name carried by `AuthorizedAgent` vs `AuthorizedAgentByName`.
    `services/__init__.py` eagerly imports docker_service/template_service
    (Docker SDK), so the `services` package itself is replaced with a light
    stub package and the four submodules voip.py imports are attached to it.

    Stubs are installed/removed by manually saving and restoring ONLY the
    specific sys.modules keys we touch — NOT via `patch.dict`. `patch.dict`
    clears the *entire* sys.modules on exit and restores the snapshot taken at
    entry; real `dependencies` (→ `jose` → `cryptography`) is imported for the
    first time *inside* exec_module, so it would be wiped and later re-imported
    fresh, splitting cryptography's type identity and breaking jose's HMAC
    backend in later JWT tests (surfaced as JWSError "Expected instance of
    hashes.HashAlgorithm" in test_voice_auth / test_websocket_auth). Leaving
    the real modules imported during exec in place is both correct and the
    normal post-conftest state.
    """
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []  # mark as a package so submodule imports resolve

    voip_svc = types.ModuleType("services.voip_service")
    voip_svc.voip_service = MagicMock()
    voip_svc.normalize_e164 = lambda x: x

    idem_svc = types.ModuleType("services.idempotency_service")
    idem_svc.make_agent_scope = MagicMock()
    idem_svc.begin = MagicMock()
    idem_svc.complete = MagicMock()
    idem_svc.fail = MagicMock()

    settings_mod = types.ModuleType("services.settings_service")
    settings_mod.settings_service = MagicMock()

    audit_mod = types.ModuleType("services.platform_audit_service")
    audit_mod.platform_audit_service = MagicMock()
    audit_mod.AuditEventType = MagicMock()

    path = _BACKEND / "routers" / "voip.py"
    spec = importlib.util.spec_from_file_location("routers.voip", str(path))
    mod = importlib.util.module_from_spec(spec)

    stubs = {
        "services": services_pkg,
        "services.voip_service": voip_svc,
        "services.idempotency_service": idem_svc,
        "services.settings_service": settings_mod,
        "services.platform_audit_service": audit_mod,
        "routers.voip": mod,
    }
    # Save only the keys we touch; restore them in finally. Real modules that
    # voip.py imports during exec (dependencies, database, models, jose, …)
    # are intentionally left loaded.
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, original in saved.items():
            if original is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = original
    return mod


voip_router_mod = _load_voip_router()


def _find_call_route():
    """Return the APIRoute for POST .../voip/call on the voip auth_router."""
    for route in voip_router_mod.auth_router.routes:
        methods = getattr(route, "methods", set()) or set()
        if route.path.endswith("/voip/call") and "POST" in methods:
            return route
    raise AssertionError("POST .../voip/call route not found on voip auth_router")


# ── The regression: the shipped route resolves the agent from {agent_name} ───

class TestVoipCallPathParam:
    def test_route_template_uses_agent_name(self):
        route = _find_call_route()
        assert route.path.endswith("/{agent_name}/voip/call"), (
            f"route template changed unexpectedly: {route.path!r}"
        )

    def test_flat_path_params_are_agent_name_not_name(self):
        """FastAPI's flattened dependant must require `agent_name`, not `name`.

        This is the exact #1069 condition: a `name` path param under an
        `{agent_name}` template is unfillable → HTTP 422 on every call.
        """
        from fastapi.dependencies.utils import get_flat_dependant

        route = _find_call_route()
        flat = get_flat_dependant(route.dependant)
        path_param_names = {p.name for p in flat.path_params}

        assert "agent_name" in path_param_names, (
            f"POST /voip/call must resolve the agent from the {{agent_name}} "
            f"path segment; flattened path params were {path_param_names}"
        )
        assert "name" not in path_param_names, (
            "POST /voip/call still declares a `name` path param — this is the "
            "#1069 regression (AuthorizedAgent under an {agent_name} template). "
            f"Flattened path params: {path_param_names}"
        )


# ── Dependency-layer contract that makes the fix correct ─────────────────────

class TestAuthorizedAgentAliasContract:
    """Pin why the fix works: the alias families carry different Path names."""

    def test_by_name_dependency_uses_agent_name_path_param(self):
        import dependencies

        sig = inspect.signature(dependencies.get_authorized_agent_by_name)
        assert "agent_name" in sig.parameters
        assert "name" not in sig.parameters

    def test_bare_dependency_uses_name_path_param(self):
        import dependencies

        # The bare alias is correct for {name} routes (schedules, chat, …);
        # it is the WRONG choice under an {agent_name} template — that pairing
        # is precisely what caused #1069.
        sig = inspect.signature(dependencies.get_authorized_agent)
        assert "name" in sig.parameters
        assert "agent_name" not in sig.parameters
