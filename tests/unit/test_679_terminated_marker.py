"""Unit tests for #679: the agent-side ``_terminated`` marker in
``process_registry``.

When an operator cancels a running task, ``terminate()`` SIGINTs the Claude
subprocess. Claude catches SIGINT, emits a graceful final message, and exits 0 —
indistinguishable from a genuine success by return code alone. The ``_terminated``
marker (mirroring ``_recently_completed``) lets the chat handler relabel that
turn ``cancelled``.

The marker is recorded **after a successful SIGINT send, still-running branch
only** — never on ``already_finished``/``not_found``/signal-failure (C2), and is
cleared on ``register()`` so a #678 in-line retry reusing the execution_id can't
inherit a stale cancel label (C10). Read-not-pop so both the graceful relabel and
a later SIGKILL→504 check see it.

Module under test: docker/base-image/agent_server/services/process_registry.py
Mirrors the import shim in tests/unit/test_recently_completed_buffer.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

_STUBBED_MODULE_NAMES = [
    "agent_server",
    "agent_server.services",
    "agent_server.utils",
    "agent_server.services.process_registry",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot/restore sys.modules so the namespace stubs we inject for the
    import shim don't leak into other tests (gated by tests/lint_sys_modules.py)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _import_process_registry():
    if "agent_server" not in sys.modules:
        stub = types.ModuleType("agent_server")
        stub.__path__ = [str(_AGENT_SERVER_DIR)]
        sys.modules["agent_server"] = stub
    if "agent_server.services" not in sys.modules:
        stub = types.ModuleType("agent_server.services")
        stub.__path__ = [str(_AGENT_SERVER_DIR / "services")]
        sys.modules["agent_server.services"] = stub
    if "agent_server.utils" not in sys.modules:
        stub = types.ModuleType("agent_server.utils")
        stub.__path__ = [str(_AGENT_SERVER_DIR / "utils")]
        sys.modules["agent_server.utils"] = stub
    import agent_server.services.process_registry as pr_mod  # noqa: WPS433
    return pr_mod


def _fake_process(pid: int = 1234, *, running: bool = True, returncode: int = 0) -> MagicMock:
    p = MagicMock(spec=subprocess.Popen)
    p.pid = pid
    p.poll.return_value = None if running else returncode
    p.wait.return_value = returncode
    p.returncode = returncode
    return p


def _quiet_signals(pr_mod, monkeypatch, *, signal_raises: bool = False):
    """Stub the process-group signal + cgroup-sweep helpers so terminate() runs
    without touching real processes. Returns the SIGINT/SIGKILL mock."""
    sig = MagicMock()
    if signal_raises:
        sig.side_effect = OSError("no such process")
    monkeypatch.setattr(pr_mod, "_signal_process_tree", sig)
    monkeypatch.setattr(pr_mod, "kill_cgroup_orphans", MagicMock(return_value=0))
    return sig


@pytest.mark.unit
class TestTerminatedMarker:
    def test_terminate_marks_after_successful_signal(self, monkeypatch):
        pr_mod = _import_process_registry()
        _quiet_signals(pr_mod, monkeypatch)
        reg = pr_mod.ProcessRegistry()
        reg.register("exec-1", _fake_process(running=True))

        assert reg.was_terminated("exec-1") is False  # not yet terminated
        result = reg.terminate("exec-1")
        assert result["success"] is True
        assert reg.was_terminated("exec-1") is True

    def test_terminate_does_not_mark_on_signal_failure(self, monkeypatch):
        """C2: a SIGINT send that raises must NOT mark the turn cancelled —
        the kill-signal never landed, so the turn will exit on its own terms."""
        pr_mod = _import_process_registry()
        _quiet_signals(pr_mod, monkeypatch, signal_raises=True)
        reg = pr_mod.ProcessRegistry()
        reg.register("exec-1", _fake_process(running=True))

        result = reg.terminate("exec-1")
        assert result["success"] is False
        assert result["reason"] == "error"
        assert reg.was_terminated("exec-1") is False

    def test_terminate_does_not_mark_on_already_finished(self, monkeypatch):
        pr_mod = _import_process_registry()
        _quiet_signals(pr_mod, monkeypatch)
        reg = pr_mod.ProcessRegistry()
        reg.register("exec-1", _fake_process(running=False, returncode=0))

        result = reg.terminate("exec-1")
        assert result["reason"] == "already_finished"
        assert reg.was_terminated("exec-1") is False

    def test_terminate_does_not_mark_on_not_found(self, monkeypatch):
        pr_mod = _import_process_registry()
        _quiet_signals(pr_mod, monkeypatch)
        reg = pr_mod.ProcessRegistry()

        result = reg.terminate("ghost")
        assert result["reason"] == "not_found"
        assert reg.was_terminated("ghost") is False

    def test_register_clears_stale_marker(self, monkeypatch):
        """C10: an execution_id reused by the #678 in-line retry must not inherit
        the prior attempt's cancel label."""
        pr_mod = _import_process_registry()
        reg = pr_mod.ProcessRegistry()
        reg._terminated["exec-1"] = time.time()
        assert reg.was_terminated("exec-1") is True

        reg.register("exec-1", _fake_process(running=True))
        assert reg.was_terminated("exec-1") is False

    def test_was_terminated_is_read_not_pop(self, monkeypatch):
        """Read, not pop — the SIGKILL→504 path checks the same marker after the
        graceful-relabel path already read it."""
        pr_mod = _import_process_registry()
        reg = pr_mod.ProcessRegistry()
        reg._terminated["exec-1"] = time.time()
        assert reg.was_terminated("exec-1") is True
        assert reg.was_terminated("exec-1") is True  # still set on the second read

    def test_marker_evicts_past_ttl(self, monkeypatch):
        pr_mod = _import_process_registry()
        monkeypatch.setattr(pr_mod, "TERMINATED_TTL_SECONDS", 0.05)
        reg = pr_mod.ProcessRegistry()
        reg._terminated["exec-1"] = time.time()
        assert reg.was_terminated("exec-1") is True

        time.sleep(0.1)
        assert reg.was_terminated("exec-1") is False
        assert "exec-1" not in reg._terminated  # cleared, not just filtered

    def test_multi_execution_isolation(self, monkeypatch):
        pr_mod = _import_process_registry()
        _quiet_signals(pr_mod, monkeypatch)
        reg = pr_mod.ProcessRegistry()
        reg.register("exec-a", _fake_process(running=True))
        reg.register("exec-b", _fake_process(running=True))

        reg.terminate("exec-a")
        assert reg.was_terminated("exec-a") is True
        assert reg.was_terminated("exec-b") is False

    def test_ttl_sized_for_observability_window(self):
        """The marker is best-effort observability; the TTL must comfortably
        cover a worst-case backend finalize delay (same posture as
        RECENTLY_COMPLETED_TTL_SECONDS)."""
        pr_mod = _import_process_registry()
        assert pr_mod.TERMINATED_TTL_SECONDS >= 120
