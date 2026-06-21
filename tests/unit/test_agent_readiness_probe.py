"""
Unit tests for the agent readiness probe (#406).

`wait_for_agent_ready` polls an agent's /health until 200 or timeout.
Callers treat False as "proceed anyway and let downstream retries cope" —
the probe itself must never raise.
"""

import asyncio
import importlib.util
import os
import sys

import httpx
import pytest

# Resolve backend path whether the test runs from the repo
# (repo/tests/unit/*.py → repo/src/backend) or inside a container where /app
# is the backend root.
_candidates = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")),
    "/app",
]
_backend = next(
    (p for p in _candidates if os.path.isfile(os.path.join(p, "services", "agent_service", "lifecycle.py"))),
    None,
)
assert _backend, "Could not locate backend path containing services/agent_service/lifecycle.py"
if _backend not in sys.path:
    sys.path.insert(0, _backend)

# Load lifecycle module directly; loading through `services.*` would pull in
# docker/db/redis init, which we don't need for a pure async function test.
_spec = importlib.util.spec_from_file_location(
    "agent_service_lifecycle",
    os.path.join(_backend, "services", "agent_service", "lifecycle.py"),
)
# The module imports `from services.docker_service import ...` etc at top level,
# so we stub those before exec_module instead of loading the package directly.


@pytest.fixture
def wait_for_agent_ready(monkeypatch):
    """Load `wait_for_agent_ready` in isolation with its heavy deps stubbed out."""
    import types
    # Stub heavy imports the module pulls at top level.
    stubs = {
        "database": types.SimpleNamespace(db=None),
        "services.docker_service": types.SimpleNamespace(
            docker_client=None, get_agent_container=lambda _n: None
        ),
        "services.docker_utils": types.SimpleNamespace(
            container_stop=None, container_remove=None, container_start=None,
            container_reload=None, volume_get=None, volume_create=None,
            containers_run=None,
        ),
        "services.agent_service.helpers": types.SimpleNamespace(
            validate_base_image=None,
            check_shared_folder_mounts_match=None,
            check_api_key_env_matches=None,
            check_github_pat_env_matches=None,
            check_resource_limits_match=None,
            check_full_capabilities_match=None,
            check_guardrails_env_matches=None,
            check_agent_auth_token_env_matches=None,  # added to lifecycle.py import (#1159)
            is_claude_runtime=None,  # added to lifecycle.py import (#1187)
        ),
        "services.settings_service": types.SimpleNamespace(
            get_anthropic_api_key=None, get_github_pat=None,
            get_agent_full_capabilities=None,
            get_agent_default_resources=None,  # added to lifecycle.py import (#725)
        ),
        "services.skill_service": types.SimpleNamespace(skill_service=None),
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # read_only is a relative import — can't stub via sys.modules easily.
    # Instead, eval the module with an injected __name__ that avoids the relative
    # import by loading just the function text.
    src_path = os.path.join(_backend, "services", "agent_service", "lifecycle.py")

    # Simplest: exec the file under a fake package context.
    pkg = types.ModuleType("services.agent_service")
    pkg.__path__ = [os.path.join(_backend, "services", "agent_service")]
    monkeypatch.setitem(sys.modules, "services.agent_service", pkg)
    monkeypatch.setitem(sys.modules, "services.agent_service.read_only",
                        types.SimpleNamespace(inject_read_only_hooks=None,
                                              remove_read_only_hooks=None))

    spec = importlib.util.spec_from_file_location(
        "services.agent_service.lifecycle", src_path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.wait_for_agent_ready


def _install_fake_httpx(monkeypatch, handler):
    """Patch httpx.AsyncClient so it returns responses produced by `handler`."""
    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_returns_true_when_agent_ready_immediately(wait_for_agent_ready, monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"status": "healthy"})

    _install_fake_httpx(monkeypatch, handler)
    result = asyncio.run(wait_for_agent_ready("bot", timeout_s=5, poll_interval_s=0.01))
    assert result is True


def test_returns_true_after_initial_connect_errors(wait_for_agent_ready, monkeypatch):
    """Simulate FastAPI not listening for first 2 polls, then becomes ready."""
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("not listening yet")
        return httpx.Response(200, json={"status": "healthy"})

    _install_fake_httpx(monkeypatch, handler)
    result = asyncio.run(wait_for_agent_ready("bot", timeout_s=5, poll_interval_s=0.01))
    assert result is True
    assert attempts["n"] == 3


def test_returns_false_on_timeout(wait_for_agent_ready, monkeypatch):
    def handler(req):
        raise httpx.ConnectError("server never starts")

    _install_fake_httpx(monkeypatch, handler)
    result = asyncio.run(wait_for_agent_ready("bot", timeout_s=0.1, poll_interval_s=0.02))
    assert result is False


def test_unexpected_exception_swallowed(wait_for_agent_ready, monkeypatch):
    """Any exception during poll (not just Connect/Read) must not bubble."""
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("weird transport error")
        return httpx.Response(200, json={"status": "healthy"})

    _install_fake_httpx(monkeypatch, handler)
    result = asyncio.run(wait_for_agent_ready("bot", timeout_s=5, poll_interval_s=0.01))
    assert result is True


def test_non_200_response_keeps_polling(wait_for_agent_ready, monkeypatch):
    """500/503 responses from startup should not be treated as ready."""
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "healthy"})

    _install_fake_httpx(monkeypatch, handler)
    result = asyncio.run(wait_for_agent_ready("bot", timeout_s=5, poll_interval_s=0.01))
    assert result is True
    assert attempts["n"] == 3
