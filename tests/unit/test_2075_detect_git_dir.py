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
import os
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


# The names below are the lint_sys_modules.py "named snapshot/restore" pair: every key
# `_load_git_service` evicts is under this prefix, and the fixture restores the family.
_STUBBED_MODULE_NAMES = ["services.git_service"]  # prefix — the whole family is restored


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Put the whole `services.git_service*` family back exactly as it was (#2859).

    `_load_git_service` evicts the family and re-imports it under stubs. The
    `monkeypatch.delitem` calls restore every key that EXISTED before the test,
    but the submodules the re-import CREATES were never recorded, so they stayed
    cached after monkeypatch's undo removed the package key — and the next file
    to import the package got a fresh parent with no `.token_scrub`/`.conflicts`
    attribute (13 failures in test_ent615, #2859). This runs after monkeypatch's
    undo (autouse fixtures tear down last) and makes the end state the start state.
    """
    saved = {k: v for k, v in sys.modules.items() if k.startswith("services.git_service")}
    try:
        yield
    finally:
        for key in [k for k in list(sys.modules) if k.startswith("services.git_service")]:
            del sys.modules[key]
        sys.modules.update(saved)


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

    # #1028: git_service is a package; the alias names the module that
    # owns the functions under test, so patches land where the code looks.
    import services.git_service.gitignore as gs
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


def test_no_repo_places_a_new_one_at_home_whatever_workspace_holds(tmp_path, monkeypatch):
    """No repository anywhere: git answers nothing and placement is the home
    directory — the only root the agent server reads (#2938). A populated
    `workspace/` used to route a NEW repo there, and every git route on the
    agent side then said "not enabled" forever.
    """
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    (home / "workspace" / "seed.txt").write_text("x")
    assert _detect_against_fs(gs, home) == "/home/developer"


@pytest.mark.asyncio
async def test_no_repo_runs_no_content_probe(monkeypatch):
    """#2938: the pre-#2075 `find workspace | head | wc` heuristic is gone —
    the only exec a repo-less container sees is git's own toplevel probe."""
    gs = _load_git_service(monkeypatch)
    seen = []

    async def _exec(container_name: str, command: str, timeout: int = 5):
        seen.append(command)
        if "rev-parse" in command:
            return {"exit_code": 128, "output": ""}
        return {"exit_code": 0, "output": "1\n"}     # a populated workspace, if anyone asked

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer"

    assert all("rev-parse" in c for c in seen), seen
    assert not any("workspace -mindepth" in c for c in seen)


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
    # A rejected probe is "no repository": placement is home, with no second
    # exec (#2938 retired the content heuristic that used to run here).
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_git_failure_falls_back(monkeypatch):
    """A git probe that fails is "no repository" — placement is home (#2938),
    never the workspace the agent server cannot see."""
    gs = _load_git_service(monkeypatch)

    async def _exec(container_name: str, command: str, timeout: int = 5):
        if "rev-parse" in command:
            return {"exit_code": 128, "output": ""}
        return {"exit_code": 0, "output": "1\n"}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._detect_git_dir("agent-x") == "/home/developer"


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


# ---------------------------------------------------------------------------
# #2245 — a filesystem boundary must not look like "no repository"
# ---------------------------------------------------------------------------
#
# Git stops discovery at a mount point by default, and says so exactly:
#
#   fatal: not a git repository (or any parent up to mount point /home/developer)
#   Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).
#
# So on an agent whose `workspace/` is its own mount (bind mount, distinct
# volume, overlay), the probe started inside it never reaches a repository rooted
# at the home directory: `_git_toplevel` returns None, the caller falls through to
# the content heuristic, and a populated `workspace/` answers
# `/home/developer/workspace` — the #2075 misclassification, reintroduced by a
# mount rather than by a heuristic.
#
# Verified in the real topology before writing these tests, since a unit test
# cannot create a mount (user namespaces are unavailable in CI sandboxes and
# `mount` needs privilege). In a `trinity-agent-base` container with a tmpfs at
# `/home/developer/workspace` and a repo at `/home/developer`:
#
#   st_dev home      : 220
#   st_dev workspace : 1048621          <- a genuine boundary
#   pre-fix  probe   : exit=128, "Stopping at filesystem boundary ..."
#   post-fix probe   : exit=0,   "/home/developer"
#
# What is testable here without a mount is the plumbing and the guard: that the
# variable actually reaches the git process, and that crossing the boundary did
# not loosen the containment check which is what makes crossing safe.


def _git_shim(bin_dir: Path, record: Path) -> None:
    """A fake `git` that records the environment it was invoked with."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'{{ printf "GIT_DISCOVERY_ACROSS_FILESYSTEM=%s\\n" "${{GIT_DISCOVERY_ACROSS_FILESYSTEM:-<unset>}}";'
        f'  printf "ARGS=%s\\n" "$*"; }} >> {shlex.quote(str(record))}\n'
        "echo /home/developer\n"
    )
    shim.chmod(0o755)


@pytest.mark.asyncio
async def test_the_probe_exports_the_discovery_flag_to_git(tmp_path, monkeypatch):
    """The variable must reach the git PROCESS, not merely appear in the script.

    A static substring check would pass on a script where the assignment landed
    after a pipe, inside the wrong quoting layer, or on the shell rather than on
    `git` — all of which leave the flag unset where it matters. So this runs the
    real script with a `git` shim on PATH and reads back the environment git
    actually saw.
    """
    gs = _load_git_service(monkeypatch)
    record = tmp_path / "env.txt"
    _git_shim(tmp_path / "bin", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    async def _exec(container_name: str, command: str, timeout: int = 5):
        proc = subprocess.run(shlex.split(command), capture_output=True, text=True)
        return {"exit_code": proc.returncode, "output": proc.stdout + proc.stderr}

    with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
        assert await gs._git_toplevel("agent-x") == "/home/developer"

    seen = record.read_text()
    assert "GIT_DISCOVERY_ACROSS_FILESYSTEM=1" in seen, seen
    # The probe start point is unchanged by the fix: still `workspace/` when it
    # exists, which is what makes the walk-up (and therefore the boundary) matter.
    assert "rev-parse --show-toplevel" in seen, seen
    assert "-C /home/developer/workspace" in seen or "-C /home/developer" in seen, seen


@pytest.mark.asyncio
async def test_crossing_the_boundary_does_not_loosen_containment(monkeypatch):
    """Discovery may now leave the mount; the answer still may not leave home.

    This is the whole safety argument for the flag, so it is pinned separately
    from `test_toplevel_outside_agent_home_is_rejected`: crossing a boundary is
    precisely what lets git return a path from an enclosing tree, so the refusal
    has to hold for answers that a boundary-crossing walk could now produce.
    """
    gs = _load_git_service(monkeypatch)

    for outside in ("/", "/home", "/home/developer-other", "/etc", "/opt/repo"):
        async def _exec(container_name: str, command: str, timeout: int = 5, _o=outside):
            assert "GIT_DISCOVERY_ACROSS_FILESYSTEM=1" in command
            return {"exit_code": 0, "output": f"{_o}\n"}

        with patch.object(gs, "execute_command_in_container", AsyncMock(side_effect=_exec)):
            assert await gs._git_toplevel("agent-x") is None, outside


def test_a_boundary_is_not_evidence_of_a_missing_repo(tmp_path, monkeypatch):
    """The bug in one assertion: git failing from inside `workspace/` while a
    home-rooted repo exists must NOT resolve to `workspace/`.

    On the host there is no boundary, so the failure is simulated at the seam git
    would fail at — a non-zero probe — while the filesystem still holds the
    home-rooted repo and a populated `workspace/`. Pre-#2245 that combination is
    what the content heuristic turned into `/home/developer/workspace`; the point
    of the flag is that the probe no longer fails in the first place, and this
    records what the wrong answer looked like.
    """
    gs = _load_git_service(monkeypatch)
    home = _make_home(tmp_path)
    _git(home, "init", "-q")
    (home / "workspace" / "cache.db").write_text("data")

    # Sanity: with a working probe (no boundary) the real script gets it right.
    assert _detect_against_fs(gs, home) == str(home)

    # And this is the pre-fix path — kept as documentation of the failure mode
    # the flag removes: a boundary-failed probe reads as "no repository". Before
    # #2938 that degraded to the content heuristic, which answered
    # `/home/developer/workspace` and rooted a NEW repo where the agent server
    # never looks; now it answers the home directory, the same place the probe
    # itself would have answered had it crossed the boundary — so even the
    # failure mode is no longer a different placement.
    import asyncio

    async def _boundary_failure(container_name: str, command: str, timeout: int = 5):
        if "rev-parse" in command:
            return {"exit_code": 128, "output":
                    "fatal: not a git repository (or any parent up to mount point "
                    f"{home})\nStopping at filesystem boundary "
                    "(GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n"}
        return {"exit_code": 0, "output": "1\n"}

    with patch.object(gs, "AGENT_HOME_DIR", str(home)), \
         patch.object(gs, "LEGACY_WORKSPACE_DIR", str(home / "workspace")), \
         patch.object(gs, "execute_command_in_container",
                      AsyncMock(side_effect=_boundary_failure)):
        degraded = asyncio.run(gs._detect_git_dir("agent-x"))
    # The literal, not `home`: `NEW_REPO_ROOT` is the agent server's fixed root,
    # not a patched module constant.
    assert degraded == "/home/developer"
