"""
Dual ahead/behind computation for the agent-server git status (#389 P6).

The old `get_git_status()` compared `HEAD` against `origin/<pull_branch>`,
which for `trinity/*` branches redirects to `origin/main`. That hides external
writes to the working branch (P6). The fix returns BOTH tuples:

- ahead_main / behind_main   — vs `origin/main` (template improvements)
- ahead_working / behind_working — vs `origin/<current_branch>` (peer divergence)

These tests exercise the real logic against throwaway git repos, mirroring
the approach in tests/unit/test_git_pull_branch.py.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Make the agent-server package importable so we can hit the real helper
# rather than duplicating it in the test.
#
# `pyproject.toml:7` puts `tests` first on sys.path, and `tests/agent_server/`
# exists as a test-helpers package. Without evicting the cached shadow first,
# `import agent_server.routers.git` resolves to the helpers package (no
# `routers/` submodule) and ImportError. Same pattern as conftest.py:65 for
# the `utils` shadow.
import importlib.util

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Evict any previously cached `agent_server` (shadow or real) so the
# explicit file-based loader below wins regardless of sys.path order.
for _mod in list(sys.modules):
    if _mod == "agent_server" or _mod.startswith("agent_server."):
        sys.modules.pop(_mod, None)

# Force-load the real agent_server package from docker/base-image. Without
# this, pytest's rootdir machinery (`tests/unit/pytest.ini` makes `tests/` a
# search root) lets `tests/agent_server/__init__.py` shadow the real package
# and `import agent_server.routers` raises ModuleNotFoundError. Same pattern
# as conftest.py:38 for the `utils` shadow.
_AS_INIT = _BASE_IMAGE / "agent_server" / "__init__.py"
_as_spec = importlib.util.spec_from_file_location(
    "agent_server", str(_AS_INIT),
    submodule_search_locations=[str(_BASE_IMAGE / "agent_server")],
)
_as_mod = importlib.util.module_from_spec(_as_spec)
sys.modules["agent_server"] = _as_mod
_as_spec.loader.exec_module(_as_mod)

# Import via absolute package path.
from agent_server.routers.git import (  # noqa: E402
    _compute_ahead_behind,
    _get_pull_branch,
    _dual_ahead_behind_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=15)


def _init_repo(local_dir: Path, remote_dir: Path) -> None:
    _run(["git", "init", "--bare", "-b", "main"], remote_dir)
    _run(["git", "init", "-b", "main"], local_dir)
    _run(["git", "config", "user.email", "test@test.com"], local_dir)
    _run(["git", "config", "user.name", "Test"], local_dir)
    _run(["git", "remote", "add", "origin", str(remote_dir)], local_dir)
    (local_dir / "README.md").write_text("hello")
    _run(["git", "add", "."], local_dir)
    _run(["git", "commit", "-m", "initial"], local_dir)
    _run(["git", "push", "-u", "origin", "main"], local_dir)


@pytest.fixture
def repos(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _init_repo(local, remote)
    yield local, remote
    shutil.rmtree(local, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeAheadBehind:
    """_compute_ahead_behind(home_dir, branch) returns (ahead, behind) vs origin/<branch>."""

    def test_zero_when_in_sync(self, repos):
        local, _ = repos
        ahead, behind = _compute_ahead_behind(local, "main")
        assert (ahead, behind) == (0, 0)

    def test_ahead_when_local_commits(self, repos):
        local, _ = repos
        (local / "new.txt").write_text("x")
        _run(["git", "add", "."], local)
        _run(["git", "commit", "-m", "local commit"], local)
        ahead, behind = _compute_ahead_behind(local, "main")
        assert (ahead, behind) == (1, 0)

    def test_behind_when_remote_commits(self, repos):
        local, remote = repos
        # Simulate an external peer pushing to origin/main via a sibling clone.
        peer = local.parent / "peer"
        peer.mkdir()
        _run(["git", "clone", str(remote), str(peer)], peer.parent)
        _run(["git", "config", "user.email", "peer@test.com"], peer)
        _run(["git", "config", "user.name", "Peer"], peer)
        (peer / "peer.txt").write_text("peer")
        _run(["git", "add", "."], peer)
        _run(["git", "commit", "-m", "peer commit"], peer)
        _run(["git", "push", "origin", "main"], peer)

        _run(["git", "fetch", "origin"], local)
        ahead, behind = _compute_ahead_behind(local, "main")
        assert (ahead, behind) == (0, 1)

    def test_missing_ref_returns_zero(self, repos):
        local, _ = repos
        ahead, behind = _compute_ahead_behind(local, "does-not-exist")
        assert (ahead, behind) == (0, 0)


class TestDualAheadBehindPayload:
    """_dual_ahead_behind_payload returns 4 fields plus legacy aliases."""

    def test_main_branch_pins_both_to_same_values(self, repos):
        """On `main`, working_branch resolves to main; both tuples match."""
        local, _ = repos
        payload = _dual_ahead_behind_payload("main", local)
        assert payload["ahead_main"] == payload["ahead_working"]
        assert payload["behind_main"] == payload["behind_working"]
        # Legacy aliases preserved.
        assert payload["ahead"] == payload["ahead_main"]
        assert payload["behind"] == payload["behind_main"]

    def test_trinity_branch_returns_both_tuples(self, repos):
        """On a trinity/* branch, ahead/behind vs main and vs working differ."""
        local, remote = repos

        # Create and push working branch.
        _run(["git", "checkout", "-b", "trinity/alpha/abc123"], local)
        (local / "work.txt").write_text("w1")
        _run(["git", "add", "."], local)
        _run(["git", "commit", "-m", "working commit 1"], local)
        _run(["git", "push", "-u", "origin", "trinity/alpha/abc123"], local)

        # Peer force-pushes to the working branch (P5 clobber simulation).
        peer = local.parent / "peer"
        peer.mkdir()
        _run(["git", "clone", str(remote), str(peer)], peer.parent)
        _run(["git", "config", "user.email", "peer@test.com"], peer)
        _run(["git", "config", "user.name", "Peer"], peer)
        _run(["git", "checkout", "-b", "trinity/alpha/abc123", "origin/main"], peer)
        (peer / "peer-work.txt").write_text("peer")
        _run(["git", "add", "."], peer)
        _run(["git", "commit", "-m", "peer commit A"], peer)
        (peer / "peer-work2.txt").write_text("peer2")
        _run(["git", "add", "."], peer)
        _run(["git", "commit", "-m", "peer commit B"], peer)
        _run(
            ["git", "push", "--force", "origin", "trinity/alpha/abc123"], peer
        )

        # Refresh the local fetch cache and check.
        _run(["git", "fetch", "origin"], local)
        payload = _dual_ahead_behind_payload("trinity/alpha/abc123", local)

        # vs main: local has 1 extra commit, main is at the base → ahead=1, behind=0.
        assert payload["ahead_main"] == 1
        assert payload["behind_main"] == 0
        # vs working branch (now owned by peer): local has its own tip (1 commit
        # diverged from base), peer has 2 commits from the same base.
        assert payload["ahead_working"] == 1
        assert payload["behind_working"] == 2
        # Legacy alias tracks ahead_main/behind_main (existing behavior).
        assert payload["ahead"] == payload["ahead_main"]
        assert payload["behind"] == payload["behind_main"]

    def test_pull_branch_redirection_still_works(self, repos):
        """_get_pull_branch still returns 'main' for trinity/* — unchanged."""
        local, _ = repos
        _run(["git", "checkout", "-b", "trinity/alpha/deadbeef"], local)
        assert _get_pull_branch("trinity/alpha/deadbeef", local) == "main"
        assert _get_pull_branch("main", local) == "main"
