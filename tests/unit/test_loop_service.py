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
import sys
from dataclasses import dataclass, field
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
