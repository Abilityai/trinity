"""
GitHub PAT propagation service unit tests (#211).

Tests the pure-logic helpers (env patching) and the orchestration function
`propagate_github_pat` with docker_service, database, and httpx all mocked.
No running backend required.
"""
import importlib.util
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path for direct imports of `models`.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Stub utils.helpers (tests/utils shadows src/backend/utils in this env).
if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    sys.modules["utils.helpers"] = _helpers


# Stub heavy dependencies so the service module loads without docker / redis /
# a live DB, bypassing services/__init__.py. These stubs are installed ONLY for
# the duration of the `service` fixture (via patch.dict), never permanently:
# a permanent sys.modules install of the fake `services` package leaks into
# later test modules — e.g. test_whatsapp_adapter then fails importing
# container_get_archive from services.docker_utils against the fake namespace
# (#211 sys.modules-pollution fix).
_fake_database = types.ModuleType("database")
_fake_database.db = MagicMock()

_fake_services_pkg = types.ModuleType("services")
_fake_services_pkg.__path__ = [os.path.join(_backend_path, "services")]

_fake_docker_service = types.ModuleType("services.docker_service")
_fake_docker_service.list_all_agents_fast = MagicMock(return_value=[])
# #1264: github_pat_propagation_service now imports get_agent_container at module
# top, so the stub must expose it for the fresh module load to succeed.
_fake_docker_service.get_agent_container = MagicMock(return_value=None)

_STUB_MODULES = {
    "database": _fake_database,
    "services": _fake_services_pkg,
    "services.docker_service": _fake_docker_service,
}


def _load_service():
    """Load the service module directly, bypassing services/__init__.py."""
    path = os.path.join(
        _backend_path, "services", "github_pat_propagation_service.py"
    )
    spec = importlib.util.spec_from_file_location(
        "services.github_pat_propagation_service", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.github_pat_propagation_service"] = module
    spec.loader.exec_module(module)
    return module


# Override package-level fixtures that try to talk to a real backend.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


@pytest.fixture
def service():
    """Fresh service module loaded under confined sys.modules stubs.

    The heavy-dependency stubs are installed only for this fixture, and exactly
    the keys we touch are restored on teardown. This is a deliberate per-key
    save/restore rather than ``patch.dict(sys.modules, ...)``: patch.dict does a
    global ``sys.modules.clear()`` + rebuild on exit, which corrupts unrelated
    real modules (e.g. ``services.docker_utils`` for the later
    test_whatsapp_adapter file → ``cannot import container_get_archive ...
    (unknown location)``). Scoping the restore to our own keys keeps the fake
    ``services`` package from leaking without disturbing anything else (#211).
    """
    _stub_keys = (*_STUB_MODULES, "services.github_pat_propagation_service")
    _saved = {k: sys.modules.get(k) for k in _stub_keys}
    sys.modules.update(_STUB_MODULES)
    sys.modules.pop("services.github_pat_propagation_service", None)
    try:
        yield _load_service()
    finally:
        for k in _stub_keys:
            if _saved[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = _saved[k]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_env_has_github_pat_detects_present(service):
    env = 'FOO="bar"\nGITHUB_PAT="ghp_old"\nBAZ="qux"\n'
    assert service._env_has_github_pat(env) is True


def test_env_has_github_pat_detects_absent(service):
    env = 'FOO="bar"\nBAZ="qux"\n'
    assert service._env_has_github_pat(env) is False


def test_patch_env_preserves_other_keys(service):
    env = 'FOO="bar"\nGITHUB_PAT="ghp_old"\nBAZ="qux"\n'
    result = service._patch_env_github_pat(env, "ghp_new")
    assert 'FOO="bar"' in result
    assert 'BAZ="qux"' in result
    assert 'GITHUB_PAT="ghp_new"' in result
    assert 'GITHUB_PAT="ghp_old"' not in result


def test_patch_env_escapes_embedded_quotes(service):
    env = 'GITHUB_PAT="ghp_old"\n'
    tricky = 'ghp_weird"quote'
    result = service._patch_env_github_pat(env, tricky)
    # Matches the escaping done by the agent's own .env writer.
    assert r'GITHUB_PAT="ghp_weird\"quote"' in result


def test_patch_env_only_replaces_github_pat_line(service):
    env = (
        'SOME_GITHUB_PAT_LIKE="not-this"\n'
        'GITHUB_PAT="ghp_old"\n'
        'ANOTHER="keep"\n'
    )
    result = service._patch_env_github_pat(env, "ghp_new")
    assert 'SOME_GITHUB_PAT_LIKE="not-this"' in result
    assert 'ANOTHER="keep"' in result
    assert 'GITHUB_PAT="ghp_new"' in result


# ---------------------------------------------------------------------------
# propagate_github_pat orchestration
# ---------------------------------------------------------------------------


def _agent(name: str, status: str = "running"):
    a = MagicMock()
    a.name = name
    a.status = status
    return a


def _make_async_client(read_responses: dict, inject_responses: dict):
    """Build an AsyncMock httpx.AsyncClient that routes per-URL."""

    async def _get(url, params=None, timeout=None, headers=None):
        agent = url.split("http://agent-")[1].split(":")[0]
        resp = read_responses[agent]
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=resp)
        return r

    async def _post(url, json=None, timeout=None, headers=None):
        agent = url.split("http://agent-")[1].split(":")[0]
        behavior = inject_responses[agent]
        if isinstance(behavior, Exception):
            raise behavior
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value=behavior)
        # Capture the payload for assertions.
        r._sent_json = json
        return r

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(side_effect=_get)
    client_instance.post = AsyncMock(side_effect=_post)

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client_instance)
    client_cm.__aexit__ = AsyncMock(return_value=False)
    return client_cm, client_instance


@pytest.mark.asyncio
async def test_skips_agent_with_per_agent_pat(service):
    """Agents with a per-agent PAT configured must not be touched."""
    with patch.object(service, "list_all_agents_fast", return_value=[_agent("a1")]), \
         patch.object(service, "db") as mock_db:
        mock_db.has_agent_github_pat.return_value = True

        result = await service.propagate_github_pat("ghp_new")

    assert result.total_running == 1
    assert result.updated == []
    assert len(result.skipped) == 1
    assert result.skipped[0].status == "skipped_per_agent_pat"
    assert result.failed == []


@pytest.mark.asyncio
async def test_skips_agent_without_github_pat_in_env(service):
    """Agents whose .env does not contain GITHUB_PAT are skipped (AC)."""
    with patch.object(service, "list_all_agents_fast", return_value=[_agent("a1")]), \
         patch.object(service, "db") as mock_db:
        mock_db.has_agent_github_pat.return_value = False

        client_cm, _ = _make_async_client(
            read_responses={"a1": {"files": {".env": 'OTHER="x"\n'}}},
            inject_responses={},
        )
        with patch("services.github_pat_propagation_service.httpx.AsyncClient",
                   return_value=client_cm):
            result = await service.propagate_github_pat("ghp_new")

    assert result.updated == []
    assert any(s.status == "skipped_no_pat" and s.agent_name == "a1"
               for s in result.skipped)


@pytest.mark.asyncio
async def test_updates_agent_and_preserves_other_keys(service):
    """Happy path: env is merged, GITHUB_PAT replaced, other keys kept."""
    original_env = 'FOO="keep"\nGITHUB_PAT="ghp_old"\nBAR="also-keep"\n'

    with patch.object(service, "list_all_agents_fast", return_value=[_agent("a1")]), \
         patch.object(service, "db") as mock_db:
        mock_db.has_agent_github_pat.return_value = False

        client_cm, client = _make_async_client(
            read_responses={"a1": {"files": {".env": original_env}}},
            inject_responses={"a1": {"status": "success", "files_written": [".env"]}},
        )
        with patch("services.github_pat_propagation_service.httpx.AsyncClient",
                   return_value=client_cm):
            result = await service.propagate_github_pat("ghp_new")

    assert result.updated == ["a1"]
    assert result.failed == []

    # Verify the payload sent to inject preserved other keys.
    post_call = client.post.await_args
    sent_env = post_call.kwargs["json"]["files"][".env"]
    assert 'FOO="keep"' in sent_env
    assert 'BAR="also-keep"' in sent_env
    assert 'GITHUB_PAT="ghp_new"' in sent_env
    assert 'GITHUB_PAT="ghp_old"' not in sent_env


@pytest.mark.asyncio
async def test_non_running_agents_ignored(service):
    """Only running agents are considered targets."""
    with patch.object(service, "list_all_agents_fast",
                      return_value=[_agent("a1", status="stopped"),
                                    _agent("a2", status="running")]), \
         patch.object(service, "db") as mock_db:
        mock_db.has_agent_github_pat.return_value = False

        client_cm, client = _make_async_client(
            read_responses={"a2": {"files": {".env": 'GITHUB_PAT="ghp_old"\n'}}},
            inject_responses={"a2": {"status": "success", "files_written": [".env"]}},
        )
        with patch("services.github_pat_propagation_service.httpx.AsyncClient",
                   return_value=client_cm):
            result = await service.propagate_github_pat("ghp_new")

    assert result.total_running == 1
    assert result.updated == ["a2"]
    # Stopped agent never hit the wire.
    assert all("a1" not in str(c) for c in client.get.call_args_list)


@pytest.mark.asyncio
async def test_partial_failure_does_not_block_others(service):
    """One failing agent should not stop the rest from being updated."""
    import httpx

    with patch.object(service, "list_all_agents_fast",
                      return_value=[_agent("good"), _agent("bad")]), \
         patch.object(service, "db") as mock_db:
        mock_db.has_agent_github_pat.return_value = False

        client_cm, _ = _make_async_client(
            read_responses={
                "good": {"files": {".env": 'GITHUB_PAT="ghp_old"\n'}},
                "bad": {"files": {".env": 'GITHUB_PAT="ghp_old"\n'}},
            },
            inject_responses={
                "good": {"status": "success", "files_written": [".env"]},
                "bad": httpx.ConnectError("boom"),
            },
        )
        with patch("services.github_pat_propagation_service.httpx.AsyncClient",
                   return_value=client_cm):
            result = await service.propagate_github_pat("ghp_new")

    assert "good" in result.updated
    assert any(f.agent_name == "bad" and f.status == "failed" for f in result.failed)


@pytest.mark.asyncio
async def test_no_running_agents_returns_empty_result(service):
    with patch.object(service, "list_all_agents_fast", return_value=[]), \
         patch.object(service, "db"):
        result = await service.propagate_github_pat("ghp_new")

    assert result.total_running == 0
    assert result.updated == []
    assert result.skipped == []
    assert result.failed == []
