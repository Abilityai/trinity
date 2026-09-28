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
import types

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Pytest's rootdir machinery (`tests/unit/pytest.ini` makes `tests/` a search
# root) can let `tests/agent_server/__init__.py` shadow the real package, so
# `from agent_server.routers.git import ...` would otherwise raise
# ModuleNotFoundError. The unit conftest's `_preload_real_agent_server` shim
# has already swapped in a namespace package whose `__path__` points to
# `docker/base-image/agent_server` — but only if some earlier test hadn't
# already cached the wrong (shadow) one. Verify the cache reflects the real
# path; if not, replace it with the namespace shim. Importantly we do NOT
# `exec_module(__init__.py)` here — that would create a *second* agent_server
# module object distinct from the one earlier-collected unit files (e.g.
# `test_drain_bounded.py`) already imported attributes from. monkeypatch
# would then patch the new module while our test fixtures call into the old
# one and the patch never lands. (#728 regression observed during the
# pollution audit.)
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [_real_path]  # type: ignore[attr-defined]
    _stub.__package__ = "agent_server"
    sys.modules["agent_server"] = _stub

# Import via absolute package path. The submodules below will each execute
# their own module body via the conftest-installed namespace package.
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


# ---------------------------------------------------------------------------
# #2105 — the working tuple is measured against the agent's OWN branch
# ---------------------------------------------------------------------------


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", name], repo)


def _peer(remote: Path, parent: Path) -> Path:
    peer = parent / "peer"
    _run(["git", "clone", str(remote), str(peer)], parent)
    _run(["git", "config", "user.email", "peer@test.com"], peer)
    _run(["git", "config", "user.name", "Peer"], peer)
    return peer


class TestWorkingTupleOnAnyBranchName:
    """#2105: a branch named neither `main` nor `trinity/*` used to get the
    `origin/main` counts labelled as `ahead_working` — a fleet renamed to
    `asus/*` reported ~9,000 unpushed commits while the true gap was 0."""

    def test_pushed_branch_far_from_main_reports_zero_unpushed(self, repos):
        local, _ = repos
        _run(["git", "checkout", "-b", "asus/oracle-3-ai"], local)
        for i in range(3):
            _commit(local, f"work{i}.txt")
        _run(["git", "push", "-u", "origin", "asus/oracle-3-ai"], local)

        payload = _dual_ahead_behind_payload("asus/oracle-3-ai", local)
        assert (payload["ahead_main"], payload["behind_main"]) == (3, 0)
        assert (payload["ahead_working"], payload["behind_working"]) == (0, 0)

    def test_unpushed_and_peer_commits_on_a_non_trinity_branch(self, repos):
        local, remote = repos
        _run(["git", "checkout", "-b", "asus/cleon"], local)
        _commit(local, "base.txt")
        _run(["git", "push", "-u", "origin", "asus/cleon"], local)

        peer = _peer(remote, local.parent)
        _run(["git", "checkout", "asus/cleon"], peer)
        _commit(peer, "peer.txt")
        _run(["git", "push", "origin", "asus/cleon"], peer)

        _commit(local, "mine.txt")
        _run(["git", "fetch", "origin"], local)
        payload = _dual_ahead_behind_payload("asus/cleon", local)
        assert (payload["ahead_working"], payload["behind_working"]) == (1, 1)
        assert (payload["ahead_main"], payload["behind_main"]) == (2, 0)

    def test_master_repo_without_main_counts_the_unpushed_commit(self, tmp_path):
        """Second field report: a `master` repo with no `main` read 0 ahead
        for 7 days while one commit sat unpushed."""
        local, remote = tmp_path / "local", tmp_path / "remote"
        local.mkdir()
        remote.mkdir()
        _run(["git", "init", "--bare", "-b", "master"], remote)
        _run(["git", "init", "-b", "master"], local)
        _run(["git", "config", "user.email", "t@test.com"], local)
        _run(["git", "config", "user.name", "T"], local)
        _run(["git", "remote", "add", "origin", str(remote)], local)
        _commit(local, "a.txt")
        _run(["git", "push", "-u", "origin", "master"], local)
        _commit(local, "b.txt")

        payload = _dual_ahead_behind_payload("master", local)
        assert (payload["ahead_working"], payload["behind_working"]) == (1, 0)
        # No origin/main: the main tuple is unknown, never a believable 0.
        assert payload["ahead_main"] is None and payload["behind_main"] is None
        assert payload["ahead"] is None and payload["behind"] is None

    def test_never_pushed_branch_counts_commits_on_no_remote(self, repos):
        local, _ = repos
        _run(["git", "checkout", "-b", "asus/fresh"], local)
        _commit(local, "one.txt")
        _commit(local, "two.txt")

        payload = _dual_ahead_behind_payload("asus/fresh", local)
        # Nothing upstream to be behind; both commits exist on no remote.
        assert payload["ahead_working"] == 2
        assert payload["behind_working"] is None

    def test_trinity_branch_never_pushed_is_not_read_as_main(self, repos):
        local, _ = repos
        _commit(local, "on-main.txt")
        _run(["git", "push", "origin", "main"], local)
        _run(["git", "checkout", "-b", "trinity/a/1"], local)
        _commit(local, "w.txt")
        payload = _dual_ahead_behind_payload("trinity/a/1", local)
        assert payload["ahead_working"] == 1
        assert payload["ahead_main"] == 1
