"""#1595 — survivable git maintenance (agent-server slice).

Git auto-gc detaches to PID 1 and is SIGKILLed by the orphan sweep every time;
the #1596 threshold repack was itself a bare, sweep-killable agent-server child
that also blocked the event loop. Covers:

  - run_registered: sweep registration pairing, call-time TTL, process-GROUP
    kill on timeout (a bare proc.kill() orphans pack-objects holding the pipe
    and wedges communicate()), check semantics, registry fail-open
  - stale-lock hygiene age gates (gc.pid / index.lock / tmp_pack_*)
  - maintenance guards: pack OR loose trigger, backoff, disk preflight
  - maintenance backoff bookkeeping in _write_sync_state_file (+ atomic write)
  - _run_auto_sync_once: repo-lock skip, metrics on the failure path, a swept
    `git status` fails loudly instead of reading as "nothing to commit"
  - _with_repo_lock: mutating endpoints 409 (agent_busy) under contention
  - auto_sync loop dispatches the cycle via asyncio.to_thread
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import importlib.util

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Evict any previously cached `agent_server` (shadow or real) so the explicit
# file-based loader below wins regardless of sys.path order (same pattern as
# test_agent_server_auto_sync.py).
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
from agent_server.routers.git import (  # noqa: E402
    _REPO_LOCK,
    _collect_git_object_stats,
    _maybe_run_git_maintenance,
    _read_sync_state_file,
    _reap_stale_git_litter,
    _run_auto_sync_once,
    _with_repo_lock,
    _write_sync_state_file,
)
from agent_server import auto_sync  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingRegistry:
    def __init__(self):
        self.events = []

    def add_transient_pid(self, pid, *, ttl_seconds=None):
        self.events.append(("add", pid, ttl_seconds))

    def remove_transient_pid(self, pid):
        self.events.append(("remove", pid))


@pytest.fixture
def registry(monkeypatch):
    """Own the process-registry module key so run_registered's call-time
    lazy import resolves to our recorder (leak-proof per learnings)."""
    rec = _RecordingRegistry()
    stub = types.ModuleType("agent_server.services.process_registry")
    stub.get_process_registry = lambda: rec
    monkeypatch.setitem(
        sys.modules, "agent_server.services.process_registry", stub
    )
    return rec


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=20)


def _init_repo(local_dir: Path, remote_dir: Path) -> None:
    _run(["git", "init", "--bare", "-b", "main"], remote_dir)
    _run(["git", "init", "-b", "main"], local_dir)
    _run(["git", "config", "user.email", "test@test.com"], local_dir)
    _run(["git", "config", "user.name", "Test"], local_dir)
    _run(["git", "remote", "add", "origin", str(remote_dir)], local_dir)
    (local_dir / "README.md").write_text("hello")
    _run(["git", "add", "."], local_dir)
    _run(["git", "commit", "-m", "initial"], local_dir)
    _run(["git", "push", "-u", "origin", "main"], local_dir)


@pytest.fixture
def repo(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _init_repo(local, remote)
    (local / ".trinity").mkdir()
    yield local
    shutil.rmtree(local, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)


def _age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------------------
# run_registered
# ---------------------------------------------------------------------------


class TestRunRegistered:
    def test_success_returns_completed_process(self, registry):
        res = run_registered(["echo", "hi"], timeout=10)
        assert res.returncode == 0
        assert res.stdout.strip() == "hi"

    def test_registers_then_unregisters_with_call_time_ttl(self, registry):
        run_registered(["true"], timeout=42, ttl_headroom=60)
        kinds = [e[0] for e in registry.events]
        assert kinds == ["add", "remove"]
        add = registry.events[0]
        assert add[2] == pytest.approx(102.0)  # timeout + headroom, derived at call time

    def test_unregisters_on_timeout(self, registry):
        with pytest.raises(subprocess.TimeoutExpired):
            run_registered(["sleep", "30"], timeout=0.3)
        assert registry.events[-1][0] == "remove"

    def test_timeout_kills_the_process_group(self, registry):
        """bash forks a sleep child holding the inherited stderr pipe. A
        single-pid kill leaves the child alive and communicate() blocked on
        the pipe for the full 60s — a prompt return proves killpg reaped the
        whole group (the pack-objects scenario)."""
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_registered(
                ["bash", "-c", "sleep 60 & sleep 60"], timeout=0.5,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"communicate() wedged {elapsed:.0f}s — group not killed"
        # The direct child must be gone (reaped by communicate()).
        child_pid = registry.events[0][1]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"child {child_pid} still alive after group kill")

    def test_check_raises_called_process_error(self, registry):
        with pytest.raises(subprocess.CalledProcessError):
            run_registered(["false"], timeout=10, check=True)
        assert registry.events[-1][0] == "remove"

    def test_fail_open_when_registry_unavailable(self, monkeypatch):
        broken = types.ModuleType("agent_server.services.process_registry")

        def _boom():
            raise RuntimeError("registry down")

        broken.get_process_registry = _boom
        monkeypatch.setitem(
            sys.modules, "agent_server.services.process_registry", broken
        )
        res = run_registered(["echo", "still-runs"], timeout=10)
        assert res.stdout.strip() == "still-runs"


# ---------------------------------------------------------------------------
# Stale-lock hygiene
# ---------------------------------------------------------------------------


class TestReapStaleGitLitter:
    @pytest.fixture
    def git_dir(self, tmp_path):
        d = tmp_path / ".git" / "objects" / "pack"
        d.mkdir(parents=True)
        return tmp_path

    def test_stale_gc_pid_and_index_lock_removed(self, git_dir):
        stale_pid = git_dir / ".git" / "gc.pid"
        stale_lock = git_dir / ".git" / "index.lock"
        stale_pid.write_text("123 deadhost")
        stale_lock.write_text("")
        _age(stale_pid, 2 * 3600)
        _age(stale_lock, 2 * 3600)
        _reap_stale_git_litter(git_dir, repack_budget_seconds=1800)
        assert not stale_pid.exists()
        assert not stale_lock.exists()

    def test_fresh_locks_kept(self, git_dir):
        fresh_pid = git_dir / ".git" / "gc.pid"
        fresh_lock = git_dir / ".git" / "index.lock"
        fresh_pid.write_text("123 host")
        fresh_lock.write_text("")
        _reap_stale_git_litter(git_dir, repack_budget_seconds=1800)
        assert fresh_pid.exists()
        assert fresh_lock.exists()

    def test_tmp_pack_age_gate_tracks_repack_budget(self, git_dir):
        pack_dir = git_dir / ".git" / "objects" / "pack"
        abandoned = pack_dir / "tmp_pack_abandoned"
        in_flight = pack_dir / "tmp_pack_inflight"
        abandoned.write_bytes(b"x")
        in_flight.write_bytes(b"x")
        _age(abandoned, 1800 + 301)  # older than budget + 300s slack
        _age(in_flight, 1800 - 600)  # within the current attempt's budget
        _reap_stale_git_litter(git_dir, repack_budget_seconds=1800)
        assert not abandoned.exists()
        assert in_flight.exists()

    def test_missing_git_dir_is_noop(self, tmp_path):
        _reap_stale_git_litter(tmp_path / "nope", repack_budget_seconds=1800)


# ---------------------------------------------------------------------------
# Maintenance guards
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_maintenance_runs(monkeypatch):
    """Record the git module's run_registered invocations without running git."""
    calls = []

    def _fake(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(git_mod, "run_registered", _fake)
    return calls


class TestMaintenanceTrigger:
    def test_below_both_thresholds_is_noop(self, repo, fake_maintenance_runs):
        stats = {"pack_count": 1, "loose_objects": 10, "size_pack_kb": 1}
        assert _maybe_run_git_maintenance(repo, stats) is None
        assert fake_maintenance_runs == []

    def test_no_data_never_repacks_blind(self, repo, fake_maintenance_runs):
        stats = {"pack_count": None, "loose_objects": None, "size_pack_kb": None}
        assert _maybe_run_git_maintenance(repo, stats) is None
        assert fake_maintenance_runs == []

    def test_pack_threshold_triggers(self, repo, fake_maintenance_runs):
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        result = _maybe_run_git_maintenance(repo, stats)
        assert result and result.startswith("repacked")
        assert any("repack" in argv for argv in fake_maintenance_runs)

    def test_loose_threshold_triggers_with_few_packs(self, repo, fake_maintenance_runs):
        """#1595: with gc.auto=0 garbage accumulates as LOOSE objects — the
        pack-count trigger alone would almost never fire post-fix."""
        stats = {"pack_count": 1, "loose_objects": 7000, "size_pack_kb": 1}
        result = _maybe_run_git_maintenance(repo, stats)
        assert result and result.startswith("repacked")

    def test_prune_uses_grace_not_now(self, repo, fake_maintenance_runs):
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        _maybe_run_git_maintenance(repo, stats)
        flat = [" ".join(argv) for argv in fake_maintenance_runs]
        assert any("--unpack-unreachable=1.hour.ago" in c for c in flat)
        assert any("--prune=1.hour.ago" in c for c in flat)
        assert not any("--prune=now" in c for c in flat)

    def test_backoff_gate_skips(self, repo, fake_maintenance_runs):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state = dict(_read_sync_state_file(repo))
        state["maintenance_next_attempt_at"] = future
        (repo / ".trinity" / "sync-state.json").write_text(json.dumps(state))
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        assert _maybe_run_git_maintenance(repo, stats) == "backoff"
        assert fake_maintenance_runs == []

    def test_disk_preflight_skips_when_low(self, repo, fake_maintenance_runs, monkeypatch):
        monkeypatch.setattr(
            git_mod.shutil, "disk_usage",
            lambda _: types.SimpleNamespace(total=100, used=99, free=1024),
        )
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 10_000_000}
        assert _maybe_run_git_maintenance(repo, stats) == "skipped_low_disk"
        assert fake_maintenance_runs == []

    def test_repack_failure_reports_failed(self, repo, monkeypatch):
        def _boom(argv, **kwargs):
            raise subprocess.CalledProcessError(1, argv, output="", stderr="boom")

        monkeypatch.setattr(git_mod, "run_registered", _boom)
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        assert _maybe_run_git_maintenance(repo, stats) == "failed"

    def test_timeout_read_at_call_time(self, repo, monkeypatch):
        monkeypatch.setenv("GIT_MAINTENANCE_TIMEOUT_SECONDS", "7")
        seen = {}

        def _capture(argv, **kwargs):
            if "repack" in argv:
                seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(list(argv), 0, "", "")

        monkeypatch.setattr(git_mod, "run_registered", _capture)
        stats = {"pack_count": 25, "loose_objects": 0, "size_pack_kb": 1}
        _maybe_run_git_maintenance(repo, stats)
        assert seen["timeout"] == 7


# ---------------------------------------------------------------------------
# Sync-state writer: maintenance bookkeeping + atomic write
# ---------------------------------------------------------------------------


class TestSyncStateWriter:
    def test_failed_maintenance_increments_and_backs_off(self, repo):
        state = _write_sync_state_file(repo, "success", maintenance_status="failed")
        assert state["maintenance_failures"] == 1
        first_next = datetime.fromisoformat(state["maintenance_next_attempt_at"])
        assert first_next > datetime.now(timezone.utc)

        state = _write_sync_state_file(repo, "success", maintenance_status="failed")
        assert state["maintenance_failures"] == 2
        second_next = datetime.fromisoformat(state["maintenance_next_attempt_at"])
        assert second_next > first_next  # exponential: 1h → 2h

    def test_repacked_resets_backoff(self, repo):
        _write_sync_state_file(repo, "success", maintenance_status="failed")
        state = _write_sync_state_file(repo, "success", maintenance_status="repacked 25->1")
        assert state["maintenance_failures"] == 0
        assert state["maintenance_next_attempt_at"] is None

    def test_skip_statuses_leave_counter_alone(self, repo):
        _write_sync_state_file(repo, "success", maintenance_status="failed")
        state = _write_sync_state_file(
            repo, "success", maintenance_status="skipped_low_disk"
        )
        assert state["maintenance_failures"] == 1
        assert state["maintenance_status"] == "skipped_low_disk"

    def test_none_maintenance_preserves_fields(self, repo):
        _write_sync_state_file(repo, "success", maintenance_status="failed")
        state = _write_sync_state_file(repo, "success")
        assert state["maintenance_failures"] == 1
        assert state["maintenance_status"] == "failed"

    def test_metrics_roundtrip_and_none_preserves(self, repo):
        _write_sync_state_file(repo, "success", pack_count=5, loose_objects=100)
        state = _write_sync_state_file(repo, "failed", last_error_summary="x")
        assert state["pack_count"] == 5
        assert state["loose_objects"] == 100

    def test_write_is_atomic_no_tmp_left_behind(self, repo):
        _write_sync_state_file(repo, "success", pack_count=1)
        trinity = repo / ".trinity"
        assert (trinity / "sync-state.json").exists()
        assert not list(trinity.glob("*.tmp"))
        # File is valid JSON after the replace.
        assert json.loads((trinity / "sync-state.json").read_text())["pack_count"] == 1


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


class TestAutoSyncCycle:
    def test_skips_when_repo_lock_held(self, repo):
        assert _REPO_LOCK.acquire(blocking=False)
        try:
            result = _run_auto_sync_once(repo)
        finally:
            _REPO_LOCK.release()
        assert result == {"status": "skipped", "reason": "repo_busy"}
        # Skip is not a failure — no sync-state write.
        assert not (repo / ".trinity" / "sync-state.json").exists()

    def test_failure_path_carries_metrics(self, repo):
        """A failing push must still report pack/loose counts — the sickest
        repos are exactly the ones that must not go dark."""
        shutil.rmtree(repo.parent / "remote")
        (repo / "newfile.txt").write_text("change")
        result = _run_auto_sync_once(repo)
        assert result["status"] == "failed"
        state = _read_sync_state_file(repo)
        assert state["pack_count"] is not None
        assert state["loose_objects"] is not None

    def test_swept_git_status_fails_loudly(self, repo, monkeypatch):
        """rc −9 / empty stdout from a swept `git status` must fail the cycle,
        never read as 'nothing to commit' + fake success."""
        real = git_mod.run_registered

        def _swept(argv, **kwargs):
            if "status" in argv:
                raise subprocess.CalledProcessError(-9, argv, output="", stderr="")
            return real(argv, **kwargs)

        monkeypatch.setattr(git_mod, "run_registered", _swept)
        result = _run_auto_sync_once(repo)
        assert result["status"] == "failed"

    def test_success_records_maintenance_status(self, repo, monkeypatch):
        monkeypatch.setattr(
            git_mod, "_maybe_run_git_maintenance", lambda h, s: "repacked 25->1"
        )
        (repo / "newfile.txt").write_text("change")
        result = _run_auto_sync_once(repo)
        assert result["status"] == "success"
        assert result["maintenance"] == "repacked 25->1"
        assert _read_sync_state_file(repo)["maintenance_status"] == "repacked 25->1"


# ---------------------------------------------------------------------------
# Endpoint lock decorator
# ---------------------------------------------------------------------------


class TestWithRepoLock:
    def test_contention_returns_409_agent_busy(self):
        @_with_repo_lock
        async def endpoint():
            return "ran"

        assert _REPO_LOCK.acquire(blocking=False)
        try:
            response = asyncio.run(endpoint())
        finally:
            _REPO_LOCK.release()
        assert response.status_code == 409
        assert response.headers["X-Conflict-Type"] == "agent_busy"

    def test_uncontended_runs_and_releases(self):
        @_with_repo_lock
        async def endpoint():
            assert _REPO_LOCK.locked()
            return "ran"

        assert asyncio.run(endpoint()) == "ran"
        assert not _REPO_LOCK.locked()

    def test_lock_released_on_exception(self):
        @_with_repo_lock
        async def endpoint():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            asyncio.run(endpoint())
        assert not _REPO_LOCK.locked()


# ---------------------------------------------------------------------------
# Loop dispatch
# ---------------------------------------------------------------------------


class TestLoopDispatchesViaThread:
    def test_cycle_runs_in_to_thread(self, repo, monkeypatch):
        recorded = {}

        async def _fake_to_thread(fn, *args):
            recorded["fn"] = fn
            recorded["args"] = args
            raise asyncio.CancelledError  # first iteration is enough

        monkeypatch.setattr(auto_sync.asyncio, "to_thread", _fake_to_thread)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(auto_sync.run_auto_sync_loop(repo, interval_seconds=0))
        # Resolve the expected function through sys.modules at assert time:
        # sibling test modules re-exec the agent_server package at collection,
        # so the loop's call-time relative import can land on a NEWER module
        # object than this file's import-time capture (learnings 2026-07-08).
        live_git_mod = sys.modules["agent_server.routers.git"]
        assert recorded["fn"] is live_git_mod._run_auto_sync_once
        assert recorded["args"] == (repo,)


# ---------------------------------------------------------------------------
# Object stats collection
# ---------------------------------------------------------------------------


class TestSummarizeGitErrorRedaction:
    def test_pat_bearing_url_is_redacted(self):
        raw = (
            "fatal: unable to access "
            "'https://x-access-token:ghp_abc123XYZ@github.com/org/repo.git/': 403"
        )
        summary = git_mod._summarize_git_error(raw)
        assert "ghp_abc123XYZ" not in summary
        assert "x-access-token" not in summary
        assert "***@github.com" in summary

    def test_plain_error_untouched(self):
        assert git_mod._summarize_git_error("merge conflict in foo.txt") == (
            "merge conflict in foo.txt"
        )


class TestCollectGitObjectStats:
    def test_real_repo_yields_ints(self, repo):
        stats = _collect_git_object_stats(repo)
        assert isinstance(stats["loose_objects"], int)
        assert isinstance(stats["pack_count"], int)

    def test_missing_git_dir_yields_nones(self, tmp_path):
        stats = _collect_git_object_stats(tmp_path)
        assert stats == {
            "loose_objects": None, "pack_count": None, "size_pack_kb": None,
        }
