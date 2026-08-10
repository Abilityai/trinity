"""
Regression tests for #2075 — `_detect_git_dir` picked the wrong repo root.

The bug: the root was chosen by a *content* probe — any non-empty
`/home/developer/workspace` was declared a legacy repo root, even when the
real repository was rooted at `/home/developer` and `workspace/` was just a
data directory. Everything that treats the result as a filesystem path then
read/wrote the wrong tree: the compatibility snapshot, its `.gitignore`
auto-fix, and the per-Push `.gitignore` migration (#462), which silently
no-op'd because its `[ -d <dir>/.git ]` guard tested the wrong path.

The fix asks git (`rev-parse --show-toplevel`, which walks *up*) and keeps the
content heuristic only as the no-repository fallback, so fresh-agent placement
in `initialize_git_in_container` stays byte-compatible.

The shell tests below run the *real* probe script produced by the production
code against real git repos in a temp directory — the only difference from
production is the host filesystem vs. the agent container.
"""
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest


_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


def _load_git_service(monkeypatch):
    """Import git_service with heavy dependencies mocked out.

    Every `sys.modules` mutation goes through ``monkeypatch`` (#762 lint), so
    the stubs — and the stub-built module this import leaves behind — are
    unwound when the test ends instead of leaking into later tests.
    """
    stubs = {
        mod: Mock()
        for mod in (
            "docker", "docker.errors", "docker.types",
            "redis", "redis.asyncio",
            "database",
            "services.docker_service",
        )
    }
    stubs["database"].db = Mock()
    stubs["database"].AgentGitConfig = Mock
    stubs["database"].GitSyncResult = Mock

    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)

    for key in [k for k in list(sys.modules) if k.startswith("services.git_service")]:
        monkeypatch.delitem(sys.modules, key)
    # Records the (possibly absent) pre-test state of the key the import below
    # creates, so monkeypatch's undo removes the stub-built module either way.
    monkeypatch.setitem(sys.modules, "services.git_service", None)
    monkeypatch.delitem(sys.modules, "services.git_service")

    import services.git_service as gs
    return gs


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "developer"
    (home / "workspace").mkdir(parents=True)
    return home


def _detect_against_fs(gs, home: Path) -> str:
    """Run the real `_detect_git_dir` with the container exec replaced by a
    local subprocess, and the two path constants pointed at ``home``.
    """
    async def _exec(container_name: str, command: str, timeout: int = 5):
        proc = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            cwd=str(home),
        )
        return {"exit_code": proc.returncode, "output": proc.stdout + proc.stderr}

    import asyncio
    with patch.object(gs, "AGENT_HOME_DIR", str(home)), \
         patch.object(gs, "LEGACY_WORKSPACE_DIR", str(home / "workspace")), \
         patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        return asyncio.run(gs._detect_git_dir("agent-test"))


# ---------------------------------------------------------------------------
# The reported bug, against a real filesystem
# ---------------------------------------------------------------------------

def test_home_rooted_repo_with_populated_workspace_resolves_to_home(tmp_path, monkeypatch):
    """The repro: repo at the home root, `workspace/` holding data files."""
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    _git(home, "init", "-q")
    (home / "workspace" / "cache.db").write_text("data")
    (home / "template.yaml").write_text("name: test\n")

    assert _detect_against_fs(gs, home) == str(home)


def test_genuinely_workspace_rooted_repo_still_resolves_to_workspace(tmp_path, monkeypatch):
    """A real legacy agent — repo rooted *inside* `workspace/` — is unchanged.

    `--show-toplevel` walks up from the probe point, so the nearest enclosing
    repository wins.
    """
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    _git(home / "workspace", "init", "-q")
    (home / "workspace" / "template.yaml").write_text("name: legacy\n")

    assert _detect_against_fs(gs, home) == str(home / "workspace")


def test_nested_workspace_repo_wins_over_home_repo(tmp_path, monkeypatch):
    """Known limitation, pinned: an agent re-initialised while misdetected has
    a genuine nested repo and is indistinguishable from a legacy agent — git's
    own answer is `workspace/`, and this fix keeps it there.
    """
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    _git(home, "init", "-q")
    _git(home / "workspace", "init", "-q")

    assert _detect_against_fs(gs, home) == str(home / "workspace")


def test_no_repo_defers_to_the_content_heuristic(tmp_path, monkeypatch):
    """No repository anywhere: git answers nothing and placement is decided by
    the (hardcoded-path) legacy heuristic — the branch
    `initialize_git_in_container` needs for a repo that does not exist yet.
    """
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    (home / "workspace" / "seed.txt").write_text("x")

    # The heuristic probes the real container paths, so on the host it answers
    # for `/home/developer`, not the temp tree. What this proves is that the
    # git probe declined and the fallback decided.
    assert _detect_against_fs(gs, home) in (
        "/home/developer",
        "/home/developer/workspace",
    )


@pytest.mark.asyncio
async def test_fallback_heuristic_is_byte_compatible(monkeypatch):
    """The fresh-agent placement probe is unchanged from the pre-#2075 code."""
    gs = _load_git_service(monkeypatch)
    seen = []

    async def _exec(container_name: str, command: str, timeout: int = 5):
        seen.append(command)
        if "rev-parse" in command:
            return {"exit_code": 128, "output": ""}
        return {"exit_code": 0, "output": "0\n"}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer"

    assert seen[-1] == (
        'bash -c "[ -d /home/developer/workspace ] && '
        'find /home/developer/workspace -mindepth 1 -maxdepth 1 | '
        'head -1 | wc -l"'
    )


def test_no_workspace_directory_at_all(tmp_path, monkeypatch):
    gs = _load_git_service(monkeypatch)
    home = tmp_path / "developer"
    home.mkdir()
    _git(home, "init", "-q")

    assert _detect_against_fs(gs, home) == str(home)


# ---------------------------------------------------------------------------
# Contract of the git probe itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_toplevel_outside_agent_home_is_rejected(monkeypatch):
    """A toplevel outside `/home/developer` is never trusted — the caller
    falls back rather than operating on an arbitrary path."""
    gs = _load_git_service(monkeypatch)
    calls = []

    async def _exec(container_name: str, command: str, timeout: int = 5):
        calls.append(command)
        if "rev-parse" in command:
            return {"exit_code": 0, "output": "/tmp/somewhere-else\n"}
        return {"exit_code": 0, "output": "0\n"}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer"
    assert any("rev-parse --show-toplevel" in c for c in calls)
    # The fallback heuristic ran only because the probe was rejected.
    assert any("find /home/developer/workspace" in c for c in calls)


@pytest.mark.asyncio
async def test_git_failure_falls_back(monkeypatch):
    gs = _load_git_service(monkeypatch)

    async def _exec(container_name: str, command: str, timeout: int = 5):
        if "rev-parse" in command:
            return {"exit_code": 128, "output": ""}
        return {"exit_code": 0, "output": "1\n"}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer/workspace"


@pytest.mark.asyncio
async def test_successful_probe_skips_the_content_heuristic(monkeypatch):
    """A repo-rooted answer costs exactly one exec — the old heuristic must
    not run and must not be able to override git."""
    gs = _load_git_service(monkeypatch)
    calls = []

    async def _exec(container_name: str, command: str, timeout: int = 5):
        calls.append(command)
        return {"exit_code": 0, "output": "/home/developer\n"}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer"
    assert len(calls) == 1
