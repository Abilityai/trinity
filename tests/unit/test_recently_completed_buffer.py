"""Unit test for #921 simplification: agent-side recently-completed
buffer in `process_registry`.

The buffer is what closes the natural-completion race the backend used
to two-cycle-confirm around. This test exercises the primitive directly:
- `unregister()` writes an entry with the current timestamp
- `list_recently_completed_ids()` returns IDs within the TTL window
- Entries past the TTL are evicted lazily on read
- `register()` does NOT touch the buffer

Module under test: docker/base-image/agent_server/services/process_registry.py
"""
from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Import shim to load agent_server.services.process_registry without booting
# the full agent_server package (routers/__init__.py pulls in optional deps).
# The bare sys.modules writes below are guarded by the _restore_sys_modules
# autouse fixture so they can't leak into other test files in the same
# pytest session — same pattern as tests/unit/test_telegram_webhook_backfill.py.
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
    """Snapshot sys.modules before each test and restore after, so the
    namespace stubs we inject for the import shim don't leak into other
    tests that exercise the real agent_server package or share name
    prefixes."""
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
    """Lazy-import inside the fixture's scope. Stubs the parent packages
    just enough that `agent_server.services.process_registry` resolves
    without triggering routers/__init__.py's optional-deps import chain."""
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
    from agent_server.services.process_registry import (  # noqa: WPS433
        ProcessRegistry,
        RECENTLY_COMPLETED_TTL_SECONDS,
    )
    return ProcessRegistry, RECENTLY_COMPLETED_TTL_SECONDS


def _fake_process(pid: int = 1234) -> MagicMock:
    """A subprocess.Popen stand-in just realistic enough for register()."""
    p = MagicMock(spec=subprocess.Popen)
    p.pid = pid
    p.poll.return_value = None
    return p


@pytest.mark.unit
class TestRecentlyCompletedBuffer:
    def test_unregister_records_id_in_buffer(self):
        ProcessRegistry, _ = _import_process_registry()
        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        assert reg.list_recently_completed_ids() == []  # not finished yet

        reg.unregister("exec-1")
        assert "exec-1" in reg.list_recently_completed_ids()

    def test_register_does_not_touch_buffer(self):
        ProcessRegistry, _ = _import_process_registry()
        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        assert reg.list_recently_completed_ids() == []

    def test_buffer_evicts_entries_past_ttl(self, monkeypatch):
        """Lazy expiry — past-TTL entries are dropped on the next read."""
        ProcessRegistry, _ = _import_process_registry()
        # Tighten the TTL so the test doesn't need to wait minutes.
        import agent_server.services.process_registry as pr_mod
        monkeypatch.setattr(pr_mod, "RECENTLY_COMPLETED_TTL_SECONDS", 0.05)

        reg = ProcessRegistry()
        reg.register("exec-1", _fake_process())
        reg.unregister("exec-1")
        assert "exec-1" in reg.list_recently_completed_ids()

        time.sleep(0.1)
        assert reg.list_recently_completed_ids() == []
        # And the underlying dict is cleared, not just filtered on read.
        assert "exec-1" not in reg._recently_completed

    def test_multiple_unregisters_accumulate(self):
        ProcessRegistry, _ = _import_process_registry()
        reg = ProcessRegistry()
        for eid in ("a", "b", "c"):
            reg.register(eid, _fake_process())
            reg.unregister(eid)
        assert set(reg.list_recently_completed_ids()) == {"a", "b", "c"}

    def test_unregister_unknown_id_still_buffers(self):
        """The buffer tracks the lifecycle event, not just known processes.
        Even if `register` was never called (e.g. a defensive unregister
        from an error path), the ID gets buffered so any racing watchdog
        view sees it as 'just completed'."""
        ProcessRegistry, _ = _import_process_registry()
        reg = ProcessRegistry()
        reg.unregister("ghost-id")
        assert "ghost-id" in reg.list_recently_completed_ids()

    def test_ttl_sized_above_observed_race_window(self):
        """#921's incident showed a 55s gap between agent unregister and
        backend success-write. The default TTL must comfortably exceed
        that with headroom for backend slowness — this guards a future
        refactor from accidentally shrinking it."""
        _, ttl = _import_process_registry()
        assert ttl >= 120
