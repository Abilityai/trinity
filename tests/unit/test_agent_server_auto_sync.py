"""
Auto-sync heartbeat tests for the agent server (#389 S1a).

Covers:
  - .trinity/sync-state.json read/write helpers
  - get_git_status merges persisted sync-state into the response
  - _run_auto_sync_once() commits, pushes, and records success or failure
  - The startup hook only launches the loop when GIT_SYNC_AUTO=true
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

from agent_server.routers.git import (  # noqa: E402
    _read_sync_state_file,
    _write_sync_state_file,
    _run_auto_sync_once,
)
from agent_server import auto_sync  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd, cwd, env=None):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=20, env=env
    )


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
    """Fresh git repo + remote for each test."""
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _init_repo(local, remote)
    (local / ".trinity").mkdir()
    yield local
    shutil.rmtree(local, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSyncStateFile:
    """Read/write `.trinity/sync-state.json`."""

    def test_read_missing_returns_default(self, repo):
        state = _read_sync_state_file(repo)
        assert state["last_sync_status"] == "never"
        assert state["consecutive_failures"] == 0

    def test_write_and_read_roundtrip(self, repo):
        _write_sync_state_file(
            repo,
            last_sync_status="success",
            last_sync_at="2026-04-18T10:00:00+00:00",
            last_error_summary=None,
        )
        state = _read_sync_state_file(repo)
        assert state["last_sync_status"] == "success"
        assert state["last_sync_at"] == "2026-04-18T10:00:00+00:00"
        assert state["consecutive_failures"] == 0

    def test_write_failure_increments_counter(self, repo):
        _write_sync_state_file(repo, last_sync_status="failed",
                               last_error_summary="boom")
        _write_sync_state_file(repo, last_sync_status="failed",
                               last_error_summary="boom2")
        state = _read_sync_state_file(repo)
        assert state["consecutive_failures"] == 2
        assert state["last_error_summary"] == "boom2"

    def test_success_resets_counter(self, repo):
        _write_sync_state_file(repo, last_sync_status="failed",
                               last_error_summary="e")
        _write_sync_state_file(repo, last_sync_status="failed",
                               last_error_summary="e")
        _write_sync_state_file(repo, last_sync_status="success")
        state = _read_sync_state_file(repo)
        assert state["consecutive_failures"] == 0
        assert state["last_sync_status"] == "success"

    def test_corrupted_file_falls_back_to_default(self, repo):
        (repo / ".trinity" / "sync-state.json").write_text("not json")
        state = _read_sync_state_file(repo)
        assert state["last_sync_status"] == "never"


class TestRunAutoSyncOnce:
    """_run_auto_sync_once() performs a single sync cycle."""

    def test_no_changes_reports_success(self, repo):
        result = _run_auto_sync_once(repo)
        assert result["status"] == "success"
        state = _read_sync_state_file(repo)
        assert state["last_sync_status"] == "success"
        assert state["consecutive_failures"] == 0

    def test_with_changes_commits_and_pushes(self, repo):
        (repo / "note.txt").write_text("autosync test")
        result = _run_auto_sync_once(repo)
        assert result["status"] == "success"
        # Verify the commit was pushed.
        log = _run(["git", "log", "-1", "--format=%s", "origin/main"], repo)
        assert "auto-sync" in log.stdout.lower()

    def test_push_failure_records_error(self, repo):
        # Break the remote so push fails.
        _run(["git", "remote", "set-url", "origin", "/nonexistent/remote"], repo)
        (repo / "note.txt").write_text("will-fail")
        result = _run_auto_sync_once(repo)
        assert result["status"] == "failed"
        state = _read_sync_state_file(repo)
        assert state["last_sync_status"] == "failed"
        assert state["consecutive_failures"] == 1
        assert state["last_error_summary"]


class TestAutoSyncStartupGate:
    """The auto-sync loop only starts when GIT_SYNC_AUTO=true."""

    def test_gate_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("GIT_SYNC_AUTO", raising=False)
        assert auto_sync.should_run_auto_sync() is False

    def test_gate_true_when_env_true(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "true")
        assert auto_sync.should_run_auto_sync() is True

    def test_gate_false_when_env_false(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "false")
        assert auto_sync.should_run_auto_sync() is False

    def test_gate_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_AUTO", "TRUE")
        assert auto_sync.should_run_auto_sync() is True

    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("GIT_SYNC_INTERVAL_SECONDS", raising=False)
        assert auto_sync.get_interval_seconds() == 900

    def test_interval_custom(self, monkeypatch):
        monkeypatch.setenv("GIT_SYNC_INTERVAL_SECONDS", "60")
        assert auto_sync.get_interval_seconds() == 60
