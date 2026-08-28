"""#2433 — agent-side *pending* registration in ``process_registry``.

An execution the agent-server has ACCEPTED (``/api/task`` handler entry, the
#1083 async spawn, ``/api/chat`` waiting on the execution lock) but not yet
SPAWNED (still queued in the headless thread pool, or waiting on the chat lock)
was invisible to ``/api/executions/running`` — so the backend watchdog
classified it as an orphan after 60s, wrote a false ``failed`` and released its
slot while the turn later ran anyway (billed, overbooked).

Pinned here:
- ``register_pending`` → ``list_pending_ids``; ``discard_pending`` is idempotent
- ``register()`` PROMOTES a pending entry (pops it) — one id, one state
- pending entries expire lazily past ``accepted_at + timeout + 60`` (a leaked
  entry must never become an immortal ``running`` row)
- ``terminate()`` on a pending id records the #679 cancel marker, keeps the
  entry, returns ``cancelled_before_start``
- ``register()`` CONSUMES a cancel requested while pending: kills the process
  group it was just handed and KEEPS the ``_terminated`` marker (the C10 clear
  applies only to entries with no cancel flag) — closes the check→Popen→
  register race that erased the cancel
- ``list_recently_completed_ids`` also reports registered processes that have
  EXITED but not yet been unregistered (the post-exit drain window, C)
- ``unregister()`` discards any pending entry (belt)

Module under test: docker/base-image/agent_server/services/process_registry.py
Import shim mirrors tests/unit/test_recently_completed_buffer.py.
"""
from __future__ import annotations

import signal
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

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
    import agent_server.services.process_registry as pr  # noqa: WPS433
    return pr


def _fake_process(pid: int = 4242, poll=None):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = poll
    proc.returncode = poll
    return proc


# ---------------------------------------------------------------------------
# pending lifecycle
# ---------------------------------------------------------------------------

def test_register_pending_is_listed_until_discarded():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    assert reg.list_pending_ids() == ["exec-1"]
    reg.discard_pending("exec-1")
    assert reg.list_pending_ids() == []
    reg.discard_pending("exec-1")  # idempotent
    reg.discard_pending("never-registered")


def test_register_pending_ignores_empty_id():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending(None)
    reg.register_pending("")
    assert reg.list_pending_ids() == []


def test_register_promotes_pending_to_running():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.register("exec-1", _fake_process(), metadata={"type": "task"})
    assert reg.list_pending_ids() == []
    assert [e["execution_id"] for e in reg.list_running()] == ["exec-1"]


def test_pending_entry_expires_lazily_past_deadline():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.register_pending("exec-2", timeout_seconds=900)
    # Simulate a leaked entry whose window has lapsed.
    reg._pending["exec-1"]["deadline"] = time.time() - 1
    assert reg.list_pending_ids() == ["exec-2"]
    assert "exec-1" not in reg._pending


def test_pending_deadline_covers_the_execution_budget():
    """The window must not be shorter than the run it protects (the #1501 rule
    for transient pids): accepted_at + timeout + slack."""
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    before = time.time()
    reg.register_pending("exec-1", timeout_seconds=3600)
    deadline = reg._pending["exec-1"]["deadline"]
    assert deadline >= before + 3600 + pr.PENDING_SLACK_SECONDS - 1
    # No timeout → the agent default window, never zero.
    reg.register_pending("exec-2")
    assert reg._pending["exec-2"]["deadline"] > time.time() + 60


# ---------------------------------------------------------------------------
# cancel while pending
# ---------------------------------------------------------------------------

def test_terminate_on_pending_marks_cancel_and_keeps_entry():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    result = reg.terminate("exec-1")
    assert result == {"success": True, "returncode": None, "reason": "cancelled_before_start"}
    assert reg.was_terminated("exec-1")
    # The entry stays until the owning handler's finally — popping it here
    # would let the next watchdog sweep orphan a row whose thread is about to
    # raise CancelledBeforeStart.
    assert reg.list_pending_ids() == ["exec-1"]
    assert reg._pending["exec-1"]["cancel_requested"] is True


def test_terminate_on_unknown_id_is_still_not_found():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    assert reg.terminate("nope") == {"success": False, "reason": "not_found"}


def test_register_consumes_cancel_kills_group_and_keeps_marker(monkeypatch):
    """The race: thread-top check passes → terminate() lands → Popen →
    register(). Before #2433 register() cleared the marker (C10) and the turn
    ran to a billed SUCCESS."""
    pr = _import_process_registry()
    sent = []
    monkeypatch.setattr(
        pr, "_signal_process_tree", lambda process, sig, pgid=None: sent.append((process.pid, sig, pgid))
    )
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.terminate("exec-1")
    proc = _fake_process(pid=777)
    reg.register("exec-1", proc, metadata={"type": "task", "pgid": 777})
    assert sent == [(777, signal.SIGKILL, 777)]
    assert reg.was_terminated("exec-1"), "cancel marker must survive promotion"
    assert reg.list_pending_ids() == []


def test_register_without_cancel_still_clears_stale_marker(monkeypatch):
    """C10 (#679) preserved: a #678 retry reusing the execution_id must not
    inherit the previous attempt's cancel label."""
    pr = _import_process_registry()
    sent = []
    monkeypatch.setattr(
        pr, "_signal_process_tree", lambda process, sig, pgid=None: sent.append(sig)
    )
    reg = pr.ProcessRegistry()
    reg._terminated["exec-1"] = time.time()  # stale marker from a previous attempt
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.register("exec-1", _fake_process(), metadata={"type": "task"})
    assert sent == []
    assert not reg.was_terminated("exec-1")


def test_register_kill_failure_never_raises(monkeypatch):
    pr = _import_process_registry()

    def boom(process, sig, pgid=None):
        raise OSError("no such process")

    monkeypatch.setattr(pr, "_signal_process_tree", boom)
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.terminate("exec-1")
    reg.register("exec-1", _fake_process(), metadata={"type": "task"})
    assert reg.was_terminated("exec-1")


# ---------------------------------------------------------------------------
# post-exit drain window (C) + belts
# ---------------------------------------------------------------------------

def test_recently_completed_includes_exited_but_registered():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register("running", _fake_process(pid=1, poll=None), metadata={})
    reg.register("drained", _fake_process(pid=2, poll=0), metadata={})
    assert [e["execution_id"] for e in reg.list_running()] == ["running"]
    assert set(reg.list_recently_completed_ids()) == {"drained"}
    reg.unregister("drained")
    assert set(reg.list_recently_completed_ids()) == {"drained"}


def test_unregister_discards_pending():
    pr = _import_process_registry()
    reg = pr.ProcessRegistry()
    reg.register_pending("exec-1", timeout_seconds=900)
    reg.unregister("exec-1")
    assert reg.list_pending_ids() == []
