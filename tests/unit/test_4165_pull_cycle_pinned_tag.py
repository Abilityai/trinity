"""A tag-pinned agent's pull cycle is a named skip, not a failure every tick (trinity#4165).

`github:owner/repo@v1.2.0` with a tag clones with `git clone -b v1.2.0`
(source mode, `startup.sh`), which leaves the working copy on a DETACHED HEAD:
the agent runs exactly the release it was pinned to. Pull sync is on by default
for every `github:` agent, and `_run_pull_once` recorded
`failed: detached HEAD: nothing to pull onto` on every cycle, forever — a
failure streak for an agent doing precisely what it was asked to.

Contract:
  - a detached HEAD with no operation under way is `skipped`, with the reason
    named, every cycle; `consecutive_pull_failures` stays 0 and HEAD does not
    move, even when origin's branch moves on;
  - a detached HEAD because a rebase stopped part-way is still `failed`: that
    is a stuck tree, not a pin, and must stay loud.

Real repos (a bare origin holding a tag and a moving `main`, the agent cloned
`-b <tag>` as `startup.sh` does), the real cycle — the `test_ent703_pull_cycle`
harness shape.
"""
from __future__ import annotations

import subprocess

import pytest

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server.routers import git as git_router


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
def pinned(tmp_path, monkeypatch):
    """(agent, human, tag_commit): the agent cloned `-b v1.0.0` (an annotated
    tag); origin's `main` has since moved past it."""
    origin, human, agent = tmp_path / "origin.git", tmp_path / "human", tmp_path / "agent"
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

    _git(tmp_path, "clone", "-q", "-b", "v1.0.0", str(origin), str(agent))
    _identity(agent)
    (agent / ".trinity").mkdir()
    assert _git(agent, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0, \
        "precondition: a clone of a tag is on a detached HEAD"

    (human / "role.md").write_text("role v2 — after the release\n")
    _git(human, "commit", "-qam", "v2")
    _git(human, "push", "-q", "origin", "HEAD:main")
    monkeypatch.setattr(git_router, "_executions_in_flight", lambda: 0)
    return agent, human, tag_commit


def _state(agent):
    return git_router._read_sync_state_file(agent)


class TestPinnedTagPull:
    def test_every_cycle_is_a_named_skip_not_a_failure(self, pinned):
        agent, _, tag_commit = pinned
        for _ in range(3):
            result = git_router._run_pull_once(agent)
            assert result["status"] == "skipped", result
            assert "detached HEAD" in result["error"]
        st = _state(agent)
        assert st["last_pull_status"] == "skipped"
        assert st["consecutive_pull_failures"] == 0
        assert "detached HEAD" in st["last_pull_error"]

    def test_the_pin_holds_while_origin_moves_on(self, pinned):
        agent, _, tag_commit = pinned
        git_router._run_pull_once(agent)
        assert _out(agent, "rev-parse", "HEAD") == tag_commit
        assert (agent / "role.md").read_text() == "role v1\n"

    def test_a_stuck_rebase_is_still_a_failure(self, pinned):
        """A rebase stopped on a conflict also leaves HEAD detached. That is a
        stuck tree, not a pin: it must stay a failure, so the streak shows."""
        agent, human, _ = pinned
        _git(agent, "checkout", "-q", "-b", "work")
        (agent / "role.md").write_text("role — the agent's\n")
        _git(agent, "commit", "-qam", "agent")
        _git(agent, "fetch", "-q", "origin", "main")
        assert _git(agent, "rebase", "origin/main", check=False).returncode != 0
        assert _git(agent, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

        result = git_router._run_pull_once(agent)
        assert result["status"] == "failed", result
        assert _state(agent)["consecutive_pull_failures"] == 1
