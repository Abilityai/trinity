"""Regression for #523: the terminate endpoint must dispatch the
synchronous registry.terminate() call to a thread-pool executor so the
asyncio event loop stays responsive to concurrent /health probes.

registry.terminate() does up to 7s of process.wait() (5s SIGINT grace +
2s SIGKILL grace). Calling it directly from an async route handler
blocks the event loop for that entire window — concurrent /health
requests time out, the backend circuit breaker opens, and UI fan-out
hangs. The fix wraps the call in loop.run_in_executor; this test pins
that contract so it cannot regress.

Module under test:
    docker/base-image/agent_server/routers/chat.py::terminate_execution
"""
from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Import the router without booting the full agent_server package.
# Same shim pattern as test_signal_exit_classification.py.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER_DIR = _PROJECT_ROOT / "docker" / "base-image" / "agent_server"

if "agent_server" not in sys.modules:
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [str(_AGENT_SERVER_DIR)]
    sys.modules["agent_server"] = _stub

# Bypass routers/__init__.py — it imports every router module (snapshot,
# git, etc.) and pulls in optional deps like python-multipart that this
# test doesn't need. A namespace-package shim lets chat.py be imported
# in isolation.
if "agent_server.routers" not in sys.modules:
    _routers_stub = types.ModuleType("agent_server.routers")
    _routers_stub.__path__ = [str(_AGENT_SERVER_DIR / "routers")]
    sys.modules["agent_server.routers"] = _routers_stub

from agent_server.routers import chat as chat_router  # noqa: E402
from agent_server.routers.chat import terminate_execution  # noqa: E402
from agent_server.services import process_registry as registry_module  # noqa: E402
from agent_server.utils import subprocess_pgroup  # noqa: E402


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_dispatches_to_executor():
    """registry.terminate must run on a thread different from the event
    loop thread. If someone reverts the run_in_executor wrap and calls
    registry.terminate synchronously, this test fails."""
    main_thread_id = threading.get_ident()
    terminate_thread_ids: list[int] = []

    def fake_terminate(execution_id):
        terminate_thread_ids.append(threading.get_ident())
        # Simulate process.wait() so we can also observe the event loop
        # is responsive while the call is in flight.
        time.sleep(0.3)
        return {"success": True, "returncode": 0}

    fake_registry = MagicMock()
    fake_registry.terminate = fake_terminate

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        async def quick_yield_after_dispatch():
            # Let terminate dispatch to the executor first.
            await asyncio.sleep(0.05)
            # Now measure how long a single yield takes. If the event
            # loop is blocked, this will be roughly the remaining sleep
            # duration (~0.25s). If properly dispatched, sub-millisecond.
            t = time.monotonic()
            await asyncio.sleep(0)
            return time.monotonic() - t

        terminate_task = asyncio.create_task(terminate_execution("test-exec"))
        yield_elapsed = await quick_yield_after_dispatch()
        result = await terminate_task

    assert terminate_thread_ids, "fake_terminate was never invoked"
    assert terminate_thread_ids[0] != main_thread_id, (
        "registry.terminate ran on the event loop thread — synchronous "
        "process.wait() will block /health (#523 regression)"
    )
    assert yield_elapsed < 0.05, (
        f"event loop blocked for {yield_elapsed:.3f}s during terminate; "
        f"expected sub-millisecond yield (#523 regression)"
    )
    assert result == {"status": "terminated", "execution_id": "test-exec"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_passes_through_not_found():
    """registry.terminate -> {success: False, reason: not_found} must
    surface as HTTP 404 even after the executor wrap."""
    from fastapi import HTTPException

    fake_registry = MagicMock()
    fake_registry.terminate = lambda execution_id: {
        "success": False,
        "reason": "not_found",
    }

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await terminate_execution("missing-exec")

    assert exc_info.value.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminate_passes_through_already_finished():
    """A process that already exited must surface as 200 with status
    'already_finished' — same shape as before the executor wrap."""
    fake_registry = MagicMock()
    fake_registry.terminate = lambda execution_id: {
        "success": False,
        "reason": "already_finished",
        "returncode": 0,
    }

    with patch(
        "agent_server.routers.chat.get_process_registry",
        return_value=fake_registry,
    ):
        result = await terminate_execution("already-done")

    assert result == {
        "status": "already_finished",
        "execution_id": "already-done",
        "returncode": 0,
    }


# ---------------------------------------------------------------------------
# #3408: the router's failure replies, driven through a REAL ProcessRegistry.
#
# The backend reads a 2xx as stopped and a 404 as already gone; both free the
# execution's capacity. Every other status reads as "not terminated". The
# tests below run the real registry.terminate() behind the real route, with a
# real disposable child, in the style of test_runtime_process_ownership.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_registry(monkeypatch):
    """A fresh ProcessRegistry behind the route, as on an agent server that has
    just booted. The test owns only its own child, never the surrounding
    cgroup, so the orphan sweep is stubbed in both modules that call it."""
    value = registry_module.ProcessRegistry()
    monkeypatch.setattr(chat_router, "get_process_registry", lambda: value)
    monkeypatch.setattr(registry_module, "kill_cgroup_orphans", lambda **kw: 0)
    monkeypatch.setattr(subprocess_pgroup, "kill_cgroup_orphans", lambda **kw: 0)
    return value


@pytest.fixture
def live_child():
    """A real disposable child in its own session (so its group is never the
    test's). Each test replaces the registry's signal helper, so no signal is
    ever sent to a process group."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        # The class method, not the instance attribute a test may have capped.
        subprocess.Popen.wait(process, timeout=5)
        process.stdin.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signal_error_keeps_the_generic_500_with_its_text(
    real_registry, live_child, monkeypatch
):
    """reason "error" (the stop could not even be sent) keeps the existing
    generic 500 whose detail is the error text, and the child stays owned."""
    from fastapi import HTTPException

    real_registry.register("signal-error-exec", live_child)
    monkeypatch.setattr(
        registry_module,
        "_signal_process_tree",
        Mock(side_effect=PermissionError("denied")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await terminate_execution("signal-error-exec")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "denied"
    assert live_child.poll() is None, "control: nothing stopped the child"
    assert real_registry.is_execution_running("signal-error-exec")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconfirmed_stop_is_named_and_never_reads_as_gone(
    real_registry, live_child, monkeypatch
):
    """#3408 AC 4: a child that outlives SIGINT and SIGKILL is reported by name.

    The status stays outside 2xx and is never 404 (either frees capacity for a
    child that is still alive); only the detail changes, to a string that
    starts with the reason. The entry stays registered, so termination can be
    retried, and a retry the child does respond to succeeds."""
    from fastapi import HTTPException

    real_registry.register("unconfirmed-exec", live_child)
    sent = []
    deliver = False

    def signal_tree(process, sig, pgid=None):
        sent.append(sig)
        assert process is live_child, "only this test's own child is ever signalled"
        if deliver:
            process.send_signal(sig)  # pid only, never a process group

    monkeypatch.setattr(registry_module, "_signal_process_tree", signal_tree)
    # Both of the registry's waits really run and really time out (the signals
    # are not delivered); cap them so the test does not sit out 5 s + 2 s.
    real_wait = live_child.wait
    monkeypatch.setattr(
        live_child, "wait", lambda timeout=None: real_wait(timeout=0.05)
    )

    with pytest.raises(HTTPException) as exc_info:
        await terminate_execution("unconfirmed-exec")

    assert sent == [signal.SIGINT, signal.SIGKILL], "control: both stops were tried"
    assert live_child.poll() is None, "control: the child survived both waits"
    status = exc_info.value.status_code
    assert not 200 <= status < 300, "an unconfirmed stop must never read as stopped"
    assert status != 404, "404 reads as gone and would free a live child's capacity"
    assert status == 500
    detail = exc_info.value.detail
    assert isinstance(detail, str), "the UI renders the detail as text"
    assert detail.startswith("termination_unconfirmed"), detail
    assert "unconfirmed-exec" in detail
    assert real_registry.is_execution_running("unconfirmed-exec")
    assert live_child.pid in real_registry.active_execution_pids()
    assert "unconfirmed-exec" not in real_registry.list_recently_completed_ids()

    # The kept entry can be stopped again once the child does respond.
    monkeypatch.setattr(live_child, "wait", real_wait)
    deliver = True
    result = await terminate_execution("unconfirmed-exec")

    assert result == {"status": "terminated", "execution_id": "unconfirmed-exec"}
    assert live_child.poll() is not None
    assert not real_registry.is_execution_running("unconfirmed-exec")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_install_empty_registry_still_answers_404(
    real_registry, monkeypatch
):
    """Other shape, fresh install: the agent server has just booted and its
    registry holds nothing. An unknown id still answers 404, the one reply the
    backend reads as "gone", and no signal is sent."""
    from fastapi import HTTPException

    signal_tree = Mock()
    monkeypatch.setattr(registry_module, "_signal_process_tree", signal_tree)
    assert real_registry.list_running() == [], "control: a just-booted registry"

    with pytest.raises(HTTPException) as exc_info:
        await terminate_execution("never-registered")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Execution not found"
    signal_tree.assert_not_called()
