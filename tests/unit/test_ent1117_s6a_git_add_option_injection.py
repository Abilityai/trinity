"""abilityai/trinity-enterprise#1117 — `git add` option injection in the agent
server's Push (`POST /api/git/sync`).

`sync_to_github` stages the caller's `paths`. Built as
`["git", "add"] + list(request.paths)`, a `paths` element that starts with `-`
is read by git as an OPTION, not a pathspec: `paths: ["-f", ".env"]` force-stages
the gitignored credentials file, and the same sync then commits and pushes it.
The caller is the agent's owner, an admin, or the agent's own key (the
`git_sync` MCP tool), none of whom need a shell to do it.

These tests drive the REAL endpoint body against a scratch repo with a local
bare `origin`, with only the hardcoded `/home/developer` redirected and git's
global/system config disabled so the operator's own git config can't leak in.
The assertion is on what actually reached `origin`, which is the harm.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Guarded eviction — same pattern as test_4001_no_auto_maintenance.py
# (enforced by tests/lint_sys_modules.py).
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _AS_INIT = _BASE_IMAGE / "agent_server" / "__init__.py"
    _as_spec = importlib.util.spec_from_file_location(
        "agent_server", str(_AS_INIT),
        submodule_search_locations=[str(_BASE_IMAGE / "agent_server")],
    )
    _as_mod = importlib.util.module_from_spec(_as_spec)
    sys.modules["agent_server"] = _as_mod
    _as_spec.loader.exec_module(_as_mod)

from agent_server.routers import git as git_mod  # noqa: E402

# Restore whatever this module installed after each test (tests/lint_sys_modules.py).
_STUBBED_MODULE_NAMES = [
    name for name in sys.modules if name == "agent_server" or name.startswith("agent_server.")
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A cloned agent home: `.env` is gitignored and present on disk, `origin`
    is a local bare repo, `main` is pushed and tracked."""
    empty_cfg = tmp_path / "gitconfig"
    empty_cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)

    h = tmp_path / "home"
    h.mkdir()
    _git(["init", "-q", "-b", "main"], h)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        _git(["config", key, value], h)
    (h / ".gitignore").write_text(".env\n")
    (h / "notes.md").write_text("v1\n")
    _git(["add", ".gitignore", "notes.md"], h)
    _git(["commit", "-qm", "init"], h)
    _git(["remote", "add", "origin", str(origin)], h)
    _git(["push", "-q", "-u", "origin", "main"], h)

    (h / ".env").write_text("ANTHROPIC_API_KEY=sk-test-not-a-real-key\n")

    real_path = Path
    monkeypatch.setattr(
        git_mod, "Path",
        lambda *a: h if a == ("/home/developer",) else real_path(*a),
    )
    return h


def _sync(**kwargs):
    # `__wrapped__` skips `_with_repo_lock` (an in-process lock, not git).
    return asyncio.run(
        git_mod.sync_to_github.__wrapped__(git_mod.GitSyncRequest(**kwargs))
    )


def _origin_files(home):
    """Every path in the tip of origin/main, read from the bare repo itself."""
    origin = home.parent / "origin.git"
    return set(_git(["ls-tree", "-r", "--name-only", "main"], origin).split())


def test_dash_f_cannot_force_stage_and_push_an_ignored_env(home):
    """The reported exploit: `paths: ["-f", ".env"]`."""
    assert ".env" not in _origin_files(home)

    result = _sync(paths=["-f", ".env"], message="m")

    pushed = _origin_files(home)
    assert ".env" not in pushed, (
        "`-f` in `paths` was read as a git option: the gitignored .env was "
        f"force-staged, committed and pushed to origin (sync result: {result})"
    )
    tracked = set(_git(["ls-files"], home).split())
    assert ".env" not in tracked
    assert result["commit_sha"] is None and result["files_changed"] == 0


def test_long_option_in_paths_is_a_pathspec_not_an_option(home):
    """Same class through a different option: `--force` spelt long."""
    _sync(paths=["--force", ".env"], message="m")
    assert ".env" not in _origin_files(home)


def test_plain_paths_still_stage_and_push(home):
    """The fix must not break the documented subset sync, including a path
    whose dash is not the first character."""
    (home / "notes.md").write_text("v2\n")
    (home / "sub").mkdir()
    (home / "sub" / "-x.md").write_text("dash in the middle\n")

    result = _sync(paths=["notes.md", "sub/-x.md"], message="m")

    assert result["success"] is True and result["files_changed"] == 2
    pushed = _origin_files(home)
    assert {"notes.md", "sub/-x.md"} <= pushed
    assert ".env" not in pushed


def test_characterization_no_paths_branch_still_runs_git_add_all(home, monkeypatch):
    """Characterization (green before and after the fix): with no `paths` the
    endpoint still stages with exactly `git add -A`, which respects .gitignore,
    and pushes the tracked change."""
    seen = []
    real_run = git_mod.subprocess.run

    def recording_run(argv, *a, **kw):
        seen.append(list(argv))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(git_mod.subprocess, "run", recording_run)
    (home / "notes.md").write_text("v2\n")

    result = _sync(message="m")

    adds = [argv for argv in seen if argv[:2] == ["git", "add"]]
    assert adds == [["git", "add", "-A"]]
    assert result["success"] is True and result["files_changed"] == 1
    pushed = _origin_files(home)
    assert "notes.md" in pushed and ".env" not in pushed
