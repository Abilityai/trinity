"""Wiring tests for the dispatch breaker ↔ task-execution seam (#526).

The breaker's *own* state machine is covered by
``tests/unit/test_dispatch_breaker.py`` (routing / fail-open) and
``tests/integration/test_dispatch_breaker.py`` (the Lua machine). What was
UNtested is how ``task_execution_service`` drives it. This file pins that seam:

  * ``dispatch_breaker_active`` — the combined gate (#1): global master switch
    short-circuits the per-agent SELECT (zero-cost-when-off), fail-safe → False.
  * ``_record_dispatch_terminal`` routing — the SUCCESS-terminal reset path
    (``error_code=None`` → ``record_outcome(None)``) and the recovery
    (open→closed) audit, plus the disabled no-op. Verified by mocking
    ``DispatchBreaker`` and capturing what ``_spawn_bg`` schedules — no Redis.
  * ``_spawn_bg`` — holds a strong ref until the task finishes, then discards it
    (the #2 GC-safety guarantee).

Deliberately NOT here: the open→drain *effect* (breaker opens → backlog fails
out + audit fires). That integration path is owned by the parallel
eng-review-findings work in ``tests/integration/test_dispatch_breaker.py``; this
file asserts only the routing decision, not the drain side effect, to avoid
duplicating it.

Known gap: the 3b drain-path dispatch re-check inside ``execute_task`` (open
breaker + ``slot_already_held`` + ``not dispatch_gate_checked`` → fast-fail
CIRCUIT_OPEN) is not unit-tested here — driving ``execute_task`` requires mocking
the full capacity / agent-call / activity surface. The boolean guard is simple
and the breaker-state read it depends on is covered by the acquire-gate tests.

Bootstrap mirrors ``tests/unit/test_auto_retry_reader_race.py`` — relies on
``tests/unit/conftest.py`` for the ``utils`` / ``agent_server`` stubs.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "src" / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

import config  # noqa: E402
import services.task_execution_service as mod  # noqa: E402
from services.dispatch_breaker import Transition  # noqa: E402
from services.task_execution_service import (  # noqa: E402
    TaskExecutionErrorCode,
    dispatch_breaker_active,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# dispatch_breaker_active — combined gate (#1)
# ---------------------------------------------------------------------------


class TestDispatchBreakerActive:
    def test_global_off_short_circuits_per_agent_select(self, monkeypatch):
        """Global master switch off → False WITHOUT the per-agent SELECT
        (restores the zero-cost-when-off invariant, D7)."""
        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
        db_mock = MagicMock()
        monkeypatch.setattr(mod, "db", db_mock)
        assert dispatch_breaker_active("a") is False
        db_mock.get_circuit_breaker_enabled.assert_not_called()

    def test_global_on_reads_per_agent_opt_in(self, monkeypatch):
        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)
        db_mock = MagicMock()
        db_mock.get_circuit_breaker_enabled.return_value = True
        monkeypatch.setattr(mod, "db", db_mock)
        assert dispatch_breaker_active("a") is True
        db_mock.get_circuit_breaker_enabled.assert_called_once_with("a")

    def test_global_on_per_agent_off_is_false(self, monkeypatch):
        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)
        db_mock = MagicMock()
        db_mock.get_circuit_breaker_enabled.return_value = False
        monkeypatch.setattr(mod, "db", db_mock)
        assert dispatch_breaker_active("a") is False

    def test_db_error_is_fail_safe_false(self, monkeypatch):
        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)
        db_mock = MagicMock()
        db_mock.get_circuit_breaker_enabled.side_effect = RuntimeError("db down")
        monkeypatch.setattr(mod, "db", db_mock)
        assert dispatch_breaker_active("a") is False


# ---------------------------------------------------------------------------
# _record_dispatch_terminal routing (reset / recovery / disabled no-op)
# ---------------------------------------------------------------------------


class _FakeBreaker:
    """Records record_outcome calls and returns a pre-set Transition."""

    def __init__(self, transition: Transition):
        self._t = transition
        self.outcomes: list = []

    def record_outcome(self, error_code):
        self.outcomes.append(error_code)
        return self._t


def _coro_name(coro) -> str:
    return getattr(
        coro, "__qualname__", getattr(getattr(coro, "cr_code", None), "co_name", "")
    )


def _run_terminal(monkeypatch, *, transition, breaker_enabled, error_code):
    """Drive _record_dispatch_terminal with a mocked DispatchBreaker, capturing
    the coroutines _spawn_bg would schedule (closed afterward, never run)."""
    constructed: list = []
    scheduled: list = []

    def _factory(agent_name):
        b = _FakeBreaker(transition)
        constructed.append(b)
        return b

    monkeypatch.setattr(mod, "DispatchBreaker", _factory)
    monkeypatch.setattr(mod, "_spawn_bg", scheduled.append)
    try:
        asyncio.run(
            mod._record_dispatch_terminal("agent-x", breaker_enabled, error_code)
        )
        return constructed, [_coro_name(c) for c in scheduled]
    finally:
        for c in scheduled:
            c.close()  # avoid "coroutine was never awaited" warnings


class TestRecordDispatchTerminalRouting:
    def test_disabled_is_total_noop(self, monkeypatch):
        """breaker_enabled=False → breaker never constructed, nothing scheduled."""
        constructed, names = _run_terminal(
            monkeypatch,
            transition=Transition("closed", "open"),  # would-open, but ignored
            breaker_enabled=False,
            error_code=TaskExecutionErrorCode.AUTH,
        )
        assert constructed == []
        assert names == []

    def test_success_terminal_calls_record_outcome_none(self, monkeypatch):
        """SUCCESS terminal passes None → record_outcome(None) (the reset path).
        A plain closed→closed success schedules no background work."""
        constructed, names = _run_terminal(
            monkeypatch,
            transition=Transition("closed", "closed"),
            breaker_enabled=True,
            error_code=None,
        )
        assert len(constructed) == 1
        assert constructed[0].outcomes == [None]
        assert names == []  # neither opened nor closed-recovery

    def test_recovery_schedules_recovery_audit(self, monkeypatch):
        """A success that closes a previously-open breaker (open→closed) routes to
        the recovery audit, NOT the backlog drain."""
        _constructed, names = _run_terminal(
            monkeypatch,
            transition=Transition("open", "closed"),
            breaker_enabled=True,
            error_code=None,
        )
        assert names == ["_audit_circuit_transition"]


# ---------------------------------------------------------------------------
# _spawn_bg — strong-ref retention (#2 GC safety)
# ---------------------------------------------------------------------------


class TestSpawnBgRefRetention:
    def test_ref_held_until_done_then_discarded(self):
        async def _run():
            mod._background_breaker_tasks.clear()
            started = asyncio.Event()
            release = asyncio.Event()

            async def _work():
                started.set()
                await release.wait()

            mod._spawn_bg(_work())
            await started.wait()
            # Strong ref retained while the task is in flight.
            assert len(mod._background_breaker_tasks) == 1
            release.set()
            await asyncio.sleep(0)  # let _work() finish
            await asyncio.sleep(0)  # let the done-callback run discard()
            assert len(mod._background_breaker_tasks) == 0

        asyncio.run(_run())
