"""#2524 — a fan-out batch is joined from the DB, not gathered in a coroutine.

`FanOutService.execute` used to build a `dict[task_id, FanOutTaskResult]` inside
one `asyncio.gather`, which meant the batch existed only for as long as the HTTP
request that started it. Two consequences, and both are why `fan_out` sat in the
stranded half of `PULL_REACHABLE_TRIGGERS` (#2048):

* a pull-claimed subtask returns nothing to collect — `execute_task` returns as
  soon as the row is queued and the turn runs later, in the agent's worker;
* nothing could answer about the batch afterwards: no `async_mode`, no status
  endpoint, and a disconnect lost it entirely.

`test_inter_agent_timeout_unit.py` covers the parts of the old contract that had
to survive (per-subtask `timeout_seconds=None` forwarding, the outer deadline,
`async_mode`, the status aggregate). This file covers the properties a
DB-joined batch newly has to have:

  1. the join wakes the caller only when the LAST row is terminal;
  2. the join is idempotent and cheap on terminals that are not fan-out at all;
  3. the terminal fan-out actually reaches it;
  4. a row is created at slot grant, never up front — a pre-created row waiting
     behind the semaphore is bulk-FAILed by the #106 no-session sweep (as
     RUNNING) or double-dispatched by the backlog drain (as QUEUED);
  5. `max_concurrency` still paces the push path — deleting the semaphore would
     turn the excess into `CapacityFull` failures;
  6. the sync wait covers the whole batch, and a deadline never turns an
     undispatched subtask into a failure;
  7. a raised dispatch is closed by a proper terminal writer.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

_OPEN = ("queued", "running", "pending_retry")


def _run(coro):
    return asyncio.run(coro)


class _DB:
    """The `schedule_executions` surface a fan-out batch reaches."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._n = 0
        self.open_counts = 0

    def create_task_execution(self, **kw):
        self._n += 1
        eid = f"exec_{self._n}"
        self.rows[eid] = {
            "id": eid,
            "agent_name": kw.get("agent_name"),
            "fan_out_id": kw.get("fan_out_id"),
            "fan_out_task_id": kw.get("fan_out_task_id"),
            "subscription_id": kw.get("subscription_id"),
            "status": "running",
            "response": None, "error": None, "cost": None,
            "context_used": None, "duration_ms": None,
        }
        return SimpleNamespace(id=eid, fan_out_id=kw.get("fan_out_id"))

    def finish(self, eid, status="success", response="ok", error=None):
        self.rows[eid].update(status=status, response=response, error=error)

    def get_fan_out_executions(self, agent_name, fan_out_id, limit=200):
        return [
            dict(r) for r in self.rows.values()
            if r["fan_out_id"] == fan_out_id and r["agent_name"] == agent_name
        ][:limit]

    def count_fan_out_open(self, fan_out_id):
        self.open_counts += 1
        return sum(
            1 for r in self.rows.values()
            if r["fan_out_id"] == fan_out_id and r["status"] in _OPEN
        )

    def get_execution(self, eid):
        row = self.rows.get(eid)
        return SimpleNamespace(**row) if row else None

    def get_execution_timeout(self, agent_name):
        return 600

    def get_max_parallel_tasks(self, agent_name):
        return 3

    def get_agent_subscription_id(self, agent_name):
        return "sub_1"

    def update_execution_status(self, *, execution_id, status, error=None, **_kw):
        row = self.rows.get(execution_id)
        if row is None or row["status"] not in _OPEN:
            return False  # the CAS: a terminal row is never overwritten
        # Must actually move the row: the batch is joined by COUNTing open rows,
        # so a fake that only returns True leaves the batch open forever and the
        # sync caller waits out its whole budget.
        row["status"] = getattr(status, "value", status)
        row["error"] = error
        return True


@pytest.fixture
def env(monkeypatch):
    """`(fan_out_service, db, calls, script)` with a scripted task service."""
    from services import fan_out_service as fos

    db = _DB()
    calls: list = []
    script = {"behaviour": "success"}

    class _TaskService:
        async def execute_task(self, **kwargs):
            calls.append(kwargs)
            eid = kwargs["execution_id"]
            if script["behaviour"] == "raise":
                raise RuntimeError("dispatch crash")
            if script["behaviour"] == "queued":
                # Pull: the row is on the durable queue; the turn runs later.
                return MagicMock(status="queued", execution_id=eid)
            if script["behaviour"] == "hang":
                await asyncio.sleep(30)
            db.finish(eid)
            return MagicMock(status="success", execution_id=eid)

    monkeypatch.setattr(fos, "db", db)
    monkeypatch.setattr(fos, "get_task_execution_service", lambda: _TaskService())
    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=db))
    fos._inflight_batches.clear()
    return fos, db, calls, script


def _tasks(fos, n):
    return [fos.FanOutTaskInput(id=f"t{i}", message=f"task {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# 1 + 2. The join
# ---------------------------------------------------------------------------


class TestJoin:
    def test_it_wakes_only_when_the_last_row_is_terminal(self, env):
        fos, db, _calls, _script = env
        fan_out_id = "fo_join"
        ids = [
            db.create_task_execution(
                agent_name="a1", fan_out_id=fan_out_id, fan_out_task_id=f"t{i}"
            ).id
            for i in range(3)
        ]
        with patch.object(fos, "signal_fan_out_batch") as signal:
            db.finish(ids[0])
            assert _run(fos.join_fan_out_on_terminal(ids[0])) is False
            db.finish(ids[1], status="failed", response=None, error="boom")
            assert _run(fos.join_fan_out_on_terminal(ids[1])) is False
            signal.assert_not_called()

            db.finish(ids[2])
            assert _run(fos.join_fan_out_on_terminal(ids[2])) is True
            signal.assert_called_once_with(fan_out_id)

    def test_it_is_idempotent_on_a_repeated_terminal(self, env):
        """Pull re-delivers. The join only signals, and signalling a resolved or
        absent waiter is a no-op — so a duplicate terminal costs a count, never
        a second aggregate or a double wake of a caller that has moved on."""
        fos, db, _calls, _script = env
        eid = db.create_task_execution(
            agent_name="a1", fan_out_id="fo_dup", fan_out_task_id="t0"
        ).id
        db.finish(eid)
        with patch.object(fos, "signal_fan_out_batch") as signal:
            assert _run(fos.join_fan_out_on_terminal(eid)) is True
            assert _run(fos.join_fan_out_on_terminal(eid)) is True
            assert signal.call_count == 2  # cheap and harmless, never a re-apply

    def test_a_non_fanout_terminal_costs_one_read_and_no_count(self, env):
        """Every terminal in the fleet passes through this. A row with no
        `fan_out_id` must not reach the batch COUNT."""
        fos, db, _calls, _script = env
        eid = db.create_task_execution(agent_name="a1", fan_out_id=None).id
        db.finish(eid)
        before = db.open_counts
        assert _run(fos.join_fan_out_on_terminal(eid)) is False
        assert db.open_counts == before

    def test_it_ignores_an_unknown_or_missing_execution(self, env):
        fos, _db, _calls, _script = env
        assert _run(fos.join_fan_out_on_terminal(None)) is False
        assert _run(fos.join_fan_out_on_terminal("exec_nope")) is False


# ---------------------------------------------------------------------------
# 3. The terminal fan-out reaches it
# ---------------------------------------------------------------------------


def test_spawn_task_terminal_event_also_joins_the_fan_out():
    """Asserted rather than assumed: the join hangs off the wrapper every
    CAS-won terminal writer already calls, beside the #2523 loop advance — and
    NOT inside `emit_task_terminal_event`, which returns early when no event
    subscription matches (the common case)."""
    from services import event_dispatch_service as eds

    seen: list = []

    async def _fake_join(execution_id):
        seen.append(execution_id)

    async def _drive():
        with (
            patch.object(eds, "emit_task_terminal_event", AsyncMock()),
            patch.dict(
                sys.modules,
                {
                    "services.loop_service": MagicMock(
                        advance_loop_on_terminal=AsyncMock()
                    ),
                    "services.fan_out_service": MagicMock(
                        join_fan_out_on_terminal=_fake_join
                    ),
                },
            ),
        ):
            eds.spawn_task_terminal_event("a1", "exec_1", terminal_status="success")
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0)

    asyncio.run(_drive())
    assert seen == ["exec_1"]


def test_a_broken_loop_advance_does_not_skip_the_fan_out_join():
    """Each consumer is guarded separately, so one raising cannot cost the other
    its terminal — the failure mode a single shared try/except would have."""
    from services import event_dispatch_service as eds

    seen: list = []

    async def _boom(_execution_id):
        raise RuntimeError("loop bookkeeping exploded")

    async def _fake_join(execution_id):
        seen.append(execution_id)

    async def _drive():
        with patch.dict(
            sys.modules,
            {
                "services.loop_service": MagicMock(advance_loop_on_terminal=_boom),
                "services.fan_out_service": MagicMock(
                    join_fan_out_on_terminal=_fake_join
                ),
            },
        ):
            await eds._terminal_side_effects("exec_1")

    asyncio.run(_drive())
    assert seen == ["exec_1"]


# ---------------------------------------------------------------------------
# 4 + 5 + 6. Dispatch shape
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_a_row_is_created_at_slot_grant_not_up_front(self, env):
        """#2524 review, reproduced against the real sweep in
        `test_2524_fanout_real_schema.py`. A row created up front and left
        waiting behind the semaphore is `RUNNING` with no session and a
        `started_at` from admission — exactly what the #106 no-session sweep
        FAILs after 60s. So the row for subtask k must not exist until subtask k
        holds a slot: at each dispatch, rows == dispatches so far."""
        fos, db, calls, _script = env
        rows_at_dispatch: list = []

        class _Peeking:
            async def execute_task(self, **kwargs):
                calls.append(kwargs)
                rows_at_dispatch.append(len(db.rows))
                await asyncio.sleep(0)
                db.finish(kwargs["execution_id"])
                return MagicMock(status="success", execution_id=kwargs["execution_id"],
                                 error_code=None)

        original = fos.get_task_execution_service
        fos.get_task_execution_service = lambda: _Peeking()
        try:
            result = _run(fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 4), max_concurrency=1,
            ))
        finally:
            fos.get_task_execution_service = original
        assert rows_at_dispatch == [1, 2, 3, 4]
        assert result.completed == 4
        # SUB-004: the row this service creates carries the subscription, and
        # execute_task is told it too (the pull payload reads it from there).
        assert {r["subscription_id"] for r in db.rows.values()} == {"sub_1"}
        assert {c["subscription_id"] for c in calls} == {"sub_1"}
        assert {r["fan_out_task_id"] for r in db.rows.values()} == {"t0", "t1", "t2", "t3"}

    def test_a_queued_subtask_leaves_the_batch_open(self, env):
        """The pull path. `execute_task` returning QUEUED is not an outcome —
        the row is on the durable queue and the worker's terminal is what
        eventually closes the batch."""
        fos, db, calls, script = env
        script["behaviour"] = "queued"

        async def _drive():
            res = await fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 3), max_concurrency=3,
                async_mode=True,
            )
            for _ in range(200):
                if len(calls) == 3:
                    break
                await asyncio.sleep(0)
            return res

        result = _run(_drive())
        assert result.status == "accepted"
        assert len(calls) == 3, "all three were dispatched"
        # …and every row is STILL open: a QUEUED return is not an outcome.
        assert db.count_fan_out_open(result.fan_out_id) == 3
        agg = fos.build_aggregate("a1", result.fan_out_id, ["t0", "t1", "t2"])
        assert all(r.status == "running" for r in agg.results)

    def test_a_sync_call_waits_for_queued_rows_to_reach_a_terminal(self, env):
        """The sync edge adapter: every dispatch returned QUEUED, so the caller
        must wait on the rows — and answer with the worker's result, not with
        the empty QUEUED return."""
        fos, db, _calls, script = env
        script["behaviour"] = "queued"

        async def _drive():
            call = asyncio.create_task(fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 2), max_concurrency=2,
                timeout_seconds=5,
            ))
            for _ in range(200):
                if len(db.rows) == 2:
                    break
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            assert not call.done(), "must not answer while rows are queued"
            for eid in list(db.rows):
                db.finish(eid, response=f"worker {eid}")
                await fos.join_fan_out_on_terminal(eid)
            return await call

        result = _run(_drive())
        assert result.status == "completed"
        assert result.completed == 2
        assert [r.response for r in result.results] == ["worker exec_1", "worker exec_2"]

    def test_max_concurrency_still_paces_the_push_path(self, env):
        """The `max_concurrency` decision #2524 had to record. The semaphore
        stays around the `execute_task` call: on push that call spans the whole
        turn, so it paces dispatch exactly as before — deleting it would fire N
        dispatches at an agent whose `max_parallel_tasks` is 3 and turn the
        excess into `CapacityFull` failures. Under pull the same call returns in
        milliseconds, so it self-releases and the worker pool becomes the cap —
        no branch needed."""
        fos, db, calls, _script = env
        inflight = {"now": 0, "peak": 0}

        class _Counting:
            async def execute_task(self, **kwargs):
                inflight["now"] += 1
                inflight["peak"] = max(inflight["peak"], inflight["now"])
                await asyncio.sleep(0)
                inflight["now"] -= 1
                calls.append(kwargs)
                db.finish(kwargs["execution_id"])
                return MagicMock(status="success", execution_id=kwargs["execution_id"])

        original = fos.get_task_execution_service
        fos.get_task_execution_service = lambda: _Counting()
        try:
            result = _run(fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 6), max_concurrency=2,
            ))
        finally:
            fos.get_task_execution_service = original
        assert result.completed == 6
        assert inflight["peak"] <= 2, inflight

    def test_error_code_is_carried_from_a_push_result(self, env):
        """`schedule_executions` has no `error_code` column, so the code
        `execute_task` returned is the only source — it must reach the
        aggregate rather than being dropped for `null`."""
        fos, db, _calls, _script = env

        class _Failing:
            async def execute_task(self, **kwargs):
                db.finish(kwargs["execution_id"], status="failed", response=None,
                          error="Agent at capacity")
                return MagicMock(status="failed", execution_id=kwargs["execution_id"],
                                 error_code=SimpleNamespace(value="capacity"))

        original = fos.get_task_execution_service
        fos.get_task_execution_service = lambda: _Failing()
        try:
            result = _run(fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 1), max_concurrency=1,
            ))
        finally:
            fos.get_task_execution_service = original
        assert result.results[0].status == "failed"
        assert result.results[0].error_code == "capacity"

    def test_a_deadline_reports_the_undispatched_tail_as_running(self, env):
        """The deadline bounds the wait, not the work — and a subtask still
        waiting for a slot has not failed either. It reports `running`, and the
        dispatch carries on (it is shielded from the deadline)."""
        fos, db, calls, script = env
        script["behaviour"] = "hang"

        async def _drive():
            res = await fos.FanOutService().execute(
                agent_name="a1", tasks=_tasks(fos, 3), max_concurrency=1,
                timeout_seconds=0,
            )
            await asyncio.sleep(0)
            return res, list(fos._inflight_batches)

        result, still_dispatching = _run(_drive())
        assert result.status == "deadline_exceeded"
        assert [r.status for r in result.results] == ["running"] * 3
        assert result.failed == 0
        assert still_dispatching, "the deadline must not cancel the dispatch"

    def test_a_raised_dispatch_closes_its_row_as_a_terminal_writer(self, env, monkeypatch):
        """A raise writes no terminal, so nothing would close the row — the
        batch would hang open. `_fail_subtask` is therefore a terminal writer
        like any other: on a WON CAS it closes the activity (#1804) and emits
        through `spawn_task_terminal_event` (#1578, which also carries the
        join)."""
        fos, db, _calls, script = env
        script["behaviour"] = "raise"
        emitted: list = []
        closed: list = []

        from services import event_dispatch_service as eds
        from services.activity_service import activity_service

        monkeypatch.setattr(
            eds, "spawn_task_terminal_event",
            lambda agent, eid, **kw: emitted.append((agent, eid, kw["terminal_status"])),
        )

        async def _close(eid, *_a, **_kw):
            closed.append(eid)

        monkeypatch.setattr(activity_service, "close_execution_activity", _close)
        result = _run(fos.FanOutService().execute(
            agent_name="a1", tasks=_tasks(fos, 2), max_concurrency=2,
        ))
        assert result.total == 2
        assert result.failed == 2
        assert result.status == "completed"  # the batch itself is settled
        assert sorted(closed) == ["exec_1", "exec_2"]
        assert sorted(e[1] for e in emitted) == ["exec_1", "exec_2"]

        # A lost CAS (the row already went terminal) has no side effects.
        emitted.clear(); closed.clear()
        eid = db.create_task_execution(agent_name="a1", fan_out_id="fo_lost").id
        db.finish(eid)
        _run(fos.FanOutService._fail_subtask("a1", eid, RuntimeError("late")))
        assert emitted == [] and closed == []


class TestWaitBudget:
    """#2524 review: with no caller deadline the wait used to be one subtask's
    bound, so a 20-task batch at concurrency 3 returned `deadline_exceeded` on
    work the old unbounded `gather` returned complete."""

    def test_it_covers_every_wave_of_the_batch(self, env):
        fos, _db, _calls, _script = env
        # 600s per run (the fake), max_parallel_tasks 3: ceil(20/3) = 7 waves.
        assert fos.FanOutService._wait_budget("a1", None, 20, 3) == 7 * 600 + 120

    def test_concurrency_above_the_agents_parallelism_does_not_shrink_it(self, env):
        fos, _db, _calls, _script = env
        assert fos.FanOutService._wait_budget("a1", None, 20, 10) == 7 * 600 + 120

    def test_an_explicit_deadline_is_verbatim(self, env):
        fos, _db, _calls, _script = env
        assert fos.FanOutService._wait_budget("a1", 45, 20, 3) == 45.0


# ---------------------------------------------------------------------------
# 7. The sync edge adapter — what unstranded `a2a` and `operator_response`
# ---------------------------------------------------------------------------


class _AdapterDB:
    def __init__(self, row=None):
        self.row = row

    def get_execution(self, _eid):
        return SimpleNamespace(**self.row) if self.row else None

    def get_execution_timeout(self, _agent):
        return 300


class TestSyncEdgeAdapter:
    """`task_execution_service.dispatch_and_await_terminal` (#2524).

    The last two stranded triggers were stranded on the same thing: their caller
    genuinely needs the answer in-line (`routers/a2a` turns `result.response`
    into a JSON-RPC artifact; `operator_resume_service` records `result.status`
    as the ent#329 dispatch receipt), and a pull dispatch returns QUEUED. Neither
    needs a *receipt to poll* — each needs to BLOCK CORRECTLY while the turn
    happens somewhere else, which is a different thing and one `sync_waiter`
    already did for `/task`.
    """

    def _service(self, monkeypatch, *, result, db=None):
        from services import task_execution_service as tes

        svc = MagicMock()
        svc.execute_task = AsyncMock(return_value=result)
        monkeypatch.setattr(tes, "get_task_execution_service", lambda: svc)
        if db is not None:
            monkeypatch.setattr(tes, "db", db)
        return tes, svc

    def test_a_push_dispatch_is_returned_untouched(self, monkeypatch):
        """On push the turn ran inside the await — there is nothing to wait for,
        and the adapter must not add a DB round-trip to every call."""
        from services.execution_envelope import TaskExecutionResult

        pushed = TaskExecutionResult(
            execution_id="exec_1", status="success", response="hi",
        )
        tes, svc = self._service(monkeypatch, result=pushed)
        waited = MagicMock()
        monkeypatch.setattr(
            "services.sync_waiter.wait_for_sync_terminal", waited
        )

        out = _run(tes.dispatch_and_await_terminal(
            agent_name="a1", message="m", triggered_by="a2a",
        ))
        assert out is pushed
        waited.assert_not_called()

    def test_a_queued_dispatch_waits_and_rebuilds_from_the_row(self, monkeypatch):
        """The pull path. QUEUED is not an outcome — the row is on the durable
        queue and the worker's terminal is the answer."""
        from services.execution_envelope import TaskExecutionResult

        queued = TaskExecutionResult(
            execution_id="exec_9", status="queued", response="",
        )
        db = _AdapterDB(row={
            "status": "success", "response": "answered later", "error": None,
            "cost": 0.02, "context_used": 10, "context_max": 200000,
            "claude_session_id": "s1",
        })
        tes, _svc = self._service(monkeypatch, result=queued, db=db)

        seen = {}

        async def _wait(execution_id, timeout):
            seen["execution_id"] = execution_id
            seen["timeout"] = timeout
            return None  # the poll fallback: "re-read the row"

        monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", _wait)

        out = _run(tes.dispatch_and_await_terminal(
            agent_name="a1", message="m", triggered_by="a2a",
        ))
        assert seen["execution_id"] == "exec_9"
        assert seen["timeout"] == 300 + 120  # agent timeout + buffer
        assert out.status == "success"
        assert out.response == "answered later"
        assert out.execution_id == "exec_9"
        assert out.cost == 0.02

    def test_a_wait_that_times_out_fails_the_result_without_raising(self, monkeypatch):
        """Same rule as a fan-out deadline: the wait is bounded, the work is not.
        The execution keeps running and its real terminal still lands on the row,
        so raising here would be both wrong and useless to the caller."""
        from services.execution_envelope import (
            TaskExecutionErrorCode, TaskExecutionResult,
        )

        queued = TaskExecutionResult(
            execution_id="exec_slow", status="queued", response="",
        )
        tes, _svc = self._service(monkeypatch, result=queued, db=_AdapterDB())

        async def _timeout(_execution_id, _timeout):
            raise asyncio.TimeoutError()

        monkeypatch.setattr("services.sync_waiter.wait_for_sync_terminal", _timeout)

        out = _run(tes.dispatch_and_await_terminal(
            agent_name="a1", message="m", triggered_by="a2a", wait_timeout=5,
        ))
        assert out.status == "failed"
        assert out.error_code == TaskExecutionErrorCode.TIMEOUT
        assert out.execution_id == "exec_slow"

    def test_a_vanished_row_falls_back_to_the_dispatch_result(self, monkeypatch):
        from services.execution_envelope import TaskExecutionResult

        queued = TaskExecutionResult(
            execution_id="exec_gone", status="queued", response="",
        )
        tes, _svc = self._service(monkeypatch, result=queued, db=_AdapterDB(row=None))
        monkeypatch.setattr(
            "services.sync_waiter.wait_for_sync_terminal", AsyncMock(return_value=None)
        )

        out = _run(tes.dispatch_and_await_terminal(
            agent_name="a1", message="m", triggered_by="a2a",
        ))
        assert out is queued  # nothing better to report; never None
