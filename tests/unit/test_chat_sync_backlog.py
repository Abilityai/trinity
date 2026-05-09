"""
Sync `/task` Long-Poll on Backlog Tests (issue #498)

Sync parallel calls (parallel=true, async=false) used to fail terminally with
429 when the agent was at capacity. After #498 they spill to the SAME backlog
the async path uses (BACKLOG-001) and long-poll on the open HTTP connection
until the execution reaches a terminal status.

These tests cover the in-process primitives in services/sync_waiter.py:
- `_sync_waiters` registry semantics
- `signal_sync_waiter` — fires registered waiter, no-op otherwise
- `wait_for_sync_terminal` — event happy path, DB-poll fallback, timeout

The full router integration (pre-acquire → enqueue → wait → return) requires
FastAPI dependency injection plumbing and is left to integration tests; here
we verify the in-process primitives the router relies on.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Bootstrap: same path-shadow protection used by test_backlog.py — pytest
# auto-adds tests/ which has its own utils package. Backend code does
# `from utils.helpers ...` and we need that to resolve to src/backend/utils.
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def waiter_module(monkeypatch):
    """Import services.sync_waiter against a stub `database` module.

    The wait helper does a late `from database import db` to avoid a load-
    time cycle. We install a fake module up-front so the import resolves.
    """
    fake_db = MagicMock()
    fake_db_module = types.SimpleNamespace(db=fake_db)
    monkeypatch.setitem(sys.modules, "database", fake_db_module)

    # Reset cached sync_waiter module so the fake_db patch takes effect.
    sys.modules.pop("services.sync_waiter", None)
    import services.sync_waiter as sw

    sw._sync_waiters.clear()
    # Expose the fake db on the module so tests can stub get_execution per-test.
    sw._test_db = fake_db
    return sw


@pytest.fixture
def fast_poll(monkeypatch, waiter_module):
    """Shorten the DB-poll interval so timeout tests don't wall-clock-sleep."""
    monkeypatch.setattr(waiter_module, "SYNC_WAITER_POLL_INTERVAL", 0.05)
    return waiter_module


# ---------------------------------------------------------------------------
# signal_sync_waiter
# ---------------------------------------------------------------------------


class TestSignalSyncWaiter:
    def test_noop_when_no_waiter_registered(self, waiter_module):
        """Async fire-and-forget path doesn't register a waiter — must be safe."""
        waiter_module.signal_sync_waiter(
            "never-registered", result=MagicMock(), chat_session_id=None
        )
        assert "never-registered" not in waiter_module._sync_waiters

    def test_noop_when_execution_id_empty(self, waiter_module):
        # Either empty string or None must not raise.
        waiter_module.signal_sync_waiter("", result=MagicMock(), chat_session_id="cs-1")
        waiter_module.signal_sync_waiter(None, result=MagicMock(), chat_session_id="cs-1")

    @pytest.mark.asyncio
    async def test_fires_registered_waiter_with_payload(self, waiter_module):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        waiter_module._sync_waiters["exec-x"] = fut

        sentinel_result = MagicMock(name="TaskExecutionResult")
        waiter_module.signal_sync_waiter(
            "exec-x", result=sentinel_result, chat_session_id="cs-9"
        )

        payload = await asyncio.wait_for(fut, timeout=0.1)
        assert payload == {"result": sentinel_result, "chat_session_id": "cs-9"}
        # signal helper itself doesn't pop the registry (the wait helper does).
        waiter_module._sync_waiters.pop("exec-x", None)

    @pytest.mark.asyncio
    async def test_silent_when_waiter_already_done(self, waiter_module):
        """If the caller disconnected and cancelled the future, signal must not crash."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.cancel()
        waiter_module._sync_waiters["exec-cancelled"] = fut

        # Must not raise InvalidStateError despite cancelled future.
        waiter_module.signal_sync_waiter(
            "exec-cancelled", result=None, chat_session_id=None
        )

        waiter_module._sync_waiters.pop("exec-cancelled", None)


# ---------------------------------------------------------------------------
# wait_for_sync_terminal — event happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWaitForSyncTerminalEvent:
    async def test_returns_payload_when_signaled(self, waiter_module):
        """Drain happy path: fired by signal_sync_waiter — wait returns payload."""
        sentinel = object()

        async def _fire_after_delay():
            await asyncio.sleep(0.05)
            waiter_module.signal_sync_waiter(
                "exec-1", result=sentinel, chat_session_id="cs-1"
            )

        # No DB needed for the event path; stub get_execution to be safe.
        waiter_module._test_db.get_execution = MagicMock(return_value=None)

        firer = asyncio.create_task(_fire_after_delay())
        try:
            result = await waiter_module.wait_for_sync_terminal(
                "exec-1", timeout=2.0
            )
            assert result == {"result": sentinel, "chat_session_id": "cs-1"}
        finally:
            firer.cancel()
        # Registry cleaned up on exit.
        assert "exec-1" not in waiter_module._sync_waiters

    async def test_signal_with_none_result_still_returns_payload(self, waiter_module):
        """Signal with None result still wakes — the dict envelope is what matters."""
        waiter_module._test_db.get_execution = MagicMock(return_value=None)

        async def _fire():
            await asyncio.sleep(0.02)
            waiter_module.signal_sync_waiter(
                "exec-2", result=None, chat_session_id=None
            )

        firer = asyncio.create_task(_fire())
        try:
            payload = await waiter_module.wait_for_sync_terminal(
                "exec-2", timeout=1.0
            )
            assert payload == {"result": None, "chat_session_id": None}
        finally:
            firer.cancel()


# ---------------------------------------------------------------------------
# wait_for_sync_terminal — DB-poll fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWaitForSyncTerminalPollFallback:
    async def test_poll_returns_none_when_db_shows_terminal(self, fast_poll):
        """When a non-drain code path flips the row terminal (corrupt
        metadata, expire_stale, cleanup recovery), the event is never set.
        The poll fallback must catch it and return None to signal the
        caller to reconstruct the response from the DB row.
        """
        sw = fast_poll
        running_row = MagicMock(status="running")
        failed_row = MagicMock(status="failed")
        sw._test_db.get_execution = MagicMock(side_effect=[running_row, failed_row])

        result = await sw.wait_for_sync_terminal("exec-poll-1", timeout=2.0)
        assert result is None
        assert "exec-poll-1" not in sw._sync_waiters

    async def test_poll_recognizes_all_terminal_statuses(self, fast_poll):
        """success, failed, cancelled all qualify as terminal."""
        sw = fast_poll
        for status in ("success", "failed", "cancelled"):
            sw._sync_waiters.clear()
            sw._test_db.get_execution = MagicMock(return_value=MagicMock(status=status))
            result = await sw.wait_for_sync_terminal(f"exec-{status}", timeout=1.0)
            assert result is None, f"status={status} should be terminal"

    async def test_poll_ignores_non_terminal_statuses(self, fast_poll):
        """queued and running are NOT terminal — wait should NOT return on them."""
        sw = fast_poll
        sw._test_db.get_execution = MagicMock(return_value=MagicMock(status="queued"))
        with pytest.raises(asyncio.TimeoutError):
            await sw.wait_for_sync_terminal("exec-stuck", timeout=0.3)


# ---------------------------------------------------------------------------
# wait_for_sync_terminal — timeout & cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWaitForSyncTerminalTimeout:
    async def test_timeout_when_neither_event_nor_poll_fires(self, fast_poll):
        sw = fast_poll
        sw._test_db.get_execution = MagicMock(return_value=MagicMock(status="running"))
        with pytest.raises(asyncio.TimeoutError):
            await sw.wait_for_sync_terminal("exec-timeout", timeout=0.2)
        # Registry MUST be cleaned even on timeout.
        assert "exec-timeout" not in sw._sync_waiters

    async def test_registry_cleaned_on_caller_cancellation(self, fast_poll):
        """If the HTTP request is cancelled mid-wait, the wait coroutine
        raises CancelledError. Registry must still be cleaned (no leak).
        """
        sw = fast_poll
        sw._test_db.get_execution = MagicMock(return_value=MagicMock(status="running"))

        async def _wait():
            return await sw.wait_for_sync_terminal("exec-cancel", timeout=10)

        task = asyncio.create_task(_wait())
        await asyncio.sleep(0.05)  # let it register
        assert "exec-cancel" in sw._sync_waiters
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "exec-cancel" not in sw._sync_waiters


# ---------------------------------------------------------------------------
# Concurrent waiters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConcurrentWaiters:
    async def test_signals_dont_cross_fire(self, waiter_module):
        """Two concurrent waiters for different execution_ids: signaling one
        must not wake the other.
        """
        sw = waiter_module
        sw._test_db.get_execution = MagicMock(return_value=None)

        async def _wait(eid):
            return await sw.wait_for_sync_terminal(eid, timeout=2.0)

        t1 = asyncio.create_task(_wait("exec-A"))
        t2 = asyncio.create_task(_wait("exec-B"))
        await asyncio.sleep(0.05)
        assert "exec-A" in sw._sync_waiters
        assert "exec-B" in sw._sync_waiters

        sw.signal_sync_waiter("exec-A", result="result-A", chat_session_id="cs-A")
        payload_a = await asyncio.wait_for(t1, timeout=1.0)
        assert payload_a == {"result": "result-A", "chat_session_id": "cs-A"}

        # exec-B is still waiting; must not have fired.
        assert not t2.done()
        assert "exec-B" in sw._sync_waiters

        sw.signal_sync_waiter("exec-B", result="result-B", chat_session_id=None)
        payload_b = await asyncio.wait_for(t2, timeout=1.0)
        assert payload_b == {"result": "result-B", "chat_session_id": None}


# ---------------------------------------------------------------------------
# Regression: terminal status frozenset matches the enum
# ---------------------------------------------------------------------------


def test_terminal_statuses_match_enum(waiter_module):
    """If TaskExecutionStatus grows a new terminal value (e.g. EXPIRED), the
    poll fallback must include it or sync waiters miss the wake-up. This
    regression test forces a deliberate choice when the enum changes.
    """
    from models import TaskExecutionStatus

    # PENDING_RETRY is transient by design (#271 state machine, models.py:198-199):
    # RUNNING → PENDING_RETRY → RUNNING. Excluding it from the terminal set is
    # load-bearing — sync waiters must keep polling on a retry-pending row instead
    # of resolving early with no result. Enumerate non-terminals explicitly so a
    # new enum value forces a deliberate placement choice.
    non_terminal = {
        TaskExecutionStatus.QUEUED,
        TaskExecutionStatus.RUNNING,
        TaskExecutionStatus.PENDING_RETRY,
    }
    expected_terminal = set(TaskExecutionStatus) - non_terminal
    assert waiter_module.TERMINAL_TASK_STATUSES == frozenset(expected_terminal), (
        "TaskExecutionStatus enum gained a new value; update either the "
        "non_terminal set above (if transient) or TERMINAL_TASK_STATUSES in "
        "services/sync_waiter.py (if terminal) to decide whether sync waiters "
        "should wake on it."
    )
