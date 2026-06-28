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
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
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


class _FakeDB:
    """Minimal in-memory mock matching the loop_service surface."""

    def __init__(self):
        self.loops: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}
        self._next_loop = 0
        self._next_run = 0

    # ---- loop CRUD ----
    def create_loop(self, **kwargs) -> dict:
        self._next_loop += 1
        loop_id = f"loop_{self._next_loop}"
        row = {
            "id": loop_id,
            "status": "queued",
            "runs_completed": 0,
            "stop_reason": None,
            "last_response": None,
            "error": None,
            "created_at": "now",
            "started_at": None,
            "completed_at": None,
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
            self.loops[loop_id]["started_at"] = "now"

    def update_loop_progress(self, loop_id: str, *, runs_completed: int, last_response):
        self.loops[loop_id]["runs_completed"] = runs_completed
        self.loops[loop_id]["last_response"] = last_response

    def finalize_loop(self, loop_id: str, *, status: str, stop_reason: str, error=None):
        self.loops[loop_id]["status"] = status
        self.loops[loop_id]["stop_reason"] = stop_reason
        self.loops[loop_id]["error"] = error
        self.loops[loop_id]["completed_at"] = "now"

    def list_non_terminal_loops(self):
        return [
            dict(r) for r in self.loops.values()
            if r["status"] in ("queued", "running")
        ]

    def mark_orphan_loops_interrupted(self) -> int:
        n = 0
        for r in self.loops.values():
            if r["status"] in ("queued", "running"):
                r["status"] = "interrupted"
                r["stop_reason"] = "interrupted"
                n += 1
        return n

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


@dataclass
class _FakeTaskService:
    """Records execute_task() calls and returns scripted results."""
    results: list = field(default_factory=list)  # list[_Result]
    calls: list = field(default_factory=list)
    _idx: int = 0

    async def execute_task(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results[self._idx] if self._idx < len(self.results) else _Result()
        self._idx += 1
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def loop_module(monkeypatch):
    """Import services.loop_service with mocks installed."""
    from services import loop_service as ls

    fake_db = _FakeDB()
    fake_task_service = _FakeTaskService()

    monkeypatch.setattr(ls, "db", fake_db)
    monkeypatch.setattr(ls, "get_task_execution_service", lambda: fake_task_service)
    monkeypatch.setattr(ls, "_websocket_manager", None)

    return ls, fake_db, fake_task_service


def _run(coro):
    return asyncio.run(coro)


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
            # Wait for the loop's task to finish
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
    def test_stop_loop_flips_status_to_stopped(self, loop_module):
        ls, db, ts = loop_module

        # Slow each iteration enough that stop_loop catches the runner
        # between iterations.
        async def slow_execute(**kwargs):
            await asyncio.sleep(0.05)
            return _Result(response="r")

        ts.execute_task = slow_execute  # type: ignore

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=10,
                delay_seconds=0,
            )
            # Let the first iteration kick off, then stop.
            await asyncio.sleep(0.01)
            outcome = await service.stop_loop(row["id"])
            assert outcome in ("stopping", "already_done")
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"
        assert loop["runs_completed"] < 10

    def test_stop_loop_on_already_terminal_returns_already_done(self, loop_module):
        ls, db, ts = loop_module
        ts.results = [_Result()]

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=1,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
    def test_orphan_sweep_marks_running_as_interrupted(self, loop_module):
        ls, db, _ = loop_module
        db.create_loop(agent_name="a", message_template="m", max_runs=1)
        db.create_loop(agent_name="a", message_template="m", max_runs=1)
        loop_ids = list(db.loops.keys())
        # Simulate one already running
        db.loops[loop_ids[1]]["status"] = "running"

        n = db.mark_orphan_loops_interrupted()
        assert n == 2
        for lid in loop_ids:
            assert db.loops[lid]["status"] == "interrupted"
            assert db.loops[lid]["stop_reason"] == "interrupted"

    def test_orphan_sweep_idempotent(self, loop_module):
        ls, db, _ = loop_module
        assert db.mark_orphan_loops_interrupted() == 0
        db.create_loop(agent_name="a", message_template="m", max_runs=1)
        assert db.mark_orphan_loops_interrupted() == 1
        # Second call: already interrupted, no-op.
        assert db.mark_orphan_loops_interrupted() == 0


# ---------------------------------------------------------------------------
# Runner — wall-clock deadline (#1156)
# ---------------------------------------------------------------------------

class _FakeClock:
    """Controllable stand-in for ``datetime`` inside loop_service.

    Only ``utcnow()`` is exercised by the runner; it returns the current
    fake instant. Tests advance ``now`` (directly or via a task that bumps
    it each run) to drive the deadline deterministically — no real sleeping.
    """
    now = datetime(2026, 1, 1, 0, 0, 0)

    @classmethod
    def utcnow(cls):
        return cls.now


class TestDeadline:
    def _install_clock(self, ls, monkeypatch, *, advance_per_run: float):
        """Swap in the fake clock; each execute_task advances it."""
        _FakeClock.now = datetime(2026, 1, 1, 0, 0, 0)
        monkeypatch.setattr(ls, "datetime", _FakeClock)

        async def _exec(**kwargs):
            ts = self._ts
            ts.calls.append(kwargs)
            result = ts.results[ts._idx] if ts._idx < len(ts.results) else _Result()
            ts._idx += 1
            _FakeClock.now = _FakeClock.now + timedelta(seconds=advance_per_run)
            return result

        return _exec

    def test_deadline_stops_loop_at_boundary(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response=f"r{i}") for i in range(1, 6)]
        ts.execute_task = self._install_clock(ls, monkeypatch, advance_per_run=6)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=5,
                max_duration_seconds=10,  # ~1.6 runs fit before the deadline
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
        ts.execute_task = self._install_clock(ls, monkeypatch, advance_per_run=999)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=5,
                max_duration_seconds=10,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["stop_reason"] == "deadline_exceeded"
        assert loop["runs_completed"] == 1  # the in-flight run completed
        runs = db.list_loop_runs(loop_id)
        assert runs[0]["status"] == "completed"
        assert runs[0]["response"] == "done-run"

    def test_delay_does_not_sleep_past_deadline(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response="r1"), _Result(response="r2")]
        ts.execute_task = self._install_clock(ls, monkeypatch, advance_per_run=3)

        # Capture sleep durations and advance the fake clock by them instead
        # of really sleeping.
        slept: list[float] = []

        async def _fake_sleep(secs):
            slept.append(secs)
            _FakeClock.now = _FakeClock.now + timedelta(seconds=secs)

        monkeypatch.setattr(ls.asyncio, "sleep", _fake_sleep)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=5,
                delay_seconds=100,        # would blow way past the deadline
                max_duration_seconds=10,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["stop_reason"] == "deadline_exceeded"
        # run1 t0→3, then delay capped to remaining (10−3=7), not the full 100.
        assert slept == [7]

    def test_no_deadline_runs_all_when_unset(self, loop_module, monkeypatch):
        ls, db, ts = loop_module
        self._ts = ts
        ts.results = [_Result(response=f"r{i}") for i in range(1, 4)]
        # Clock jumps far each run; with no deadline it must be ignored.
        ts.execute_task = self._install_clock(ls, monkeypatch, advance_per_run=10_000)

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1",
                message_template="m",
                max_runs=3,
                max_duration_seconds=None,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
            return row["id"]

        loop_id = _run(go())
        loop = db.get_loop(loop_id)
        assert loop["status"] == "completed"
        assert loop["stop_reason"] == "max_runs_reached"
        assert loop["runs_completed"] == 3


# ---------------------------------------------------------------------------
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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

        async def exec_flip(**kwargs):
            ts.calls.append(kwargs)
            idx = ts._idx
            ts._idx += 1
            # On the 3rd call (the run that would trip no_progress at K=3),
            # set should_stop on the live handle mid-run.
            if idx == 2:
                for h in captured["service"]._handles.values():
                    h.should_stop = True
            return ts.results[idx]

        ts.execute_task = exec_flip

        async def go():
            service = ls.LoopService()
            captured["service"] = service
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
        ts.results = [_Result(response="same") for _ in range(10)]

        async def _exec(**kwargs):
            ts.calls.append(kwargs)
            idx = ts._idx
            ts._idx += 1
            _FakeClock.now = _FakeClock.now + timedelta(seconds=4)
            return ts.results[idx]

        ts.execute_task = _exec

        async def go():
            service = ls.LoopService()
            row = await service.start_loop(
                agent_name="a1", message_template="m", max_runs=10,
                no_progress_threshold=3, max_duration_seconds=10,
            )
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
            handle = service._handles.get(row["id"])
            if handle is not None:
                await handle.task
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
