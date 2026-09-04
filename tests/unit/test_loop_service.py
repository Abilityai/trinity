"""Unit tests for LoopService — sequential agent loops (#740).

Exercises the in-process loop runner against mocked DB + task execution
service. Covers:
- fixed mode (runs exactly max_runs)
- until mode (stops on signal)
- until mode hitting max_runs without signal
- template substitution ({{run}}, {{previous_response}})
- graceful stop (cooperative)
- task-failure terminates the loop with stop_reason='error'
- restart-recovery orphan sweep
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Bootstrap src/backend on sys.path (same convention as test_capacity_manager.py).
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)

# Modules this test shadows by clearing them from sys.modules before
# re-importing the src/backend-rooted versions. Declared as a top-level
# list so the autouse fixture below can save+restore them, preventing
# pollution into sibling test files (matches the precedent in
# tests/unit/test_telegram_webhook_backfill.py — required by the
# sys-modules lint baseline).
_STUBBED_MODULE_NAMES = (
    # #2080: the four `utils*` entries that used to head this list are gone —
    # they named the `tests/utils` shadow, which is now `tests/testkit`, so
    # popping `utils` would evict the real backend package.
)
for _shadow in _STUBBED_MODULE_NAMES:
    sys.modules.pop(_shadow, None)  # noqa: lint-allowed via _restore_sys_modules
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the shadowed `utils*` modules and restore after each test.

    The bootstrap above swaps the test-runner's top-level `utils` package
    for `src/backend/utils` so LoopService's imports resolve. Without
    this fixture, the swap would leak into sibling test files that
    depend on the original `tests/unit/utils/*` helpers.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _Result:
    """Stand-in for TaskExecutionResult."""
    status: str = "success"
    response: str = "ok"
    execution_id: str = "exec_x"
    cost: Optional[float] = 0.01
    context_used: Optional[int] = 10
    error: Optional[str] = None
    error_code: Optional[str] = None


class _FakeExecution:
    """Stand-in for a `schedule_executions` row (only what the advance reads)."""

    def __init__(self, execution_id: str, agent_name: str, loop_id: str):
        self.id = execution_id
        self.agent_name = agent_name
        self.loop_id = loop_id
        self.status = "running"
        self.response = None
        self.error = None
        self.cost = None
        self.duration_ms = None


class _FakeDB:
    """Minimal in-memory mock matching the loop_service surface.

    #2523: the loop is advanced by execution terminals now, so this fake also
    stands in for the execution-row surface (`create_task_execution`,
    `get_execution`, `update_execution_status`) and the CAS/claim primitives the
    advance and the due-loop sweep run on.
    """

    def __init__(self):
        self.loops: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}
        self.executions: dict[str, _FakeExecution] = {}
        self._next_loop = 0
        self._next_run = 0
        self._next_exec = 0
        # #2523: the #1156 deadline is measured from the persisted `started_at`,
        # so a fake-clock test has to stamp it on the fake timeline too — a real
        # `started_at` against a 2026-01-01 fake clock puts every deadline
        # decades in the past.
        self.clock = datetime.utcnow

    # ---- loop CRUD ----
    def create_loop(self, **kwargs) -> dict:
        self._next_loop += 1
        loop_id = f"loop_{self._next_loop}"
        row = {
            "id": loop_id,
            "status": "queued",
            "runs_completed": 0,
            "failed_runs": 0,
            "stop_reason": None,
            "last_response": None,
            "error": None,
            "created_at": "now",
            "started_at": None,
            "completed_at": None,
            "next_run_at": None,
            "stop_requested_at": None,
            **kwargs,
        }
        self.loops[loop_id] = row
        self.runs[loop_id] = []
        return dict(row)

    def get_loop(self, loop_id: str):
        return dict(self.loops[loop_id]) if loop_id in self.loops else None

    def mark_loop_running(self, loop_id: str):
        if self.loops[loop_id]["status"] == "queued":
            self.loops[loop_id]["status"] = "running"
            self.loops[loop_id]["started_at"] = _iso(self.clock())

    def update_loop_progress(self, loop_id: str, *, runs_completed: int, last_response, failed_runs=None):
        self.loops[loop_id]["runs_completed"] = runs_completed
        self.loops[loop_id]["last_response"] = last_response
        if failed_runs is not None:
            self.loops[loop_id]["failed_runs"] = failed_runs

    def finalize_loop(self, loop_id: str, *, status: str, stop_reason: str, error=None, failed_runs=None):
        self.loops[loop_id]["status"] = status
        self.loops[loop_id]["stop_reason"] = stop_reason
        self.loops[loop_id]["error"] = error
        self.loops[loop_id]["completed_at"] = "now"
        if failed_runs is not None:
            self.loops[loop_id]["failed_runs"] = failed_runs

    def list_non_terminal_loops(self):
        return [
            dict(r) for r in self.loops.values()
            if r["status"] in ("queued", "running")
        ]

    # ---- #2523 claims / parking ----
    def claim_loop_advance(self, loop_id: str, run_number: int) -> bool:
        row = self.loops.get(loop_id)
        if row is None:
            return False
        if row["status"] in _TERMINAL_LOOP_STATUSES:
            return False
        if row["runs_completed"] != run_number - 1:
            return False
        row["runs_completed"] = run_number
        return True

    def request_loop_stop(self, loop_id: str) -> bool:
        row = self.loops.get(loop_id)
        if row is None or row["status"] in _TERMINAL_LOOP_STATUSES:
            return False
        row["stop_requested_at"] = _iso(datetime.utcnow())
        return True

    def schedule_loop_next_run(self, loop_id: str, next_run_at: str) -> None:
        self.loops[loop_id]["next_run_at"] = next_run_at

    def claim_due_loop(self, loop_id: str, next_run_at: str) -> bool:
        row = self.loops.get(loop_id)
        if row is None or row["status"] in _TERMINAL_LOOP_STATUSES:
            return False
        if row.get("next_run_at") != next_run_at:
            return False
        row["next_run_at"] = None
        return True

    def list_due_loops(self, now: str, *, limit: int = 100):
        return [
            dict(r) for r in self.loops.values()
            if r.get("next_run_at")
            and r["next_run_at"] <= now
            and r["status"] not in _TERMINAL_LOOP_STATUSES
        ][:limit]

    # ---- run rows ----
    def start_loop_run(self, loop_id: str, run_number: int, *, execution_id=None) -> str:
        self._next_run += 1
        rid = f"lr_{self._next_run}"
        self.runs[loop_id].append({
            "id": rid,
            "loop_id": loop_id,
            "run_number": run_number,
            "execution_id": execution_id,
            "status": "running",
            "response": None,
            "error": None,
            "cost": None,
            "duration_ms": None,
            "started_at": "now",
            "completed_at": None,
        })
        return rid

    def finalize_loop_run(self, run_id: str, **kwargs):
        for runs in self.runs.values():
            for r in runs:
                if r["id"] == run_id:
                    for k, v in kwargs.items():
                        if k == "execution_id" and v is None:
                            continue  # COALESCE: don't overwrite with None
                        r[k] = v
                    r["completed_at"] = "now"
                    return

    def list_loop_runs(self, loop_id: str):
        return [dict(r) for r in sorted(
            self.runs.get(loop_id, []), key=lambda r: r["run_number"],
        )]

    def get_loop_run_by_execution(self, execution_id: str):
        for runs in self.runs.values():
            for r in runs:
                if r["execution_id"] == execution_id:
                    return dict(r)
        return None

    # ---- execution rows ----
    def create_task_execution(self, **kwargs):
        self._next_exec += 1
        eid = f"exec_{self._next_exec}"
        row = _FakeExecution(eid, kwargs.get("agent_name"), kwargs.get("loop_id"))
        self.executions[eid] = row
        return row

    def get_execution(self, execution_id: str):
        return self.executions.get(execution_id)

    def update_execution_status(self, *, execution_id: str, status, error=None, **_kw):
        row = self.executions.get(execution_id)
        if row is None:
            return False
        row.status = getattr(status, "value", status)
        row.error = error
        return True


@dataclass
class _FakeTaskService:
    """Records execute_task() calls, writes the scripted terminal, returns it.

    #2523: production writes the terminal onto the execution row and the loop is
    advanced from it, so the fake has to do the same — a fake that only returned
    a result would exercise a path the real service does not have.
    """
    db: Any = None
    results: list = field(default_factory=list)  # list[_Result]
    calls: list = field(default_factory=list)
    _idx: int = 0

    async def execute_task(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results[self._idx] if self._idx < len(self.results) else _Result()
        self._idx += 1
        # A scripted Exception models the raised-exception failure surface.
        if isinstance(result, BaseException):
            raise result
        execution_id = kwargs.get("execution_id")
        row = self.db.executions.get(execution_id) if self.db else None
        if row is not None:
            row.status = result.status
            row.response = result.response
            row.error = result.error
            row.cost = result.cost
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TERMINAL_LOOP_STATUSES = frozenset(
    {"completed", "completed_with_errors", "stopped", "failed", "interrupted"}
)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds") + "Z"


@pytest.fixture
def loop_module(monkeypatch):
    """Import services.loop_service with mocks installed."""
    from services import loop_service as ls

    fake_db = _FakeDB()
    fake_task_service = _FakeTaskService(db=fake_db)

    monkeypatch.setattr(ls, "db", fake_db)
    monkeypatch.setattr(ls, "get_task_execution_service", lambda: fake_task_service)
    monkeypatch.setattr(ls, "_websocket_manager", None)
    ls._inflight_dispatches.clear()

    return ls, fake_db, fake_task_service


def _run(coro):
    """Run a coroutine and let the terminal-driven chain settle (#2523).

    A loop no longer completes inside the awaited call: `start_loop` dispatches
    iteration 1 and returns, and each iteration's advance spawns the next. So
    every driver here has to drain `_inflight_dispatches` before asserting, or
    it asserts on a loop that is one iteration in.
    """
    async def _driver():
        result = await coro
        from services import loop_service as ls

        for _ in range(20000):
            if not ls._inflight_dispatches:
                break
            await asyncio.sleep(0)
        return result

    return asyncio.run(_driver())


def _sweep(svc):
    """Run the due-loop sweep and settle, as the 5s backend tick does."""
    return _run(svc.dispatch_due_loops())


def _scripted_exec(ts, db, *, on_call=None):
    """An `execute_task` stand-in that writes the scripted terminal (#2523).

    Every test that overrides `ts.execute_task` needs this: the advance reads
    the execution ROW, not the returned result, so an override that only
    returns exercises a path production does not have. `on_call(idx)` runs
    before the terminal is written, for tests that need to poke state mid-run.
    """
    async def _exec(**kwargs):
        ts.calls.append(kwargs)
        idx = ts._idx
        ts._idx += 1
        if on_call is not None:
            await on_call(idx)
        result = ts.results[idx] if idx < len(ts.results) else _Result()
        if isinstance(result, BaseException):
            raise result
        row = db.executions.get(kwargs.get("execution_id"))
        if row is not None:
            row.status = result.status
            row.response = result.response
            row.error = result.error
            row.cost = result.cost
        return result

    return _exec


def _past() -> str:
    """An ISO-Z instant already in the past — makes a parked loop due now."""
    return _iso(datetime.utcnow() - timedelta(seconds=1))


# ---------------------------------------------------------------------------
# Template substitution
# ---------------------------------------------------------------------------

class TestRenderTemplate:
    def test_run_placeholder(self, loop_module):
        ls, _, _ = loop_module
        assert ls._render_template("hi {{run}}", 3, None) == "hi 3"

    def test_previous_response_empty_on_first_run(self, loop_module):
        ls, _, _ = loop_module
        assert ls._render_template("p={{previous_response}}", 1, None) == "p="

    def test_previous_response_truncated_to_trailing_2000(self, loop_module):
        ls, _, _ = loop_module
        big = "a" * 5000
        out = ls._render_template("{{previous_response}}", 2, big)
        assert len(out) == 2000
        assert out == "a" * 2000

    def test_both_placeholders(self, loop_module):
        ls, _, _ = loop_module
        out = ls._render_template("r={{run}}/p={{previous_response}}", 2, "xyz")
        assert out == "r=2/p=xyz"


# ---------------------------------------------------------------------------
# Runner — fixed mode
# ---------------------------------------------------------------------------

class TestFixedMode:
    def test_runs_exactly_max_runs_times(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response=f"r{i}") for i in range(1, 4)]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="step {{run}}",
                max_runs=3,
            )
            # #2523: no in-process handle to await — `_run` drains the
            # terminal-driven dispatch chain instead.
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3
        assert len(ts.calls) == 3
        # Rendered messages reflect iteration numbers
        assert ts.calls[0]["message"] == "step 1"
        assert ts.calls[2]["message"] == "step 3"
        # triggered_by + loop_id wired through
        assert ts.calls[0]["triggered_by"] == "loop"
        assert ts.calls[0]["loop_id"] == loop_id


# ---------------------------------------------------------------------------
# Runner — failure policy (#1167)
# ---------------------------------------------------------------------------

def _drive(ls, **start_kwargs):
    """Start a loop, await its background task, return its id."""
    async def go():
        service = ls.LoopService()
        row = await service.start_loop(**start_kwargs)
        return row["id"]
    return _run(go())


class TestFailurePolicy:
    def test_abort_mode_default_stops_on_first_failure(self, loop_module):
        """Default on_failure='abort' preserves fail-fast behavior."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="r1"),
            _Result(status="failed", error="boom", error_code="AGENT_ERROR"),
            _Result(response="r3"),
        ]
        loop_id = _drive(
            ls, agent_name="a1", message_template="step {{run}}", max_runs=3,
        )
        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "error"
        assert len(ts.calls) == 2  # stopped after the failed run, never ran #3
        assert loop["failed_runs"] == 1

    def test_continue_mode_proceeds_past_failure(self, loop_module):
        """on_failure='continue' tolerates a failed run and finishes max_runs."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="r1"),
            _Result(status="failed", error="boom", error_code="TIMEOUT"),
            _Result(response="r3"),
        ]
        loop_id = _drive(
            ls,
            agent_name="a1",
            message_template="step {{run}} prev={{previous_response}}",
            max_runs=3,
            on_failure="continue",
            max_consecutive_failures=3,
        )
        loop = db.get_loop(loop_id)
        assert len(ts.calls) == 3  # all three ran
        assert loop["status"] == "completed_with_errors"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["failed_runs"] == 1
        assert loop["runs_completed"] == 3
        # {{previous_response}} carries the last *successful* response (r1),
        # NOT the failed run-2 response.
        assert ts.calls[2]["message"] == "step 3 prev=r1"

    def test_continue_mode_consecutive_cutoff_aborts(self, loop_module):
        """Continue mode still terminates once consecutive failures hit the cap."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(status="failed", error=f"boom{i}", error_code="AUTH")
            for i in range(5)
        ]
        loop_id = _drive(
            ls,
            agent_name="a1",
            message_template="step {{run}}",
            max_runs=5,
            on_failure="continue",
            max_consecutive_failures=2,
        )
        loop = db.get_loop(loop_id)
        assert len(ts.calls) == 2  # aborted at the 2nd consecutive failure
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "max_consecutive_failures"
        assert loop["failed_runs"] == 2

    def test_continue_mode_tolerates_raised_exception(self, loop_module):
        """The raised-exception surface is honored by continue mode too."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="r1"),
            RuntimeError("kaboom"),  # raised inside execute_task
            _Result(response="r3"),
        ]
        loop_id = _drive(
            ls, agent_name="a1", message_template="step {{run}}", max_runs=3,
            on_failure="continue", max_consecutive_failures=3,
        )
        loop = db.get_loop(loop_id)
        assert len(ts.calls) == 3
        assert loop["status"] == "completed_with_errors"
        assert loop["failed_runs"] == 1

    def test_continue_mode_applies_delay_after_raised_exception(self, loop_module):
        """The exception surface honors delay_seconds too (surface parity)."""
        ls, db, ts = loop_module
        ts.execute_task = _scripted_exec(ts, db)
        ts.results = [
            RuntimeError("boom"),       # run 1 raises → delay should still apply
            _Result(response="ok2"),    # run 2 (last) → no trailing delay
        ]
        service = ls.LoopService()
        loop_id = _run(service.start_loop(
            agent_name="a1", message_template="step {{run}}", max_runs=2,
            on_failure="continue", max_consecutive_failures=3, delay_seconds=5,
        ))["id"]

        # #2523: the pause is a PARK, not an `asyncio.sleep` — the raised run 1
        # is tolerated, and the loop stops here holding a future `next_run_at`
        # rather than occupying a coroutine for 5s.
        loop = db.get_loop(loop_id)
        assert len(ts.calls) == 1
        assert loop["next_run_at"] is not None
        assert loop["status"] == "running"

        # Make it due and let the sweep bring it back.
        db.loops[loop_id]["next_run_at"] = _past()
        _sweep(service)

        loop = db.get_loop(loop_id)
        assert len(ts.calls) == 2
        assert loop["status"] == "completed_with_errors"
        # Run 2 is the last, so no trailing park.
        assert loop["next_run_at"] is None

    def test_continue_mode_resets_streak_on_success(self, loop_module):
        """A success resets the consecutive-failure counter (alternating runs)."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(status="failed", error="f1", error_code="AGENT_ERROR"),
            _Result(response="ok2"),
            _Result(status="failed", error="f3", error_code="AGENT_ERROR"),
            _Result(response="ok4"),
            _Result(status="failed", error="f5", error_code="AGENT_ERROR"),
        ]
        loop_id = _drive(
            ls, agent_name="a1", message_template="step {{run}}", max_runs=5,
            on_failure="continue", max_consecutive_failures=2,
        )
        loop = db.get_loop(loop_id)
        # Never 2 in a row → runs all 5, completes with errors.
        assert len(ts.calls) == 5
        assert loop["status"] == "completed_with_errors"
        assert loop["failed_runs"] == 3


# ---------------------------------------------------------------------------
# Runner — until mode
# ---------------------------------------------------------------------------

class TestUntilMode:
    def test_stops_when_signal_appears(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="working..."),
            _Result(response="still working..."),
            _Result(response="all good [[DONE]]"),
            _Result(response="should not run"),
        ]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=10,
                stop_signal="[[DONE]]",
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "stop_signal_matched"
        assert loop["runs_completed"] == 3
        assert len(ts.calls) == 3  # 4th not called

    def test_until_mode_hits_max_runs_without_signal(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="no signal here") for _ in range(2)]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=2,
                stop_signal="[[DONE]]",
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 2


# ---------------------------------------------------------------------------
# Runner — previous_response wiring
# ---------------------------------------------------------------------------

class TestPreviousResponse:
    def test_previous_response_threaded_between_iterations(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="alpha"),
            _Result(response="beta"),
            _Result(response="gamma"),
        ]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="prev={{previous_response}}",
                max_runs=3,
            )
            return row["id"]

        loop_id = _run(go())
        # Iteration 1: empty; 2: alpha; 3: beta
        assert ts.calls[0]["message"] == "prev="
        assert ts.calls[1]["message"] == "prev=alpha"
        assert ts.calls[2]["message"] == "prev=beta"


# ---------------------------------------------------------------------------
# Runner — graceful stop
# ---------------------------------------------------------------------------

class TestStopLoop:
    def test_stop_requested_mid_run_finalizes_at_the_next_boundary(self, loop_module):
        """A stop arriving while run 2 is in flight lets that run finish, then
        finalizes `user_stopped` — the cooperative contract, unchanged by #2523.

        The stop is a column now (`stop_requested_at`), not a flag on an
        in-memory handle, so it is read at the boundary from the row.
        """
        ls, db, ts = loop_module
        ts.results = [_Result(response=f"r{i}") for i in range(10)]
        captured: dict = {}

        async def _on_call(idx):
            if idx == 1:  # during run 2
                await captured["service"].stop_loop(captured["loop_id"])

        ts.execute_task = _scripted_exec(ts, db, on_call=_on_call)

        # `start_loop` dispatches run 1 before returning, so the callback would
        # not yet have the loop id — create + dispatch explicitly instead.
        async def driver():
            service = ls.LoopService()
            captured["service"] = service
            row = db.create_loop(
                agent_name="a1", message_template="m", max_runs=10,
                delay_seconds=0, stop_signal=None, timeout_per_run=None,
                max_duration_seconds=None, max_cost_usd=None,
                no_progress_threshold=None, on_failure="abort",
                max_consecutive_failures=3, model=None, allowed_tools=None,
                started_by_user_id=None, started_by_user_email=None,
                source_agent_name=None, source_mcp_key_id=None,
                source_mcp_key_name=None,
            )
            captured["loop_id"] = row["id"]
            db.mark_loop_running(row["id"])
            await service._dispatch_run(db.get_loop(row["id"]), run_number=1)
            return row["id"]

        loop_id = _run(driver())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"
        # Run 2 was in flight when the stop landed and still completed.
        assert loop["runs_completed"] == 2
        assert len(ts.calls) == 2

    def test_stop_on_a_parked_loop_finalizes_immediately(self, loop_module):
        """#2523: a loop waiting out its `delay_seconds` has nothing in flight,
        so no terminal is coming to notice the request — `stop_loop` has to
        finalize it itself or it would sit `running` until the sweep."""
        ls, db, ts = loop_module
        ts.results = [_Result(response="r1")]
        ts.execute_task = _scripted_exec(ts, db)

        service = ls.LoopService()
        loop_id = _run(service.start_loop(
            agent_name="a1", message_template="m", max_runs=5, delay_seconds=30,
        ))["id"]
        assert db.get_loop(loop_id)["next_run_at"] is not None

        assert _run(service.stop_loop(loop_id)) == "stopping"
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"
        assert loop["next_run_at"] is None
        assert len(ts.calls) == 1  # the park was never dispatched

    def test_stop_works_without_the_process_that_started_the_loop(self, loop_module):
        """The reason the flag became a column. `stop_loop` used to read an
        in-memory `_LoopHandle`, so it only worked in the process that started
        the loop — and when it found none (after a restart) it gave up and
        finalized the loop `interrupted`. A fresh service instance now serves it.
        """
        ls, db, ts = loop_module
        ts.results = [_Result(response="r1")]
        ts.execute_task = _scripted_exec(ts, db)

        starter = ls.LoopService()
        loop_id = _run(starter.start_loop(
            agent_name="a1", message_template="m", max_runs=5, delay_seconds=30,
        ))["id"]

        other_process = ls.LoopService()
        assert _run(other_process.stop_loop(loop_id)) == "stopping"
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"

    def test_stop_loop_on_already_terminal_returns_already_done(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result()]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=1,
            )
            return service, row["id"]

        service, loop_id = _run(go())

        async def check():
            return await service.stop_loop(loop_id)

        assert _run(check()) == "already_done"


# ---------------------------------------------------------------------------
# Runner — failure path
# ---------------------------------------------------------------------------

class TestFailure:
    def test_failed_iteration_terminates_loop_with_error(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [
            _Result(response="ok"),
            _Result(status="failed", response=None, error="boom",
                    error_code="agent_error"),
            _Result(response="should not run"),
        ]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "error"
        assert loop["runs_completed"] == 2  # second iteration counted, even though it failed
        assert len(ts.calls) == 2

    def test_cancelled_iteration_stops_loop(self, loop_module):
        """#679: a CANCELLED iteration is non-success — the loop must stop (the
        else branch finalizes it), never continue treating cancel as success."""
        ls, db, ts = loop_module
        ts.results = [
            _Result(status="cancelled", response="", error="Execution cancelled by user",
                    error_code=None),
            _Result(response="should not run"),
        ]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"       # stops, not treated as success
        assert loop["stop_reason"] == "error"
        assert loop["runs_completed"] == 1
        assert len(ts.calls) == 1               # the 2nd iteration never ran

    def test_iteration_exception_aborts_loop(self, loop_module):
        ls, db, ts = loop_module

        async def boom(**kwargs):
            raise RuntimeError("dispatch crash")

        ts.execute_task = boom  # type: ignore

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "error"
        assert "dispatch crash" in (loop["error"] or "")


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    """#2523 replaced `mark_orphan_loops_interrupted` with a reconcile.

    The old hook flipped EVERY non-terminal loop to `interrupted` on boot, which
    was correct while a loop was an `asyncio.Task` whose state died with the
    process — and is pure data loss now that the loop lives on its row. Nothing
    is interrupted any more; the reconcile re-arms only what actually lost its
    dispatch.
    """

    def test_a_loop_with_nothing_in_flight_is_rearmed_not_interrupted(self, loop_module):
        ls, db, _ = loop_module
        row = db.create_loop(agent_name="a", message_template="m", max_runs=3)
        db.mark_loop_running(row["id"])

        service = ls.LoopService()
        assert _run(service.reconcile_after_restart()) == 1

        loop = db.get_loop(row["id"])
        assert loop["status"] == "running"          # NOT interrupted
        assert loop["stop_reason"] is None
        assert loop["next_run_at"] is not None      # due now → the sweep takes it

    def test_a_loop_with_a_live_execution_is_left_alone(self, loop_module):
        """Its terminal — or `cleanup_service`'s recovery of it, which writes
        one — advances the loop. Re-arming it here would dispatch a second
        concurrent iteration."""
        ls, db, _ = loop_module
        row = db.create_loop(agent_name="a", message_template="m", max_runs=3)
        db.mark_loop_running(row["id"])
        execution = db.create_task_execution(agent_name="a", loop_id=row["id"])
        execution.status = "running"
        db.start_loop_run(row["id"], 1, execution_id=execution.id)

        service = ls.LoopService()
        assert _run(service.reconcile_after_restart()) == 0
        assert db.get_loop(row["id"])["next_run_at"] is None

    def test_a_loop_whose_execution_died_is_advanced_not_rearmed(self, loop_module):
        """The run row still says `running` but its execution is terminal, so the
        terminal event was lost with the restart.

        The recovery is to ADVANCE from that terminal, not to re-arm: `runs_completed`
        has not moved, so a re-arm would dispatch a second row for the same
        `run_number` and bill the iteration twice.
        """
        ls, db, ts = loop_module
        ts.execute_task = _scripted_exec(ts, db)
        row = db.create_loop(
            agent_name="a", message_template="m", max_runs=3, on_failure="abort",
            delay_seconds=0, stop_signal=None, timeout_per_run=None,
            max_duration_seconds=None, max_cost_usd=None, no_progress_threshold=None,
            max_consecutive_failures=3, model=None, allowed_tools=None,
            started_by_user_id=None, started_by_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None,
        )
        db.mark_loop_running(row["id"])
        execution = db.create_task_execution(agent_name="a", loop_id=row["id"])
        execution.status = "failed"
        execution.error = "lost to the restart"
        db.start_loop_run(row["id"], 1, execution_id=execution.id)

        service = ls.LoopService()
        assert _run(service.reconcile_after_restart()) == 1

        loop = db.get_loop(row["id"])
        assert loop["runs_completed"] == 1
        assert loop["status"] == "failed"          # abort mode, run 1 failed
        assert loop["stop_reason"] == "error"
        # Exactly one run row for iteration 1 — no duplicate dispatch.
        assert [r["run_number"] for r in db.list_loop_runs(row["id"])] == [1]
        assert len(ts.calls) == 0

    def test_a_loop_whose_execution_row_is_gone_is_closed_not_stranded(self, loop_module):
        ls, db, ts = loop_module
        ts.execute_task = _scripted_exec(ts, db)
        row = db.create_loop(
            agent_name="a", message_template="m", max_runs=3, on_failure="abort",
            delay_seconds=0, stop_signal=None, timeout_per_run=None,
            max_duration_seconds=None, max_cost_usd=None, no_progress_threshold=None,
            max_consecutive_failures=3, model=None, allowed_tools=None,
            started_by_user_id=None, started_by_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None,
        )
        db.mark_loop_running(row["id"])
        db.start_loop_run(row["id"], 1, execution_id="exec_vanished")

        service = ls.LoopService()
        _run(service.reconcile_after_restart())
        assert db.get_loop(row["id"])["status"] in ("failed", "running")
        # The loop is not left pinned on a `running` run row forever.
        assert db.list_loop_runs(row["id"])[0]["status"] != "running"

    def test_an_already_parked_loop_is_left_to_the_sweep(self, loop_module):
        ls, db, _ = loop_module
        row = db.create_loop(agent_name="a", message_template="m", max_runs=3)
        db.mark_loop_running(row["id"])
        future = _iso(datetime.utcnow() + timedelta(seconds=300))
        db.schedule_loop_next_run(row["id"], future)

        service = ls.LoopService()
        assert _run(service.reconcile_after_restart()) == 0
        assert db.get_loop(row["id"])["next_run_at"] == future

    def test_terminal_loops_are_untouched_and_it_is_idempotent(self, loop_module):
        ls, db, _ = loop_module
        service = ls.LoopService()
        assert _run(service.reconcile_after_restart()) == 0

        row = db.create_loop(agent_name="a", message_template="m", max_runs=1)
        db.finalize_loop(row["id"], status="completed", stop_reason="max_runs_reached")
        assert _run(service.reconcile_after_restart()) == 0

        live = db.create_loop(agent_name="a", message_template="m", max_runs=1)
        db.mark_loop_running(live["id"])
        assert _run(service.reconcile_after_restart()) == 1
        # Second pass: already parked, no double re-arm.
        assert _run(service.reconcile_after_restart()) == 0


# ---------------------------------------------------------------------------
# Runner — wall-clock deadline (#1156)
# ---------------------------------------------------------------------------

class _FakeClock:
    """Controllable stand-in for ``datetime`` inside loop_service.

    ``utcnow()`` returns the current fake instant; tests advance ``now``
    (directly or via a task that bumps it each run) to drive the deadline
    deterministically — no real sleeping. ``fromisoformat`` passes straight
    through: #2523 reads the persisted `started_at` back to compute the
    deadline, so the substitute has to parse as well as tell the time.
    """
    now = datetime(2026, 1, 1, 0, 0, 0)

    @classmethod
    def utcnow(cls):
        return cls.now

    @staticmethod
    def fromisoformat(value):
        return datetime.fromisoformat(value)


class TestDeadline:
    def _install_clock(self, ls, db, monkeypatch, *, advance_per_run: float):
        """Swap in the fake clock; each execute_task advances it."""
        _FakeClock.now = datetime(2026, 1, 1, 0, 0, 0)
        monkeypatch.setattr(ls, "datetime", _FakeClock)
        db.clock = _FakeClock.utcnow

        async def _exec(**kwargs):
            ts = self._ts
            ts.calls.append(kwargs)
            result = ts.results[ts._idx] if ts._idx < len(ts.results) else _Result()
            ts._idx += 1
            # #2523: write the terminal onto the execution row, as the real
            # dispatch does — the advance reads the row, not the return value.
            row = db.executions.get(kwargs.get("execution_id"))
            if row is not None:
                row.status = result.status
                row.response = result.response
                row.error = result.error
                row.cost = result.cost
            _FakeClock.now = _FakeClock.now + timedelta(seconds=advance_per_run)
            return result

        return _exec

    def test_deadline_stops_loop_at_boundary(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response=f"r{i}") for i in range(1, 6)]
        ts.execute_task = self._install_clock(ls, db, monkeypatch, advance_per_run=6)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=5,
                max_duration_seconds=10,  # ~1.6 runs fit before the deadline
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "deadline_exceeded"
        # Run 1 (t0→6) and run 2 (t6→12) both started before the deadline; the
        # boundary check before run 3 (t12 ≥ 10) trips. max_runs never reached.
        assert loop["runs_completed"] == 2
        assert len(ts.calls) == 2

    def test_in_flight_run_is_not_killed_mid_turn(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response="done-run")]
        # One run pushes the clock well past the deadline; that run must still
        # finalize as completed (deadline is enforced only at the boundary).
        ts.execute_task = self._install_clock(ls, db, monkeypatch, advance_per_run=999)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=5,
                max_duration_seconds=10,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["stop_reason"] == "deadline_exceeded"
        assert loop["runs_completed"] == 1  # the in-flight run completed
        runs = db.list_loop_runs(loop_id)
        assert runs[0]["status"] == "completed"
        assert runs[0]["response"] == "done-run"

    def test_park_does_not_reach_past_the_deadline(self, loop_module, monkeypatch):
        """#2523: the inter-run pause is a parked `next_run_at`, not a sleep, but
        it is still capped to the remaining #1156 budget — a park that outlived
        the deadline would hold the loop `running` long after it should have
        finalized."""
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response="r1"), _Result(response="r2")]
        ts.execute_task = self._install_clock(ls, db, monkeypatch, advance_per_run=3)

        service = ls.LoopService()
        loop_id = _run(service.start_loop(
            agent_name="a1",
            message_template="m",
            max_runs=5,
            delay_seconds=100,        # would blow way past the deadline
            max_duration_seconds=10,
        ))["id"]

        loop = db.get_loop(loop_id)
        # run 1 ran t0→3; the park is capped to the remaining 7s, so it lands ON
        # the deadline (t10) rather than 100s past it.
        assert loop["next_run_at"] == _iso(datetime(2026, 1, 1, 0, 0, 10))
        assert len(ts.calls) == 1

        # When it comes due the deadline has arrived, so the sweep finalizes
        # rather than dispatching.
        _FakeClock.now = datetime(2026, 1, 1, 0, 0, 10)
        db.loops[loop_id]["next_run_at"] = _past()
        _sweep(service)

        loop = db.get_loop(loop_id)
        assert loop["stop_reason"] == "deadline_exceeded"
        assert loop["status"] == "stopped"
        assert len(ts.calls) == 1  # run 2 never dispatched

    def test_no_deadline_runs_all_when_unset(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response=f"r{i}") for i in range(1, 4)]
        # Clock jumps far each run; with no deadline it must be ignored.
        ts.execute_task = self._install_clock(ls, db, monkeypatch, advance_per_run=10_000)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=3,
                max_duration_seconds=None,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3


# ---------------------------------------------------------------------------
# Runner — cost budget (#1155)
# ---------------------------------------------------------------------------

class TestBudget:
    """max_cost_usd as an iteration-boundary gate (boundary-only precedence)."""

    @staticmethod
    def _drive(ls, service, **start_kwargs):
        async def go():
            row = await service.start_loop(
                agent_name="a1", message_template="m", **start_kwargs,
            )
            return row["id"]
        return _run(go())

    def test_budget_stops_at_boundary(self, loop_module):
        ls, db, ts = loop_module
        # cost 0.01/run, budget 0.025: runs 1–3 execute (acc 0.01, 0.02, 0.03);
        # the boundary before run 4 sees 0.03 >= 0.025 and stops.
        ts.results = [_Result(response=f"r{i}", cost=0.01) for i in range(1, 6)]
        service = ls.LoopService()
        loop_id = self._drive(ls, service, max_runs=5, max_cost_usd=0.025)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "budget_exhausted"
        assert loop["runs_completed"] == 3
        assert len(ts.calls) == 3

    def test_in_flight_run_not_killed(self, loop_module):
        ls, db, ts = loop_module
        # The very first run overshoots the budget by 500x; it must still
        # finalize as completed (boundary-only — never killed mid-turn). The
        # NEXT boundary then stops the loop.
        ts.results = [_Result(response="big", cost=5.0)]
        service = ls.LoopService()
        loop_id = self._drive(ls, service, max_runs=5, max_cost_usd=0.01)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "budget_exhausted"
        assert loop["runs_completed"] == 1
        runs = db.list_loop_runs(loop_id)
        assert runs[0]["status"] == "completed"

    def test_null_cost_counts_zero(self, loop_module, caplog):
        ls, db, ts = loop_module
        # Cost reporting is broken (all None): fail-open — the loop runs all
        # max_runs iterations (NULL counts as 0) and a WARN is emitted per run.
        ts.results = [_Result(response=f"r{i}", cost=None) for i in range(1, 4)]
        service = ls.LoopService()
        with caplog.at_level("WARNING"):
            loop_id = self._drive(ls, service, max_runs=3, max_cost_usd=0.05)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3
        assert any("reported no cost" in r.message for r in caplog.records)

    def test_no_budget_runs_all(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response=f"r{i}", cost=99.0) for i in range(1, 4)]
        service = ls.LoopService()
        loop_id = self._drive(ls, service, max_runs=3, max_cost_usd=None)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3

    def test_nan_cost_does_not_poison(self, loop_module, caplog):
        ls, db, ts = loop_module
        # Run 1 reports NaN (ignored — accumulator stays 0); run 2 reports a
        # real 0.10 that crosses the 0.05 budget, so the boundary before run 3
        # trips. Without the finite guard, NaN would poison the accumulator and
        # `NaN >= budget` would never fire. The NaN run must also WARN — under
        # an active budget a non-finite cost is a metering fault, not silent.
        ts.results = [
            _Result(response="r1", cost=float("nan")),
            _Result(response="r2", cost=0.10),
            _Result(response="r3", cost=0.10),
        ]
        service = ls.LoopService()
        with caplog.at_level("WARNING"):
            loop_id = self._drive(ls, service, max_runs=5, max_cost_usd=0.05)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "budget_exhausted"
        assert loop["runs_completed"] == 2
        assert any("non-finite cost" in r.message for r in caplog.records)

    def test_budget_vs_signal_precedence(self, loop_module):
        ls, db, ts = loop_module
        # A single run both blows the budget AND matches the stop_signal. The
        # end-of-run stop_signal check fires within the same iteration, before
        # the next boundary — so stop_signal_matched wins (boundary-only spec).
        ts.results = [_Result(response="all done [[DONE]]", cost=5.0)]
        service = ls.LoopService()
        loop_id = self._drive(
            ls, service, max_runs=5, max_cost_usd=0.01, stop_signal="[[DONE]]",
        )

        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "stop_signal_matched"
        assert loop["runs_completed"] == 1
# Runner — no-progress / doom-loop detection (#1157)
# ---------------------------------------------------------------------------

class _FakeWS:
    """Captures broadcast payloads for the WS contract assertion."""

    def __init__(self):
        self.events: list[dict] = []

    async def broadcast(self, payload):
        self.events.append(json.loads(payload))


class TestNoProgress:
    def test_stops_after_k_identical_responses(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="same") for _ in range(10)]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "no_progress"
        assert loop["runs_completed"] == 3
        assert len(ts.calls) == 3

    def test_near_miss_resets_counter(self, loop_module):
        ls, db, ts = loop_module
        # A, A, B, A, A, A → counter resets on B; stops on the 3rd trailing A.
        ts.results = [
            _Result(response="A"), _Result(response="A"), _Result(response="B"),
            _Result(response="A"), _Result(response="A"), _Result(response="A"),
            _Result(response="A"),  # would-be 7th, must not run
        ]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "no_progress"
        assert loop["runs_completed"] == 6
        assert len(ts.calls) == 6

    def test_disabled_with_zero_runs_to_max_runs(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="same") for _ in range(4)]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=4,
                no_progress_threshold=0,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 4

    def test_disabled_with_none_runs_to_max_runs(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="same") for _ in range(3)]

        async def go():
            service = ls.LoopService()
            # no_progress_threshold omitted → service default None → disabled
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3

    def test_whitespace_normalization_counts_as_identical(self, loop_module):
        ls, db, ts = loop_module
        # "hi" and "hi  \n" normalize to the same fingerprint.
        ts.results = [_Result(response="hi"), _Result(response="hi  \n"),
                      _Result(response="should not run")]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=5,
                no_progress_threshold=2,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "no_progress"
        assert loop["runs_completed"] == 2
        assert len(ts.calls) == 2

    def test_distinct_words_do_not_collide(self, loop_module):
        """`" ".join` preserves word boundaries: "foo bar" != "foobar"."""
        ls, _, _ = loop_module
        assert ls._fingerprint("foo bar") != ls._fingerprint("foobar")
        assert ls._fingerprint("hi") == ls._fingerprint("  hi  \n")
        # empty / None / whitespace-only all collapse to the same fingerprint
        assert ls._fingerprint(None) == ls._fingerprint("")
        assert ls._fingerprint("   \n ") == ls._fingerprint("")

    def test_stop_signal_takes_precedence(self, loop_module):
        """stop_signal is checked before no_progress, so it wins. All responses
        contain the signal → the loop stops on run 1 with stop_signal_matched,
        never accumulating a no_progress count."""
        ls, db, ts = loop_module
        ts.results = [_Result(response="done [[STOP]]") for _ in range(10)]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                stop_signal="[[STOP]]", no_progress_threshold=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["stop_reason"] == "stop_signal_matched"
        assert loop["status"] == "completed"
        assert loop["runs_completed"] == 1

    def test_user_stop_takes_precedence_over_no_progress(self, loop_module):
        """A pending user-stop on the K-th run must finalize user_stopped, not
        no_progress."""
        ls, db, ts = loop_module
        ts.results = [_Result(response="same") for _ in range(10)]

        captured: dict = {}

        async def _on_call(idx):
            # On the 3rd call (the run that would trip no_progress at K=3),
            # request the stop mid-run. #2523: a column, not a handle flag.
            if idx == 2:
                db.request_loop_stop(captured["loop_id"])

        ts.execute_task = _scripted_exec(ts, db, on_call=_on_call)

        async def go():
            service = ls.LoopService()
            row = db.create_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3, stop_signal=None, delay_seconds=0,
                timeout_per_run=None, max_duration_seconds=None,
                max_cost_usd=None, on_failure="abort",
                max_consecutive_failures=3, model=None, allowed_tools=None,
                started_by_user_id=None, started_by_user_email=None,
                source_agent_name=None, source_mcp_key_id=None,
                source_mcp_key_name=None,
            )
            captured["loop_id"] = row["id"]
            db.mark_loop_running(row["id"])
            await service._dispatch_run(db.get_loop(row["id"]), run_number=1)
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"

    def test_deadline_takes_precedence_over_no_progress(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        # reuse the deadline test's fake clock
        _FakeClock.now = datetime(2026, 1, 1, 0, 0, 0)
        monkeypatch.setattr(ls, "datetime", _FakeClock)
        db.clock = _FakeClock.utcnow
        ts.results = [_Result(response="same") for _ in range(10)]

        async def _advance(_idx):
            return None

        base = _scripted_exec(ts, db, on_call=_advance)

        async def _exec(**kwargs):
            result = await base(**kwargs)
            _FakeClock.now = _FakeClock.now + timedelta(seconds=4)
            return result

        ts.execute_task = _exec

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3, max_duration_seconds=10,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        # run1 t0→4, run2 t4→8, run3 t8→12: at run-3's no_progress check the
        # deadline (10) is passed, so deadline_exceeded wins at the next boundary.
        assert loop["stop_reason"] == "deadline_exceeded"
        assert loop["status"] == "stopped"

    def test_threshold_above_max_runs_never_fires(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="same"), _Result(response="same")]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=2,
                no_progress_threshold=3,
            )
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 2

    def test_ws_completed_event_carries_no_progress(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="same") for _ in range(5)]
        ws = _FakeWS()
        ls.set_websocket_manager(ws)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=5,
                no_progress_threshold=2,
            )
            return row["id"]

        try:
            loop_id = _run(go())
        finally:
            ls.set_websocket_manager(None)

        completed = [e for e in ws.events if e["type"] == "loop_completed"]
        assert len(completed) == 1
        assert completed[0]["stop_reason"] == "no_progress"
        assert completed[0]["status"] == "stopped"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_get_status_returns_loop_plus_runs(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result(response="hi"), _Result(response="bye")]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=2,
            )
            return service, row["id"]

        service, loop_id = _run(go())
        status = service.get_status(loop_id)
        assert status["id"] == loop_id
        assert status["status"] == "completed"
        assert status["runs_completed"] == 2
        assert len(status["runs"]) == 2
        assert [r["run_number"] for r in status["runs"]] == [1, 2]

    def test_get_status_unknown_returns_none(self, loop_module):
        ls, _, _ = loop_module
        service = ls.LoopService()
        assert service.get_status("does_not_exist") is None
