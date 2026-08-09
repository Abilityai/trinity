"""
Tests for agent repo-root detection in `_detect_git_dir`.

`_detect_git_dir` decided an agent's repository root by probing whether
`/home/developer/workspace` had any content, which tests *content* rather than
*legacy-ness*: an agent whose repository is rooted at `/home/developer` but that
also keeps a populated, non-git data directory under `workspace/` was reported
as workspace-rooted. Every consumer that treats the result as a filesystem path
— the compatibility collector, the `.gitignore` migration and its `.git` guard,
and the compatibility fix endpoint — then read and wrote a subdirectory's files
as if they were the repository root's.

The fix asks git instead: `git rev-parse --show-toplevel` walks UP from the
starting directory, so the nearest enclosing repository wins and a genuinely
workspace-rooted legacy repository still resolves to `workspace/`. The old
content heuristic is retained verbatim as the no-repository fallback, because
`initialize_git_in_container` uses this value to choose where to CREATE a
repository and fresh-agent placement must stay byte-compatible.

Module: src/backend/services/git_service.py
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def _load_git_service():
    """Import git_service with heavy dependencies mocked out."""
    mock_modules = {}
    for mod in [
        "docker",
        "docker.errors",
        "docker.types",
        "redis",
        "redis.asyncio",
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
        import services.git_service as gs
    return gs


class _FakeExec:
    """Async stand-in for `execute_command_in_container`, keyed on the command.

    Exactly two probes reach it:
      * the git-root probe  — contains `git rev-parse --show-toplevel`
      * the content probe   — contains `find /home/developer/workspace`
    Anything else is a bug in the test, not the code, so it raises.
    """

    def __init__(
        self,
        *,
        toplevel=None,
        workspace_populated=False,
        probe_output=None,
        probe_exit=None,
        raises=False,
    ):
        self.toplevel = toplevel  # str | None (None = no repo)
        self.workspace_populated = workspace_populated
        self.probe_output = probe_output  # raw override, wins over `toplevel`
        self.probe_exit = probe_exit  # raw override
        self.raises = raises
        self.calls: list[str] = []

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        self.calls.append(command)
        if self.raises:
            raise RuntimeError("container is not running")
        if "git rev-parse --show-toplevel" in command:
            if self.probe_output is not None or self.probe_exit is not None:
                return {
                    "exit_code": self.probe_exit or 0,
                    "output": self.probe_output or "",
                }
            if self.toplevel is None:
                return {
                    "exit_code": 128,
                    "output": "",
                }  # rev-parse's stderr is /dev/null'd
            return {"exit_code": 0, "output": self.toplevel + "\n"}
        if "find /home/developer/workspace" in command:
            return (
                {"exit_code": 0, "output": "1\n"}
                if self.workspace_populated
                else {"exit_code": 1, "output": ""}
            )
        raise AssertionError(f"unexpected command: {command!r}")


class _FakeMigrateExec(_FakeExec):
    """`_FakeExec` plus the branches `_migrate_workspace_gitignore` needs.

    Models an agent whose real repository is rooted at `/home/developer`: the
    `[ -d <dir>/.git ]` guard succeeds only for that path and fails for
    `workspace/`. The merge and rm-cached commands are accepted and recorded.

    The catch-all deliberately returns success rather than raising: the caller
    wraps its body in `except Exception`, which would swallow an `AssertionError`
    into a log line instead of failing the test.
    """

    TRUE_ROOT_GUARD = "[ -d /home/developer/.git ]"
    WORKSPACE_GUARD = "[ -d /home/developer/workspace/.git ]"

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        if (
            "git rev-parse --show-toplevel" in command
            or "find /home/developer/workspace" in command
        ):
            return await super().__call__(container_name, command, timeout)
        self.calls.append(command)
        if self.WORKSPACE_GUARD in command:
            return {"exit_code": 1, "output": ""}
        if self.TRUE_ROOT_GUARD in command:
            return {"exit_code": 0, "output": ""}
        # `.gitignore` merge / `git rm --cached` sweep.
        return {"exit_code": 0, "output": ""}


# --------------------------------------------------------------------------
# Case 1 — the defect itself.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_populated_workspace_with_home_rooted_repo_returns_home():
    """A home-rooted repo + a populated non-git `workspace/` resolves to home.

    This is the bug: the content heuristic classifies any non-empty
    `workspace/` as a legacy repo root, so every path-based consumer is
    pointed at a data directory instead of the repository.
    """
    gs = _load_git_service()
    fake = _FakeExec(toplevel="/home/developer", workspace_populated=True)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer", (
        "git reports the repo root as /home/developer, but _detect_git_dir "
        f"returned {result!r} — the populated workspace/ data directory was "
        "mistaken for a legacy repo root"
    )


# --------------------------------------------------------------------------
# Case 2 — the legacy layout must keep working.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_workspace_rooted_repo_returns_workspace():
    """A genuinely workspace-rooted repo still resolves to `workspace/`."""
    gs = _load_git_service()
    fake = _FakeExec(toplevel="/home/developer/workspace", workspace_populated=True)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer/workspace", (
        "a repo actually rooted at workspace/ must keep resolving there; "
        f"got {result!r}"
    )


# --------------------------------------------------------------------------
# Cases 3 & 4 — the no-repository fallback (init placement guarantee).
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_repo_populated_workspace_falls_back_to_workspace():
    """No repo anywhere + populated workspace/ keeps the historical answer.

    `initialize_git_in_container` uses this value to choose where to run
    `git init`, so fresh-agent placement must remain byte-compatible.
    """
    gs = _load_git_service()
    fake = _FakeExec(toplevel=None, workspace_populated=True)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer/workspace", (
        "with no repository present the retained content heuristic must give "
        f"the historical answer; got {result!r}"
    )


@pytest.mark.asyncio
async def test_no_repo_empty_workspace_falls_back_to_home():
    """No repo anywhere + empty workspace/ keeps the historical answer."""
    gs = _load_git_service()
    fake = _FakeExec(toplevel=None, workspace_populated=False)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer", (
        "with no repository and an empty workspace/ the historical answer is "
        f"/home/developer; got {result!r}"
    )


# --------------------------------------------------------------------------
# Cases 5a/5b/5c — probe robustness.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_garbage_probe_output_falls_back_to_heuristic():
    """A zero-exit probe with unusable output is rejected, not acted on.

    `workspace_populated=True` makes this discriminating: the fallback answers
    `workspace/`, so the assertion cannot be satisfied by the probe value.
    """
    gs = _load_git_service()
    fake = _FakeExec(
        probe_exit=0,
        probe_output="bash: git: command not found\n",
        workspace_populated=True,
    )

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer/workspace", (
        "unparseable probe output must fall through to the content heuristic; "
        f"got {result!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("toplevel", ["/home/developer2/repo", "/", "/home"])
async def test_lookalike_root_is_rejected(toplevel):
    """Roots outside `/home/developer` are refused, including look-alikes.

    Guards the prefix test: a naive `startswith` or a blind accept would
    return the probe value instead of falling back.
    """
    gs = _load_git_service()
    fake = _FakeExec(toplevel=toplevel, workspace_populated=True)

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer/workspace", (
        f"probe root {toplevel!r} is outside /home/developer and must be "
        f"rejected in favour of the fallback; got {result!r}"
    )


@pytest.mark.asyncio
async def test_exec_failure_propagates():
    """A genuine raise still propagates to the caller.

    Callers depend on this: the push path reports a `detect`-stage failure and
    the inspection path must never claim agreement on an unknown state.
    """
    gs = _load_git_service()
    fake = _FakeExec(raises=True)

    with patch.object(gs, "execute_command_in_container", fake):
        with pytest.raises(RuntimeError):
            await gs._detect_git_dir("agent-test")


# --------------------------------------------------------------------------
# Case 6 — noisy but valid probe output.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_noisy_probe_output_is_parsed():
    """A valid root is found even when the exec channel adds noise.

    `workspace_populated=True` keeps this non-vacuous: without it the fallback
    would also answer `/home/developer`.
    """
    gs = _load_git_service()
    fake = _FakeExec(
        probe_exit=0,
        probe_output="warning: something\n/home/developer\n",
        workspace_populated=True,
    )

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._detect_git_dir("agent-test")

    assert result == "/home/developer", (
        "the valid root line must be picked out of noisy probe output; "
        f"got {result!r}"
    )


# --------------------------------------------------------------------------
# Case 7 — the content probe is not issued when git answers.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_content_probe_skipped_when_repo_found():
    """When git resolves the root, the content heuristic is never consulted."""
    gs = _load_git_service()
    fake = _FakeExec(toplevel="/home/developer")

    with patch.object(gs, "execute_command_in_container", fake):
        await gs._detect_git_dir("agent-test")

    assert not any("find /home/developer/workspace" in c for c in fake.calls), (
        "the content heuristic must not run once git has resolved the repo "
        f"root; commands issued: {fake.calls}"
    )
    assert (
        len(fake.calls) == 1
    ), f"expected exactly one exec (the git-root probe); got {fake.calls}"


# --------------------------------------------------------------------------
# Case 8 — the downstream consumer that motivated the fix.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_migrate_gitignore_targets_the_real_repo_root():
    """The `.gitignore` migration runs at the real root, not at `workspace/`.

    On an affected agent the `[ -d <dir>/.git ]` guard is issued against
    `workspace/`, fails, and the whole migration silently no-ops — which is why
    fleet-wide ignore patterns never reached these agents.

    Asserted on the presence of `cd /home/developer &&` because the migration's
    command builders emit a RELATIVE `.gitignore` path, so an absolute-path
    assertion would be unfalsifiable. The trailing `&&` matters on both sides:
    the root probe itself starts `cd /home/developer/workspace 2>/dev/null ||`
    and is recorded unconditionally, so a bare `cd /home/developer/workspace`
    negative assertion would match the probe before AND after the fix.
    """
    gs = _load_git_service()
    fake = _FakeMigrateExec(toplevel="/home/developer", workspace_populated=True)

    with patch.object(gs, "execute_command_in_container", fake):
        await gs._migrate_workspace_gitignore("someagent")

    assert any("cd /home/developer &&" in c for c in fake.calls), (
        "the migration never ran at the real repo root — the .git guard was "
        f"issued against the wrong directory and the migration no-opped; "
        f"commands issued: {fake.calls}"
    )
    assert not any("cd /home/developer/workspace &&" in c for c in fake.calls), (
        "no migration command may target the workspace/ subdirectory; "
        f"commands issued: {fake.calls}"
    )
