"""A manual Pull leaves a tag-pinned agent where it is, and says why (trinity#4470).

`github:owner/repo@v1.0.0` clones with `git clone -b v1.0.0`, which leaves the
working copy on a DETACHED HEAD: the agent runs exactly the release it was
pinned to (trinity#4165). The automatic pull cycle (`_run_pull_once`) skips such
an agent with a named reason. The manual route (`POST /api/git/pull` — the Git
tab's Pull and the `git_pull` MCP tool) did not: it read the branch with
`git rev-parse --abbrev-ref HEAD`, which prints the literal `HEAD` when
detached, pulled `origin/HEAD` — the remote's DEFAULT branch — and
fast-forwarded the agent off its tag. `force_reset` hard-reset it there.

Contract:
  - every strategy (`clean`, `stash_reapply`, `force_reset`) on a detached HEAD
    with no operation under way is refused 400 with the reason named; HEAD, the
    files and any uncommitted work are exactly as they were;
  - a detached HEAD because a rebase stopped part-way is NOT read as a pin: it
    keeps the handling it had (the pull is attempted and fails as a conflict);
  - an agent on a branch pulls as before.

Real repos (a bare origin holding a tag and a moving `main`, the agent cloned
`-b <tag>` as `startup.sh` does) and the real route function — the
`test_4165_pull_cycle_pinned_tag` harness shape.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server.routers import git as git_router

STRATEGIES = ("clean", "stash_reapply", "force_reset")


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=20, check=check)


def _out(repo, *args):
    return _git(repo, *args).stdout.strip()


def _identity(repo):
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """(agent_dir_factory, human, tag_commit, main_commit). Origin holds an
    annotated tag `v1.0.0` and a `main` that has since moved past it. The
    route's hardcoded /home/developer is pointed at whichever agent dir the
    test builds."""
    origin, human = tmp_path / "origin.git", tmp_path / "human"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(human))
    _identity(human)
    (human / "role.md").write_text("role v1\n")
    (human / ".gitignore").write_text(".trinity/\n")
    _git(human, "add", "-A")
    _git(human, "commit", "-q", "-m", "v1")
    _git(human, "tag", "-a", "v1.0.0", "-m", "release 1")
    _git(human, "push", "-q", "origin", "HEAD:main", "v1.0.0")
    tag_commit = _out(human, "rev-parse", "HEAD")
    (human / "role.md").write_text("role v2 — after the release\n")
    (human / "only_on_main.md").write_text("added after the tag\n")
    _git(human, "add", "-A")
    _git(human, "commit", "-q", "-m", "v2")
    _git(human, "push", "-q", "origin", "HEAD:main")
    main_commit = _out(human, "rev-parse", "HEAD")

    agent = tmp_path / "agent"
    real_path = Path
    monkeypatch.setattr(
        git_router, "Path",
        lambda *a: real_path(agent) if a == ("/home/developer",) else real_path(*a))

    def clone(*clone_args):
        _git(tmp_path, "clone", "-q", *clone_args, str(origin), str(agent))
        _identity(agent)
        (agent / ".trinity").mkdir()
        return agent

    return clone, human, tag_commit, main_commit


def _pull(strategy):
    """The route function itself, as FastAPI calls it."""
    return asyncio.run(git_router.pull_from_github(git_router.GitPullRequest(strategy=strategy)))


class TestManualPullOnAPinnedTag:
    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_every_strategy_is_refused_and_nothing_moves(self, repos, strategy):
        clone, _, tag_commit, _ = repos
        agent = clone("-b", "v1.0.0")
        assert _git(agent, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0, \
            "precondition: a clone of a tag is on a detached HEAD"

        with pytest.raises(HTTPException) as exc:
            _pull(strategy)

        assert exc.value.status_code == 400
        assert "pinned to a tag or commit" in exc.value.detail
        assert "nothing was changed" in exc.value.detail.lower()
        assert _out(agent, "rev-parse", "HEAD") == tag_commit, "the pin holds"
        assert (agent / "role.md").read_text() == "role v1\n"
        assert not (agent / "only_on_main.md").exists(), "main's later file did not arrive"

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_uncommitted_work_on_a_pinned_agent_survives(self, repos, strategy):
        """`force_reset` is the destructive one: before the fix it hard-reset a
        pinned agent to the default branch and cleaned its untracked files."""
        clone, _, tag_commit, _ = repos
        agent = clone("-b", "v1.0.0")
        (agent / "role.md").write_text("role v1 — edited by the agent\n")
        (agent / "notes.md").write_text("untracked work\n")

        with pytest.raises(HTTPException) as exc:
            _pull(strategy)

        assert exc.value.status_code == 400
        assert _out(agent, "rev-parse", "HEAD") == tag_commit
        assert (agent / "role.md").read_text() == "role v1 — edited by the agent\n"
        assert (agent / "notes.md").read_text() == "untracked work\n"
        assert _out(agent, "stash", "list") == "", "nothing was stashed either"

    def test_a_commit_pinned_agent_is_refused_the_same_way(self, repos):
        """A detached HEAD at a bare commit (not a tag) is the same pin."""
        clone, _, tag_commit, _ = repos
        agent = clone()
        _git(agent, "checkout", "-q", "--detach", tag_commit)

        with pytest.raises(HTTPException) as exc:
            _pull("clean")

        assert exc.value.status_code == 400
        assert _out(agent, "rev-parse", "HEAD") == tag_commit


class TestWhatDidNotChange:
    def test_an_agent_on_a_branch_still_pulls(self, repos):
        clone, human, _, main_commit = repos
        agent = clone()
        _git(agent, "reset", "-q", "--hard", "v1.0.0")   # on `main`, one commit behind

        result = _pull("clean")

        assert result["success"] is True
        assert _out(agent, "rev-parse", "HEAD") == main_commit
        assert (agent / "only_on_main.md").exists()

    def test_a_stuck_rebase_is_not_read_as_a_pin(self, repos):
        """A rebase stopped on a conflict also leaves HEAD detached. That is a
        stuck tree, not a pin: the route must not answer "pinned", and keeps
        the handling it had before."""
        clone, _, _, _ = repos
        agent = clone()
        _git(agent, "reset", "-q", "--hard", "v1.0.0")
        (agent / "role.md").write_text("role — the agent's\n")
        _git(agent, "commit", "-qam", "agent")
        _git(agent, "fetch", "-q", "origin", "main")
        assert _git(agent, "rebase", "origin/main", check=False).returncode != 0
        assert _git(agent, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

        try:
            out = _pull("clean")
            detail = getattr(out, "body", b"").decode() if hasattr(out, "body") else str(out)
        except HTTPException as e:
            detail = str(e.detail)

        assert "pinned to a tag or commit" not in detail

    def test_an_unreadable_head_is_not_answered_pinned(self, repos, monkeypatch):
        """`symbolic-ref -q` exits 1 for a detached HEAD and 128 for a real
        error. Only the first is a pin: a repo git cannot read must not be told
        "pinned to a tag or commit"."""
        clone, _, _, _ = repos
        agent = clone()
        real_run = subprocess.run

        def run(argv, *a, **kw):
            if list(argv[:2]) == ["git", "symbolic-ref"]:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not a git repository")
            return real_run(argv, *a, **kw)
        monkeypatch.setattr(git_router.subprocess, "run", run)

        try:
            out = _pull("clean")
            detail = str(out)
        except HTTPException as e:
            detail = str(e.detail)

        assert "pinned to a tag or commit" not in detail
