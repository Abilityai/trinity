"""
Platform git children never start git auto-maintenance (#4001).

`fetch`, `pull`, `commit`, `merge` and `rebase` each run `git maintenance run
--auto` when a repo-local config re-enables it. A bounded platform child then
waits on a repack, its timeout kills the group, and the SIGKILL strands
`objects/maintenance.lock`, which git never expires: auto-maintenance is off
for that repo from then on, silently.

Related flow: docs/memory/feature-flows/git-sync-health.md
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Guarded eviction — same pattern as test_1595_git_maintenance.py (enforced by
# tests/lint_sys_modules.py).
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

from agent_server.utils.registered_run import run_registered  # noqa: E402
from agent_server.routers import git as git_mod  # noqa: E402

# Commands that call git's `run_auto_maintenance`.
_AUTO_MAINTENANCE_VERBS = {"fetch", "pull", "commit", "merge", "rebase"}


def _sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A repo whose LOCAL config re-enables auto-maintenance at the lowest
    threshold, so any command that honours it starts `maintenance run`."""
    d = tmp_path / "repo"
    d.mkdir()
    _sh(["git", "init", "-q"], d)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("maintenance.auto", "true"), ("gc.auto", "1")):
        _sh(["git", "config", key, value], d)
    for i in range(3):
        (d / f"f{i}").write_text(str(i))
        _sh(["git", "add", f"f{i}"], d)
        _sh(["git", "commit", "-qm", str(i)], d)
    return d


def _traced_commit(repo, runner, monkeypatch):
    """Commit through `runner` with GIT_TRACE on; return the trace."""
    monkeypatch.setenv("GIT_TRACE", "1")
    (repo / "new").write_text("x")
    _sh(["git", "add", "new"], repo)
    return runner(repo)


def test_fixture_starts_maintenance_without_the_flag(repo, monkeypatch):
    """Control: the fixture really does trigger auto-maintenance on commit."""
    trace = _traced_commit(
        repo,
        lambda r: subprocess.run(["git", "commit", "-qm", "c"], cwd=r,
                                 capture_output=True, text=True).stderr,
        monkeypatch,
    )
    assert "maintenance run --auto" in trace


def test_run_registered_git_child_starts_no_maintenance(repo, monkeypatch):
    trace = _traced_commit(
        repo,
        lambda r: run_registered(["git", "commit", "-qm", "c"], cwd=str(r)).stderr,
        monkeypatch,
    )
    assert "maintenance run" not in trace


def test_run_registered_reports_the_callers_argv(repo):
    """The splice is for the child only: `_with_stash` names the verb of a
    timed-out command from `exc.cmd[1]` ("git pull timed out")."""
    done = run_registered(["git", "status", "--short"], cwd=str(repo))
    assert done.args == ["git", "status", "--short"]
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run_registered(["git", "-c", "alias.zz=!sleep 5", "zz"], cwd=str(repo), timeout=0.5)
    assert exc.value.cmd[:2] == ["git", "-c"] and exc.value.cmd[-1] == "zz"
    assert "maintenance.auto=false" not in exc.value.cmd


def test_run_registered_leaves_non_git_argv_alone(tmp_path):
    out = run_registered(["echo", "-c", "x"], cwd=str(tmp_path))
    assert out.stdout.strip() == "-c x"


def test_git_helper_child_starts_no_maintenance(repo, monkeypatch):
    """`_git` backs the reset-to-main path's fetch."""
    trace = _traced_commit(
        repo, lambda r: git_mod._git(["commit", "-qm", "c"], r).stderr, monkeypatch,
    )
    assert "maintenance run" not in trace


class _Recorder:
    """Stands in for `subprocess.run` inside the operator endpoints: records
    every git argv and answers just enough for each strategy to proceed."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        out = ""
        if "--abbrev-ref" in argv or "--show-current" in argv or "symbolic-ref" in argv:
            out = "trinity/agent\n"
        elif "status" in argv:
            out = "M  file.txt\n"
        elif "rev-parse" in argv:
            out = "0" * 40 + "\n"
        elif "rev-list" in argv:
            out = "0\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def maintenance_capable(self):
        """(verb, argv) for every recorded git call whose verb runs
        auto-maintenance."""
        found = []
        for argv in self.calls:
            if not argv or argv[0] != "git":
                continue
            rest = argv[1:]
            while rest and rest[0] == "-c":
                rest = rest[2:]
            if rest and rest[0] in _AUTO_MAINTENANCE_VERBS:
                found.append((rest[0], argv))
        return found


def _flagged(argv):
    return any(
        argv[i] == "-c" and argv[i + 1] == "maintenance.auto=false"
        for i in range(len(argv) - 1)
    )


@pytest.fixture
def operator_home(tmp_path, monkeypatch):
    """Point the endpoints' hardcoded /home/developer at a temp dir and swap
    in the recorder. The endpoints' own git calls never reach a real git."""
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    real_path = Path
    monkeypatch.setattr(
        git_mod, "Path",
        lambda *a: home if a == ("/home/developer",) else real_path(*a),
    )
    rec = _Recorder()
    monkeypatch.setattr(git_mod.subprocess, "run", rec)
    # Everything routed through run_registered is covered at the seam; record
    # it too so a strategy can proceed past it.
    monkeypatch.setattr(git_mod, "run_registered",
                        lambda argv, **kw: rec(["git", "-c", "maintenance.auto=false", *argv[1:]]))
    return rec


def _drive(coro):
    try:
        asyncio.run(coro)
    except Exception:  # noqa: BLE001 — only the argv the endpoint built matters
        pass


def test_operator_endpoints_flag_every_maintenance_capable_call(operator_home):
    for strategy in ("clean", "stash_reapply", "force_reset"):
        _drive(git_mod.pull_from_github.__wrapped__(git_mod.GitPullRequest(strategy=strategy)))
    for strategy in ("normal", "pull_first", "force_push"):
        _drive(git_mod.sync_to_github.__wrapped__(
            git_mod.GitSyncRequest(strategy=strategy, message="m")))

    found = operator_home.maintenance_capable()
    verbs = {verb for verb, _ in found}
    assert {"fetch", "pull", "commit"} <= verbs, verbs
    unflagged = [argv for _, argv in found if not _flagged(argv)]
    assert unflagged == []


class TestMaintenanceLockReap:
    @pytest.fixture
    def home(self, tmp_path):
        (tmp_path / ".git" / "objects" / "pack").mkdir(parents=True)
        return tmp_path

    def test_stale_maintenance_lock_removed(self, home):
        lock = home / ".git" / "objects" / "maintenance.lock"
        lock.write_text("")
        old = time.time() - 2 * 3600
        os.utime(lock, (old, old))
        git_mod._reap_stale_git_litter(home, repack_budget_seconds=1800)
        assert not lock.exists()

    def test_fresh_maintenance_lock_kept(self, home):
        lock = home / ".git" / "objects" / "maintenance.lock"
        lock.write_text("")
        git_mod._reap_stale_git_litter(home, repack_budget_seconds=1800)
        assert lock.exists()
