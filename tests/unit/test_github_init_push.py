"""
Tests for GitHub sync initialization command sequence (#256).

Verifies that `initialize_git_in_container` pushes the initial commit to
GitHub in both code paths:

- Empty remote: force push creates initial history (pre-existing behavior)
- Remote already has main: fast-forward push sends the agent's workspace
  on top of origin/main (bug fix — push was previously missing)

Module: src/backend/services/git_service.py
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def _load_git_service():
    """Import git_service with heavy dependencies mocked out."""
    mock_modules = {}
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        mock_modules[mod] = Mock()

    # `database` exposes `db`, `AgentGitConfig`, `GitSyncResult` at import time
    mock_modules["database"].db = Mock()
    mock_modules["database"].AgentGitConfig = Mock
    mock_modules["database"].GitSyncResult = Mock

    with patch.dict("sys.modules", mock_modules):
        # Force reimport
        for key in list(sys.modules.keys()):
            if key.startswith("services.git_service"):
                del sys.modules[key]
        # #1028: git_service is a package; the alias names the module that
        # owns the functions under test, so patches land where the code looks.
        import services.git_service.provisioning as gs
    return gs


class _FakeExec:
    """Records commands and returns canned results for git operations."""

    def __init__(self, remote_has_main: bool, commit_succeeds: bool = True):
        self.remote_has_main = remote_has_main
        self.commit_succeeds = commit_succeeds
        self.calls: list[str] = []

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        # Capture the raw git command (the bit after `cd <dir> && `)
        inner = command
        if " && " in command:
            inner = command.split(" && ", 1)[1].rstrip('"')
        self.calls.append(inner)

        # Workspace content check — pretend /home/developer/workspace is empty
        if "find /home/developer/workspace" in command:
            return {"exit_code": 0, "output": "0"}

        # .gitignore write
        if "GITIGNORE_EOF" in command:
            return {"exit_code": 0, "output": ""}

        # origin/main existence check
        if "git rev-parse --verify origin/main" in inner:
            return {
                "exit_code": 0 if self.remote_has_main else 1,
                "output": "abc123" if self.remote_has_main else "fatal: needed a revision"
            }

        # Commit may legitimately have nothing to commit
        if inner.startswith("git commit") and not self.commit_succeeds:
            return {"exit_code": 0, "output": "Nothing to commit"}

        # Final verification
        if "git rev-parse --git-dir" in inner:
            return {"exit_code": 0, "output": ".git"}

        # Everything else: success
        return {"exit_code": 0, "output": ""}


@pytest.mark.asyncio
async def test_empty_remote_force_pushes():
    """Initial push happens when origin/main doesn't exist yet."""
    gs = _load_git_service()
    fake = _FakeExec(remote_has_main=False)

    with patch.object(gs, "execute_command_in_container", fake), \
            patch.object(gs.gitignore, "execute_command_in_container", fake), \
            patch.object(gs.remotes, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert result.success, f"expected success, got error: {result.error}"
    # The bug: the old behaviour for empty-remote was already correct,
    # this test just pins it so a refactor doesn't regress it.
    assert any("git push -u origin main --force" in c for c in fake.calls), \
        f"expected force push to empty remote, got: {fake.calls}"


@pytest.mark.asyncio
async def test_existing_remote_pushes_after_commit():
    """
    Regression test for #256.

    When the remote already has a main branch, the old code reset to
    origin/main, committed the workspace, and then did nothing — the
    commit never reached GitHub. The fix adds a `git push -u origin main`
    after the commit so the agent's workspace actually lands on GitHub.
    """
    gs = _load_git_service()
    fake = _FakeExec(remote_has_main=True)

    with patch.object(gs, "execute_command_in_container", fake), \
            patch.object(gs.gitignore, "execute_command_in_container", fake), \
            patch.object(gs.remotes, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert result.success, f"expected success, got error: {result.error}"

    # Fast-forward push (not --force) to preserve any existing history
    push_calls = [c for c in fake.calls if c.startswith("git push")]
    assert push_calls, \
        f"BUG: no git push issued on remote-has-main path. Calls: {fake.calls}"
    assert any(c == "git push -u origin main" for c in push_calls), \
        f"expected fast-forward 'git push -u origin main', got: {push_calls}"
    assert not any("--force" in c for c in push_calls), \
        f"must not force-push when remote has history, got: {push_calls}"


@pytest.mark.asyncio
async def test_existing_remote_reset_precedes_commit():
    """Command order: reset → add → commit → push (remote-has-main path)."""
    gs = _load_git_service()
    fake = _FakeExec(remote_has_main=True)

    with patch.object(gs, "execute_command_in_container", fake), \
            patch.object(gs.gitignore, "execute_command_in_container", fake), \
            patch.object(gs.remotes, "execute_command_in_container", fake):
        await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    # Find the indices of the four operations in commit_commands
    def idx(substr):
        for i, c in enumerate(fake.calls):
            if substr in c:
                return i
        return -1

    reset_i = idx("git reset origin/main")
    add_i = idx("git add .")
    commit_i = idx("git commit -m")
    push_i = idx("git push -u origin main")

    assert reset_i >= 0 and add_i >= 0 and commit_i >= 0 and push_i >= 0, \
        f"missing expected commands, got: {fake.calls}"
    assert reset_i < add_i < commit_i < push_i, \
        f"commands out of order: reset@{reset_i} add@{add_i} commit@{commit_i} push@{push_i}"


@pytest.mark.asyncio
async def test_existing_remote_nothing_to_commit_still_pushes():
    """
    Edge case: workspace is already identical to origin/main.

    The commit step reports 'Nothing to commit' (exit 0 via the `|| echo`
    fallback). The subsequent push is a no-op against an up-to-date remote,
    but we still issue it so upstream tracking gets set.
    """
    gs = _load_git_service()
    fake = _FakeExec(remote_has_main=True, commit_succeeds=False)

    with patch.object(gs, "execute_command_in_container", fake), \
            patch.object(gs.gitignore, "execute_command_in_container", fake), \
            patch.object(gs.remotes, "execute_command_in_container", fake):
        result = await gs.initialize_git_in_container(
            agent_name="test-agent",
            github_repo="owner/repo",
            github_pat="ghp_fake",
            create_working_branch=False,
        )

    assert result.success
    assert any(c == "git push -u origin main" for c in fake.calls), \
        "push should run even when there was nothing new to commit"
