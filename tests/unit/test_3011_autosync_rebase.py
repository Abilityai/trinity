"""
#3011 — the auto-sync heartbeat reconciles before it pushes.

Real git repos (a bare "remote", the agent's clone, and a second "foreign"
clone that pushes underneath the agent):
  - foreign commit on the branch → next cycle rebases and pushes
  - conflicting foreign commit → rebase aborted, `diverged: …` recorded,
    repo exactly as it was, remote untouched
  - source-mode agent on the default branch → refused, nothing committed or
    pushed; fork-to-own and non-default branches are not refused
  - the post-rebase push is lease-protected: a push landing between fetch and
    push is rejected, never overwritten
  - sync-state gains `behind_after_fetch` and `last_successful_push_at`
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server.routers import git as git_router
from agent_server.routers.git import (
    _read_sync_state_file,
    _run_auto_sync_once,
)


def _run(cmd, cwd):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=20, check=True,
    )


def _out(cmd, cwd) -> str:
    return _run(cmd, cwd).stdout.strip()


def _identity(repo: Path) -> None:
    _run(["git", "config", "user.email", "test@test.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)


@pytest.fixture(autouse=True)
def _clean_git_env(monkeypatch):
    for key in ("GIT_SOURCE_MODE", "GIT_UPSTREAM_REPO"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def world(tmp_path):
    """(agent clone, bare remote, foreign clone) — all on `main`."""
    remote = tmp_path / "remote.git"
    agent = tmp_path / "agent"
    foreign = tmp_path / "foreign"
    remote.mkdir()
    agent.mkdir()
    _run(["git", "init", "--bare", "-b", "main"], remote)
    _run(["git", "init", "-b", "main"], agent)
    _identity(agent)
    _run(["git", "remote", "add", "origin", str(remote)], agent)
    (agent / "README.md").write_text("line one\n")
    (agent / ".gitignore").write_text(".trinity/\n")
    _run(["git", "add", "."], agent)
    _run(["git", "commit", "-m", "initial"], agent)
    _run(["git", "push", "-u", "origin", "main"], agent)
    (agent / ".trinity").mkdir()
    _run(["git", "clone", "-q", str(remote), str(foreign)], tmp_path)
    _identity(foreign)
    return agent, remote, foreign


def _foreign_commit(foreign: Path, name: str, content: str, branch: str = "main") -> str:
    _run(["git", "pull", "-q", "--rebase", "origin", branch], foreign)
    (foreign / name).write_text(content)
    _run(["git", "add", name], foreign)
    _run(["git", "commit", "-q", "-m", f"foreign: {name}"], foreign)
    _run(["git", "push", "-q", "origin", f"HEAD:{branch}"], foreign)
    return _out(["git", "rev-parse", "HEAD"], foreign)


def _remote_head(remote: Path, branch: str = "main") -> str:
    return _out(["git", "rev-parse", branch], remote)


def _remote_subjects(remote: Path, branch: str = "main") -> list:
    return _out(["git", "log", "--format=%s", branch], remote).splitlines()


def _rebase_in_progress(repo: Path) -> bool:
    git_dir = repo / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


class TestForeignPush:
    def test_foreign_commit_is_rebased_over_and_pushed(self, world):
        agent, remote, foreign = world
        _foreign_commit(foreign, "theirs.txt", "from a human\n")
        (agent / "mine.txt").write_text("from the agent\n")

        result = _run_auto_sync_once(agent)

        assert result["status"] == "success", result
        subjects = _remote_subjects(remote)
        assert subjects[0].startswith("Trinity auto-sync")
        assert "foreign: theirs.txt" in subjects
        # Linear: the agent commit sits directly on top of the foreign one.
        assert subjects[1] == "foreign: theirs.txt"
        assert _out(["git", "rev-list", "--merges", "--count", "HEAD"], agent) == "0"
        assert _remote_head(remote) == _out(["git", "rev-parse", "HEAD"], agent)
        state = _read_sync_state_file(agent)
        assert state["behind_after_fetch"] == 1
        assert state["last_successful_push_at"] == state["last_sync_at"]
        assert state["consecutive_failures"] == 0

    def test_stuck_before_fix_now_recovers_across_cycles(self, world):
        """The reported shape: every cycle after a foreign push kept failing."""
        agent, remote, foreign = world
        (agent / "a.txt").write_text("1\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        _foreign_commit(foreign, "b.txt", "2\n")
        (agent / "c.txt").write_text("3\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        _foreign_commit(foreign, "d.txt", "4\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        assert _remote_head(remote) == _out(["git", "rev-parse", "HEAD"], agent)
        assert (agent / "d.txt").read_text() == "4\n"

    def test_behind_only_fast_forwards(self, world):
        agent, remote, foreign = world
        foreign_sha = _foreign_commit(foreign, "theirs.txt", "x\n")
        result = _run_auto_sync_once(agent)
        assert result["status"] == "success"
        assert _out(["git", "rev-parse", "HEAD"], agent) == foreign_sha
        assert _remote_head(remote) == foreign_sha
        assert _read_sync_state_file(agent)["behind_after_fetch"] == 1

    def test_up_to_date_records_zero_behind(self, world):
        agent, _, _ = world
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        assert _read_sync_state_file(agent)["behind_after_fetch"] == 0

    def test_branch_missing_on_remote_is_created(self, world):
        agent, remote, _ = world
        _run(["git", "checkout", "-q", "-b", "trinity/agent/abc"], agent)
        (agent / "mine.txt").write_text("x\n")
        result = _run_auto_sync_once(agent)
        assert result["status"] == "success", result
        assert _remote_head(remote, "trinity/agent/abc") == _out(
            ["git", "rev-parse", "HEAD"], agent
        )


class TestConflict:
    def test_conflict_aborts_and_records_divergence(self, world):
        agent, remote, foreign = world
        foreign_sha = _foreign_commit(foreign, "README.md", "human edit\n")
        (agent / "README.md").write_text("agent edit\n")

        result = _run_auto_sync_once(agent)

        assert result["status"] == "failed"
        assert result["error"] == "diverged: rebase conflict on main"
        state = _read_sync_state_file(agent)
        assert state["last_error_summary"] == "diverged: rebase conflict on main"
        assert state["consecutive_failures"] == 1
        assert state["behind_after_fetch"] == 1
        assert state["last_successful_push_at"] is None
        # Tree untouched: no rebase left behind, the agent's work intact and
        # committed on its own line, nothing reset.
        assert not _rebase_in_progress(agent)
        assert (agent / "README.md").read_text() == "agent edit\n"
        assert _out(["git", "status", "--porcelain"], agent) == ""
        assert _out(["git", "log", "-1", "--format=%s"], agent).startswith(
            "Trinity auto-sync"
        )
        # Remote never overwritten.
        assert _remote_head(remote) == foreign_sha

    def test_conflict_keeps_failing_without_rewriting(self, world):
        """Three cycles → three failures (the sync_failing threshold), same head."""
        agent, remote, foreign = world
        foreign_sha = _foreign_commit(foreign, "README.md", "human edit\n")
        (agent / "README.md").write_text("agent edit\n")
        local_heads = set()
        for _ in range(3):
            assert _run_auto_sync_once(agent)["status"] == "failed"
            local_heads.add(_out(["git", "rev-parse", "HEAD"], agent))
        assert len(local_heads) == 1
        assert _read_sync_state_file(agent)["consecutive_failures"] == 3
        assert _remote_head(remote) == foreign_sha


class TestLease:
    def test_push_landing_between_fetch_and_push_is_rejected(self, world, monkeypatch):
        agent, remote, foreign = world
        _foreign_commit(foreign, "first.txt", "1\n")
        (agent / "mine.txt").write_text("x\n")

        real = git_router.run_registered
        pushed = {}

        def racing(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "push"]:
                pushed["cmd"] = list(cmd)
                pushed["sha"] = _foreign_commit(foreign, "race.txt", "2\n")
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(git_router, "run_registered", racing)
        result = _run_auto_sync_once(agent)

        assert result["status"] == "failed"
        assert any(a.startswith("--force-with-lease=refs/heads/main:") for a in pushed["cmd"])
        assert "--force" not in pushed["cmd"]
        assert _remote_head(remote) == pushed["sha"]  # the racing push survived

    def test_no_rebase_means_plain_push(self, world, monkeypatch):
        agent, _, _ = world
        (agent / "mine.txt").write_text("x\n")
        real = git_router.run_registered
        pushes = []

        def spy(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "push"]:
                pushes.append(list(cmd))
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(git_router, "run_registered", spy)
        assert _run_auto_sync_once(agent)["status"] == "success"
        assert pushes == [["git", "push", "origin", "HEAD"]]


class TestSourceModeRefusal:
    def test_source_mode_on_default_branch_is_refused(self, world, monkeypatch):
        agent, remote, _ = world
        monkeypatch.setenv("GIT_SOURCE_MODE", "true")
        before_local = _out(["git", "rev-parse", "HEAD"], agent)
        before_remote = _remote_head(remote)
        (agent / "mine.txt").write_text("x\n")

        result = _run_auto_sync_once(agent)

        assert result["status"] == "failed"
        assert result["error"] == "refused: source-mode on main"
        assert _read_sync_state_file(agent)["last_error_summary"] == (
            "refused: source-mode on main"
        )
        # Nothing committed, nothing pushed — the clone stays a pull-only mirror.
        assert _out(["git", "rev-parse", "HEAD"], agent) == before_local
        assert _remote_head(remote) == before_remote
        assert "mine.txt" in _out(["git", "status", "--porcelain"], agent)

    def test_default_branch_read_from_origin_head(self, world, monkeypatch):
        agent, remote, _ = world
        monkeypatch.setenv("GIT_SOURCE_MODE", "true")
        _run(["git", "checkout", "-q", "-b", "trunk"], agent)
        _run(["git", "push", "-q", "-u", "origin", "trunk"], agent)
        _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD",
              "refs/remotes/origin/trunk"], agent)
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["error"] == "refused: source-mode on trunk"

    def test_source_mode_on_other_branch_pushes(self, world, monkeypatch):
        agent, remote, _ = world
        monkeypatch.setenv("GIT_SOURCE_MODE", "true")
        _run(["git", "checkout", "-q", "-b", "feature-x"], agent)
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        assert _remote_head(remote, "feature-x") == _out(["git", "rev-parse", "HEAD"], agent)

    def test_fork_to_own_by_env_is_not_refused(self, world, monkeypatch):
        agent, remote, _ = world
        monkeypatch.setenv("GIT_SOURCE_MODE", "true")
        monkeypatch.setenv("GIT_UPSTREAM_REPO", "Org/template")
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["status"] == "success"
        assert _remote_head(remote) == _out(["git", "rev-parse", "HEAD"], agent)

    def test_fork_to_own_by_upstream_remote_is_not_refused(self, world, monkeypatch):
        """GIT_UPSTREAM_REPO is not re-derived on recreate; the remote survives."""
        agent, remote, _ = world
        monkeypatch.setenv("GIT_SOURCE_MODE", "true")
        _run(["git", "remote", "add", "upstream", "https://example.com/Org/template.git"], agent)
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["status"] == "success"

    def test_working_branch_mode_on_main_is_not_refused(self, world):
        agent, _, _ = world
        (agent / "mine.txt").write_text("x\n")
        assert _run_auto_sync_once(agent)["status"] == "success"


class TestOperatorPathUnchanged:
    def test_sync_to_github_does_not_use_the_heartbeat_reconcile(self):
        import inspect

        src = inspect.getsource(git_router.sync_to_github)
        assert "_rebase_onto_remote" not in src
        assert "_is_shared_source_branch" not in src
