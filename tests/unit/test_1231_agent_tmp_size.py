"""Unit tests for #1231: agent /tmp tmpfs size is operator-configurable via
AGENT_TMP_SIZE, with noexec,nosuid fixed and a safe default.

The agent /tmp was a hardcoded 100 MB noexec,nosuid tmpfs. It fills easily
(e.g. `gh` CLI install artifacts that hardcode /tmp and bypass the #1098
TMPDIR redirect), after which every /tmp write — including git's commit
scratch — fails with "No space left on device", silently breaking autonomous
runs' persist step. The size is now read from AGENT_TMP_SIZE (default 512m);
only the size is tunable — the security flags stay hardcoded.

Loaded by file path (stdlib-only) so the test doesn't drag the
docker / fastapi / database transitive imports of the agent_service package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CAPS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "backend" / "services" / "agent_service" / "capabilities.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("caps_tmpsize_under_test", _CAPS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- _resolve_agent_tmp_size (call-time env read) -----------------------

def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_TMP_SIZE", raising=False)
    assert _load()._resolve_agent_tmp_size() == "512m"


@pytest.mark.parametrize("value", ["256m", "512m", "1g", "2g", "100m"])
def test_valid_values_pass_through(monkeypatch, value):
    monkeypatch.setenv("AGENT_TMP_SIZE", value)
    assert _load()._resolve_agent_tmp_size() == value


def test_case_folds_and_strips(monkeypatch):
    monkeypatch.setenv("AGENT_TMP_SIZE", "  1G ")
    assert _load()._resolve_agent_tmp_size() == "1g"


@pytest.mark.parametrize("bad", ["512", "512Mi", "512MB", "0.5g", "g", "abc", "-1m", ""])
def test_invalid_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("AGENT_TMP_SIZE", bad)
    assert _load()._resolve_agent_tmp_size() == "512m"


# --- AGENT_TMPFS_MOUNT (import-time spec) -------------------------------

def test_mount_spec_default_shape(monkeypatch):
    monkeypatch.delenv("AGENT_TMP_SIZE", raising=False)
    mount = _load().AGENT_TMPFS_MOUNT
    assert mount == {"/tmp": "noexec,nosuid,size=512m"}


def test_mount_spec_honors_env(monkeypatch):
    monkeypatch.setenv("AGENT_TMP_SIZE", "2g")
    mount = _load().AGENT_TMPFS_MOUNT
    assert mount == {"/tmp": "noexec,nosuid,size=2g"}


def test_security_flags_always_present(monkeypatch):
    """noexec,nosuid are hardcoded — a configured size must never drop them
    (the load-bearing security posture: a compromised agent can't stage or
    execute payloads on /tmp)."""
    for value in ("256m", "garbage", "8g"):
        monkeypatch.setenv("AGENT_TMP_SIZE", value)
        spec = _load().AGENT_TMPFS_MOUNT["/tmp"]
        assert spec.startswith("noexec,nosuid,size=")
        assert "exec" not in spec.replace("noexec", "")  # no stray exec flag
