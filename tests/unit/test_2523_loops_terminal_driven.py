"""#2523 — a loop runs off execution terminals, not an in-process `for` loop.

`LoopService._run` used to be one long-lived `asyncio.Task` per loop: a `for`
over iterations that `await`ed `execute_task` and kept every cross-iteration
value (previous response, accumulated cost, consecutive failures, the #1157
fingerprints, the stop flag, the inter-run sleep) in Python locals. Two
consequences, both fixed here:

* the loop could not run on the durable queue — a pull-claimed row returns no
  `TaskExecutionResult`, so a driver built on reading one is structurally
  push-only (`pull_pilot.PULL_REACHABLE_TRIGGERS`, #2048);
* the loop could not survive a restart — the state died with the coroutine, so
  `cleanup_service` flipped every in-flight loop to `interrupted`.

`test_loop_service.py` already covers the behaviour that had to stay identical
(the seven stop conditions and their precedence, template substitution, cost
budget, failure policy). This file covers what is *new*, i.e. the properties a
terminal-driven driver has to have and a `for` loop never needed:

  1. the advance is idempotent under at-least-once delivery;
  2. a queued (pull) dispatch does NOT advance — the worker's terminal does;
  3. the terminal fan-out actually reaches the advance;
  4. a fast-fail dispatch still advances (it writes no terminal event);
  5. the due-loop sweep claims each parked loop exactly once.

Pure unit test — the fakes from `test_loop_service.py` are deliberately NOT
imported; this file builds the minimum surface each property needs so a change
to that file's harness cannot silently weaken these.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

_TERMINAL = frozenset(
    {"completed", "completed_with_errors", "stopped", "failed", "interrupted"}
)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds") + "Z"


def _run(coro):
    async def _driver():
        result = await coro
        from services import loop_service as ls

        for _ in range(20000):
            if not ls._inflight_dispatches:
                break
            await asyncio.sleep(0)
        return result

    return asyncio.run(_driver())


class _Execution:
    def __init__(self, eid, status="running", response=None, error=None, cost=None):
        self.id = eid
        self.status = status
        self.response = response
        self.error = error
        self.cost = cost
        self.duration_ms = None


class _DB:
    """The slice of `database.db` the loop driver touches."""

    def __init__(self):
        self.loops = {}
        self.runs = {}
        self.executions = {}
        self._n = 0
        self.advance_claims = []

    # loops
    def create_loop(self, **kw):
        self._n += 1
        lid = f"loop_{self._n}"
        self.loops[lid] = {
            "id": lid, "status": "queued", "runs_completed": 0, "failed_runs": 0,
            "stop_reason": None, "last_response": None, "error": None,
            "created_at": "now", "started_at": None, "completed_at": None,
            "next_run_at": None, "stop_requested_at": None, **kw,
        }
        self.runs[lid] = []
        return dict(self.loops[lid])

    def get_loop(self, lid):
        return dict(self.loops[lid]) if lid in self.loops else None

    def mark_loop_running(self, lid):
        if self.loops[lid]["status"] == "queued":
            self.loops[lid]["status"] = "running"
            self.loops[lid]["started_at"] = _iso(datetime.utcnow())

    def update_loop_progress(self, lid, *, runs_completed, last_response, failed_runs=None):
        self.loops[lid]["runs_completed"] = runs_completed
        self.loops[lid]["last_response"] = last_response
        if failed_runs is not None:
            self.loops[lid]["failed_runs"] = failed_runs

    def finalize_loop(self, lid, *, status, stop_reason, error=None, failed_runs=None):
        self.loops[lid].update(
            status=status, stop_reason=stop_reason, error=error, completed_at="now"
        )
        if failed_runs is not None:
            self.loops[lid]["failed_runs"] = failed_runs

    def list_non_terminal_loops(self):
        return [dict(r) for r in self.loops.values() if r["status"] in ("queued", "running")]

    def claim_loop_advance(self, lid, run_number):
        row = self.loops.get(lid)
        won = (
            row is not None
            and row["status"] not in _TERMINAL
            and row["runs_completed"] == run_number - 1
        )
        self.advance_claims.append((lid, run_number, won))
        if won:
            row["runs_completed"] = run_number
        return won

    def request_loop_stop(self, lid):
        row = self.loops.get(lid)
        if row is None or row["status"] in _TERMINAL:
            return False
        row["stop_requested_at"] = _iso(datetime.utcnow())
        return True

    def schedule_loop_next_run(self, lid, next_run_at):
        self.loops[lid]["next_run_at"] = next_run_at

    def claim_due_loop(self, lid, next_run_at):
        row = self.loops.get(lid)
        if row is None or row["status"] in _TERMINAL:
            return False
        if row.get("next_run_at") != next_run_at:
            return False
        row["next_run_at"] = None
        return True

    def list_due_loops(self, now, *, limit=100):
        return [
            dict(r) for r in self.loops.values()
            if r.get("next_run_at") and r["next_run_at"] <= now
            and r["status"] not in _TERMINAL
        ][:limit]

    # runs
    def start_loop_run(self, lid, run_number, *, execution_id=None):
        rid = f"lr_{lid}_{run_number}"
        self.runs[lid].append({
            "id": rid, "loop_id": lid, "run_number": run_number,
            "execution_id": execution_id, "status": "running", "response": None,
            "error": None, "cost": None, "duration_ms": None,
            "started_at": "now", "completed_at": None,
        })
        return rid

    def finalize_loop_run(self, rid, **kw):
        for runs in self.runs.values():
            for r in runs:
                if r["id"] == rid:
                    for k, v in kw.items():
                        if k == "execution_id" and v is None:
                            continue
                        r[k] = v
                    r["completed_at"] = "now"
                    return

    def list_loop_runs(self, lid):
        return [dict(r) for r in sorted(self.runs.get(lid, []), key=lambda r: r["run_number"])]

    def get_loop_run_by_execution(self, eid):
        for runs in self.runs.values():
            for r in runs:
                if r["execution_id"] == eid:
                    return dict(r)
        return None

    # executions
    def create_task_execution(self, **kw):
        eid = f"exec_{len(self.executions) + 1}"
        self.executions[eid] = _Execution(eid)
        return self.executions[eid]

    def get_execution(self, eid):
        return self.executions.get(eid)

    def update_execution_status(self, *, execution_id, status, error=None, **_kw):
        row = self.executions.get(execution_id)
        if row is None:
            return False
        row.status = getattr(status, "value", status)
        row.error = error
        return True


@pytest.fixture
def env(monkeypatch):
    """`(loop_service, db, calls)` with a scripted task service installed."""
    from services import loop_service as ls

    db = _DB()
    calls = []
    script = {"status": "success", "response": "ok", "cost": 0.01, "error": None}

    class _TaskService:
        async def execute_task(self, **kwargs):
            calls.append(kwargs)
            behaviour = script.get("behaviour")
            if behaviour == "raise":
                raise RuntimeError("dispatch crash")
            row = db.executions.get(kwargs.get("execution_id"))
            if behaviour == "queued":
                # Pull: the row is on the durable queue and stays `running`
                # until the agent's worker reports its terminal.
                return MagicMock(status="queued", execution_id=kwargs["execution_id"])
            if row is not None:
                row.status = script["status"]
                row.response = script["response"]
                row.error = script["error"]
                row.cost = script["cost"]
            return MagicMock(
                status=script["status"], execution_id=kwargs["execution_id"]
            )

    monkeypatch.setattr(ls, "db", db)
    monkeypatch.setattr(ls, "get_task_execution_service", lambda: _TaskService())
    monkeypatch.setattr(ls, "_websocket_manager", None)
    ls._inflight_dispatches.clear()
    return ls, db, calls, script


def _start(ls, **kw):
    kw.setdefault("agent_name", "a1")
    kw.setdefault("message_template", "m {{run}}")
    kw.setdefault("max_runs", 3)
    return _run(ls.LoopService().start_loop(**kw))["id"]


# ---------------------------------------------------------------------------
# 1. The advance is idempotent — the property at-least-once delivery demands
# ---------------------------------------------------------------------------


class TestAdvanceIsIdempotent:
    def test_a_redelivered_terminal_does_not_run_the_next_iteration_twice(self, env):
        """Pull re-delivers: a lease expiry, or a late callback racing the
        reaper, can present the SAME terminal twice. Under a `for` loop that was
        impossible; here it would fire iteration N+1 twice and bill for it."""
        ls, db, calls, script = env
        service = ls.LoopService()
        loop_id = db.create_loop(
            agent_name="a1", message_template="m", max_runs=5, delay_seconds=0,
            stop_signal=None, timeout_per_run=None, max_duration_seconds=None,
            max_cost_usd=None, no_progress_threshold=None, on_failure="abort",
            max_consecutive_failures=3, model=None, allowed_tools=None,
            started_by_user_id=None, started_by_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None,
        )["id"]
        db.mark_loop_running(loop_id)
        script["behaviour"] = "queued"  # keep the chain from running away
        _run(service._dispatch_run(db.get_loop(loop_id), run_number=1))
        assert len(calls) == 1

        # The worker reports its terminal — twice.
        execution_id = db.runs[loop_id][0]["execution_id"]
        db.executions[execution_id].status = "success"
        db.executions[execution_id].response = "r1"
        assert _run(service.advance_on_terminal(execution_id)) is True
        assert _run(service.advance_on_terminal(execution_id)) is False

        assert db.get_loop(loop_id)["runs_completed"] == 1
        assert len(calls) == 2  # iteration 2 dispatched exactly once
        won = [c for c in db.advance_claims if c[2]]
        assert len(won) == 1

    def test_an_unknown_execution_is_ignored(self, env):
        ls, db, _calls, _script = env
        assert _run(ls.LoopService().advance_on_terminal("exec_nope")) is False
        assert _run(ls.LoopService().advance_on_terminal(None)) is False

    def test_a_terminal_for_an_already_finalized_loop_cannot_resurrect_it(self, env):
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        loop_id = _start(ls, max_runs=5)
        execution_id = db.runs[loop_id][0]["execution_id"]

        db.finalize_loop(loop_id, status="stopped", stop_reason="user_stopped")
        db.executions[execution_id].status = "success"
        assert _run(service.advance_on_terminal(execution_id)) is False
        assert db.get_loop(loop_id)["status"] == "stopped"
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 2. The pull path — a queued dispatch does not advance
# ---------------------------------------------------------------------------


class TestPullDispatch:
    def test_a_queued_dispatch_waits_for_the_workers_terminal(self, env):
        """The whole point of #2523. Under pull, `execute_task` returns QUEUED
        as soon as the row is on the durable queue; advancing on that return
        would run the next iteration while this one has not started."""
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        loop_id = _start(ls, max_runs=3)

        assert len(calls) == 1
        loop = db.get_loop(loop_id)
        assert loop["status"] == "running"
        assert loop["runs_completed"] == 0
        assert db.runs[loop_id][0]["status"] == "running"

        # The worker finishes and reports.
        execution_id = db.runs[loop_id][0]["execution_id"]
        db.executions[execution_id].status = "success"
        db.executions[execution_id].response = "from-the-worker"
        _run(ls.LoopService().advance_on_terminal(execution_id))

        assert len(calls) == 2
        assert db.get_loop(loop_id)["runs_completed"] == 1
        assert db.get_loop(loop_id)["last_response"] == "from-the-worker"

    def test_the_execution_row_is_stamped_on_the_run_before_dispatch(self, env):
        """`agent_loop_runs.execution_id` has to exist BEFORE the turn can
        finish, because the advance starts from an execution id. The pre-#2523
        runner filled it in afterwards — it already knew which run it awaited."""
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        loop_id = _start(ls, max_runs=2)

        run = db.runs[loop_id][0]
        assert run["execution_id"] is not None
        assert calls[0]["execution_id"] == run["execution_id"]
        assert db.get_loop_run_by_execution(run["execution_id"])["run_number"] == 1


# ---------------------------------------------------------------------------
# 3. The terminal fan-out reaches the advance
# ---------------------------------------------------------------------------


class TestTerminalFanOut:
    def test_spawn_task_terminal_event_also_advances_the_loop(self):
        """The wiring, asserted rather than assumed. It hangs off
        `spawn_task_terminal_event` — the wrapper every CAS-won terminal writer
        already calls — and NOT inside `emit_task_terminal_event`, which returns
        early when no event subscription matches (the common case)."""
        from services import event_dispatch_service as eds

        seen = []

        async def _fake_advance(execution_id):
            seen.append(execution_id)

        async def _drive():
            with (
                patch.object(eds, "emit_task_terminal_event", AsyncMock()),
                patch.dict(
                    sys.modules,
                    {"services.loop_service": MagicMock(
                        advance_loop_on_terminal=_fake_advance
                    )},
                ),
            ):
                eds.spawn_task_terminal_event(
                    "a1", "exec_1", terminal_status="success"
                )
                for _ in range(200):
                    if seen:
                        break
                    await asyncio.sleep(0)

        asyncio.run(_drive())
        assert seen == ["exec_1"]

    def test_a_broken_advance_never_breaks_the_terminal(self):
        """It runs on the already-billed terminal path; a loop bookkeeping fault
        must not propagate."""
        from services import event_dispatch_service as eds

        async def _boom(_execution_id):
            raise RuntimeError("loop bookkeeping exploded")

        async def _drive():
            with patch.dict(
                sys.modules,
                {"services.loop_service": MagicMock(advance_loop_on_terminal=_boom)},
            ):
                await eds._advance_loop("exec_1")

        asyncio.run(_drive())  # must not raise


# ---------------------------------------------------------------------------
# 4. Fast-fail dispatch still advances
# ---------------------------------------------------------------------------


class TestFastFailStillAdvances:
    def test_a_raised_dispatch_fails_the_row_and_advances(self, env):
        """A raise writes no terminal, so nothing would fire the hook — without
        the direct advance in `_run_and_advance` the loop would stall forever in
        `running` with one open run row."""
        ls, db, calls, script = env
        script["behaviour"] = "raise"
        loop_id = _start(ls, max_runs=3)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "error"
        assert "dispatch crash" in (loop["error"] or "")
        assert db.runs[loop_id][0]["status"] == "failed"

    def test_a_failed_result_advances_without_a_terminal_event(self, env):
        """`execute_task`'s capacity / circuit-open / ephemeral fast-fails write
        a FAILED row directly and return — they never reach a CAS-won terminal
        writer, so no event fires and the direct advance is the only thing that
        moves the loop."""
        ls, db, calls, script = env
        script.update(status="failed", response=None, error="Agent at capacity")
        loop_id = _start(ls, max_runs=3)

        loop = db.get_loop(loop_id)
        assert loop["status"] == "failed"
        assert loop["stop_reason"] == "error"
        assert len(calls) == 1  # aborted, did not keep dispatching


# ---------------------------------------------------------------------------
# 5. The due-loop sweep
# ---------------------------------------------------------------------------


class TestDueSweep:
    def test_a_parked_loop_is_dispatched_once_when_due(self, env):
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        loop_id = _start(ls, max_runs=3, delay_seconds=60)
        # Finish run 1 so the loop parks before run 2.
        execution_id = db.runs[loop_id][0]["execution_id"]
        db.executions[execution_id].status = "success"
        db.executions[execution_id].response = "r1"
        _run(service.advance_on_terminal(execution_id))

        assert len(calls) == 1
        assert db.get_loop(loop_id)["next_run_at"] is not None

        # Not due yet.
        assert _run(service.dispatch_due_loops()) == 0
        assert len(calls) == 1

        db.loops[loop_id]["next_run_at"] = _iso(datetime.utcnow() - timedelta(seconds=1))
        assert _run(service.dispatch_due_loops()) == 1
        assert len(calls) == 2
        assert db.get_loop(loop_id)["next_run_at"] is None

    def test_a_second_worker_sweeping_the_same_row_dispatches_nothing(self, env):
        """The sweep runs in every backend worker, so the claim is a CAS on the
        exact `next_run_at` read — two workers seeing one due row means exactly
        one dispatch."""
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        loop_id = _start(ls, max_runs=3, delay_seconds=60)
        execution_id = db.runs[loop_id][0]["execution_id"]
        db.executions[execution_id].status = "success"
        _run(service.advance_on_terminal(execution_id))
        db.loops[loop_id]["next_run_at"] = _iso(datetime.utcnow() - timedelta(seconds=1))

        assert _run(service.dispatch_due_loops()) == 1
        assert _run(ls.LoopService().dispatch_due_loops()) == 0
        assert len(calls) == 2

    def test_a_stop_that_arrived_while_parked_wins_over_the_dispatch(self, env):
        """The sweep re-evaluates instead of dispatching blind — a stop or a
        deadline can arrive while the loop sits parked."""
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        loop_id = _start(ls, max_runs=5, delay_seconds=60)
        execution_id = db.runs[loop_id][0]["execution_id"]
        db.executions[execution_id].status = "success"
        _run(service.advance_on_terminal(execution_id))

        db.loops[loop_id]["stop_requested_at"] = _iso(datetime.utcnow())
        db.loops[loop_id]["next_run_at"] = _iso(datetime.utcnow() - timedelta(seconds=1))
        assert _run(service.dispatch_due_loops()) == 0

        loop = db.get_loop(loop_id)
        assert loop["status"] == "stopped"
        assert loop["stop_reason"] == "user_stopped"
        assert len(calls) == 1

    def test_a_rearmed_queued_loop_starts_properly(self, env):
        """A crash between `create_loop` and `mark_loop_running` leaves the row
        `queued` with no `started_at`. The sweep has to flip it, or the loop
        runs forever reporting `queued` and — because the #1156 deadline is
        measured from `started_at` — with no deadline at all."""
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        loop_id = db.create_loop(
            agent_name="a1", message_template="m", max_runs=3, delay_seconds=0,
            stop_signal=None, timeout_per_run=None, max_duration_seconds=60,
            max_cost_usd=None, no_progress_threshold=None, on_failure="abort",
            max_consecutive_failures=3, model=None, allowed_tools=None,
            started_by_user_id=None, started_by_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None,
        )["id"]
        assert db.get_loop(loop_id)["status"] == "queued"

        assert _run(service.reconcile_after_restart()) == 1
        db.loops[loop_id]["next_run_at"] = _iso(datetime.utcnow() - timedelta(seconds=1))
        assert _run(service.dispatch_due_loops()) == 1

        loop = db.get_loop(loop_id)
        assert loop["status"] == "running"
        assert loop["started_at"] is not None
        assert len(calls) == 1

    def test_one_bad_loop_does_not_stall_the_sweep(self, env, monkeypatch):
        ls, db, calls, script = env
        script["behaviour"] = "queued"
        service = ls.LoopService()
        good = _start(ls, max_runs=3, delay_seconds=60)
        execution_id = db.runs[good][0]["execution_id"]
        db.executions[execution_id].status = "success"
        _run(service.advance_on_terminal(execution_id))
        db.loops[good]["next_run_at"] = _iso(datetime.utcnow() - timedelta(seconds=2))

        db.loops["broken"] = {
            "id": "broken", "status": "running", "runs_completed": 0,
            "next_run_at": _iso(datetime.utcnow() - timedelta(seconds=3)),
        }
        db.runs["broken"] = []

        assert _run(service.dispatch_due_loops()) == 1
        assert db.get_loop(good)["next_run_at"] is None
