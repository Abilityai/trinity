"""#4000 — git maintenance and git-health stats run on EVERY auto-sync tick.

`run_one_cycle` used to return before the sync cycle when the owner's
`auto_sync_enabled` flag was off, and the reap / measure / repack lived inside
the sync cycle after a successful push. Self-pushing agents (flag off) and
agents whose push keeps failing therefore never got maintenance or stats, with
the base image's own git auto-maintenance disabled. Covers:

  - the off-path tick maintains and records stats + `git_stats_at`
  - the on-path tick maintains even when the push fails
  - the health writer leaves every sync / pull field alone, re-reads at write
    time, and stamps `git_stats_at` only when something was measured
  - the maintenance backoff counter carries across on/off ticks
  - repo_busy skip, disk preflight counting loose bytes, reflog expire + prune
    as the follow-up (never `git gc`)
  - the loops are scheduled with no env and no platform coords; first-tick jitter
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Guarded eviction — same pattern as test_1595_git_maintenance.py (see the
# #728 / trinity-enterprise#620 note there; enforced by tests/lint_sys_modules.py).
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

from agent_server.routers import git as _git_import  # noqa: E402,F401
from agent_server import auto_sync  # noqa: E402

pytestmark = pytest.mark.unit

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


def _git():
    """The live router module: the loop's call-time relative import resolves
    through sys.modules, which a sibling file may have re-executed."""
    return sys.modules["agent_server.routers.git"]


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=20)


@pytest.fixture
def repo(tmp_path):
    local, remote = tmp_path / "local", tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _run(["git", "init", "--bare", "-b", "main"], remote)
    _run(["git", "init", "-b", "main"], local)
    _run(["git", "config", "user.email", "test@test.com"], local)
    _run(["git", "config", "user.name", "Test"], local)
    _run(["git", "remote", "add", "origin", str(remote)], local)
    (local / "README.md").write_text("hello")
    _run(["git", "add", "."], local)
    _run(["git", "commit", "-m", "initial"], local)
    _run(["git", "push", "-u", "origin", "main"], local)
    (local / ".trinity").mkdir()
    return local


@pytest.fixture
def no_platform(monkeypatch):
    """No coords: the env (GIT_SYNC_AUTO) decides the flag."""
    for var in ("TRINITY_BACKEND_URL", "TRINITY_MCP_API_KEY", "AGENT_NAME", "GIT_SYNC_AUTO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_SYNC_LOCK_WAIT_SECONDS", "0")


def _run_ticks(home, monkeypatch, ticks=1, between=None):
    """Drive the real loop for `ticks` cycles. Sleep #1 is the boot sleep;
    sleep #N+1 follows tick N and may flip state via `between(N)`."""
    seen = {"n": 0}

    async def fake_sleep(_seconds):
        seen["n"] += 1
        if seen["n"] > ticks:
            raise asyncio.CancelledError
        if between and seen["n"] > 1:
            between(seen["n"] - 1)

    monkeypatch.setattr(auto_sync.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(auto_sync.run_auto_sync_loop(home, interval_seconds=1))


def _state(home):
    return _git()._read_sync_state_file(home)


def _recent(iso):
    stamp = datetime.fromisoformat(iso)
    return stamp.tzinfo is not None and datetime.now(timezone.utc) - stamp < timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Every tick maintains
# ---------------------------------------------------------------------------


class TestEveryTickMaintains:
    def test_off_path_tick_records_git_health(self, repo, no_platform, monkeypatch):
        _run_ticks(repo, monkeypatch)
        state = _state(repo)
        assert isinstance(state["pack_count"], int)
        assert isinstance(state["loose_objects"], int)
        if _run(["du", "-sb", "."], repo).returncode == 0:  # GNU du only (not macOS)
            assert isinstance(state["git_dir_bytes"], int) and state["git_dir_bytes"] > 0
        assert _recent(state["git_stats_at"])
        # Never a sync outcome: a never-synced agent must not turn green.
        assert state["last_sync_status"] == "never"
        assert state["last_sync_at"] is None

    def test_on_path_failing_push_still_maintains(self, repo, no_platform, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        considered = []

        def fake_maintenance(home, stats):
            considered.append(stats)
            return "repacked 25->1"

        monkeypatch.setattr(_git(), "_maybe_run_git_maintenance", fake_maintenance)
        shutil.rmtree(repo.parent / "remote")
        (repo / "newfile.txt").write_text("change")
        _run_ticks(repo, monkeypatch)
        assert len(considered) == 1
        state = _state(repo)
        assert state["last_sync_status"] == "failed"
        assert state["consecutive_failures"] == 1
        assert state["maintenance_status"] == "repacked 25->1"
        assert isinstance(state["pack_count"], int)
        assert _recent(state["git_stats_at"])

    def test_backoff_counter_carries_across_on_off_ticks(self, repo, no_platform, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        monkeypatch.setattr(_git(), "_maybe_run_git_maintenance", lambda h, s: "failed")
        _run_ticks(repo, monkeypatch, ticks=2,
                   between=lambda n: monkeypatch.setenv("GIT_SYNC_AUTO", "false"))
        state = _state(repo)
        assert state["maintenance_failures"] == 2
        assert datetime.fromisoformat(state["maintenance_next_attempt_at"]) > datetime.now(timezone.utc)
        assert state["last_sync_status"] == "success"  # tick 1's push, untouched by tick 2

    def test_no_repo_skips_everything(self, tmp_path, no_platform):
        ran = []

        async def go():
            async with auto_sync.httpx.AsyncClient() as client:
                return await auto_sync.run_one_cycle(
                    client, tmp_path, lambda h: ran.append("sync"), lambda h: ran.append("maint"))

        assert asyncio.run(go()) is None
        assert ran == []


# ---------------------------------------------------------------------------
# The health writer
# ---------------------------------------------------------------------------

_SYNC_FIELDS = {
    "last_sync_status": "failed",
    "last_sync_at": "2026-10-01T00:00:00+00:00",
    "last_error_summary": "push boom",
    "consecutive_failures": 3,
    "last_successful_push_at": "2026-09-30T00:00:00+00:00",
    "behind_after_fetch": 2,
    "last_pull_at": "2026-10-01T00:05:00+00:00",
    "last_pull_status": "failed",
    "last_pull_error": "pull boom",
    "behind_after_pull": 4,
}


def _seed(home, **extra):
    state = dict(_git()._SYNC_STATE_DEFAULT, **_SYNC_FIELDS, **extra)
    (home / ".trinity" / "sync-state.json").write_text(json.dumps(state))


class TestRecordGitHealth:
    def test_leaves_sync_and_pull_fields_untouched(self, repo):
        _seed(repo)
        state = _git()._record_git_health(
            repo, 4096, {"pack_count": 2, "loose_objects": 9}, None)
        for key, value in _SYNC_FIELDS.items():
            assert state[key] == value, key
        assert (state["git_dir_bytes"], state["pack_count"], state["loose_objects"]) == (4096, 2, 9)
        assert _recent(state["git_stats_at"])
        assert _state(repo) == state

    def test_unmeasured_stats_keep_prior_and_do_not_stamp(self, repo):
        _seed(repo, pack_count=7, loose_objects=70, git_dir_bytes=700)
        state = _git()._record_git_health(
            repo, None, {"pack_count": None, "loose_objects": None}, "backoff")
        assert (state["git_dir_bytes"], state["pack_count"], state["loose_objects"]) == (700, 7, 70)
        assert state["git_stats_at"] is None
        assert state["maintenance_status"] == "backoff"

    def test_write_landing_during_maintenance_survives(self, repo, no_platform, monkeypatch):
        git_mod = _git()

        def slow_repack(home, stats):
            # A write outside _REPO_LOCK (the boot-time lock-recovery fold uses
            # the same `_patch_sync_state`) lands while the repack runs.
            git_mod._patch_sync_state(home, {"last_pull_status": "success",
                                             "last_pull_at": "2026-10-27T12:00:00+00:00"})
            return "repacked 30->1"

        monkeypatch.setattr(git_mod, "_maybe_run_git_maintenance", slow_repack)
        result = git_mod._run_git_maintenance_once(repo)
        assert result["maintenance"] == "repacked 30->1"
        state = _state(repo)
        assert state["last_pull_status"] == "success"
        assert state["last_pull_at"] == "2026-10-27T12:00:00+00:00"
        assert state["maintenance_status"] == "repacked 30->1"

    def test_repo_busy_skips_without_writing(self, repo, no_platform, monkeypatch):
        git_mod = _git()
        called = []
        monkeypatch.setattr(git_mod, "_maybe_run_git_maintenance",
                            lambda h, s: called.append(1) or "repacked 1->1")
        assert git_mod._REPO_LOCK.acquire(blocking=False)
        try:
            result = git_mod._run_git_maintenance_once(repo)
        finally:
            git_mod._REPO_LOCK.release()
        assert result == {"status": "skipped", "reason": "repo_busy"}
        assert called == []
        assert not (repo / ".trinity" / "sync-state.json").exists()


# ---------------------------------------------------------------------------
# Maintenance itself
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runs(monkeypatch):
    calls = []

    def _fake(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(_git(), "run_registered", _fake)
    return calls


class TestMaintenance:
    def test_disk_preflight_counts_loose_bytes(self, repo, fake_runs, monkeypatch):
        monkeypatch.setattr(
            _git().shutil, "disk_usage",
            lambda _: types.SimpleNamespace(total=0, used=0, free=50 * 1024 * 1024),
        )
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1,
                 "size_loose_kb": 100_000}  # 100 MB loose vs 50 MB free
        assert _git()._maybe_run_git_maintenance(repo, stats) == "skipped_low_disk"
        assert fake_runs == []

    def test_stats_carry_loose_size(self, repo):
        assert isinstance(_git()._collect_git_object_stats(repo)["size_loose_kb"], int)

    def test_follow_up_is_reflog_expire_and_prune_never_gc(self, repo, fake_runs):
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        assert _git()._maybe_run_git_maintenance(repo, stats).startswith("repacked")
        assert not any("gc" in argv for argv in fake_runs)
        assert "repack" in fake_runs[0]
        assert fake_runs[1:] == [
            ["git", "reflog", "expire", "--all"],
            ["git", "prune", "--expire=1.hour.ago"],
        ]


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class _App:
    def __init__(self):
        self.handlers = {}

    def on_event(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class TestScheduling:
    def test_loops_scheduled_with_no_env_and_no_coords(self, no_platform, monkeypatch):
        monkeypatch.delenv("GIT_SYNC_PULL", raising=False)
        app = _App()
        auto_sync.schedule_auto_sync_if_enabled(app)
        assert {"startup", "shutdown"} <= set(app.handlers)

    def test_first_tick_is_jittered(self, tmp_path, no_platform, monkeypatch):
        drawn = []

        def fake_uniform(a, b):
            drawn.append((a, b))
            return b / 2

        slept = []

        async def first_sleep(seconds):
            slept.append(seconds)
            raise asyncio.CancelledError

        monkeypatch.setattr(random, "uniform", fake_uniform)
        monkeypatch.setattr(auto_sync.asyncio, "sleep", first_sleep)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(auto_sync.run_auto_sync_loop(tmp_path, interval_seconds=100))
        assert drawn == [(0, 100)]
        assert slept == [150]
