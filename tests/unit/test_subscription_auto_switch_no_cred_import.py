"""Regression test for #606: the SUB-003 subscription auto-switch path must
not re-enter file-based credential injection for subscription-mode agents.

The chain under test (entered at ``_restart_agent``, the smallest entry
point that still exercises the real lifecycle injection logic)::

    subscription_auto_switch._restart_agent(agent_name)
        → container_stop(container)              # explicit stop
        → start_agent_internal(agent_name)
            → container_reload(container)        # marks .status = "stopped"
            → was_already_running = False        # so the #421 skip does NOT apply
            → inject_assigned_credentials(...)
                → db.get_agent_subscription_id() → "sub-new-id"
                → SHORT-CIRCUIT: returns {"status": "skipped",
                                          "reason": "subscription_mode", ...}

Load-bearing claim this test pins (must remain unmistakable to future
readers): on the auto-switch path, ``_restart_agent`` explicitly calls
``container_stop`` BEFORE ``start_agent_internal``. By the time
``start_agent_internal`` runs ``container_reload``, the container's
``.status`` is ``"stopped"``, so ``was_already_running`` is ``False`` and
the ``#421`` ``skip_injection`` branch does NOT apply.
``inject_assigned_credentials`` is therefore reached every time, and the
ONLY thing shielding us from the spurious ``.credentials.enc`` file-import
log is the subscription-mode short-circuit at
``services/agent_service/lifecycle.py:155``.

A leaf-level test on ``inject_assigned_credentials`` alone (already
covered by ``test_inject_assigned_credentials.py::
test_subscription_mode_skips_import_with_clear_reason``) would miss this
chain-level guarantee — a future refactor of ``_perform_auto_switch`` or
``_restart_agent`` could quietly bypass the guard without that leaf test
failing. This test pins the call chain end-to-end so the contract holds
regardless of how the auto-switch internals evolve.

Explicit non-claim: this is NOT a concurrency test. There is no per-agent
switch/restart lock around ``assign_subscription_to_agent`` +
``container_stop`` + ``start_agent_internal``, so concurrent 429s for the
same agent could race. That's its own concern, out of scope for #606.

Modules under test:
    src/backend/services/subscription_auto_switch.py::_restart_agent
    src/backend/services/agent_service/lifecycle.py::start_agent_internal
    src/backend/services/agent_service/lifecycle.py::inject_assigned_credentials
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)


# ─── sys.modules snapshot/restore (Issue #762) ───────────────────────────
#
# This file loads ``lifecycle.py`` and ``subscription_auto_switch.py`` in
# isolation with their heavy deps stubbed via ``sys.modules`` at IMPORT
# time — the lint's preferred ``monkeypatch.setitem`` is fixture-scoped
# and cannot rewind a stub planted before any test runs. We adopt the
# named snapshot/restore escape hatch documented in
# ``tests/lint_sys_modules.py`` (precedent:
# ``tests/unit/test_telegram_webhook_backfill.py``): the autouse fixture
# snapshots these slots at test setup and restores them at teardown so
# cross-file pollution is bounded to this module's loaders, not leaked
# into unrelated tests sharing the pytest session.

_STUBBED_MODULE_NAMES = [
    # _preload_credential_encryption
    "services",
    "services.credential_encryption",
    # _SYS_MOCKS (transient during _load_lifecycle, but defensive)
    "database",
    "docker",
    "services.docker_service",
    "services.docker_utils",
    "services.settings_service",
    "services.skill_service",
    "fastapi",
    # _load_lifecycle (private package + the public services.agent_service slot)
    "agent_service_pkg_under_test_606",
    "agent_service_pkg_under_test_606.helpers",
    "agent_service_pkg_under_test_606.read_only",
    "agent_service_pkg_under_test_606.file_sharing",
    "services.agent_service",
    "services.agent_service.helpers",
    "services.agent_service.read_only",
    "services.agent_service.file_sharing",
    # _load_auto_switch
    "services.subscription_auto_switch",
    "db_models",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    Bounds the blast radius of this file's module-loader stubs so they
    cannot leak into other test files in the same pytest session.
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


# ─── Module loaders ──────────────────────────────────────────────────────
#
# Mirrors the approach in ``test_inject_assigned_credentials.py``: load
# ``lifecycle.py`` in isolation with its heavy deps stubbed via sys.modules,
# so we exercise the REAL ``inject_assigned_credentials`` +
# ``start_agent_internal`` without paying for the full backend boot
# (which trips on the #589 REDIS_URL config check at import time).


def _preload_credential_encryption():
    """Return the in-process ``services.credential_encryption`` module,
    loading it once if needed. **Must be idempotent** — another unit-test
    file (``test_inject_assigned_credentials.py``) does the same preload
    dance, and an unconditional ``sys.modules[...] = mod`` overwrite here
    would invalidate that file's module-level reference and break its
    ``patch.object`` calls when tests share a pytest session.
    """
    services_stub = types.ModuleType("services")
    services_stub.__path__ = [os.path.join(_BACKEND, "services")]
    sys.modules.setdefault("services", services_stub)

    existing = sys.modules.get("services.credential_encryption")
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        "services.credential_encryption",
        os.path.join(_BACKEND, "services", "credential_encryption.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["services.credential_encryption"] = mod
    spec.loader.exec_module(mod)
    return mod


_credential_encryption = _preload_credential_encryption()


_mock_db = MagicMock()


class _HTTPException(Exception):
    def __init__(self, status_code: int = 500, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail


_SYS_MOCKS = {
    "database": Mock(db=_mock_db),
    "docker": Mock(),
    "services.docker_service": Mock(
        docker_client=Mock(),
        get_agent_container=Mock(),
    ),
    "services.docker_utils": Mock(
        container_stop=AsyncMock(),
        container_remove=AsyncMock(),
        container_start=AsyncMock(),
        container_reload=AsyncMock(),
        volume_get=AsyncMock(),
        volume_create=AsyncMock(),
        containers_run=AsyncMock(),
    ),
    "services.settings_service": Mock(
        get_anthropic_api_key=Mock(return_value=""),
        get_github_pat=Mock(return_value=""),
        get_agent_full_capabilities=Mock(return_value=False),
    ),
    "services.skill_service": Mock(skill_service=MagicMock()),
    "fastapi": Mock(HTTPException=_HTTPException),
}


def _load_lifecycle():
    """Load ``lifecycle.py`` under a private package name so this test's
    copy is independent of any other test file that loads lifecycle.

    The ``services.agent_service`` slot is intentionally left populated
    after this function returns: ``subscription_auto_switch._restart_agent``
    does a lazy ``from services.agent_service import start_agent_internal``,
    and we attach our loaded ``start_agent_internal`` to that package below.
    """
    pkg_name = "agent_service_pkg_under_test_606"
    pkg_spec = importlib.util.spec_from_loader(pkg_name, loader=None, is_package=True)
    pkg = importlib.util.module_from_spec(pkg_spec)
    pkg.__path__ = [os.path.join(_BACKEND, "services", "agent_service")]
    sys.modules[pkg_name] = pkg

    helpers_mod = Mock(
        check_shared_folder_mounts_match=AsyncMock(return_value=True),
        check_api_key_env_matches=Mock(return_value=True),
        check_github_pat_env_matches=Mock(return_value=True),
        check_resource_limits_match=Mock(return_value=True),
        check_full_capabilities_match=Mock(return_value=True),
        check_guardrails_env_matches=Mock(return_value=True),
        # #1809: async — a bare Mock auto-attr is not awaitable and would fail
        # the restart chain with "object Mock can't be used in 'await'".
        check_base_image_matches=AsyncMock(return_value=True),
        validate_base_image=Mock(),
    )
    read_only_mod = Mock(inject_read_only_hooks=AsyncMock(return_value={"success": True}))
    file_sharing_mod = Mock(check_public_folder_mount_matches=Mock(return_value=True))

    pkg.get_accessible_agents = Mock(return_value=[])
    pkg.get_agent_owner_id = Mock(return_value=1)
    pkg.list_agents_data = Mock(return_value=[])

    sys.modules[f"{pkg_name}.helpers"] = helpers_mod
    sys.modules[f"{pkg_name}.read_only"] = read_only_mod
    sys.modules[f"{pkg_name}.file_sharing"] = file_sharing_mod

    if _BACKEND not in sys.path:
        sys.path.insert(0, _BACKEND)

    # #2140: the `services.agent_service*` slots are needed only while
    # lifecycle.py executes (line 27 imports from them absolutely) and used to be
    # left populated deliberately. That deliberate choice is what broke
    # `claude_only` filtering everywhere downstream —
    # `services/compatibility/spec.py` resolves `is_claude_runtime` from that
    # module lazily, so the stub answered for the rest of the session.
    from conftest import stubbed_modules

    agent_service_aliases = {
        "services.agent_service": pkg,
        "services.agent_service.helpers": helpers_mod,
        "services.agent_service.read_only": read_only_mod,
        "services.agent_service.file_sharing": file_sharing_mod,
    }
    with stubbed_modules({**_SYS_MOCKS, **agent_service_aliases}):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.lifecycle",
            os.path.join(_BACKEND, "services", "agent_service", "lifecycle.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_lifecycle()


def _load_auto_switch():
    """Import ``services.subscription_auto_switch`` with ``database`` stubbed
    so import is side-effect-free.

    ``db_models`` is pure Pydantic with no heavy deps — we import the real
    one if absent, rather than stubbing. A bare stub would persist in
    ``sys.modules`` and break unrelated tests that later need
    ``db_models.UserCreate`` or similar (e.g.
    ``test_subscription_auto_switch_pingpong.py``'s ``tmp_db`` fixture
    loads ``db.users`` transitively).
    """
    db_module = types.ModuleType("database")
    db_module.db = _mock_db
    sys.modules["database"] = db_module

    if "db_models" not in sys.modules:
        if _BACKEND not in sys.path:
            sys.path.insert(0, _BACKEND)
        import db_models  # noqa: F401  — registers in sys.modules

    sys.modules.pop("services.subscription_auto_switch", None)
    import services.subscription_auto_switch as auto_switch  # noqa: WPS433
    importlib.reload(auto_switch)
    return auto_switch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset():
    _mock_db.reset_mock()
    _mock_db.get_agent_subscription_id.return_value = None
    _mock_db.get_read_only_mode.return_value = {"enabled": False}
    _mock_db.get_agent_skill_names.return_value = []
    # Re-stamp the database stub each test — other unit files evict the
    # ``database`` slot in their teardown, and lifecycle's lazy
    # ``from database import db`` would otherwise resolve to a fresh real
    # ``database.py`` against the real DB.
    sys.modules["database"] = _SYS_MOCKS["database"]


def test_auto_switch_restart_chain_does_not_invoke_credential_import():
    """SUB-003 auto-switch path: the full chain entry at ``_restart_agent``
    must short-circuit credential injection cleanly for a subscription-mode
    agent.

    Pins the chain end-to-end. The companion leaf test in
    ``test_inject_assigned_credentials.py`` covers the short-circuit in
    isolation; this test pins that the chain ACTUALLY reaches the
    short-circuit on the auto-switch path — without it, a future refactor
    of ``_perform_auto_switch`` or ``_restart_agent`` could quietly bypass
    the guard.

    The ``reason == "subscription_mode"`` assertion is load-bearing: it
    proves the line-155 short-circuit fired, NOT the ``#421``
    ``container_already_running`` skip. A refactor that moved the
    short-circuit to a different branch (e.g. coalesced it with ``#421``)
    would silently pass without that specific assertion.
    """
    # Subscription has just been assigned by `_perform_auto_switch`.
    _mock_db.get_agent_subscription_id.return_value = "sub-new-id"

    # Sentinel: import_to_agent must NEVER be awaited on this chain. Wired
    # via the encryption service so we'd see it even if the short-circuit
    # regresses by one layer (e.g. somebody guards the retry loop but not
    # the ``get_credential_encryption_service`` call).
    sentinel_should_not_be_called = AsyncMock(
        side_effect=AssertionError(
            "import_to_agent must not be called on the auto-switch chain "
            "for a subscription-mode agent — the lifecycle.py:155 "
            "short-circuit failed to fire"
        )
    )
    mock_encryption_service = MagicMock()
    mock_encryption_service.import_to_agent = sentinel_should_not_be_called

    # Mock container starts as "running" so `_restart_agent` proceeds past
    # its early-return guard, then flips to "stopped" when
    # `container_reload` runs inside `start_agent_internal` — so
    # `was_already_running` is False and the #421 skip path does NOT fire.
    mock_container = MagicMock()
    mock_container.status = "running"

    async def reload_side_effect(c):
        c.status = "stopped"

    # Lazy imports that ``_restart_agent`` performs inside its body.
    docker_service_stub = Mock(
        get_agent_container=Mock(return_value=mock_container),
        get_agent_status_from_container=Mock(return_value=Mock(status="running")),
    )
    docker_utils_stub = Mock(container_stop=AsyncMock())

    # ``_restart_agent`` does ``from services.agent_service import
    # start_agent_internal``. Attach our loaded lifecycle's
    # ``start_agent_internal`` so the chain actually executes (rather than
    # picking up a fresh-imported, unmocked copy).
    agent_service_pkg = sys.modules.get("services.agent_service")
    if agent_service_pkg is None:
        agent_service_pkg = types.ModuleType("services.agent_service")
        sys.modules["services.agent_service"] = agent_service_pkg
    agent_service_pkg.start_agent_internal = _mod.start_agent_internal

    # Capture inject_assigned_credentials' result so we can assert on
    # ``reason == "subscription_mode"`` specifically. The chain entry
    # point ``_restart_agent`` only returns the string ``"success"`` —
    # it discards ``start_agent_internal``'s richer return dict — so we
    # interpose a recording wrapper.
    captured: dict = {}
    real_inject = _mod.inject_assigned_credentials

    async def recording_inject(agent_name, **kwargs):
        result = await real_inject(agent_name, **kwargs)
        captured["credentials_result"] = result
        return result

    auto_switch = _load_auto_switch()

    with contextlib.ExitStack() as stack:
        # Stubs for subscription_auto_switch's lazy imports.
        stack.enter_context(patch.dict(
            sys.modules,
            {
                "services.docker_service": docker_service_stub,
                "services.docker_utils": docker_utils_stub,
                "services.agent_service": agent_service_pkg,
            },
        ))
        # Stubs the chain through start_agent_internal needs. These are
        # patched on the loaded ``_mod`` directly because lifecycle.py
        # captures these names into its module globals at import time.
        stack.enter_context(patch.object(
            _mod, "container_reload", AsyncMock(side_effect=reload_side_effect)
        ))
        stack.enter_context(patch.object(_mod, "container_start", AsyncMock()))
        stack.enter_context(patch.object(
            _mod, "wait_for_agent_ready", AsyncMock(return_value=True)
        ))
        stack.enter_context(patch.object(
            _mod, "inject_assigned_credentials", recording_inject
        ))
        stack.enter_context(patch.object(
            _mod, "inject_assigned_skills",
            AsyncMock(return_value={"status": "skipped"}),
        ))
        stack.enter_context(patch.object(
            _credential_encryption,
            "get_credential_encryption_service",
            return_value=mock_encryption_service,
        ))

        result = _run(auto_switch._restart_agent("sub-agent"))

    # 1. The restart chain ran cleanly end-to-end.
    assert result == "success", (
        f"Expected `_restart_agent` to return 'success', got {result!r}"
    )

    # 2. The sentinel was never awaited — the short-circuit fired before
    #    the file-import path was reached.
    sentinel_should_not_be_called.assert_not_awaited()

    # 3. Load-bearing reason check: the short-circuit that fired is
    #    specifically the line-155 subscription-mode guard — NOT the #421
    #    ``container_already_running`` skip. A future refactor that moved
    #    the short-circuit to a different branch (or coalesced it with
    #    #421) would silently pass without this assertion.
    creds = captured.get("credentials_result")
    assert creds is not None, "inject_assigned_credentials was not invoked"
    assert creds["status"] == "skipped", (
        f"Expected credentials_result.status == 'skipped', got {creds!r}"
    )
    assert creds["reason"] == "subscription_mode", (
        f"Expected credentials_result.reason == 'subscription_mode' "
        f"(the lifecycle.py:155 short-circuit), got reason="
        f"{creds.get('reason')!r}. If this says 'container_already_running' "
        f"the #421 skip fired instead — that branch does not apply on the "
        f"auto-switch path because `_restart_agent` explicitly stops the "
        f"container before `start_agent_internal`."
    )
