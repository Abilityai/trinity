"""Regression tests for #612: agent start response no longer surfaces a
misleading ``credentials_injection: failed`` for subscription-mode agents.

The original bug: ``inject_assigned_credentials`` ran unconditionally on
agent start and tried to import ``.credentials.enc``. Subscription-mode
agents authenticate via ``CLAUDE_CODE_OAUTH_TOKEN`` env var (SUB-002) and
typically have no ``.credentials.enc``. The import raised a plain
``ValueError("No .credentials.enc file found...")`` whose message did not
match the substring ``"not found"`` that the caller used to detect the
absent-file case — so the failure leaked through as
``{"status": "failed", "error": "..."}`` even though nothing was wrong.

Two-part fix verified here:

1. ``CredentialsFileNotFoundError`` (a ``ValueError`` subclass) is now
   raised when ``.credentials.enc`` is absent, and the caller catches it
   explicitly. No more substring-match fragility.

2. Subscription-mode agents short-circuit *before* the import path runs,
   with a clear ``reason: "subscription_mode"`` so operators know the skip
   is by design.

Module under test:
    src/backend/services/agent_service/lifecycle.py::inject_assigned_credentials
    src/backend/services/credential_encryption.py::CredentialsFileNotFoundError
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)


def _preload_credential_encryption():
    """Load ``credential_encryption.py`` in isolation and register it as
    ``services.credential_encryption`` in sys.modules — without executing
    the real ``services/__init__.py`` (which pulls in ``config.py`` and
    blows up under the #589 REDIS_URL config check that's only present at
    deploy time, not in test).

    Stubs the ``services`` parent package as an empty module first so the
    full-name registration is well-formed for any later import lookups.
    """
    services_stub = types.ModuleType("services")
    services_stub.__path__ = [os.path.join(_BACKEND, "services")]
    sys.modules.setdefault("services", services_stub)

    spec = importlib.util.spec_from_file_location(
        "services.credential_encryption",
        os.path.join(_BACKEND, "services", "credential_encryption.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["services.credential_encryption"] = mod
    spec.loader.exec_module(mod)
    return mod


_credential_encryption = _preload_credential_encryption()


# ── Loader ───────────────────────────────────────────────────────────────
# The lifecycle module imports `database`, `services.skill_service`,
# `services.docker_service`, `services.docker_utils`, etc. We don't need
# them for these tests — only ``inject_assigned_credentials`` and the
# encryption service. Stub the rest so import succeeds.

_mock_db = MagicMock()
_mock_skill_service = MagicMock()


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
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
    "services.skill_service": Mock(skill_service=_mock_skill_service),
    "fastapi": Mock(HTTPException=_HTTPException),
}


def _load_lifecycle():
    pkg_name = "agent_service_pkg_under_test_612"
    pkg_spec = importlib.util.spec_from_loader(
        pkg_name, loader=None, is_package=True
    )
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
        validate_base_image=Mock(),
    )
    read_only_mod = Mock(inject_read_only_hooks=AsyncMock(return_value={"success": True}))
    file_sharing_mod = Mock(check_public_folder_mount_matches=Mock(return_value=True))

    sys.modules[f"{pkg_name}.helpers"] = helpers_mod
    sys.modules[f"{pkg_name}.read_only"] = read_only_mod
    sys.modules[f"{pkg_name}.file_sharing"] = file_sharing_mod
    sys.modules["services.agent_service"] = pkg
    sys.modules["services.agent_service.helpers"] = helpers_mod
    sys.modules["services.agent_service.read_only"] = read_only_mod
    sys.modules["services.agent_service.file_sharing"] = file_sharing_mod

    # Add backend dir to path so credential_encryption (a non-package
    # module) imports succeed inside lifecycle.
    if _BACKEND not in sys.path:
        sys.path.insert(0, _BACKEND)

    # Install the heavy-dependency mocks permanently in sys.modules. We
    # cannot use ``with patch.dict(...)`` here because lifecycle does its
    # imports lazily inside the function body (e.g. ``from database import
    # db`` is evaluated at call-time, after the patch context has exited).
    # Install once and let later test calls hit the cached mocks.
    sys.modules.update(_SYS_MOCKS)

    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.lifecycle",
        os.path.join(_BACKEND, "services", "agent_service", "lifecycle.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_lifecycle()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset():
    _mock_db.reset_mock()
    # Default: not a subscription agent.
    _mock_db.get_agent_subscription_id.return_value = None


# ── Subscription-mode short-circuit (#612) ───────────────────────────────


def _patch_get_service(**kwargs):
    """Convenience wrapper around ``patch.object`` for the pre-loaded
    encryption module — avoids the string-form ``patch(...)`` which would
    trigger loading the real ``services/__init__.py`` and crash on
    ``config.py``'s deploy-time REDIS_URL check."""
    return patch.object(
        _credential_encryption, "get_credential_encryption_service", **kwargs
    )


def test_subscription_mode_skips_import_with_clear_reason():
    """The defining condition of #612: when an agent has a subscription
    assigned, file-based credential injection is irrelevant and must NOT
    surface as ``failed``. The skip reason must be unambiguous so operators
    don't try to re-assign or recreate the container thinking something
    broke."""
    _mock_db.get_agent_subscription_id.return_value = "sub-abc-123"

    # If this fires, we've regressed — subscription agents must short-circuit
    # before the encryption service is even consulted.
    sentinel_should_not_be_called = AsyncMock(
        side_effect=AssertionError("import_to_agent must not be called for subscription agents")
    )
    mock_service = MagicMock()
    mock_service.import_to_agent = sentinel_should_not_be_called

    with _patch_get_service(return_value=mock_service):
        result = _run(_mod.inject_assigned_credentials("sub-agent"))

    sentinel_should_not_be_called.assert_not_awaited()
    assert result["status"] == "skipped"
    assert result["reason"] == "subscription_mode"
    # Reason text should mention the env-var path so operators see WHY this
    # is a skip-by-design and not an error to investigate.
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.get("detail", "")


# ── File-missing case via the new exception subclass (#612) ──────────────


def test_credentials_file_missing_returns_skipped():
    """When the file is genuinely absent, return skipped — not failed.
    Previously masked by a ``"not found" in str(e)`` substring check that
    didn't match the actual error message ``"No .credentials.enc file found
    in agent workspace"``."""
    mock_service = MagicMock()
    mock_service.import_to_agent = AsyncMock(
        side_effect=_credential_encryption.CredentialsFileNotFoundError(
            "No .credentials.enc file found in agent workspace"
        )
    )

    with _patch_get_service(return_value=mock_service):
        result = _run(_mod.inject_assigned_credentials("fresh-agent"))

    assert result["status"] == "skipped"
    assert result["reason"] == "no_credentials_enc_file"
    assert "error" not in result  # no fake failure leaking through


def test_credentials_file_not_found_error_subclasses_value_error():
    """Subclass relationship is load-bearing: the admin import endpoint at
    ``routers/credentials.py`` does ``except ValueError`` and surfaces a
    400. That path must keep working when the file is missing — operator
    explicitly asked for an import, no file is a real error there. Only
    the auto-import startup path treats absent file as a clean skip."""
    err = _credential_encryption.CredentialsFileNotFoundError("...")
    assert isinstance(err, ValueError)


# ── Successful import (regression check) ─────────────────────────────────


def test_successful_import_returns_success_with_files():
    """The happy path must keep working: a real .credentials.enc returns
    success and the imported file list."""
    mock_service = MagicMock()
    mock_service.import_to_agent = AsyncMock(
        return_value={".env": "K=V", ".mcp.json": "{}"}
    )

    with _patch_get_service(return_value=mock_service):
        result = _run(_mod.inject_assigned_credentials("regular-agent"))

    assert result["status"] == "success"
    assert result["credential_count"] == 2
    assert set(result["files"]) == {".env", ".mcp.json"}


def test_empty_files_dict_returns_skipped():
    """``import_to_agent`` returning an empty dict (file existed but
    decrypted to nothing) is treated as no-op skip rather than a failure —
    the operator has nothing to fix and the response should reflect that."""
    mock_service = MagicMock()
    mock_service.import_to_agent = AsyncMock(return_value={})

    with _patch_get_service(return_value=mock_service):
        result = _run(_mod.inject_assigned_credentials("empty-blob-agent"))

    assert result["status"] == "skipped"
    assert result["reason"] == "no_credentials_enc_file"


# ── Other failure shapes (must still retry + surface) ────────────────────


def test_other_value_error_retries_and_returns_failed():
    """ValueError shapes other than file-missing (e.g. corrupt blob,
    decrypt failure) keep the existing retry semantics. They surface as
    failed only after exhausting retries — some are transient (e.g. agent
    HTTP not yet ready)."""
    mock_service = MagicMock()
    mock_service.import_to_agent = AsyncMock(side_effect=ValueError("bad ciphertext"))

    with _patch_get_service(return_value=mock_service):
        result = _run(
            _mod.inject_assigned_credentials("corrupt-agent", max_retries=2, retry_delay=0)
        )

    assert result["status"] == "failed"
    assert "bad ciphertext" in result["error"]
    # Retried up to max_retries times.
    assert mock_service.import_to_agent.await_count == 2


def test_transient_error_recovers_on_retry():
    """A first attempt that raises a transient error should not surface as
    failed if a subsequent attempt succeeds — the retry loop's contract."""
    mock_service = MagicMock()
    mock_service.import_to_agent = AsyncMock(
        side_effect=[
            ValueError("temporarily unavailable"),
            {".env": "K=V"},
        ]
    )

    with _patch_get_service(return_value=mock_service):
        result = _run(
            _mod.inject_assigned_credentials("flaky-agent", max_retries=2, retry_delay=0)
        )

    assert result["status"] == "success"
    assert result["credential_count"] == 1


def test_encryption_not_configured_returns_skipped():
    """If the encryption key isn't set, the service constructor raises
    ValueError. That's a deliberate skip (encryption is optional) — the
    response must say so explicitly so it's not confused with a real
    failure."""
    with _patch_get_service(side_effect=ValueError("CREDENTIAL_ENCRYPTION_KEY env var not set")):
        result = _run(_mod.inject_assigned_credentials("any-agent"))

    assert result["status"] == "skipped"
    assert result["reason"] == "encryption_not_configured"
