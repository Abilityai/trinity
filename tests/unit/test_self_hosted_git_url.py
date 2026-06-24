"""
Tests for TRINITY_GIT_BASE_URL / TRINITY_GIT_API_BASE overrides (#387).

Acceptance criteria from the issue:
- `TRINITY_GIT_BASE_URL` (default `https://github.com`) — scheme+host for
  clone/push URLs.
- `TRINITY_GIT_API_BASE` (default `https://api.github.com`) — API endpoint.
- Backward compatibility: when env vars unset, behavior unchanged.

Covered surfaces:
- `src/backend/services/git_service.py:_git_remote_url`
- `src/backend/services/github_service.py:GitHubService.API_BASE`
- `src/backend/services/agent_service/crud.py` env propagation
- `docker/base-image/startup.sh` bash URL composition
"""
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_SH = _PROJECT_ROOT / "docker" / "base-image" / "startup.sh"
_CRUD_PY = _PROJECT_ROOT / "src" / "backend" / "services" / "agent_service" / "crud.py"


# ---------------------------------------------------------------------------
# git_service._git_remote_url — Python helper
# ---------------------------------------------------------------------------


def _load_git_service():
    """Import services.git_service with heavy deps mocked.

    Mirrors the pattern in tests/unit/test_github_init_push.py.
    Re-imports on every call so env-var changes take effect.
    """
    mock_modules = {}
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        mock_modules[mod] = Mock()

    mock_modules["database"].db = Mock()
    mock_modules["database"].AgentGitConfig = Mock
    mock_modules["database"].GitSyncResult = Mock

    with patch.dict("sys.modules", mock_modules):
        for key in list(sys.modules.keys()):
            if key.startswith("services.git_service"):
                del sys.modules[key]
        import services.git_service as gs
    return gs


@pytest.fixture
def unset_git_env(monkeypatch):
    """Guarantee TRINITY_GIT_* env vars are unset for the test body."""
    monkeypatch.delenv("TRINITY_GIT_BASE_URL", raising=False)
    monkeypatch.delenv("TRINITY_GIT_API_BASE", raising=False)


def test_git_remote_url_default_is_github(unset_git_env):
    """#387 backward-compat: default composition still points at github.com."""
    gs = _load_git_service()
    url = gs._git_remote_url("ghp_fake", "owner/repo")
    assert url == "https://oauth2:ghp_fake@github.com/owner/repo.git"


def test_git_remote_url_respects_gitea_override(monkeypatch):
    """Dev/self-host: gitea base URL is honored end-to-end."""
    monkeypatch.setenv("TRINITY_GIT_BASE_URL", "http://trinity-gitea-dev:3000")
    gs = _load_git_service()
    url = gs._git_remote_url("ghp_fake", "owner/repo")
    assert url == "http://oauth2:ghp_fake@trinity-gitea-dev:3000/owner/repo.git"


def test_git_remote_url_preserves_https_override(monkeypatch):
    """GHES override keeps the https scheme (no forced downgrade)."""
    monkeypatch.setenv("TRINITY_GIT_BASE_URL", "https://ghes.example.com")
    gs = _load_git_service()
    url = gs._git_remote_url("ghp_fake", "owner/repo")
    assert url == "https://oauth2:ghp_fake@ghes.example.com/owner/repo.git"


def test_git_remote_url_strips_trailing_slash(monkeypatch):
    """A trailing slash in the base URL must not produce `//owner/...`."""
    monkeypatch.setenv("TRINITY_GIT_BASE_URL", "https://gitea.example.com/")
    gs = _load_git_service()
    url = gs._git_remote_url("ghp_fake", "owner/repo")
    assert url == "https://oauth2:ghp_fake@gitea.example.com/owner/repo.git"


# ---------------------------------------------------------------------------
# github_service.GitHubService.API_BASE — class attr bound at import time
# ---------------------------------------------------------------------------


def _reload_github_service():
    """Fresh import of services.github_service (API_BASE is bound at import)."""
    for key in list(sys.modules.keys()):
        if key.startswith("services.github_service"):
            del sys.modules[key]
    backend = str(_PROJECT_ROOT / "src" / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    return importlib.import_module("services.github_service")


def test_github_service_api_base_default(unset_git_env):
    """Default API_BASE is the public GitHub API."""
    ghs = _reload_github_service()
    assert ghs.GitHubService.API_BASE == "https://api.github.com"


def test_github_service_api_base_override(monkeypatch):
    """TRINITY_GIT_API_BASE swaps the API endpoint at import time."""
    monkeypatch.setenv(
        "TRINITY_GIT_API_BASE", "http://trinity-gitea-dev:3000/api/v1"
    )
    ghs = _reload_github_service()
    assert (
        ghs.GitHubService.API_BASE == "http://trinity-gitea-dev:3000/api/v1"
    )


# ---------------------------------------------------------------------------
# crud.py env propagation — source-level assertion
# ---------------------------------------------------------------------------
#
# Rationale: create_agent_internal pulls in half the backend (DB, Docker,
# templating). A full unit test would need extensive fixtures for a
# 3-line change. Pin the behavior via source inspection instead — a
# refactor that removes the propagation still breaks this test.


def test_crud_propagates_base_url_env():
    text = _CRUD_PY.read_text()
    assert (
        "os.getenv('TRINITY_GIT_BASE_URL')" in text
        or 'os.getenv("TRINITY_GIT_BASE_URL")' in text
    ), "crud.py no longer reads TRINITY_GIT_BASE_URL"
    assert (
        "env_vars['TRINITY_GIT_BASE_URL']" in text
        or 'env_vars["TRINITY_GIT_BASE_URL"]' in text
    ), "crud.py no longer writes TRINITY_GIT_BASE_URL into agent env_vars"


# ---------------------------------------------------------------------------
# startup.sh — bash URL composition
# ---------------------------------------------------------------------------
#
# We extract the exact composition snippet from startup.sh and run it in a
# fresh bash with controlled env. If the snippet drifts, the test breaks —
# which is the point.

_COMPOSE_SNIPPET = r'''
GIT_BASE_URL="${TRINITY_GIT_BASE_URL:-https://github.com}"
GIT_BASE_URL="${GIT_BASE_URL%/}"
GIT_HOST_PATH="${GIT_BASE_URL#*://}"
GIT_SCHEME="${GIT_BASE_URL%%://*}"
CLONE_URL="${GIT_SCHEME}://oauth2:${GITHUB_PAT}@${GIT_HOST_PATH}/${GITHUB_REPO}.git"
printf '%s' "$CLONE_URL"
'''


def _run_snippet(env_overrides):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_PAT": "p",
        "GITHUB_REPO": "o/r",
    }
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", "-c", _COMPOSE_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_startup_snippet_matches_startup_sh():
    """Guard: the snippet under test still matches the real script."""
    sh = _STARTUP_SH.read_text()
    assert 'GIT_BASE_URL="${TRINITY_GIT_BASE_URL:-https://github.com}"' in sh
    assert 'GIT_BASE_URL="${GIT_BASE_URL%/}"' in sh
    assert 'GIT_HOST_PATH="${GIT_BASE_URL#*://}"' in sh
    assert 'GIT_SCHEME="${GIT_BASE_URL%%://*}"' in sh
    assert (
        'CLONE_URL="${GIT_SCHEME}://oauth2:${GITHUB_PAT}@${GIT_HOST_PATH}/${GITHUB_REPO}.git"'
        in sh
    )


def test_startup_sh_default_clone_url():
    """#387 backward-compat: unset env yields the classic GitHub URL."""
    out = _run_snippet({})
    assert out == "https://oauth2:p@github.com/o/r.git"


def test_startup_sh_respects_base_url_override():
    """Dev/self-host: gitea base URL flows all the way to CLONE_URL."""
    out = _run_snippet(
        {"TRINITY_GIT_BASE_URL": "http://trinity-gitea-dev:3000"}
    )
    assert out == "http://oauth2:p@trinity-gitea-dev:3000/o/r.git"


def test_startup_sh_strips_trailing_slash_in_base_url():
    """No `//o/r` when the operator leaves a trailing slash."""
    out = _run_snippet(
        {"TRINITY_GIT_BASE_URL": "https://gitea.example.com/"}
    )
    assert out == "https://oauth2:p@gitea.example.com/o/r.git"


def test_startup_sh_shellcheck_clean():
    """shellcheck must still be happy with startup.sh (skipped if not installed)."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", str(_STARTUP_SH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"shellcheck failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
